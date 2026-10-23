"""Tests for nua/runner.py: task dispatch, final-failure handling and the task budget.

The task modules other tasks own (process, apply, backcheck, indexes, rollup, sweep) are replaced by
small stubs, so these tests only check what the runner itself does with them.
"""
from __future__ import annotations

import json
import sys
import threading
import time
import types
from dataclasses import dataclass, field

import pytest
import yaml

import nua
from nua import runner, settings
from nua.context import Ctx
from nua.digest import digest_path, read_digest, write_digest
from nua.llm import CostCapExceeded
from nua.types import Claim, Digest, Scope


# ---------- stubs for modules other tasks own ----------

def stub(monkeypatch, name: str, **attrs) -> types.ModuleType:
    """Install a fake `nua.<name>` module for the duration of one test."""
    mod = types.ModuleType(f"nua.{name}")
    for key, value in attrs.items():
        setattr(mod, key, value)
    monkeypatch.setitem(sys.modules, f"nua.{name}", mod)
    monkeypatch.setattr(nua, name, mod, raising=False)
    return mod


@dataclass
class FakeProcessResult:
    transcript: str
    outcome: str
    call_id: str | None = None
    briefed: bool = False


def stub_process(monkeypatch, process_call=None, marked: list | None = None, *, start_backfill=None,
                 batches: dict | None = None):
    def mark_final_failure(ctx, transcript, error):
        if marked is not None:
            marked.append((transcript, error))

    return stub(monkeypatch, "process", process_call=process_call, mark_final_failure=mark_final_failure,
                start_backfill=start_backfill or (lambda *a, **k: None),
                load_batch=lambda ctx, batch_id: (batches or {}).get(batch_id),
                batch_completion_check=lambda *a, **k: False)


def stub_apply(monkeypatch) -> list[dict]:
    seen: list[dict] = []

    def apply_task(ctx, payload):
        seen.append(payload)
        return {"ok": True, "outcome": "applied", "message": "Alex added B-001 via slack", "entry_ids": ["B-001"]}

    stub(monkeypatch, "apply", apply_task=apply_task)
    return seen


def darius_digest() -> Digest:
    claim = Claim(id="c01", text="Commercial lines get less claims spend",
                  quote="on the commercial side the carriers buy a lot less", offset=(1000, 1045),
                  speaker="Darius Kemp", scope=Scope(line="general liability", party="carrier"), turns=[57])
    return Digest(call_id="2026-09-23_darius-kemp", transcript="transcripts/circleback_1.md", status="digested",
                  origin="live", claims=[claim], contact="Darius Kemp", firm="Example Mutual",
                  seat="claims adjuster (staff)")


def ops_messages(ctx: Ctx) -> list[dict]:
    return ctx.slack.live(ctx.channel("ops"))


def buttons(message: dict) -> list[dict]:
    return [e for b in message["blocks"] if b["type"] == "actions" for e in b["elements"]]


# ---------- dispatch ----------

def test_process_call_dispatch_returns_result(ctx, monkeypatch):
    seen = []

    def process_call(c, transcript, *, force=False, batch=None, **kw):
        seen.append((transcript, force, batch))
        return FakeProcessResult(transcript, "digested", "2026-09-23_darius-kemp", True)

    stub_process(monkeypatch, process_call)
    out = runner.run_task(ctx, "process_call", {"transcript": "transcripts/a.md", "force": True, "batch": "b1"})
    assert out["ok"] is True
    assert out["result"]["outcome"] == "digested"
    assert out["result"]["call_id"] == "2026-09-23_darius-kemp"
    assert seen == [("transcripts/a.md", True, "b1")]


def test_backfill_dispatch_and_batch_file_fallback(ctx, monkeypatch):
    seen = []
    stub_process(monkeypatch, start_backfill=lambda c, paths, batch_id: seen.append((paths, batch_id)),
                 batches={"def": ["transcripts/c.md"]})
    runner.run_task(ctx, "backfill", {"paths": ["transcripts/a.md", "transcripts/b.md"], "batch_id": "abc"})
    runner.run_task(ctx, "backfill", {"batch_id": "def"})
    assert seen == [(["transcripts/a.md", "transcripts/b.md"], "abc"), (["transcripts/c.md"], "def")]


def test_backfill_without_a_batch_id_is_invalid(ctx, monkeypatch):
    stub_process(monkeypatch)
    out = runner.run_task(ctx, "backfill", {"paths": ["transcripts/a.md"]}, attempt=0)
    assert out["ok"] is False
    assert buttons(ops_messages(ctx)[0]) == []


def _drop(ctx: Ctx, *names: str) -> None:
    for name in names:
        (ctx.repo.root / "transcripts" / name).write_text("Pat Lee: hello\n", encoding="utf-8")


def test_backfill_rerun_without_its_batch_file_rebuilds_the_list(ctx, monkeypatch):
    """A drop of up to 100 files is too big for a button, so Re-run carries only the batch_id. When the
    batch file was never committed (GitHub push failed), the next sync deletes it: the Re-run then takes
    the transcripts no digest points to, as the sweep would, and still posts the batch summary."""
    seen = []
    calls = {"n": 0}

    def start_backfill(c, paths, batch_id):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("GitError: push to main failed after 5 attempts")
        seen.append((paths, batch_id))

    stub_process(monkeypatch, start_backfill=start_backfill)
    paths = [f"transcripts/2026-09-{i % 28 + 1:02d}_some-long-contact-name-{i}.md" for i in range(60)]
    batch_id = "a1b2" * 10
    runner.run_task(ctx, "backfill", {"paths": paths, "batch_id": batch_id}, attempt=4)
    [button] = buttons(ops_messages(ctx)[0])
    task = json.loads(button["value"])
    assert task == {"kind": "backfill", "payload": {"batch_id": batch_id}}

    _drop(ctx, "a.md", "b.md", "cp1252.md", ".gitkeep")
    (ctx.repo.root / "transcripts" / "cp1252.md").write_bytes(b"Pat Lee: we\x92re fine\n")
    write_digest(ctx.repo, Digest(call_id="2026-09-18_b", transcript="transcripts/b.md", status="digested",
                                  origin="backfill"))
    out = runner.run_task(ctx, task["kind"], task["payload"], attempt=0)
    assert out["ok"] is True
    assert seen == [(["transcripts/a.md", "transcripts/cp1252.md"], batch_id)]


def test_backfill_rerun_with_nothing_left_to_process_is_a_no_op(ctx, monkeypatch):
    seen = []
    stub_process(monkeypatch, start_backfill=lambda c, paths, batch_id: seen.append(paths))
    _drop(ctx, "b.md")
    write_digest(ctx.repo, Digest(call_id="2026-09-18_b", transcript="transcripts/b.md", status="digested",
                                  origin="backfill"))
    out = runner.run_task(ctx, "backfill", {"batch_id": "missing"}, attempt=0)
    assert out["ok"] is True and out["result"]["paths"] == 0
    assert seen == [] and ops_messages(ctx) == []


def test_backcheck_rollup_and_sweep_dispatch(ctx, monkeypatch):
    seen = []
    stub(monkeypatch, "backcheck",
         backcheck=lambda c, payload: seen.append(("backcheck", payload)) or {"entries": [], "digests_updated": 0})
    stub(monkeypatch, "rollup", run_rollup=lambda c, *, bootstrap=False, week=None: seen.append(("rollup", bootstrap)) or {})

    @dataclass
    class Report:
        ingested: list = field(default_factory=list)

    def run_sweep(c, *, granola=None, circleback=None, granola_only=False, last_sweep=None):
        seen.append(("sweep", granola, circleback, granola_only))
        return Report()

    stub(monkeypatch, "sweep", run_sweep=run_sweep)
    monkeypatch.setattr(settings, "granola_client", lambda **kw: "GRANOLA")
    monkeypatch.setattr(settings, "circleback_clients", lambda config, **kw: {"alex": "CB"})

    assert runner.run_task(ctx, "backcheck", {})["result"] == {"entries": [], "digests_updated": 0}
    runner.run_task(ctx, "backcheck", {"after": "abc123"})      # apply's debounce payload reaches backcheck
    runner.run_task(ctx, "rollup", {"bootstrap": True})
    out = runner.run_task(ctx, "sweep", {})
    assert seen == [("backcheck", {}), ("backcheck", {"after": "abc123"}), ("rollup", True),
                    ("sweep", "GRANOLA", {"alex": "CB"}, False)]
    assert out["result"] == {"ingested": []}


def test_granola_only_sweep_skips_circleback_clients(ctx, monkeypatch):
    seen = []
    stub(monkeypatch, "sweep", run_sweep=lambda c, **kw: seen.append(kw) or {})
    monkeypatch.setattr(settings, "granola_client", lambda **kw: "GRANOLA")
    monkeypatch.setattr(settings, "circleback_clients", lambda config, **kw: pytest.fail("not needed"))
    runner.run_task(ctx, "sweep", {"granola_only": True})
    assert seen[0]["circleback"] is None and seen[0]["granola_only"] is True


def test_reindex_applies_overrides_to_digests_and_commits(tmp_repo, monkeypatch):
    (tmp_repo / "config" / "overrides.yml").write_text(
        yaml.safe_dump({"2026-09-23_darius-kemp": {"firm": "Example Mutual Inc", "seat": "claims manager / supervisor"}}), encoding="utf-8")
    ctx = Ctx.for_tests(tmp_repo)
    write_digest(ctx.repo, darius_digest())

    def regenerate(c):
        c.repo.write("calls.csv", "call_id\n")
        return ["calls.csv"]

    stub(monkeypatch, "indexes", regenerate=regenerate)
    out = runner.run_task(ctx, "reindex", {})
    d = read_digest(ctx.repo, digest_path("2026-09-23_darius-kemp"))
    assert (d.firm, d.firm_source, d.seat) == ("Example Mutual Inc", "manual", "claims manager / supervisor")
    assert out["result"]["digests_updated"] == ["digests/2026-09-23_darius-kemp.md"]
    assert set(ctx.remote.commits[-1]["paths"]) == {"digests/2026-09-23_darius-kemp.md", "calls.csv"}


def test_reindex_skips_a_digest_file_it_cannot_read(tmp_repo, monkeypatch):
    (tmp_repo / "config" / "overrides.yml").write_text(
        yaml.safe_dump({"2026-09-23_darius-kemp": {"firm": "Example Mutual Inc"}, "2026-09-01_jane-doe": {"firm": "Other"}}),
        encoding="utf-8")
    ctx = Ctx.for_tests(tmp_repo)
    write_digest(ctx.repo, darius_digest())
    broken = "---\ncall_id: 2026-09-01_jane-doe\ntranscript: transcripts/jane.md\nseat: [unclosed\n---\n"
    ctx.repo.write("digests/2026-09-01_jane-doe.md", broken)
    stub(monkeypatch, "indexes", regenerate=lambda c: ["calls.csv"])
    out = runner.run_task(ctx, "reindex", {})
    assert out["result"]["digests_updated"] == ["digests/2026-09-23_darius-kemp.md"]
    assert out["result"]["unreadable"] == ["digests/2026-09-01_jane-doe.md"]
    assert ctx.repo.read("digests/2026-09-01_jane-doe.md") == broken      # fixed by hand, never rewritten
    assert "digests/2026-09-01_jane-doe.md" not in ctx.remote.commits[-1]["paths"]


def test_reindex_posts_the_overrides_it_skips_to_ops(tmp_repo, monkeypatch):
    (tmp_repo / "config" / "overrides.yml").write_text(
        "2026-09-23_darius-kemp: {date: 2026-09-31, firm: Example Mutual Inc}\n", encoding="utf-8")
    ctx = Ctx.for_tests(tmp_repo)
    write_digest(ctx.repo, darius_digest())
    stub(monkeypatch, "indexes", regenerate=lambda c: ["calls.csv"])
    out = runner.run_task(ctx, "reindex", {})
    line = "config/overrides.yml: 2026-09-23_darius-kemp date '2026-09-31' is not a real YYYY-MM-DD date; ignored"
    assert out["result"]["override_problems"] == [line]
    assert read_digest(ctx.repo, digest_path("2026-09-23_darius-kemp")).firm == "Example Mutual Inc"   # the rest applies
    [m] = ops_messages(ctx)
    assert line in m["text"] and "2026-09-31" in m["blocks"][0]["text"]["text"]


def test_reindex_without_overrides_only_regenerates(ctx, monkeypatch):
    write_digest(ctx.repo, darius_digest())
    stub(monkeypatch, "indexes", regenerate=lambda c: ["calls.csv"])
    out = runner.run_task(ctx, "reindex", {})
    assert out["result"]["digests_updated"] == []
    assert ctx.remote.commits[-1]["paths"] == ["calls.csv"]


def test_apply_belief_change_goes_to_apply_task(ctx, monkeypatch):
    seen = stub_apply(monkeypatch)
    out = runner.run_task(ctx, "apply_belief_change", {"request_file": "requests/2026-10-06_sam_x.yml"})
    assert out["result"]["outcome"] == "applied"
    slack = {"ref": "1.2:finding:1-9", "change": {"action": "watch"}, "via": "slack", "slack_item": {}}
    runner.run_task(ctx, "apply_belief_change", slack)
    assert seen == [{"request_file": "requests/2026-10-06_sam_x.yml"}, slack]


def test_apply_belief_change_without_file_or_ref_is_invalid(ctx, monkeypatch):
    seen = stub_apply(monkeypatch)
    out = runner.run_task(ctx, "apply_belief_change", {"change": {"action": "watch"}}, attempt=0)
    assert out["ok"] is False and seen == []
    assert buttons(ops_messages(ctx)[0]) == []


def test_unknown_kind_is_a_final_failure_without_retry(ctx):
    out = runner.run_task(ctx, "nope", {}, attempt=0)
    assert out["ok"] is False
    [msg] = ops_messages(ctx)
    assert buttons(msg) == []          # nothing a Re-run could fix


# ---------- failures ----------

def test_early_failure_reraises_so_cloud_tasks_retries(ctx, monkeypatch):
    marked = []

    def process_call(c, transcript, **kw):
        raise RuntimeError("model timeout")

    stub_process(monkeypatch, process_call, marked)
    with pytest.raises(RuntimeError, match="model timeout"):
        runner.run_task(ctx, "process_call", {"transcript": "transcripts/a.md"}, attempt=3, max_attempts=5)
    assert ops_messages(ctx) == []
    assert marked == []


def test_final_attempt_posts_rerun_and_marks_failure(ctx, monkeypatch):
    marked = []

    def process_call(c, transcript, **kw):
        raise RuntimeError("model timeout " + chr(0x2014) + " again")

    stub_process(monkeypatch, process_call, marked)
    out = runner.run_task(ctx, "process_call", {"transcript": "transcripts/a.md"}, attempt=4, max_attempts=5)
    assert out["ok"] is False and out["final"] is True
    assert marked[0][0] == "transcripts/a.md" and "model timeout" in marked[0][1]
    [msg] = ops_messages(ctx)
    assert "transcripts/a.md" in msg["text"] and "5 of 5" in msg["text"]
    assert chr(0x2014) not in json.dumps(msg["blocks"]) + msg["text"]
    [button] = buttons(msg)
    assert button["action_id"] == "rerun"
    assert json.loads(button["value"]) == {"kind": "process_call",
                                           "payload": {"transcript": "transcripts/a.md", "force": True}}


def test_failure_of_an_em_dash_filename_shows_a_hyphen_and_reruns_the_real_file(ctx, monkeypatch):
    rel = "transcripts/darius " + chr(0x2014) + " notes.md"

    def process_call(c, transcript, **kw):
        raise RuntimeError("model timeout")

    stub_process(monkeypatch, process_call)
    runner.run_task(ctx, "process_call", {"transcript": rel}, attempt=4, max_attempts=5)
    [msg] = ops_messages(ctx)
    shown = [b for b in msg["blocks"] if b["type"] != "actions"]
    assert "transcripts/darius - notes.md" in msg["text"]
    assert chr(0x2014) not in msg["text"] + json.dumps(shown, ensure_ascii=False)
    [button] = buttons(msg)
    assert json.loads(button["value"])["payload"]["transcript"] == rel


def test_cost_cap_is_final_on_any_attempt(ctx, monkeypatch):
    marked = []

    def process_call(c, transcript, **kw):
        raise CostCapExceeded("spent $5.10 > cap $5.00")

    stub_process(monkeypatch, process_call, marked)
    out = runner.run_task(ctx, "process_call", {"transcript": "transcripts/a.md"}, attempt=0, max_attempts=5)
    assert out["ok"] is False
    assert len(marked) == 1
    [msg] = ops_messages(ctx)
    assert "cost cap" in msg["text"].lower()


def test_long_rerun_payload_is_shortened(ctx, monkeypatch):
    def start_backfill(c, paths, batch_id):
        raise RuntimeError("github down")

    stub_process(monkeypatch, start_backfill=start_backfill)
    paths = [f"transcripts/2026-09-{i:02d}_some-long-contact-name-{i}.md" for i in range(100)]
    runner.run_task(ctx, "backfill", {"paths": paths, "batch_id": "abc"}, attempt=4)
    [button] = buttons(ops_messages(ctx)[0])
    assert len(button["value"]) <= 2000
    assert json.loads(button["value"]) == {"kind": "backfill", "payload": {"batch_id": "abc"}}


def _slack_change_payload(block_chars: int) -> dict:
    """A Slack click on a top-level message: slack_item carries that message's locked blocks."""
    blocks = [{"type": "section", "block_id": f"b{i}", "text": {"type": "mrkdwn", "text": "x" * 180}}
              for i in range(block_chars // 200 + 1)]
    return {"ref": "1727470000.000100:verdict:B-003:1", "via": "slack",
            "change": {"action": "dismiss_verdict", "target": "B-003", "requested_by": "alex",
                       "sources": [{"call_id": "2026-09-23_darius-kemp", "span": "1000-1045"}]},
            "slack_item": {"channel": "C1", "ts": "1727470000.000100", "block_id": "verdict:B-003:1",
                           "thread_ts": None, "blocks": blocks, "text": "brief " * 50}}


def test_rerun_keeps_the_change_and_drops_the_message_copy(ctx, monkeypatch):
    def apply_task(c, payload):
        raise RuntimeError("github down")

    stub(monkeypatch, "apply", apply_task=apply_task)
    payload = _slack_change_payload(2_000)
    assert len(json.dumps({"kind": "apply_belief_change", "payload": payload})) > 2000
    runner.run_task(ctx, "apply_belief_change", payload, attempt=4)
    [button] = buttons(ops_messages(ctx)[0])
    assert len(button["value"]) <= 2000
    carried = json.loads(button["value"])["payload"]
    assert carried["change"] == payload["change"] and carried["ref"] == payload["ref"]
    assert carried["slack_item"] == {"channel": "C1", "ts": "1727470000.000100", "block_id": "verdict:B-003:1",
                                     "thread_ts": None}


def test_rerun_that_cannot_carry_the_change_posts_no_button(ctx, monkeypatch):
    def apply_task(c, payload):
        raise RuntimeError("github down")

    stub(monkeypatch, "apply", apply_task=apply_task)
    payload = _slack_change_payload(200)
    payload["change"]["statement"] = "a very long statement " * 120
    runner.run_task(ctx, "apply_belief_change", payload, attempt=4)
    [msg] = ops_messages(ctx)
    assert buttons(msg) == []
    assert "redone" in msg["text"]
    assert runner.fit_rerun_value("apply_belief_change", payload) is None


def test_failures_in_dry_run_or_without_slack_post_nothing(ctx, monkeypatch):
    stub_process(monkeypatch, lambda c, t, **kw: (_ for _ in ()).throw(RuntimeError("boom")), [])
    ctx.dry_run = True
    assert runner.run_task(ctx, "process_call", {"transcript": "t.md"}, attempt=4)["ok"] is False
    ctx.dry_run, ctx.slack = False, None
    assert runner.run_task(ctx, "process_call", {"transcript": "t.md"}, attempt=4)["ok"] is False


# ---------- budget ----------

def test_budget_stops_the_task_and_blocks_late_side_effects(ctx, monkeypatch):
    monkeypatch.setattr(runner, "BUDGET_SECONDS", 0.2)
    finished = threading.Event()
    late: list[str] = []

    def process_call(c, transcript, **kw):
        time.sleep(0.5)
        for attempt in (lambda: c.slack.post("#discovery-calls", [], "late brief"),
                        lambda: c.remote.commit_and_push("late", ["x"]),
                        lambda: c.repo.write("late.md", "x"),
                        lambda: c.llms("fake:a")):
            try:
                attempt()
            except runner.TaskTimeout:
                late.append("blocked")
        finished.set()

    stub_process(monkeypatch, process_call)
    with pytest.raises(runner.TaskTimeout):
        runner.run_task(ctx, "process_call", {"transcript": "transcripts/a.md"}, attempt=0)
    assert finished.wait(3)
    assert late == ["blocked"] * 4
    assert ctx.slack.live("#discovery-calls") == []
    assert ctx.remote.commits == [] and not ctx.repo.exists("late.md")


def test_budget_on_final_attempt_reports_to_ops(ctx, monkeypatch):
    monkeypatch.setattr(runner, "BUDGET_SECONDS", 0.1)
    stub_process(monkeypatch, lambda c, t, **kw: time.sleep(0.3), [])
    out = runner.run_task(ctx, "process_call", {"transcript": "transcripts/a.md"}, attempt=4)
    assert out["ok"] is False
    assert "budget" in ops_messages(ctx)[0]["text"]


def test_within_budget_side_effects_reach_the_real_ports(ctx, monkeypatch):
    def process_call(c, transcript, **kw):
        c.slack.post("#discovery-calls", [], "brief")
        c.queue.enqueue("backcheck", {})
        c.repo.write("x.md", "x")
        return FakeProcessResult(transcript, "digested")

    stub_process(monkeypatch, process_call)
    runner.run_task(ctx, "process_call", {"transcript": "t.md"})
    assert len(ctx.slack.live("#discovery-calls")) == 1
    assert ctx.queue.of("backcheck") and ctx.repo.exists("x.md")


def test_no_budget_runs_inline(ctx, monkeypatch):
    seen = []
    stub_process(monkeypatch, lambda c, t, **kw: seen.append(threading.current_thread().name))
    runner.run_task(ctx, "process_call", {"transcript": "t.md"}, budget=False)
    assert seen == [threading.current_thread().name]
