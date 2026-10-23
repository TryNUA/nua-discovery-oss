"""Tests for discovery/process.py: process_call, briefs, backfill batches and final failures. Offline.

Models are FakeLLMs driven by tests/labels/*.yml: the extractors return the labeled claims (the second
one only every other claim, so found_by varies), the verdict models return the labeled belief checks and
prep coverage, and repairs give up. discovery/indexes.py (Task 11) is stubbed so these tests only exercise
process.py.
"""
from __future__ import annotations

import json
import re
import shutil
import sys
import types
from datetime import datetime, timedelta
from pathlib import Path

import pytest
import yaml

import discovery
from discovery import extract as ex
from discovery import merge
from discovery import process as pr
from discovery import verdicts as vd
from discovery.bakeoff import _prompt_claims
from discovery.beliefs import parse_beliefs
from discovery.brief import render_text
from discovery.digest import load_digests, read_digest, write_digest
from discovery.ingest.granola import note_to_standard
from discovery.llm import FakeLLM, LLMError
from discovery.parse import parse, write_standard
from discovery.verify import normalize

from conftest import FIXTURE_NAMES, ROOT, fixture_text

GOLDEN = ROOT / "tests" / "golden"
LABELS = ROOT / "tests" / "labels"
EM_DASH = chr(0x2014)

DARIUS_EMAIL = "darius@example.com"
ALEX_EMAIL = "alex.rivera@gmail.com"
START = "2026-09-23T16:00:00-07:00"
DARIUS_ID = "2026-09-23_darius-kemp"
ATTORNEY = "The minute a lawyer is retained, the claim costs more, period."
CIRCLEBACK = "transcripts/circleback_123.md"
GRANOLA = "transcripts/granola_abc.md"


# ---------- indexes stub (Task 11 runs in parallel) ----------

@pytest.fixture(autouse=True)
def fake_indexes(monkeypatch):
    """Replace discovery.indexes with a small stand-in: calls.csv only, and simple evidence counts."""
    mod = types.ModuleType("discovery.indexes")
    mod.GENERATED = ["calls.csv"]
    mod.regenerated = 0

    def regenerate(ctx):
        mod.regenerated += 1
        rows = "".join(f"{d.call_id},{d.status}\n" for d in load_digests(ctx.repo))
        ctx.repo.write("calls.csv", "call_id,status\n" + rows)
        return ["calls.csv"]

    def load_verdict_overrides(repo):
        return list(yaml.safe_load(repo.read_or("config/verdict_overrides.yml", "")) or [])

    def evidence_counts(digests, beliefs, overrides=None):
        mod.overrides_seen = overrides
        dismissed = {(o.get("call_id"), o.get("target")) for o in overrides or []}
        out: dict[str, dict] = {}
        for d in digests:
            if d.status != "digested":
                continue
            for check in d.belief_checks:
                if (d.call_id, check.target) in dismissed:
                    continue
                e = out.setdefault(check.target, {"calls": set(), "seat_list": []})
                e["calls"].add(d.call_id)
                if d.seat and d.seat not in e["seat_list"]:
                    e["seat_list"].append(d.seat)
        return {k: {"calls": len(v["calls"]), "seats": len(v["seat_list"]), "seat_list": v["seat_list"]}
                for k, v in out.items()}

    mod.regenerate = regenerate
    mod.evidence_counts = evidence_counts
    mod.load_verdict_overrides = load_verdict_overrides
    mod.overrides_seen = None
    monkeypatch.setitem(sys.modules, "discovery.indexes", mod)
    monkeypatch.setattr(discovery, "indexes", mod, raising=False)
    return mod


# ---------- label-driven fake models ----------

def _label(name: str) -> dict:
    return yaml.safe_load((LABELS / f"{name}.yml").read_text(encoding="utf-8"))


LABEL_DATA = {name: _label(name) for name in FIXTURE_NAMES}
_FINDING_LINE = re.compile(r'^<finding id="([^"]+)" reader="\d+">\ntext: (.*)$', re.M)
_ENTRY_LINE = re.compile(r"^([BW]-\d+) \((?:belief|watching)\):", re.M)
_QUESTION_LINE = re.compile(r"^(\d+)\. (.+)$", re.M)


def _which(prompt: str) -> dict:
    """The label whose quotes show up most in the prompt."""
    text = normalize(prompt)
    scores = {name: sum(1 for c in lab["findings"] for q in c["quotes"] if normalize(q["quote"]) in text)
              for name, lab in LABEL_DATA.items()}
    return LABEL_DATA[max(scores, key=scores.get)]


def _claim_reply(c: dict) -> dict:
    scope = c.get("scope") or {}
    return {
        "text": c["text"],
        "quotes": [{"quote": q["quote"], "turns": [], "question_turn": q.get("question_turn"),
                    "speaker": q.get("speaker")} for q in c["quotes"]],
        "scope": {k: scope.get(k) for k in ("line", "party", "segment", "seat")},
        "flags": {"hedged": False, "firsthand": True, "prompted": False},
    }


def _extraction(label: dict, subset: bool) -> dict:
    claims = label["findings"][::2] if subset else label["findings"]
    return {
        "who": {**label["who"], "firm_source": "transcript" if label["who"].get("firm") else None},
        "summary": label.get("summary") or [],
        "findings": [_claim_reply(c) for c in claims],
        "follow_ups": label.get("follow_ups") or [],
        "mentions": label.get("mentions") or {"people": [], "firms": []},
        "ask_next": label.get("ask_next") or [],
    }


def _verdict(label: dict, prompt: str) -> dict:
    ids = {normalize(q): cid for cid, quotes in _prompt_claims(prompt).items() for q in quotes}

    def claim_ids(quotes):
        out = []
        for q in quotes or []:
            n = normalize(q)
            cid = ids.get(n) or next((i for k, i in ids.items() if n in k or k in n), None)
            if cid and cid not in out:
                out.append(cid)
        return out

    entries = set(_ENTRY_LINE.findall(prompt))
    checks = []
    for bc in label.get("belief_checks") or []:
        cids = claim_ids(bc.get("quotes"))
        if bc["target"] in entries and cids:
            checks.append({"target": bc["target"], "verdict": bc["verdict"], "claims": cids,
                           "reason": bc.get("note") or ""})
    section = prompt.split("### Prep questions", 1)[-1].split("### The call", 1)[0]
    by_text = {q["question"]: q for q in label.get("question_coverage") or []}
    coverage = []
    for number, text in _QUESTION_LINE.findall(section):
        q = by_text.get(text.strip())
        if q:
            coverage.append({"question": int(number), "status": q["status"], "claims": claim_ids(q.get("quotes")),
                             "reason": ""})
    speakers = []
    if "Speaker labels: named" not in prompt:
        speakers = [{"claim": cid, "speaker": label["who"]["contact"]} for cid in dict.fromkeys(ids.values())]
    return {"belief_checks": checks, "question_coverage": coverage, "speakers": speakers}


def _consolidation(prompt: str) -> dict:
    """Group the findings with the same text, as the two label-driven extractors return the same labels."""
    groups: dict[str, dict] = {}
    for fid, text in _FINDING_LINE.findall(prompt):
        g = groups.setdefault(text, {"ids": [], "text": text, "scope": {"line": "", "party": "", "segment": "", "seat": ""},
                                     "flags": {"hedged": False, "firsthand": True, "prompted": False}})
        g["ids"].append(fid)
    return {"groups": list(groups.values())}


def label_responder(*, subset: bool = False, fail: bool = False):
    def respond(system, prompt, schema):
        if fail:
            return LLMError("provider down")
        if schema is ex.EXTRACT_SCHEMA:
            return _extraction(_which(prompt), subset)
        if schema is vd.VERDICT_SCHEMA:
            return _verdict(_which(prompt), prompt)
        if schema is ex.REPAIR_SCHEMA:
            return {"quote": None}
        if schema is merge.CONSOLIDATE_SCHEMA:
            return _consolidation(prompt)
        return {"belief_checks": [], "question_coverage": [], "speakers": []}
    return respond


@pytest.fixture
def models(ctx):
    """Register label-driven fakes under the configured extractor and arbiter specs."""
    p = ctx.config.pipeline
    fakes = {
        p.extractors[0]: FakeLLM(p.extractors[0], label_responder()),
        p.extractors[1]: FakeLLM(p.extractors[1], label_responder(subset=True)),
        p.arbiter: FakeLLM(p.arbiter, label_responder()),
    }
    for fake in fakes.values():
        ctx.llms.register(fake)
    return fakes


def model_calls(models) -> int:
    return sum(len(f.calls) for f in models.values())


# ---------- repo helpers ----------

@pytest.fixture
def golden(tmp_repo: Path) -> Path:
    """Test beliefs, decisions and Darius's prep file in the temp repo."""
    shutil.copy(GOLDEN / "beliefs.md", tmp_repo / "beliefs.md")
    shutil.copy(GOLDEN / "decisions.md", tmp_repo / "decisions.md")
    shutil.copy(GOLDEN / "prep_darius-kemp.md", tmp_repo / "prep" / "2026-09-23_darius-kemp.md")
    return tmp_repo


def live_text(name: str = "darius-kemp", *, source: str = "circleback", source_id: str = "123",
              labels: str = "named", start: str = START, attendees: list[dict] | None = None,
              ingested: str | None = None) -> str:
    """A fixture rewritten as a standard live transcript, as the receiver would write it."""
    parsed = parse(f"transcripts/{name}.md", fixture_text(name), commit_date="2026-10-04")

    def speaker(s):
        if labels == "me_them":
            return "Me" if s == "Alex Rivera" else "Them"
        return s

    fm = {"source": source, "source_id": source_id, "ical_uid": None, "start": start, "start_source": "calendar",
          "recorded_by": "alex",
          "attendees": attendees or [{"name": "Alex Rivera", "email": ALEX_EMAIL},
                                     {"name": "Darius Kemp", "email": DARIUS_EMAIL}],
          "speaker_labels": labels}
    if ingested:
        fm["ingested_at"] = ingested
    return write_standard(fm, parsed.notes, [(t.time, speaker(t.speaker), t.text) for t in parsed.turns])


def add(repo: Path, rel: str, text: str) -> str:
    (repo / rel).parent.mkdir(parents=True, exist_ok=True)
    (repo / rel).write_text(text, encoding="utf-8")
    return rel


def add_circleback(repo: Path) -> str:
    return add(repo, CIRCLEBACK, live_text())


def add_granola(repo: Path) -> str:
    return add(repo, GRANOLA, live_text(source="granola", source_id="abc", labels="me_them"))


def top_messages(ctx, channel: str | None = None) -> list[dict]:
    channel = channel or ctx.channel("calls")
    return [m for m in ctx.slack.live(channel) if m["thread_ts"] is None]


def digest(ctx, call_id: str = DARIUS_ID):
    return read_digest(ctx.repo, f"digests/{call_id}.md")


# ---------- live calls ----------

def test_live_call_is_digested_and_briefed_once(golden, ctx, models, fake_indexes):
    path = add_circleback(golden)
    r = pr.process_call(ctx, path)
    assert (r.transcript, r.outcome, r.call_id, r.briefed) == (path, "digested", DARIUS_ID, True)

    d = digest(ctx)
    assert (d.status, d.origin, d.transcript, d.prompt_version) == ("digested", "live", path, "v1")
    assert (d.contact, d.firm, d.seat, d.date, d.date_source) == ("Darius Kemp", None, "claims adjuster (staff)", "2026-09-23", "api")
    assert d.prep == "prep/2026-09-23_darius-kemp.md"
    assert d.models["extract"] == ctx.config.pipeline.extractors
    assert d.degraded == []
    assert [c.id for c in d.claims] == [f"c{n:02d}" for n in range(1, len(d.claims) + 1)]
    findings = LABEL_DATA["darius-kemp"]["findings"]
    assert len(d.claims) == len(findings)
    assert {c.found_by for c in d.claims} == {1, 2}
    assert [len(c.quotes) for c in d.claims] == [len(f["quotes"]) for f in findings]
    assert d.verification.quotes == sum(len(f["quotes"]) for f in findings + findings[::2])
    assert d.verification.dropped == []

    assert {(c.target, c.verdict) for c in d.belief_checks} == {
        ("B-004", "contradicts"), ("B-001", "supports"), ("B-002", "supports"), ("B-003", "supports"),
        ("W-001", "supports")}
    assert {c.decided_by for c in d.belief_checks} == {"agree"}
    assert [q.status for q in d.question_coverage] == ["answered", "answered", "answered", "dodged", "never_asked"]

    beliefs = parse_beliefs((golden / "beliefs.md").read_text())
    assert d.checked == {e.id: e.version for e in beliefs.current()}
    cited = {cid for c in d.belief_checks for cid in c.claims}
    assert d.new_findings == [c.id for c in d.claims if c.id not in cited]

    # brief: one top message carrying the transcript path, thread replies recorded on the digest
    [top] = top_messages(ctx)
    assert top["metadata"] == {"event_type": "discovery_brief", "event_payload": {"key": path}}
    assert top["text"].startswith("Darius Kemp · claims adjuster (staff) · 38m · Alex · Circleback")
    assert "Touches D-002" in top["text"]
    replies = ctx.slack.replies(ctx.channel("calls"), top["ts"])
    assert d.brief.ts == top["ts"] and d.brief.thread == [m["ts"] for m in replies] and replies
    assert all(EM_DASH not in m["text"] for m in ctx.slack.messages)

    # one commit with the digest and the indexes
    [commit] = ctx.remote.commits
    assert f"digests/{DARIUS_ID}.md" in commit["paths"] and "calls.csv" in commit["paths"]

    # a re-run of the same transcript skips without models or Slack
    calls_before, messages_before = model_calls(models), len(ctx.slack.messages)
    r2 = pr.process_call(ctx, path)
    assert (r2.outcome, r2.call_id, r2.briefed) == ("skipped:current", DARIUS_ID, False)
    assert model_calls(models) == calls_before and len(ctx.slack.messages) == messages_before
    assert len(ctx.remote.commits) == 1


def test_forced_rerun_keeps_call_id_and_does_not_brief_again(golden, ctx, models):
    path = add_circleback(golden)
    first = pr.process_call(ctx, path)
    brief = digest(ctx).brief
    messages = len(ctx.slack.messages)

    again = pr.process_call(ctx, path, force=True)
    assert (again.outcome, again.call_id, again.briefed) == ("digested", first.call_id, False)
    assert digest(ctx).brief == brief
    assert len(ctx.slack.messages) == messages
    assert len(ctx.remote.commits) == 2


def test_stale_prompt_version_is_reprocessed(golden, ctx, models):
    path = add_circleback(golden)
    pr.process_call(ctx, path)
    ctx.config.pipeline.prompt_version = "v2"
    r = pr.process_call(ctx, path)
    assert (r.outcome, r.call_id, r.briefed) == ("digested", DARIUS_ID, False)
    assert digest(ctx).prompt_version == "v2"


def test_one_extractor_down_is_degraded(golden, ctx, models):
    spec = ctx.config.pipeline.extractors[1]
    ctx.llms.register(FakeLLM(spec, label_responder(fail=True)))
    r = pr.process_call(ctx, add_circleback(golden))
    d = digest(ctx)
    assert r.outcome == "digested"
    assert d.degraded == ["one_model"]
    assert {c.found_by for c in d.claims} == {1}
    assert {c.decided_by for c in d.belief_checks} == {"single"}
    assert d.models["extract"] == [ctx.config.pipeline.extractors[0]]


def test_consolidation_joins_findings_the_readers_quoted_differently(golden, ctx, models):
    """The second reader quotes only the second sentence of a two-quote finding: no quote overlaps the
    first reader's first quote, yet consolidation joins them and found_by counts both models."""
    second = ctx.config.pipeline.extractors[1]
    base = label_responder()

    def respond(system, prompt, schema):
        out = base(system, prompt, schema)
        if schema is ex.EXTRACT_SCHEMA:
            for f in out["findings"]:
                f["quotes"] = f["quotes"][-1:]
        return out

    ctx.llms.register(FakeLLM(second, respond))
    pr.process_call(ctx, add_circleback(golden))
    d = digest(ctx)
    findings = LABEL_DATA["darius-kemp"]["findings"]
    assert d.degraded == [] and len(d.claims) == len(findings)
    assert [len(c.quotes) for c in d.claims] == [len(f["quotes"]) for f in findings]
    assert {c.found_by for c in d.claims} == {2}


def test_a_failed_consolidation_falls_back_to_overlap_merge(golden, ctx, models):
    base = label_responder()

    def respond(system, prompt, schema):
        return LLMError("arbiter down") if schema is merge.CONSOLIDATE_SCHEMA else base(system, prompt, schema)

    ctx.llms.register(FakeLLM(ctx.config.pipeline.arbiter, respond))
    r = pr.process_call(ctx, add_circleback(golden))
    d = digest(ctx)
    assert r.outcome == "digested"
    assert d.degraded == ["no_consolidation"]
    assert len(d.claims) == len(LABEL_DATA["darius-kemp"]["findings"])   # the same quotes still join by overlap
    assert {c.found_by for c in d.claims} == {1, 2}


def test_both_extractors_down_raises(golden, ctx):
    for spec in ctx.config.pipeline.extractors:
        ctx.llms.register(FakeLLM(spec, label_responder(fail=True)))
    with pytest.raises(LLMError):
        pr.process_call(ctx, add_circleback(golden))
    assert not (golden / "digests" / f"{DARIUS_ID}.md").exists()


def test_slack_failure_still_commits_the_digest(golden, ctx, models):
    def down(*args, **kwargs):
        raise RuntimeError("slack is down")

    ctx.slack.post = down
    r = pr.process_call(ctx, add_circleback(golden))
    assert (r.outcome, r.briefed) == ("digested", False)
    assert digest(ctx).brief is None
    assert len(ctx.remote.commits) == 1


def test_speakers_come_from_verdicts_on_unnamed_calls(golden, ctx, models):
    pr.process_call(ctx, add_granola(golden))
    d = digest(ctx)
    assert d.speaker_labels == "me_them"
    assert {q.speaker for c in d.claims for q in c.quotes} == {"Darius Kemp"}
    assert {q.attribution for c in d.claims for q in c.quotes} == {"inferred"}


# ---------- backfill ----------

def test_backfill_never_briefs(golden, ctx, models):
    path = add(golden, "transcripts/2026-09-23_darius-kemp.md", fixture_text("darius-kemp"))
    r = pr.process_call(ctx, path)
    assert (r.outcome, r.call_id, r.briefed) == ("digested", DARIUS_ID, False)
    d = digest(ctx)
    assert (d.origin, d.date_source, d.brief) == ("backfill", "filename", None)
    assert d.prep == "prep/2026-09-23_darius-kemp.md"     # matched by contact name within the window
    assert ctx.slack.messages == []


def test_start_backfill_writes_batch_enqueues_and_summarizes_once(golden, ctx, models):
    paths = [add(golden, "transcripts/2026-09-30_lena-okafor.md", fixture_text("lena-okafor")),
             add(golden, "transcripts/darius-kemp.md", fixture_text("darius-kemp")),
             add(golden, "transcripts/wade-pruitt.md", fixture_text("wade-pruitt")),
             add(golden, "transcripts/viktor-lang.md", fixture_text("viktor-lang"))]
    pr.process_call(ctx, paths[0])          # already digested before the batch starts

    pr.start_backfill(ctx, paths, "abc123")
    batch = yaml.safe_load((golden / "config" / "batches" / "abc123.yml").read_text())
    assert batch["batch"] == "abc123" and batch["paths"] == paths
    assert ctx.remote.commits[-1]["paths"] == ["config/batches/abc123.yml"]
    queued = ctx.queue.of("process_call")
    assert [t["payload"] for t in queued] == [{"transcript": p, "batch": "abc123"} for p in paths[1:]]
    assert all(t["name"] for t in queued)

    assert pr.batch_completion_check(ctx, "abc123") is False
    for t in queued:
        r = pr.process_call(ctx, t["payload"]["transcript"], batch=t["payload"]["batch"])
        assert r.briefed is False
        posted = [m for m in ctx.slack.messages if (m["metadata"] or {}).get("event_type") == "discovery_batch"]
        assert len(posted) == (1 if t is queued[-1] else 0)

    [summary] = [m for m in ctx.slack.messages if m["metadata"]]
    assert summary["channel"] == ctx.channel("calls")
    assert summary["metadata"] == {"event_type": "discovery_batch", "event_payload": {"key": "batch:abc123"}}
    assert "4 transcripts" in summary["text"]
    assert "undated_darius-kemp" in summary["text"] and "unknown date" in summary["text"]
    assert EM_DASH not in summary["text"]
    assert len(summary["blocks"]) <= 50

    # the summary's ts is recorded in the batch file and committed
    batch = yaml.safe_load((golden / "config" / "batches" / "abc123.yml").read_text())
    assert batch["summary_ts"] == summary["ts"] and batch["paths"] == paths
    assert ctx.remote.commits[-1]["paths"] == ["config/batches/abc123.yml"]
    commits = len(ctx.remote.commits)

    # a second check (the sweep's) trusts summary_ts: no Slack scan, nothing posted or committed
    def no_scan(*a, **kw):
        raise AssertionError("summary_ts is set; Slack must not be scanned")
    ctx.slack.find_by_metadata = no_scan
    assert pr.batch_completion_check(ctx, "abc123") is True
    assert len([m for m in ctx.slack.messages if m["metadata"]]) == 1
    assert len(ctx.remote.commits) == commits
    # a skipped re-run in the batch still runs the check without posting again
    assert pr.process_call(ctx, paths[1], batch="abc123").outcome == "skipped:current"
    assert len([m for m in ctx.slack.messages if m["metadata"]]) == 1


def test_backfill_with_everything_digested_posts_summary_right_away(golden, ctx, models):
    path = add(golden, "transcripts/2026-09-23_darius-kemp.md", fixture_text("darius-kemp"))
    pr.process_call(ctx, path)
    pr.start_backfill(ctx, [path], "b2")
    assert ctx.queue.of("process_call") == []
    assert [m["metadata"]["event_payload"]["key"] for m in ctx.slack.messages if m["metadata"]] == ["batch:b2"]


def test_found_summary_is_recorded_and_a_backfill_retry_keeps_summary_ts(golden, ctx, models):
    path = add(golden, "transcripts/2026-09-23_darius-kemp.md", fixture_text("darius-kemp"))
    pr.process_call(ctx, path)
    rel = "config/batches/b3.yml"
    ctx.repo.write(rel, yaml.safe_dump({"batch": "b3", "created": "2026-09-20T10:00:00-07:00", "paths": [path]}))
    key = {"event_type": "discovery_batch", "event_payload": {"key": "batch:b3"}}
    ts = ctx.slack.post(ctx.channel("calls"), [], "Backfill batch b3 done", metadata=key)   # posted, not recorded

    assert pr.batch_completion_check(ctx, "b3") is True
    assert len([m for m in ctx.slack.messages if m["metadata"]]) == 1     # found, not posted again
    assert yaml.safe_load(ctx.repo.read(rel))["summary_ts"] == ts
    assert ctx.remote.commits[-1]["paths"] == [rel]

    pr.start_backfill(ctx, [path], "b3")            # a retry of the backfill task rewrites the file
    data = yaml.safe_load(ctx.repo.read(rel))
    assert data == {"batch": "b3", "created": "2026-09-20T10:00:00-07:00", "paths": [path], "summary_ts": ts}
    assert len([m for m in ctx.slack.messages if m["metadata"]]) == 1


def test_dry_run_check_records_no_summary_ts(golden, ctx, models):
    path = add(golden, "transcripts/2026-09-23_darius-kemp.md", fixture_text("darius-kemp"))
    pr.process_call(ctx, path)
    ctx.repo.write("config/batches/b5.yml", yaml.safe_dump({"batch": "b5", "paths": [path]}))
    ctx.dry_run = True
    assert pr.batch_completion_check(ctx, "b5") is True
    assert "summary_ts" not in yaml.safe_load(ctx.repo.read("config/batches/b5.yml"))


def test_backfill_without_queue_runs_inline(golden, ctx, models):
    ctx.queue = None
    path = add(golden, "transcripts/2026-09-23_darius-kemp.md", fixture_text("darius-kemp"))
    pr.start_backfill(ctx, [path], "b3")
    assert (golden / "digests" / f"{DARIUS_ID}.md").exists()
    assert len([m for m in ctx.slack.messages if m["metadata"]]) == 1


# ---------- stubs ----------

def test_internal_call_gets_a_stub_and_no_models(golden, ctx, models):
    attendees = [{"name": "Alex Rivera", "email": ALEX_EMAIL}, {"name": "Sam", "email": "sam@acme.dev"}]
    path = add(golden, "transcripts/circleback_900.md", live_text(source_id="900", attendees=attendees))
    r = pr.process_call(ctx, path)
    assert (r.outcome, r.call_id, r.briefed) == ("stub:internal", "circleback_900", False)
    d = digest(ctx, "circleback_900")
    assert (d.status, d.origin, d.transcript, d.recorded_by) == ("internal", "live", path, "alex")
    assert d.claims == [] and d.reason
    assert model_calls(models) == 0 and ctx.slack.messages == []
    assert "digests/circleback_900.md" in ctx.remote.commits[-1]["paths"]
    assert pr.process_call(ctx, path).outcome == "skipped:stub"


def test_parse_failed_stub_and_ops_error(golden, ctx, models):
    path = add(golden, "transcripts/notes.md", "Just some notes.\nNo transcript here.\n")
    r = pr.process_call(ctx, path)
    assert (r.outcome, r.call_id) == ("stub:parse_failed", "notes")
    d = digest(ctx, "notes")
    assert (d.status, d.origin) == ("parse_failed", "backfill")
    assert "unrecognized" in d.reason
    [error] = ctx.slack.live(ctx.channel("ops"))
    assert path in error["text"] and EM_DASH not in error["text"]
    assert model_calls(models) == 0

    assert pr.process_call(ctx, path).outcome == "skipped:stub"
    assert len(ctx.slack.live(ctx.channel("ops"))) == 1


def test_failed_stub_then_rerun_replaces_it(golden, ctx, models):
    path = add_circleback(golden)
    pr.mark_final_failure(ctx, path, "LLMError: every extractor failed")
    stub = digest(ctx, "circleback_123")
    assert (stub.status, stub.origin, stub.transcript) == ("failed", "live", path)
    assert "every extractor failed" in stub.reason
    assert "digests/circleback_123.md" in ctx.remote.commits[-1]["paths"]

    assert pr.process_call(ctx, path).outcome == "skipped:stub"   # never retried automatically

    r = pr.process_call(ctx, path, force=True)                    # Re-run
    assert (r.outcome, r.call_id, r.briefed) == ("digested", DARIUS_ID, True)
    assert not (golden / "digests" / "circleback_123.md").exists()
    assert {"digests/circleback_123.md", f"digests/{DARIUS_ID}.md"} <= set(ctx.remote.commits[-1]["paths"])
    assert [d.call_id for d in load_digests(ctx.repo)] == [DARIUS_ID]


def test_final_failure_on_existing_digest_only_marks_it(golden, ctx, models):
    path = add_circleback(golden)
    pr.process_call(ctx, path)
    before = digest(ctx)
    pr.mark_final_failure(ctx, path, "CostCapExceeded: spent $6.00")
    after = digest(ctx)
    assert after.reprocess_failed == "v1"
    after.reprocess_failed = None
    assert after == before
    assert not (golden / "digests" / "circleback_123.md").exists()


def test_final_failure_in_a_batch_runs_the_completion_check(golden, ctx, models):
    path = add(golden, "transcripts/wade-pruitt.md", fixture_text("wade-pruitt"))
    pr.start_backfill(ctx, [path], "b4")
    pr.mark_final_failure(ctx, path, "LLMError: timeout")
    [summary] = [m for m in ctx.slack.messages if m["metadata"]]
    assert "1 failed" in summary["text"] and path in summary["text"]


# ---------- duplicates and supersede ----------

def test_duplicate_loser_is_recorded_on_the_winner(golden, ctx, models):
    winner = add_circleback(golden)
    assert pr.process_call(ctx, winner).outcome == "digested"
    assert digest(ctx).duplicates == []

    loser = add_granola(golden)                  # me_them loses to named
    calls = model_calls(models)
    r = pr.process_call(ctx, loser)
    assert (r.outcome, r.call_id, r.briefed) == ("duplicate", DARIUS_ID, False)
    d = digest(ctx)
    assert d.transcript == winner and d.duplicates == [loser]
    assert model_calls(models) == calls
    assert pr.process_call(ctx, loser).outcome == "skipped:current"


def test_loser_first_stops_until_the_winner_runs(golden, ctx, models):
    winner, loser = add_circleback(golden), add_granola(golden)
    r = pr.process_call(ctx, loser)
    assert (r.outcome, r.call_id) == ("duplicate", None)
    assert load_digests(ctx.repo) == [] and model_calls(models) == 0

    r = pr.process_call(ctx, winner)
    assert (r.outcome, r.call_id) == ("digested", DARIUS_ID)
    assert digest(ctx).duplicates == [loser]


def test_tie_goes_to_the_copy_ingested_first_not_path_order(golden, ctx, models):
    # Two named Circleback recordings of one call. By path, circleback_123 would win the tie; it was
    # ingested later, so the other copy wins even though it is processed second.
    late = add(golden, CIRCLEBACK, live_text(ingested="2026-09-23T16:45:00-07:00"))
    early = add(golden, "transcripts/circleback_456.md",
                live_text(source_id="456", ingested="2026-09-23T16:40:00-07:00"))
    r = pr.process_call(ctx, late)
    assert (r.outcome, r.call_id) == ("duplicate", None)
    assert load_digests(ctx.repo) == [] and model_calls(models) == 0

    r = pr.process_call(ctx, early)
    assert (r.outcome, r.call_id) == ("digested", DARIUS_ID)
    d = digest(ctx)
    assert (d.transcript, d.duplicates) == (early, [late])


def _adhoc_note(note_id: str, fixture: str, first_item_at: str) -> dict:
    """An ad-hoc Granola note by Alex: no calendar event, only the owner listed, items from a fixture."""
    first = datetime.fromisoformat(first_item_at.replace("Z", "+00:00"))
    turns = parse(f"transcripts/{fixture}.md", fixture_text(fixture), commit_date="2026-10-04").turns
    items = []
    for i, t in enumerate(turns):
        at = (first + timedelta(seconds=5 * i)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
        me = t.speaker in ("Alex Rivera", "Me")
        items.append({"speaker": {"source": "microphone" if me else "speaker", "attribution": "me" if me else "them"},
                      "text": t.text, "start_time": at, "end_time": at})
    owner = {"name": "Alex Rivera", "email": ALEX_EMAIL}
    return {"id": note_id, "title": None, "owner": owner, "calendar_event": None, "attendees": [owner],
            "created_at": first_item_at, "summary_markdown": None, "transcript": items}


def test_two_adhoc_calls_one_cofounder_recorded_minutes_apart_are_not_copies(golden, ctx, models):
    # Alex is the only attendee Granola lists on each note, and the two starts are 9 minutes apart.
    # A Acme email never makes two calls the same call, so neither is a duplicate of the other.
    for note in (_adhoc_note("alice", "darius-kemp", "2026-09-23T17:00:00Z"),
                 _adhoc_note("bob", "lena-okafor", "2026-09-23T17:09:00Z")):
        rel, text = note_to_standard(note, config=ctx.config)
        add(golden, rel, text)
    alice, bob = "transcripts/granola_alice.md", "transcripts/granola_bob.md"
    assert pr._same_call_copies(ctx, parse(bob, ctx.repo.read(bob), commit_date="2026-10-04")) == []
    assert pr._same_call_copies(ctx, parse(alice, ctx.repo.read(alice), commit_date="2026-10-04")) == []

    first, second = pr.process_call(ctx, alice), pr.process_call(ctx, bob)
    assert (first.outcome, second.outcome) == ("digested", "digested")
    assert first.call_id != second.call_id
    assert [d.duplicates for d in load_digests(ctx.repo)] == [[], []]


def test_resumed_note_that_held_the_digest_supersedes_under_the_same_call_id(golden, ctx, models):
    # v1 (named, 09:40) holds the digest and beat the named Circleback copy (09:45) on arrival. Someone
    # hits Resume: v2 lands at 11:00. It shares v1's first arrival, so it still wins the tie.
    v1 = add(golden, GRANOLA, live_text(source="granola", source_id="abc",
                                        ingested="2026-09-24T09:40:00-07:00"))
    assert pr.process_call(ctx, v1).call_id == DARIUS_ID
    [top] = top_messages(ctx)
    circleback = add(golden, CIRCLEBACK, live_text(ingested="2026-09-24T09:45:00-07:00"))
    assert (pr.process_call(ctx, circleback).outcome, digest(ctx).duplicates) == ("duplicate", [circleback])

    longer = live_text(source="granola", source_id="abc", ingested="2026-09-24T11:00:00-07:00")
    v2 = add(golden, "transcripts/granola_abc_v2.md",
             longer.rstrip("\n") + "\n\n[37:50] **Darius Kemp**: One more thing before you go.\n")
    r = pr.process_call(ctx, v2)
    assert (r.outcome, r.call_id, r.briefed) == ("superseded", DARIUS_ID, True)
    assert [(d.call_id, d.transcript, d.duplicates) for d in load_digests(ctx.repo)] == [
        (DARIUS_ID, v2, sorted([circleback, v1]))]
    assert [m["ts"] for m in top_messages(ctx)] == [top["ts"]]      # the brief is updated in place
    assert digest(ctx).brief.ts == top["ts"]


def test_digest_records_the_call_length(golden, ctx, models):
    pr.process_call(ctx, add_circleback(golden))
    d = digest(ctx)
    assert d.duration_min == 38                  # last turn at [37:36]
    assert "38 min" in (golden / "digests" / f"{DARIUS_ID}.md").read_text()


def test_supersede_updates_the_brief_in_place(golden, ctx, models):
    first = add_granola(golden)
    assert pr.process_call(ctx, first).briefed
    [top] = top_messages(ctx)
    old_thread = [m["ts"] for m in ctx.slack.replies(ctx.channel("calls"), top["ts"])]
    assert "Granola" in top["text"]

    richer = add_circleback(golden)
    r = pr.process_call(ctx, richer)
    assert (r.outcome, r.call_id, r.briefed) == ("superseded", DARIUS_ID, True)
    d = digest(ctx)
    assert (d.transcript, d.duplicates, d.speaker_labels) == (richer, [first], "named")

    [top_after] = top_messages(ctx)
    assert top_after["ts"] == top["ts"] and "Circleback" in top_after["text"]
    deleted = {m["ts"] for m in ctx.slack.messages if m["deleted"]}
    assert set(old_thread) <= deleted
    new_thread = [m["ts"] for m in ctx.slack.replies(ctx.channel("calls"), top["ts"])]
    assert new_thread and not set(new_thread) & set(old_thread)
    assert d.brief.ts == top["ts"] and d.brief.thread == new_thread
    assert pr.process_call(ctx, richer).outcome == "skipped:current"


@pytest.mark.parametrize("citation", ["history", "verdict_override", "rejection"])
def test_cited_call_is_not_superseded(golden, ctx, models, citation):
    first = add_granola(golden)
    pr.process_call(ctx, first)
    quote = ATTORNEY
    if citation == "history":
        text = (golden / "beliefs.md").read_text().replace(
            "## Retired",
            "### W-002 An attorney on a claim makes it more expensive\n- scope:\n- history:\n"
            f'  - 2026-09-30 watch by Alex via slack, source {DARIUS_ID} "{quote}", ref 1790700000.000100:c01\n\n'
            "## Retired")
        (golden / "beliefs.md").write_text(text)
    elif citation == "verdict_override":
        (golden / "config" / "verdict_overrides.yml").write_text(yaml.safe_dump({"overrides": [
            {"call_id": DARIUS_ID, "target": "B-001", "quote": quote, "who": "Alex", "reason": "x", "ref": "r1"}]}))
    else:
        (golden / "config" / "rejected.yml").write_text(yaml.safe_dump({"rejected": [
            {"type": "add", "target": None, "claims": [{"call_id": DARIUS_ID, "quote": quote}], "who": "Sam",
             "reason": "x", "ref": "r2"}]}))
    snapshot = [dict(m) for m in ctx.slack.messages]
    calls = model_calls(models)

    richer = add_circleback(golden)
    r = pr.process_call(ctx, richer)
    assert (r.outcome, r.call_id, r.briefed) == ("duplicate", DARIUS_ID, False)
    d = digest(ctx)
    assert (d.transcript, d.duplicates) == (first, [richer])
    assert ctx.slack.messages == snapshot
    assert model_calls(models) == calls


# ---------- post_brief ----------

def test_post_brief_retry_updates_the_existing_message(golden, ctx, models):
    path = add_circleback(golden)
    pr.process_call(ctx, path)
    d = digest(ctx)
    ts, thread = d.brief.ts, list(d.brief.thread)

    d.brief = None                               # the push that recorded the brief failed
    write_digest(ctx.repo, d)
    ref = pr.post_brief(ctx, d)
    assert (ref.ts, ref.thread) == (ts, thread)
    assert len(top_messages(ctx)) == 1
    assert not any(m["deleted"] for m in ctx.slack.messages)


def test_post_brief_in_dry_run_posts_nothing(golden, ctx, models):
    pr.process_call(ctx, add_circleback(golden))
    ctx.dry_run = True
    messages = len(ctx.slack.messages)
    ref = pr.post_brief(ctx, digest(ctx))
    assert ref.ts is None and len(ctx.slack.messages) == messages


# ---------- dry run ----------

def test_dry_run_writes_files_but_skips_slack_and_remote(golden, ctx, models):
    ctx.dry_run = True
    r = pr.process_call(ctx, add_circleback(golden))
    assert (r.outcome, r.briefed) == ("digested", False)
    assert "WHAT CHANGED" in r.brief_text and "B-004" in r.brief_text
    assert (golden / "digests" / f"{DARIUS_ID}.md").exists() and (golden / "calls.csv").exists()
    assert ctx.slack.messages == [] and ctx.remote.commits == []


def test_dry_run_with_golden_paths_on_a_bare_fixture(tmp_repo, ctx, models):
    ctx.dry_run = True
    path = add(tmp_repo, "transcripts/darius-kemp.md", fixture_text("darius-kemp"))
    r = pr.process_call(ctx, path, beliefs_path=str(GOLDEN / "beliefs.md"),
                        decisions_path=str(GOLDEN / "decisions.md"), prep_path=str(GOLDEN / "prep_darius-kemp.md"))
    assert (r.outcome, r.call_id) == ("digested", "undated_darius-kemp")
    d = digest(ctx, "undated_darius-kemp")
    assert d.prep == str(GOLDEN / "prep_darius-kemp.md")
    assert [q.status for q in d.question_coverage] == ["answered", "answered", "answered", "dodged", "never_asked"]
    assert "Touches D-002" in r.brief_text
    assert "Contradicts B-004" in r.brief_text
    assert EM_DASH not in r.brief_text
    assert (tmp_repo / "beliefs.md").read_text() == "## Beliefs\n\n## Watching\n\n## Retired\n"


def test_missing_transcript_is_skipped(ctx, models):
    r = pr.process_call(ctx, "transcripts/nope.md")
    assert (r.outcome, r.call_id) == ("skipped:missing", None)


# ---------- round-1 fixes ----------

DROPPED_DARIUS = "transcripts/2026-09-23_darius-kemp.md"    # a drop named by the AGENTS.md call-id convention
UNPARSEABLE = "Just some notes.\nNo transcript here.\n"


def _at(ctx, *args):
    from datetime import datetime
    from zoneinfo import ZoneInfo
    when = datetime(*args, 9, 0, tzinfo=ZoneInfo("America/Los_Angeles"))
    ctx.now = lambda: when


def test_footer_date_is_kept_on_a_rerun_a_year_later(golden, ctx, models):
    path = add(golden, "transcripts/lena-okafor.md", fixture_text("lena-okafor"))
    first = pr.process_call(ctx, path)
    d = digest(ctx, first.call_id)
    assert (first.call_id, d.date, d.date_source) == ("2026-09-30_lena-okafor", "2026-09-30", "footer")

    _at(ctx, 2027, 10, 5)                          # past the call's anniversary
    ctx.config.pipeline.prompt_version = "v2"      # a stale re-run
    again = pr.process_call(ctx, path)
    assert (again.outcome, again.call_id) == ("digested", first.call_id)
    assert (digest(ctx, first.call_id).date, digest(ctx, first.call_id).date_source) == ("2026-09-30", "footer")

    _at(ctx, 2028, 1, 2)                           # and a forced Re-run
    pr.process_call(ctx, path, force=True)
    assert digest(ctx, first.call_id).date == "2026-09-30"


def test_footer_date_on_a_first_run_reads_against_the_batch_date(golden, ctx, models):
    path = add(golden, "transcripts/lena-okafor.md", fixture_text("lena-okafor"))
    _at(ctx, 2026, 10, 2)
    pr.start_backfill(ctx, [path], "late")         # queued; the task only runs a year later
    _at(ctx, 2027, 10, 5)
    r = pr.process_call(ctx, path, batch="late")
    assert r.call_id == "2026-09-30_lena-okafor"
    assert digest(ctx, r.call_id).date == "2026-09-30"


@pytest.mark.parametrize("status", ["parse_failed", "internal", "failed"])
def test_stub_never_overwrites_another_transcripts_digest(golden, ctx, models, status):
    live = add_circleback(golden)
    assert pr.process_call(ctx, live).call_id == DARIUS_ID
    before = (golden / "digests" / f"{DARIUS_ID}.md").read_bytes()

    if status == "parse_failed":
        add(golden, DROPPED_DARIUS, UNPARSEABLE)
        r = pr.process_call(ctx, DROPPED_DARIUS)
        assert (r.outcome, r.call_id) == ("stub:parse_failed", f"{DARIUS_ID}-2")
    elif status == "internal":
        attendees = [{"name": "Alex Rivera", "email": ALEX_EMAIL}, {"name": "Sam", "email": "sam@acme.dev"}]
        add(golden, DROPPED_DARIUS, live_text(source_id="900", attendees=attendees))
        r = pr.process_call(ctx, DROPPED_DARIUS)
        assert (r.outcome, r.call_id) == ("stub:internal", f"{DARIUS_ID}-2")
    else:
        add(golden, DROPPED_DARIUS, fixture_text("darius-kemp"))
        pr.mark_final_failure(ctx, DROPPED_DARIUS, "LLMError: every extractor failed")

    assert (golden / "digests" / f"{DARIUS_ID}.md").read_bytes() == before
    stub = digest(ctx, f"{DARIUS_ID}-2")
    assert (stub.status, stub.transcript) == (status, DROPPED_DARIUS)
    from discovery.digest import digest_for_transcript
    digests = load_digests(ctx.repo)
    assert digest_for_transcript(digests, live).call_id == DARIUS_ID
    assert digest_for_transcript(digests, DROPPED_DARIUS).call_id == f"{DARIUS_ID}-2"
    assert f"digests/{DARIUS_ID}.md" not in ctx.remote.commits[-1]["paths"]


def test_stub_id_skips_every_taken_suffix(golden, ctx, models):
    pr.process_call(ctx, add_circleback(golden))
    add(golden, "transcripts/other.md", UNPARSEABLE)
    other = digest(ctx, DARIUS_ID)
    other.call_id, other.transcript = f"{DARIUS_ID}-2", "transcripts/other.md"
    write_digest(ctx.repo, other)                  # -2 belongs to yet another transcript
    add(golden, DROPPED_DARIUS, UNPARSEABLE)
    assert pr.process_call(ctx, DROPPED_DARIUS).call_id == f"{DARIUS_ID}-3"
    assert digest(ctx, f"{DARIUS_ID}-2").transcript == "transcripts/other.md"


def test_own_stub_is_replaced_in_place_by_a_new_stub(golden, ctx, models):
    add(golden, DROPPED_DARIUS, UNPARSEABLE)
    pr.mark_final_failure(ctx, DROPPED_DARIUS, "LLMError: timeout")
    assert digest(ctx, DARIUS_ID).status == "failed"
    r = pr.process_call(ctx, DROPPED_DARIUS, force=True)
    assert (r.outcome, r.call_id) == ("stub:parse_failed", DARIUS_ID)
    assert [(d.call_id, d.status) for d in load_digests(ctx.repo)] == [(DARIUS_ID, "parse_failed")]


def test_rerun_of_a_failed_stub_keeps_the_base_call_id(golden, ctx, models):
    add(golden, DROPPED_DARIUS, fixture_text("darius-kemp"))
    pr.mark_final_failure(ctx, DROPPED_DARIUS, "LLMError: every extractor failed")
    assert [d.call_id for d in load_digests(ctx.repo)] == [DARIUS_ID]

    r = pr.process_call(ctx, DROPPED_DARIUS, force=True)
    assert (r.outcome, r.call_id) == ("digested", DARIUS_ID)
    assert [(d.call_id, d.status) for d in load_digests(ctx.repo)] == [(DARIUS_ID, "digested")]
    assert ctx.remote.commits[-1]["paths"].count(f"digests/{DARIUS_ID}.md") == 1


def _cp1252(golden, rel: str) -> str:
    text = fixture_text("darius-kemp").replace("I don't", "I don’t", 1)
    (golden / rel).write_bytes(text.encode("cp1252", errors="replace"))
    assert b"\x92" in (golden / rel).read_bytes()
    return rel


def test_non_utf8_transcript_gets_a_parse_failed_stub_and_completes_its_batch(golden, ctx, models):
    path = _cp1252(golden, DROPPED_DARIUS)
    pr.start_backfill(ctx, [path], "b8")
    r = pr.process_call(ctx, path, batch="b8")
    assert (r.outcome, r.call_id) == ("stub:parse_failed", DARIUS_ID)
    stub = digest(ctx)
    assert (stub.status, stub.origin) == ("parse_failed", "backfill")
    assert "not UTF-8" in stub.reason
    [error] = ctx.slack.live(ctx.channel("ops"))
    assert "UTF-8" in error["text"]
    assert pr.batch_completion_check(ctx, "b8") is True
    assert len([m for m in ctx.slack.messages if m["metadata"]]) == 1
    assert model_calls(models) == 0


def test_batch_summary_shows_em_dash_filenames_with_a_hyphen(golden, ctx, models):
    failed = _cp1252(golden, "transcripts/darius " + EM_DASH + " copy.md")           # parse_failed stub
    undated = add(golden, "transcripts/darius-kemp " + EM_DASH + " notes.md", fixture_text("darius-kemp"))
    pr.start_backfill(ctx, [failed, undated], "b9")
    for p in (failed, undated):
        pr.process_call(ctx, p, batch="b9")
    assert pr.batch_completion_check(ctx, "b9") is True

    [summary] = [m for m in ctx.slack.messages if (m["metadata"] or {}).get("event_type") == "discovery_batch"]
    assert "transcripts/darius - copy.md (parse failed)" in summary["text"]
    assert "(transcripts/darius-kemp - notes.md): unknown date" in summary["text"]
    assert EM_DASH not in summary["text"]
    assert EM_DASH not in repr(summary["blocks"])

    [error] = ctx.slack.live(ctx.channel("ops"))           # the parse failure's own ops error
    shown = [b for b in error["blocks"] if b["type"] != "actions"]
    assert "Could not parse transcripts/darius - copy.md" in error["text"]
    assert EM_DASH not in error["text"] + repr(shown)
    [button] = [e for b in error["blocks"] if b["type"] == "actions" for e in b["elements"]]
    assert json.loads(button["value"])["payload"]["transcript"] == failed   # Re-run still names the real file


def test_final_failure_on_a_non_utf8_transcript_still_writes_the_stub(golden, ctx, models):
    path = _cp1252(golden, DROPPED_DARIUS)
    pr.mark_final_failure(ctx, path, "UnicodeDecodeError: bad byte")
    stub = digest(ctx)
    assert (stub.status, stub.transcript, stub.origin) == ("failed", path, "backfill")


@pytest.mark.parametrize("batch", [None, "b9"])
def test_parse_failed_ops_post_has_a_rerun_button(golden, ctx, models, batch):
    import json
    path = add(golden, "transcripts/notes.md", UNPARSEABLE)
    pr.process_call(ctx, path, batch=batch)
    [error] = ctx.slack.live(ctx.channel("ops"))
    [actions] = [b for b in error["blocks"] if b["type"] == "actions"]
    [button] = actions["elements"]
    assert (actions["block_id"], button["action_id"]) == ("rerun", "rerun")
    value = json.loads(button["value"])
    expected = {"transcript": path, "force": True}
    if batch:
        expected["batch"] = batch
    assert value == {"kind": "process_call", "payload": expected}


def test_follow_up_quotes_are_checked_against_the_transcript(golden, ctx, models):
    fabricated = "I'll intro you to my contact on the Guidewire product team tomorrow."
    notes_only = "Footage is the strongest evidence he gets but is absent or useless"   # overview, not body
    base = label_responder()

    def respond(system, prompt, schema):
        out = base(system, prompt, schema)
        if schema is ex.EXTRACT_SCHEMA:
            out["follow_ups"] = [
                {"direction": "they_offered", "what": "intro to the Guidewire team", "who": "Darius Kemp",
                 "quote": fabricated},
                {"direction": "we_owe", "what": "footage numbers", "who": "Alex Rivera", "quote": notes_only},
                {"direction": "they_offered", "what": "share expertise", "who": "Darius Kemp",
                 "quote": "I'M HAPPY to help out again anytime"},
            ]
        return out

    spec = ctx.config.pipeline.extractors[0]
    ctx.llms.register(FakeLLM(spec, respond))
    pr.process_call(ctx, add_circleback(golden))
    d = digest(ctx)
    assert [f.what for f in d.follow_ups] == ["intro to the Guidewire team", "footage numbers", "share expertise"]
    assert [f.quote for f in d.follow_ups][:2] == ["", ""]
    assert d.follow_ups[2].quote == "I'm happy to help out again anytime"
    assert d.follow_ups[2].quote in (golden / CIRCLEBACK).read_text()
    assert d.verification.follow_up_quotes_dropped == 2
    assert d.verification.dropped == []
    # follow-ups stay in the digest; the brief does not post them, verified or not
    posted = " ".join(m["text"] for m in ctx.slack.messages)
    assert ctx.slack.messages and "SUMMARY" in posted
    assert fabricated not in posted and notes_only not in posted
    assert "intro to the Guidewire team" not in posted and "share expertise" not in posted


def test_verdict_step_skips_a_model_that_failed_extraction(golden, ctx, models):
    spec = ctx.config.pipeline.extractors[1]
    down = FakeLLM(spec, label_responder(fail=True))
    ctx.llms.register(down)
    pr.process_call(ctx, add_circleback(golden))
    assert len(down.calls) == 1                    # extraction only
    d = digest(ctx)
    assert d.degraded == ["one_model"] and {c.decided_by for c in d.belief_checks} == {"single"}


def test_named_call_claim_speaker_comes_from_the_turn_label(golden, ctx, models):
    base = label_responder()

    def respond(system, prompt, schema):
        out = base(system, prompt, schema)
        if schema is ex.EXTRACT_SCHEMA:
            q = next(t.text for t in parse(CIRCLEBACK, (golden / CIRCLEBACK).read_text(), commit_date="2026-10-04").turns
                     if t.speaker == "Alex Rivera" and len(t.text.split()) > 12)
            out["findings"].append({**out["findings"][0], "text": "a claim built on Acme's words",
                                    "quotes": [{"quote": q, "turns": [], "question_turn": None,
                                                "speaker": "Darius Kemp"}]})
        return out

    ctx.llms.register(FakeLLM(ctx.config.pipeline.extractors[0], respond))
    pr.process_call(ctx, add_circleback(golden))
    d = digest(ctx)
    [odd] = [c for c in d.claims if c.text == "a claim built on Acme's words"]
    assert (odd.speaker, odd.quotes[0].attribution) == ("Alex Rivera", "span")
    assert {c.speaker for c in d.claims if c is not odd} == {"Darius Kemp", "Participant 2"}


# ---------- round-2 fixes ----------

def test_rerun_of_a_failed_richer_copy_supersedes_under_the_same_call_id(golden, ctx, models):
    first = add_granola(golden)
    assert pr.process_call(ctx, first).call_id == DARIUS_ID
    [top] = top_messages(ctx)

    richer = add_circleback(golden)             # its process_call fails on the last attempt
    pr.mark_final_failure(ctx, richer, "LLMError: every extractor failed")
    assert digest(ctx, "circleback_123").status == "failed"

    r = pr.process_call(ctx, richer, force=True)                  # Re-run
    assert (r.outcome, r.call_id, r.briefed) == ("superseded", DARIUS_ID, True)
    assert [(d.call_id, d.status, d.transcript, d.duplicates) for d in load_digests(ctx.repo)] == [
        (DARIUS_ID, "digested", richer, [first])]
    assert "digests/circleback_123.md" in ctx.remote.commits[-1]["paths"]
    [top_after] = top_messages(ctx)                                # the brief is updated in place
    assert top_after["ts"] == top["ts"] and "Circleback" in top_after["text"]
    assert digest(ctx).brief.ts == top["ts"]


@pytest.mark.parametrize("why", ["loses", "cited"])
def test_rerun_of_a_failed_copy_that_loses_drops_its_stub(golden, ctx, models, why):
    if why == "loses":
        host, copy_path = add_circleback(golden), None
        pr.process_call(ctx, host)
        copy_path = add_granola(golden)          # me_them loses to named
    else:
        host = add_granola(golden)
        pr.process_call(ctx, host)
        quote = ATTORNEY
        (golden / "config" / "verdict_overrides.yml").write_text(yaml.safe_dump({"overrides": [
            {"call_id": DARIUS_ID, "target": "B-001", "quote": quote, "who": "Alex", "reason": "x", "ref": "r1"}]}))
        copy_path = add_circleback(golden)       # richer, but the call is cited
    pr.mark_final_failure(ctx, copy_path, "TaskTimeout: 25 minutes")
    stub_id = copy_path.rsplit("/", 1)[-1][:-3]
    assert digest(ctx, stub_id).status == "failed"
    calls, messages = model_calls(models), len(ctx.slack.messages)

    r = pr.process_call(ctx, copy_path, force=True)
    assert (r.outcome, r.call_id, r.briefed) == ("duplicate", DARIUS_ID, False)
    assert [(d.call_id, d.transcript, d.duplicates) for d in load_digests(ctx.repo)] == [(DARIUS_ID, host, [copy_path])]
    assert {f"digests/{DARIUS_ID}.md", f"digests/{stub_id}.md"} <= set(ctx.remote.commits[-1]["paths"])
    from discovery.digest import digest_for_transcript
    assert digest_for_transcript(load_digests(ctx.repo), copy_path).call_id == DARIUS_ID
    assert model_calls(models) == calls and len(ctx.slack.messages) == messages
    assert pr.process_call(ctx, copy_path).outcome == "skipped:current"


def _failing_posts(ctx, which):
    """Make slack.post raise on the calls `which(n)` picks (n counts posts from now, from 1)."""
    real, seen = ctx.slack.post, []

    def post(*args, **kwargs):
        seen.append(1)
        if which(len(seen)):
            raise RuntimeError("Slack 503")
        return real(*args, **kwargs)

    ctx.slack.post = post
    return real


def _thread(ctx, ts):
    return [m["ts"] for m in ctx.slack.replies(ctx.channel("calls"), ts)]


def test_supersede_that_hits_a_slack_error_mid_thread_is_retried(golden, ctx, models):
    pr.process_call(ctx, add_granola(golden))
    [top] = top_messages(ctx)
    old_thread = _thread(ctx, top["ts"])

    _failing_posts(ctx, lambda n: n == 2)        # the second reply of the rebuilt thread fails once
    r = pr.process_call(ctx, add_circleback(golden))
    assert (r.outcome, r.briefed) == ("superseded", True)
    d = digest(ctx)
    live = _thread(ctx, top["ts"])
    assert d.brief.ts == top["ts"] and d.brief.thread == live
    assert len(live) == len(old_thread) and not set(live) & set(old_thread)
    assert [m["ts"] for m in top_messages(ctx)] == [top["ts"]]


def test_supersede_with_slack_down_leaves_no_stale_brief_ref(golden, ctx, models):
    pr.process_call(ctx, add_granola(golden))
    [top] = top_messages(ctx)
    old_thread = _thread(ctx, top["ts"])

    real = _failing_posts(ctx, lambda n: n >= 2)  # Slack goes down after the first new reply
    r = pr.process_call(ctx, add_circleback(golden))
    assert (r.outcome, r.briefed) == ("superseded", False)
    d = digest(ctx)
    assert d.brief is None                        # nothing stale recorded: the sweep re-posts it
    assert f"digests/{DARIUS_ID}.md" in ctx.remote.commits[-1]["paths"]

    ctx.slack.post = real                         # the sweep's re-post rebuilds the thread under the same top
    ref = pr.post_brief(ctx, d)
    assert ref.ts == top["ts"] and ref.thread == _thread(ctx, top["ts"]) and len(ref.thread) == len(old_thread)
    assert [m["ts"] for m in top_messages(ctx)] == [top["ts"]]
    assert "Circleback" in top_messages(ctx)[0]["text"]


def test_slack_error_in_the_batch_check_does_not_fail_the_digested_call(golden, ctx, models):
    path = add(golden, "transcripts/2026-09-23_darius-kemp.md", fixture_text("darius-kemp"))
    pr.start_backfill(ctx, [path], "b1")
    real = _failing_posts(ctx, lambda n: True)

    r = pr.process_call(ctx, path, batch="b1")    # the summary post fails; the call itself did not
    assert (r.outcome, r.call_id) == ("digested", DARIUS_ID)
    assert digest(ctx).reprocess_failed is None
    assert "summary_ts" not in yaml.safe_load(ctx.repo.read("config/batches/b1.yml"))
    assert any("b1" in line for line in ctx.log)

    ctx.slack.post = real                         # the sweep's check posts it later
    assert pr.batch_completion_check(ctx, "b1") is True
    assert yaml.safe_load(ctx.repo.read("config/batches/b1.yml"))["summary_ts"]


def test_slack_error_in_the_batch_check_does_not_mask_a_final_failure(golden, ctx, models):
    path = add(golden, "transcripts/wade-pruitt.md", fixture_text("wade-pruitt"))
    pr.start_backfill(ctx, [path], "b4")
    _failing_posts(ctx, lambda n: True)
    pr.mark_final_failure(ctx, path, "LLMError: timeout")
    assert digest(ctx, "wade-pruitt").status == "failed"
    assert any("b4" in line for line in ctx.log)


# ---------- round-3 fixes ----------

def _stale_selection(ctx) -> list[str]:
    """Transcripts the nightly sweep's step 7 would re-run now."""
    from discovery.sweep import _Sweep
    ctx.queue.tasks.clear()
    _Sweep(ctx).stale_digests()
    return [t["payload"]["transcript"] for t in ctx.queue.of("process_call")]


@pytest.mark.parametrize("why", ["parse_failed", "internal", "missing"])
def test_stale_rerun_that_keeps_the_digest_marks_it_until_a_rerun(golden, ctx, models, why):
    path = add_circleback(golden)
    pr.process_call(ctx, path)
    ctx.config.pipeline.prompt_version = "v2"
    ctx.config.pipeline.reprocess_stale = True
    assert _stale_selection(ctx) == [path]
    if why == "parse_failed":
        add(golden, path, UNPARSEABLE)
    elif why == "internal":
        add(golden, path, live_text(attendees=[{"name": "Alex Rivera", "email": ALEX_EMAIL},
                                               {"name": "Sam", "email": "sam@acme.dev"}]))
    else:
        (golden / path).unlink()
    commits, calls = len(ctx.remote.commits), model_calls(models)

    first = pr.process_call(ctx, path)          # what sweep step 7 enqueues each night, never forced
    second = pr.process_call(ctx, path)
    assert first.outcome == second.outcome == f"skipped:{why}"
    assert first.call_id == second.call_id == DARIUS_ID
    d = digest(ctx)
    assert (d.status, d.prompt_version, d.reprocess_failed) == ("digested", "v1", "v2")
    assert len(ctx.remote.commits) == commits + 1                  # the mark, committed once
    assert f"digests/{DARIUS_ID}.md" in ctx.remote.commits[-1]["paths"]
    assert len(ctx.slack.live(ctx.channel("ops"))) == (1 if why == "parse_failed" else 0)
    assert _stale_selection(ctx) == []
    assert model_calls(models) == calls

    if why == "missing":
        return
    add_circleback(golden)                                          # fixed, then Re-run
    r = pr.process_call(ctx, path, force=True)
    assert (r.outcome, r.call_id) == ("digested", DARIUS_ID)
    assert (digest(ctx).prompt_version, digest(ctx).reprocess_failed) == ("v2", None)


def test_forced_rerun_that_still_fails_to_parse_posts_again(golden, ctx, models):
    path = add_circleback(golden)
    pr.process_call(ctx, path)
    ctx.config.pipeline.prompt_version = "v2"
    add(golden, path, UNPARSEABLE)
    assert pr.process_call(ctx, path).outcome == "skipped:parse_failed"
    assert pr.process_call(ctx, path, force=True).outcome == "skipped:parse_failed"   # Re-run
    assert len(ctx.slack.live(ctx.channel("ops"))) == 2
    assert digest(ctx).reprocess_failed == "v2"


# ---------- front-matter PyYAML cannot load ----------

_BAD_TAG = "---\ndate: !!int 12x\n---\n\nnothing here\n"   # PyYAML: ValueError, not a YAMLError


def test_unloadable_front_matter_gets_a_parse_failed_stub(golden, ctx, models):
    path = add(golden, "transcripts/x.md", _BAD_TAG)
    r = pr.process_call(ctx, path)
    assert (r.outcome, r.call_id) == ("stub:parse_failed", "x")
    assert (digest(ctx, "x").status, digest(ctx, "x").origin) == ("parse_failed", "backfill")
    [error] = ctx.slack.live(ctx.channel("ops"))
    assert path in error["text"]
    assert model_calls(models) == 0


def test_final_failure_on_unloadable_front_matter_still_writes_the_stub(golden, ctx, models):
    path = add(golden, "transcripts/x.md", _BAD_TAG)
    pr.mark_final_failure(ctx, path, "LLMError: timeout")
    stub = digest(ctx, "x")
    assert (stub.status, stub.transcript, stub.origin) == ("failed", path, "backfill")


@pytest.mark.parametrize("error", [ValueError("invalid literal for int()"), RecursionError("too deep"),
                                   RuntimeError("anything else")])
def test_origin_of_front_matter_that_cannot_load_is_backfill(monkeypatch, error):
    def boom(raw):
        raise error
    monkeypatch.setattr(pr.frontmatter, "split", boom)
    assert pr._origin_of("---\nsource: circleback\n---\n") == "backfill"


def test_final_failure_writes_the_stub_when_parse_raises_something_unexpected(golden, ctx, models, monkeypatch):
    path = add_circleback(golden)

    def boom(*args, **kwargs):
        raise RuntimeError("parser bug")
    monkeypatch.setattr(pr, "parse", boom)
    pr.mark_final_failure(ctx, path, "LLMError: timeout")
    stub = digest(ctx, "circleback_123")
    assert (stub.status, stub.transcript, stub.origin) == ("failed", path, "live")
    assert any(path in line and "RuntimeError" in line for line in ctx.log)


@pytest.mark.parametrize("sibling", [
    pytest.param("---\ntitle: Weekly\nattendees: 3\n---\n\nTranscript:\nMe: hello\nThem: hi\n", id="scalar-attendees"),
    pytest.param("---\ndate: !!int 12x\n---\n\nTranscript:\nMe: hello\nThem: hi\n", id="bad-int-tag"),
    pytest.param("---\ntags: " + "[" * 3000 + "]" * 3000 + "\n---\n\nTranscript:\nMe: hello\nThem: hi\n",
                 id="deep-nesting"),
    pytest.param(_BAD_TAG, id="unparseable"),
])
def test_an_odd_sibling_transcript_never_blocks_a_live_call(golden, ctx, models, sibling):
    add(golden, "transcripts/odd.md", sibling)
    path = add_circleback(golden)
    r = pr.process_call(ctx, path)
    assert (r.outcome, r.call_id, r.briefed) == ("digested", DARIUS_ID, True)
    assert digest(ctx).duplicates == []


def test_a_sibling_parse_cannot_handle_is_named_and_skipped(golden, ctx, models, monkeypatch):
    odd = add(golden, "transcripts/odd.md", live_text(source="granola", source_id="zzz"))
    path = add_circleback(golden)
    real = pr.parse

    def parse(rel, raw, **kwargs):
        if rel == odd:
            raise RuntimeError("parser bug")
        return real(rel, raw, **kwargs)
    monkeypatch.setattr(pr, "parse", parse)
    r = pr.process_call(ctx, path)
    assert (r.outcome, r.call_id) == ("digested", DARIUS_ID)
    assert any(odd in line and "RuntimeError" in line for line in ctx.log)


# ---------- brief evidence counts ----------

def test_brief_counts_leave_out_verdicts_dismissed_in_verdict_overrides(golden, ctx, models, fake_indexes):
    rows = [{"call_id": DARIUS_ID, "target": "W-001", "quote": "x", "who": "alex", "reason": "wrong read"}]
    add(golden, "config/verdict_overrides.yml", yaml.safe_dump(rows))
    pr.process_call(ctx, add_circleback(golden))
    assert fake_indexes.overrides_seen == rows
    [line] = [x for x in render_text(pr.build_brief(ctx, digest(ctx))).splitlines()
              if x.startswith("Watching W-001, supports")]
    assert "(now" not in line
    add(golden, "config/verdict_overrides.yml", "")
    [line] = [x for x in render_text(pr.build_brief(ctx, digest(ctx))).splitlines()
              if x.startswith("Watching W-001, supports")]
    assert line.endswith("(now 1 call, 1 seat)")


# ---------- a digest file that cannot be read ----------

def broken(call_id: str = "2026-09-01_jane-doe", transcript: str = "transcripts/jane.md") -> str:
    """A digest after a hand correction with a YAML slip: the firm's closing quote is missing."""
    return (f"---\ncall_id: {call_id}\ntranscript: {transcript}\nstatus: digested\norigin: live\n"
            'firm: "Example\n---\n\n# Jane Doe\n')


def test_an_unreadable_digest_never_stops_other_calls(golden, ctx, models):
    bad = add(golden, "digests/2026-09-01_jane-doe.md", broken())
    before = (golden / bad).read_bytes()
    r = pr.process_call(ctx, add_circleback(golden))
    assert (r.outcome, r.call_id, r.briefed) == ("digested", DARIUS_ID, True)
    # a final failure on another call still writes its stub
    other = add(golden, "transcripts/wade-pruitt.md", fixture_text("wade-pruitt"))
    pr.mark_final_failure(ctx, other, "LLMError: timeout")
    assert digest(ctx, "wade-pruitt").status == "failed"
    assert (golden / bad).read_bytes() == before
    assert all(bad not in c["paths"] for c in ctx.remote.commits)


@pytest.mark.parametrize("force", [False, True])
def test_a_transcript_whose_digest_is_unreadable_is_left_alone(golden, ctx, models, force):
    path = add_circleback(golden)
    bad = add(golden, f"digests/{DARIUS_ID}.md", broken(DARIUS_ID, path))
    before = (golden / bad).read_bytes()
    r = pr.process_call(ctx, path, force=force)
    assert (r.outcome, r.call_id, r.briefed) == ("skipped:unreadable", DARIUS_ID, False)
    pr.mark_final_failure(ctx, path, "LLMError: timeout")
    assert (golden / bad).read_bytes() == before
    assert sorted(p.name for p in (golden / "digests").iterdir()) == [f"{DARIUS_ID}.md"]
    assert model_calls(models) == 0 and ctx.remote.commits == [] and ctx.slack.messages == []
    assert any(bad in line for line in ctx.log)


def test_an_unreadable_files_call_id_stays_taken(golden, ctx, models):
    bad = add(golden, f"digests/{DARIUS_ID}.md", broken(DARIUS_ID, "transcripts/old.md"))
    before = (golden / bad).read_bytes()
    r = pr.process_call(ctx, add_circleback(golden))
    assert (r.outcome, r.call_id) == ("digested", f"{DARIUS_ID}-2")
    assert (golden / bad).read_bytes() == before


def test_a_stub_never_takes_an_unreadable_files_id(golden, ctx, models):
    bad = add(golden, "digests/notes.md", "# Notes to self\n")        # a stray file that is no digest
    add(golden, "transcripts/notes.md", UNPARSEABLE)
    r = pr.process_call(ctx, "transcripts/notes.md")
    assert (r.outcome, r.call_id) == ("stub:parse_failed", "notes-2")
    assert (golden / bad).read_text() == "# Notes to self\n"


@pytest.mark.parametrize("broken_copy", ["circleback", "granola"])
def test_a_copy_of_a_call_whose_digest_is_unreadable_waits(golden, ctx, models, broken_copy):
    # the other copy either loses (and would be recorded on the unreadable digest) or wins (and would
    # delete the unreadable file as a stub it replaces); neither may happen until the file is fixed
    circleback, granola = add_circleback(golden), add_granola(golden)
    owned, arriving = (circleback, granola) if broken_copy == "circleback" else (granola, circleback)
    bad = add(golden, f"digests/{DARIUS_ID}.md", broken(DARIUS_ID, owned))
    before = (golden / bad).read_bytes()
    r = pr.process_call(ctx, arriving)
    assert (r.outcome, r.call_id) == ("skipped:unreadable", DARIUS_ID)
    assert (golden / bad).read_bytes() == before
    assert sorted(p.name for p in (golden / "digests").iterdir()) == [f"{DARIUS_ID}.md"]
    assert model_calls(models) == 0 and ctx.remote.commits == []


def test_backfill_counts_a_transcript_an_unreadable_digest_owns_as_done(golden, ctx, models):
    path = add(golden, "transcripts/darius-kemp.md", fixture_text("darius-kemp"))
    bad = add(golden, "digests/undated_darius-kemp.md", broken("undated_darius-kemp", path))
    pr.start_backfill(ctx, [path], "b9")
    assert ctx.queue.of("process_call") == []
    [summary] = [m for m in ctx.slack.messages if m["metadata"]]
    assert "1 unreadable" in summary["text"] and bad in summary["text"]


# ---------- symlinked transcripts ----------

def test_a_symlinked_transcript_is_never_read_or_sent_to_a_model(golden, ctx, models, tmp_path_factory):
    secret = tmp_path_factory.mktemp("outside") / "environ"
    secret.write_text("GITHUB_APP_PRIVATE_KEY=-----BEGIN RSA PRIVATE KEY-----\n" + fixture_text("darius-kemp"),
                      encoding="utf-8")
    (golden / "transcripts" / "evil.md").symlink_to(secret)
    for force in (False, True):
        r = pr.process_call(ctx, "transcripts/evil.md", force=force)
        assert r.outcome == "skipped:missing"
    pr.start_backfill(ctx, ["transcripts/evil.md"], "b10")
    assert model_calls(models) == 0 and load_digests(ctx.repo) == []
    assert all("PRIVATE KEY" not in m["text"] for m in ctx.slack.messages)


# ---------- a short answer in the brief shows the question it answers ----------

DARIUS_QUESTION = ("yeah. And roughly how many of your files actually come with usable video? Would you say like half "
                  "of them? More or")
SHORT_ANSWER = f'  "Nowhere close to that."  (answering: {DARIUS_QUESTION})'


@pytest.fixture
def short_answer_cited(ctx, models):
    """The label-driven fakes, except the verdicts also cite Darius's short answer "Nowhere close to that." on the
    B-004 contradiction, so a short answer shows up under WHAT CHANGED."""
    base = label_responder()

    def respond(system, prompt, schema):
        out = base(system, prompt, schema)
        if schema is vd.VERDICT_SCHEMA:
            [cid] = [c for c, qs in _prompt_claims(prompt).items() if "Nowhere close to that." in qs]
            for check in out["belief_checks"]:
                if check["target"] == "B-004":
                    check["claims"].append(cid)
        return out

    p = ctx.config.pipeline
    for spec in (p.extractors[0], p.arbiter):
        ctx.llms.register(FakeLLM(spec, respond))
    ctx.llms.register(FakeLLM(p.extractors[1], label_responder(subset=True)))


def test_brief_shows_the_question_a_short_answer_answers(golden, ctx, short_answer_cited):
    path = add_circleback(golden)
    pr.process_call(ctx, path)
    [top] = top_messages(ctx)
    assert SHORT_ANSWER in top["text"].splitlines()
    assert SHORT_ANSWER in render_text(pr.build_brief(ctx, digest(ctx))).splitlines()   # a re-post reads the file

    # the transcript cannot be read: the claim text stands in for the question
    (golden / path).unlink()
    text = render_text(pr.build_brief(ctx, digest(ctx)))
    assert "answering" not in text
    assert any(line.startswith("  Claim: ") for line in text.splitlines())


# ---------- a date override that is not a real date ----------

def test_a_bad_date_override_is_ignored_and_reported(golden, ctx, models):
    # frontmatter.load_yaml keeps `2026-09-31` as text; applied as is, prep matching raised ValueError
    from discovery.frontmatter import load_yaml
    ctx.config.overrides = load_yaml(f"{DARIUS_ID}: {{date: 2026-09-31, seat: underwriter}}\n")
    path = add_circleback(golden)
    for force in (False, True):                          # the first run and a later Re-run
        r = pr.process_call(ctx, path, force=force)
        assert r.outcome == "digested"
        d = digest(ctx)
        assert (d.date, d.seat) == ("2026-09-23", "underwriter")
        assert d.date_source != "manual"
        assert d.prep == "prep/2026-09-23_darius-kemp.md"
    assert any("2026-09-31" in line and "config/overrides.yml" in line for line in ctx.log)


def test_prep_matching_treats_an_unreadable_call_date_as_unknown(golden, ctx):
    from discovery.types import Digest
    d = Digest(call_id=DARIUS_ID, transcript=CIRCLEBACK, status="digested", origin="live", date="2026-09-31",
               contact="Darius Kemp")
    assert pr._prep_for(ctx, d, None) is None            # no window without a date, and no email to match


def test_brief_shows_the_question_a_one_block_short_answer_answers(golden, ctx):
    # a Granola text export parses to one turn, so the question sits just before the answer in that turn
    # and the turn's own text runs to the end of the call
    from discovery.types import BeliefCheck, Claim, Digest
    raw = fixture_text("viktor-lang")
    parsed = parse("transcripts/viktor-lang.md", raw, commit_date="2026-10-04")
    quote = "Not really, sorry."
    at = raw.index(quote)
    claim = Claim(id="c01", text="He could not name anyone else to talk to on the video side.", quote=quote,
                  turns=[1], question_turn=1, offset=(at, at + len(quote)), speaker="Viktor Lang")
    d = Digest(call_id="2026-09-30_viktor-lang", transcript=parsed.path, status="digested", origin="backfill",
               contact="Viktor Lang", claims=[claim],
               belief_checks=[BeliefCheck(target="B-003", verdict="contradicts", claims=["c01"], decided_by="agree")])
    questions = pr._questions(ctx, d, parsed)
    assert list(questions) == ["c01"]
    assert questions["c01"].endswith("Could you point me to anyone else I should talk to, maybe somebody who works "
                                     "with video more, or a carrier examiner?")
    text = render_text(pr.build_brief(ctx, d, parsed=parsed))
    [line] = [x for x in text.splitlines() if x.startswith(f'  "{quote}"')]
    assert line.endswith("works with video more, or a carrier examiner?)")
    last_words = " ".join(parsed.turns[0].text.split()[-6:])
    assert last_words not in text


def test_dry_run_brief_shows_the_question_a_short_answer_answers(tmp_repo, ctx, short_answer_cited):
    ctx.dry_run = True
    path = add(tmp_repo, "transcripts/darius-kemp.md", fixture_text("darius-kemp"))
    r = pr.process_call(ctx, path, beliefs_path=str(GOLDEN / "beliefs.md"),
                        decisions_path=str(GOLDEN / "decisions.md"), prep_path=str(GOLDEN / "prep_darius-kemp.md"))
    assert SHORT_ANSWER in r.brief_text.splitlines()
