"""End to end, offline (Task 20): recorder or GitHub or Slack or MCP, through the receiver, the queue and the
processor, to the repo and Slack. Fakes only.

Every service shares one temporary repo. Models are the label-backed fakes behind `--fake`
(`discovery.bakeoff.install_fakes`), Slack is `ports.FakeSlack`, and the git remote is `RepoRemote`, which writes
created files into the working tree the way the next sync would bring them in. Tasks run inline:
`tasks.InlineQueue` posts each task to the processor app's `POST /task/{kind}` as soon as it is enqueued
(after the task that enqueued it, like the real queue with one dispatch at a time), and every response is
kept in `Pipeline.results`.

The last test is the P1 golden check: `uv run python -m discovery run tests/fixtures/darius-kemp.md --dry-run --fake` with
the golden beliefs, decisions and prep must match `tests/expected/brief_darius-kemp.md` on what changed
(target and verdict) and on new findings (quote spans overlapping by at least 50%, one to one).
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import re
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

import httpx
import pytest
import yaml
from fastapi.testclient import TestClient
from fastmcp import Client

from conftest import FIXTURES, ROOT, fixture_text
from discovery import __main__ as cli
from discovery import config as config_mod
from discovery import frontmatter, interact
from discovery.bakeoff import install_fakes, match_labels
from discovery.beliefs import parse_beliefs
from discovery.context import Ctx
from discovery.digest import digest_path, load_digests, read_digest
from discovery.ingest.granola import GranolaClient
from discovery.llm import CostTracker, LLMFactory
from discovery.parse import parse
from discovery.ports import FakeSlack, NullRemote, RepoFS
from discovery.tasks import InlineQueue
from discovery.types import Claim
from discovery.verify import QuoteIndex
from service import main
from service import mcp as mcp_mod

PT = ZoneInfo("America/Los_Angeles")
NOW = datetime(2026, 10, 1, 9, 0, tzinfo=PT)
STAMP = "2026-10-01T09:00:00-07:00"
CALLS = "#discovery-calls"
GOLDEN = ROOT / "tests" / "golden"
EXPECTED = ROOT / "tests" / "expected" / "brief_darius-kemp.md"
EM_DASH = chr(0x2014)

GRANOLA_SECRET = "whsec_" + base64.b64encode(b"granola-e2e-key").decode()
GH_SECRET = "github-e2e-secret"
SLACK_SECRET = "slack-e2e-secret"

# Dated copies of the fixtures, as a cofounder would drop them into transcripts/ on GitHub.
DROP = {
    "transcripts/2026-09-23_darius-kemp.md": "darius-kemp",
    "transcripts/2026-09-30_lena-okafor.md": "lena-okafor",
    "transcripts/2026-10-01_wade-pruitt.md": "wade-pruitt",
    "transcripts/2026-09-30_viktor-lang.md": "viktor-lang",
}
ATTORNEY = "The minute a lawyer is retained, the claim costs more, period."


# ---------- the pipeline ----------

class RepoRemote(NullRemote):
    """GitHub plus the next sync: create-only writes land in the working tree; commits are recorded."""

    def __init__(self, root: Path):
        super().__init__()
        self.root = root

    def create_file_only(self, rel: str, content: str, message: str) -> bool:
        path = self.root / rel
        if path.exists():
            return False
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        self.created.append(rel)
        return True


class Pipeline:
    """Receiver and processor apps over one repo, with inline tasks and shared fakes."""

    def __init__(self, root: Path):
        self.root = root
        self.slack = FakeSlack()
        self.remote = RepoRemote(root)
        self.queue = InlineQueue(self._dispatch)
        self.results: list[dict] = []
        self.granola_note: dict | None = None
        self._fakes: list | None = None
        self.processor = TestClient(main.create_processor_app(self.ctx))
        self.receiver = TestClient(main.create_receiver_app(
            self.ctx, ack=lambda: interact.AckCtx.from_ctx(self.ctx()), granola=self._granola,
            circleback=lambda p: None))

    def ctx(self) -> Ctx:
        """A fresh Ctx per request or task, like settings.build_ctx: config re-read, own cost tracker."""
        c = Ctx(config=config_mod.load(self.root), repo=RepoFS(self.root), llms=LLMFactory(CostTracker(cap_usd=1e9)),
                remote=self.remote, slack=self.slack, queue=self.queue, now=lambda: NOW)
        if self._fakes is None:
            self._fakes = install_fakes(c, root=ROOT)
        for fake in self._fakes:
            c.llms.register(fake)
        return c

    def _granola(self) -> GranolaClient:
        def handler(request: httpx.Request) -> httpx.Response:
            assert request.url.path == f"/v1/notes/{self.granola_note['id']}"
            return httpx.Response(200, json=self.granola_note)
        return GranolaClient("grn_test", http=httpx.Client(transport=httpx.MockTransport(handler)))

    def _dispatch(self, kind: str, payload: dict) -> None:
        r = self.processor.post(f"/task/{kind}", json=payload, headers={"X-CloudTasks-TaskRetryCount": "0"})
        self.results.append({"kind": kind, "payload": payload, "status": r.status_code, "body": r.json()})

    # -- checks --

    def ran(self) -> list[str]:
        return [r["kind"] for r in self.results]

    def assert_all_ok(self) -> None:
        bad = [r for r in self.results if r["status"] != 200 or not r["body"].get("ok")]
        assert not bad, bad

    def result(self, kind: str) -> dict:
        return next(r["body"]["result"] for r in self.results if r["kind"] == kind)

    def tops(self, event_type: str | None = None) -> list[dict]:
        out = [m for m in self.slack.live(CALLS) if m["thread_ts"] is None]
        if event_type is not None:
            out = [m for m in out if (m["metadata"] or {}).get("event_type") == event_type]
        return out

    # -- inputs --

    def github_push(self, added: list[str]) -> httpx.Response:
        body = json.dumps({"ref": "refs/heads/main", "after": "e2e0head", "sender": {"login": "alexrivera", "type": "User"},
                           "commits": [{"id": "e2e0head", "added": added, "removed": [], "modified": []}]}).encode()
        sig = "sha256=" + hmac.new(GH_SECRET.encode(), body, hashlib.sha256).hexdigest()
        return self.receiver.post("/github", content=body, headers={
            "X-GitHub-Event": "push", "X-Hub-Signature-256": sig, "content-type": "application/json"})

    def drop(self, files: dict[str, str]) -> list[str]:
        """Write fixture copies into transcripts/ (the pushed commit) and deliver the push webhook."""
        for rel, name in files.items():
            (self.root / rel).write_text(fixture_text(name), encoding="utf-8")
        r = self.github_push(list(files))
        assert r.status_code == 200, r.text
        return list(files)

    def slack_click(self, payload: dict) -> httpx.Response:
        body = urlencode({"payload": json.dumps(payload)}).encode()
        ts = str(int(time.time()))
        sig = "v0=" + hmac.new(SLACK_SECRET.encode(), f"v0:{ts}:".encode() + body, hashlib.sha256).hexdigest()
        return self.receiver.post("/slack/interact", content=body, headers={
            "X-Slack-Request-Timestamp": ts, "X-Slack-Signature": sig,
            "content-type": "application/x-www-form-urlencoded"})


@pytest.fixture
def pipe(tmp_repo: Path, monkeypatch) -> Pipeline:
    people = tmp_repo / "config" / "people.yml"
    data = yaml.safe_load(people.read_text(encoding="utf-8"))
    data["alex"]["slack_id"] = "U_ALEX"
    people.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    pipeline = tmp_repo / "config" / "pipeline.yml"
    pdata = yaml.safe_load(pipeline.read_text(encoding="utf-8"))
    pdata["ingest_cutoff"] = "2026-09-01T00:00:00-07:00"
    pipeline.write_text(yaml.safe_dump(pdata, sort_keys=False), encoding="utf-8")
    for name, value in {"GRANOLA_WEBHOOK_SECRET": GRANOLA_SECRET, "GITHUB_WEBHOOK_SECRET": GH_SECRET,
                        "GITHUB_APP_SLUG": "discovery-base", "REPO_BRANCH": "main", "SLACK_SIGNING_SECRET": SLACK_SECRET,
                        "DISCOVERY_DEV": "1", "DISCOVERY_TASK_MAX_ATTEMPTS": "5"}.items():
        monkeypatch.setenv(name, value)
    for name in ("GRANOLA_API_KEY", "SLACK_BOT_TOKEN", "MCP_DEV_USER", "MCP_AUTH",
                 *(f"CIRCLEBACK_API_KEY_{p.upper()}" for p in ("alex", "sam", "jordan"))):
        monkeypatch.delenv(name, raising=False)
    interact.reset_state()
    yield Pipeline(tmp_repo)
    interact.reset_state()


# ---------- helpers ----------

def _seconds(clock: str) -> int:
    total = 0
    for part in clock.split(":"):
        total = total * 60 + int(part)
    return total


def darius_note() -> dict:
    """Darius's call as the Granola API returns a note: named speakers, absolute item times, the Circleback
    overview standing in for Granola's summary. The attendee's email is on a generic domain, so no firm."""
    parsed = parse("tests/fixtures/darius-kemp.md", fixture_text("darius-kemp"))
    start = datetime(2026, 10, 1, 16, 0, tzinfo=timezone.utc)
    items = [{"speaker": {"source": "microphone" if t.speaker == "Alex Rivera" else "speaker", "name": t.speaker},
              "text": t.text,
              "start_time": (start + timedelta(seconds=_seconds(t.time))).isoformat().replace("+00:00", "Z")}
             for t in parsed.turns]
    return {"id": "not_darius", "title": "Darius / Alex", "created_at": "2026-10-01T16:00:00Z",
            "owner": {"name": "Alex Rivera", "email": "alex.rivera@gmail.com"},
            "calendar_event": {"scheduled_start_time": "2026-10-01T16:00:00Z",
                               "invitees": [{"email": "darius@example.com"}]},
            "attendees": [{"name": "Darius Kemp", "email": "darius@example.com"}],
            "summary_markdown": parsed.notes, "transcript": items}


def granola_delivery(event_type: str, note_id: str) -> tuple[bytes, dict]:
    body = json.dumps({"event_id": f"evt_{event_type}", "event_type": event_type, "note_id": note_id,
                       "occurred_at": "2026-10-01T16:35:00Z"}).encode()
    msg_id, ts = f"msg_{event_type}", str(int(time.time()))
    key = base64.b64decode(GRANOLA_SECRET[len("whsec_"):])
    sig = base64.b64encode(hmac.new(key, f"{msg_id}.{ts}.".encode() + body, hashlib.sha256).digest()).decode()
    return body, {"webhook-id": msg_id, "webhook-timestamp": ts, "webhook-signature": f"v1,{sig}",
                  "content-type": "application/json"}


def mcp_call(server, name: str, args: dict) -> dict:
    async def run():
        async with Client(server) as c:
            res = await c.call_tool(name, args)
            return res.content[0].text
    return json.loads(asyncio.run(run()))


def no_em_dash(pipe: Pipeline) -> None:
    assert all(EM_DASH not in m["text"] for m in pipe.slack.messages)


# ---------- live call: Granola webhook to brief ----------

def test_live_granola_call_from_signed_webhook_to_posted_brief(pipe):
    pipe.granola_note = darius_note()
    body, headers = granola_delivery("note.generated", "not_darius")
    r = pipe.receiver.post("/ingest/granola", content=body, headers=headers)
    assert r.status_code == 200, r.text
    rel = "transcripts/granola_not_darius.md"
    assert r.json() == {"ok": True, "transcript": rel, "created": True, "queued": True, "delay_seconds": 0}

    # the receiver wrote a standard live file, stamped with its arrival
    fm, _, _ = frontmatter.split((pipe.root / rel).read_text(encoding="utf-8"))
    assert (fm["source"], fm["source_id"], fm["recorded_by"], fm["speaker_labels"]) == \
        ("granola", "not_darius", "alex", "named")
    assert fm["ingested_at"] == STAMP

    # the processor ran process_call inline and briefed the call once
    pipe.assert_all_ok()
    assert pipe.ran() == ["process_call"]
    call_id = "2026-10-01_darius-kemp"
    assert pipe.result("process_call") == {"transcript": rel, "outcome": "digested", "call_id": call_id,
                                           "briefed": True, "brief_text": None}
    d = read_digest(pipe.ctx().repo, digest_path(call_id))
    assert (d.status, d.origin, d.transcript, d.source, d.recorded_by) == ("digested", "live", rel, "granola", "alex")
    # Granola clocks start at the first transcript item: Darius's [1:13] to [37:36] is 36 minutes
    assert (d.contact, d.firm, d.seat, d.date, d.duration_min) == (
        "Darius Kemp", None, "claims adjuster (staff)", "2026-10-01", 36)
    assert d.claims and d.verification.quotes > 0

    [top] = pipe.tops()
    assert top["metadata"] == {"event_type": "discovery_brief", "event_payload": {"key": rel}}
    assert top["text"].startswith("Darius Kemp · claims adjuster (staff) · 36m · Alex · Granola\n")
    assert "No belief touched." in top["text"]            # beliefs.md is empty before the bootstrap
    replies = pipe.slack.replies(CALLS, top["ts"])
    assert replies and replies[0]["text"].startswith("SUMMARY")
    assert "no prep matched" in replies[0]["text"]
    assert any(m["text"].startswith("NEW FINDINGS") for m in replies[1:])
    assert (d.brief.channel, d.brief.ts, d.brief.thread) == (CALLS, top["ts"], [m["ts"] for m in replies])
    assert any(digest_path(call_id) in c["paths"] and "findings.md" in c["paths"] for c in pipe.remote.commits)
    no_em_dash(pipe)

    # a redelivery acknowledges without a second file, task or brief
    body, headers = granola_delivery("note.access_granted", "not_darius")
    r = pipe.receiver.post("/ingest/granola", content=body, headers=headers)
    assert r.status_code == 200 and r.json()["created"] is False
    assert pipe.ran() == ["process_call"] and len(pipe.tops()) == 1


# ---------- backfill: a push of more than five ----------

def test_backfill_push_digests_every_copy_and_posts_one_summary(pipe):
    extra = {"transcripts/2026-09-24_darius-kemp-again.md": "darius-kemp",
             "transcripts/2026-10-02_lena-okafor-again.md": "lena-okafor"}
    paths = pipe.drop({**DROP, **extra})
    assert len(paths) == 6

    pipe.assert_all_ok()
    assert pipe.ran() == ["backfill"] + ["process_call"] * 6
    assert pipe.results[0]["payload"] == {"paths": paths, "batch_id": "e2e0head"}
    batch = yaml.safe_load((pipe.root / "config/batches/e2e0head.yml").read_text(encoding="utf-8"))
    assert batch["paths"] == paths
    assert {r["payload"]["batch"] for r in pipe.results[1:]} == {"e2e0head"}

    digests = load_digests(pipe.ctx().repo)
    assert sorted(d.transcript for d in digests) == sorted(paths)
    assert {(d.status, d.origin) for d in digests} == {("digested", "backfill")}
    assert all(d.brief is None and d.claims for d in digests)
    assert {d.call_id for d in digests} >= {"2026-09-23_darius-kemp", "2026-09-24_darius-kemp"}

    # no briefs, exactly one summary, posted by the last call to finish
    assert pipe.tops("discovery_brief") == []
    [summary] = pipe.tops("discovery_batch")
    assert summary["metadata"]["event_payload"] == {"key": "batch:e2e0head"}
    assert summary["text"].startswith("Backfill batch e2e0head done: 6 transcripts. 6 digested.")
    # the check that posted it recorded its ts in the batch file
    batch = yaml.safe_load((pipe.root / "config/batches/e2e0head.yml").read_text(encoding="utf-8"))
    assert batch["summary_ts"] == summary["ts"] and batch["paths"] == paths

    # so the nightly sweep skips the batch: no second check, no second summary
    r = pipe.receiver.post("/sweep", json={})
    assert r.status_code == 200
    pipe.assert_all_ok()
    assert pipe.ran()[7:] == ["sweep", "reindex", "backcheck"]
    assert pipe.result("sweep")["batches_checked"] == []
    assert len(pipe.tops("discovery_batch")) == 1
    no_em_dash(pipe)


# ---------- bootstrap roll-up, approval in Slack, backcheck, coverage ----------

def test_bootstrap_proposal_approved_in_slack_then_backchecked(pipe):
    pipe.drop(DROP)                                   # four calls: one process_call each, no batch
    pipe.assert_all_ok()
    assert pipe.ran() == ["process_call"] * 4
    assert len(load_digests(pipe.ctx().repo)) == 4

    # Alex runs the paused rollup-bootstrap job
    r = pipe.receiver.post("/rollup", json={"bootstrap": True})
    assert r.status_code == 200 and r.json()["queued"] == "rollup"
    pipe.assert_all_ok()
    rollup = pipe.result("rollup")
    assert rollup["key"] == "rollup:bootstrap" and rollup["proposals"] == ["boot-01"] and rollup["posted"] == 1
    [summary] = pipe.tops("discovery_rollup")
    [reply] = pipe.slack.replies(CALLS, summary["ts"])
    assert any(b.get("block_id") == "prop:boot-01" and b["type"] == "actions" for b in reply["blocks"])
    proposal = yaml.safe_load((pipe.root / "config/proposals.yml").read_text(encoding="utf-8"))["proposals"][0]
    assert (proposal["id"], proposal["type"], proposal["status"]) == ("boot-01", "add", "open")
    assert len({s["call_id"] for s in proposal["sources"]}) == 4

    # Alex clicks Approve on the proposal's thread reply
    r = pipe.slack_click({
        "type": "block_actions", "user": {"id": "U_ALEX", "name": "alex"}, "trigger_id": "trig-1",
        "container": {"type": "message", "channel_id": CALLS, "message_ts": reply["ts"], "thread_ts": summary["ts"]},
        "message": {"ts": reply["ts"], "thread_ts": summary["ts"], "blocks": reply["blocks"], "text": reply["text"]},
        "actions": [{"type": "button", "action_id": "prop_approve", "block_id": "prop:boot-01", "value": "boot-01",
                     "text": {"type": "plain_text", "text": "Approve"}}],
        "response_url": "https://hooks.slack.test/actions/1"})
    assert r.status_code == 200 and r.content == b""
    pipe.assert_all_ok()
    assert pipe.ran()[-2:] == ["apply_belief_change", "backcheck"]

    # the click locked only that item at once, and the task applied the proposal as B-001
    [lock] = pipe.slack.responses
    assert lock["payload"]["replace_original"] is True
    assert any(b.get("block_id") == "prop:boot-01" and "Queued by alex" in json.dumps(b) for b in lock["payload"]["blocks"])
    apply = pipe.result("apply_belief_change")
    assert (apply["outcome"], apply["entry_ids"]) == ("applied", ["B-001"])
    ref = f"{reply['ts']}:prop:boot-01"
    entry = parse_beliefs((pipe.root / "beliefs.md").read_text(encoding="utf-8")).get("B-001")
    assert entry.section == "beliefs" and entry.statement == proposal["statement"]
    [line] = entry.history
    assert (line.action, line.who, line.via, line.ref) == ("add", "Alex", "bootstrap", ref)
    assert len({call_id for call_id, _ in line.sources}) == 4
    proposals = yaml.safe_load((pipe.root / "config/proposals.yml").read_text(encoding="utf-8"))["proposals"]
    assert proposals[0]["status"] == "applied"
    [updated] = [m for m in pipe.slack.replies(CALLS, summary["ts"]) if m["ts"] == reply["ts"]]
    assert "Alex added B-001 via bootstrap" in json.dumps(updated["blocks"])
    [notice] = pipe.tops("discovery_ref")
    assert notice["metadata"]["event_payload"] == {"key": ref}
    assert notice["text"].startswith("Alex added B-001 via bootstrap: ")

    # the backcheck recorded the new version on every call, and beliefs-status shows full coverage
    assert pipe.result("backcheck") == {"entries": ["B-001"], "digests_updated": 4}
    assert {d.checked.get("B-001") for d in load_digests(pipe.ctx().repo)} == {ref}
    status = (pipe.root / "beliefs-status.md").read_text(encoding="utf-8")
    block = status.split("### B-001 ", 1)[1]
    assert "- checked 4/4 calls" in block.split("###", 1)[0]
    no_em_dash(pipe)


# ---------- request_change through the MCP server ----------

def test_request_change_through_mcp_is_applied_by_the_processor(pipe, monkeypatch):
    pipe.drop({"transcripts/2026-09-23_darius-kemp.md": "darius-kemp"})
    pipe.assert_all_ok()
    call_id = "2026-09-23_darius-kemp"
    assert [d.call_id for d in load_digests(pipe.ctx().repo)] == [call_id]

    monkeypatch.setenv("MCP_DEV_USER", "sam")      # sign-in off: write tools act as Sam
    server = mcp_mod.build_server(pipe.ctx)
    statement = "An attorney on a liability claim makes it more expensive from the start"
    out = mcp_call(server, "request_change", {
        "action": "watch", "statement": statement,
        "scope": {"line": "general liability", "party": "carrier"},
        "source": {"call_id": call_id, "quote": ATTORNEY.upper()}})   # case and punctuation are forgiven
    rel = out["request_file"]
    assert re.fullmatch(r"requests/2026-10-01_sam_an-attorney-on-a-liability-claim\.yml", rel)
    assert out["queued"] is True and out["requested_by"] == "sam"
    assert rel in pipe.remote.created

    # the processor applied it: W-001 with the verbatim quote, request file gone, backcheck run
    pipe.assert_all_ok()
    assert pipe.ran()[-2:] == ["apply_belief_change", "backcheck"]
    apply = pipe.result("apply_belief_change")
    assert (apply["outcome"], apply["entry_ids"]) == ("applied", ["W-001"])
    assert not (pipe.root / rel).exists()
    entry = parse_beliefs((pipe.root / "beliefs.md").read_text(encoding="utf-8")).get("W-001")
    assert entry.section == "watching" and entry.statement == statement
    assert (entry.scope.line, entry.scope.party) == ("general liability", "carrier")
    [line] = entry.history
    assert (line.action, line.who, line.via) == ("watch", "Sam", "agent")
    assert line.ref.startswith(f"{rel}@") and line.sources == [(call_id, ATTORNEY)]
    [notice] = pipe.tops("discovery_ref")
    assert notice["text"] == f"Sam added W-001 to Watching via agent: {statement}"
    [commit] = [c for c in pipe.remote.commits if rel in c["paths"]]
    assert "beliefs.md" in commit["paths"] and "beliefs-status.md" in commit["paths"]
    assert read_digest(pipe.ctx().repo, digest_path(call_id)).checked["W-001"] == line.ref

    # the same request again is caught by the MCP server before anything is committed
    with pytest.raises(Exception, match="already backs W-001"):
        mcp_call(server, "request_change", {"action": "watch", "statement": statement,
                                            "source": {"call_id": call_id, "quote": ATTORNEY}})
    no_em_dash(pipe)


# ---------- P1 golden check: the Darius dry run against tests/expected/brief_darius-kemp.md ----------

_VERDICT = {"Contradicts": "contradicts", "Scope mismatch": "scope_mismatch", "Supports": "supports"}
_CHANGE = re.compile(r"^(Contradicts|Scope mismatch|Supports) ([BW]-\d+(?:, [BW]-\d+)*)\b")
_WATCHING = re.compile(r"^Watching ([BW]-\d+), (supports|contradicts|scope mismatch)\b")


def what_changed(top: str) -> set[tuple[str, str]]:
    """(target, verdict) for every item under WHAT CHANGED, collapsed support lines included."""
    out: set[tuple[str, str]] = set()
    for line in top.split("WHAT CHANGED", 1)[1].splitlines():
        if m := _WATCHING.match(line):
            out.add((m.group(1), m.group(2).replace(" ", "_")))
        elif m := _CHANGE.match(line):
            out |= {(target, _VERDICT[m.group(1)]) for target in m.group(2).split(", ")}
    return out


def finding_quotes(findings: str) -> list[str]:
    """The quote of every finding in a NEW FINDINGS section: the quoted line under each "- " line."""
    lines = findings.splitlines()
    return [lines[i + 1].strip()[1:-1] for i, line in enumerate(lines[:-1])
            if line.startswith("- ") and lines[i + 1].strip().startswith('"')]


def expected_brief() -> tuple[str, str]:
    """(top message, new findings) from the text blocks of tests/expected/brief_darius-kemp.md."""
    blocks = re.findall(r"```text\n(.*?)```", EXPECTED.read_text(encoding="utf-8"), re.S)
    return blocks[0], next(b for b in blocks if b.startswith("NEW FINDINGS"))


def actual_brief(text: str) -> tuple[str, str]:
    """(top message, new findings) from `render_text` output (findings may span several replies)."""
    parts = re.split(r"^--- thread reply \d+ ---\n", text, flags=re.M)
    return parts[0], "\n".join(p for p in parts[1:] if p.startswith("NEW FINDINGS"))


def test_darius_dry_run_matches_the_golden_brief_by_the_p1_rule(tmp_repo, capsys):
    code = cli.main(["--root", str(tmp_repo), "run", str(FIXTURES / "darius-kemp.md"), "--dry-run", "--fake",
                     "--beliefs", str(GOLDEN / "beliefs.md"), "--decisions", str(GOLDEN / "decisions.md"),
                     "--prep", str(GOLDEN / "prep_darius-kemp.md")])
    out = capsys.readouterr().out
    assert code == 0 and EM_DASH not in out
    top, findings = actual_brief(out)
    want_top, want_findings = expected_brief()

    # what changed: the same (target, verdict) items
    assert what_changed(top) == what_changed(want_top) == {
        ("B-004", "contradicts"), ("B-001", "supports"), ("B-002", "supports"), ("B-003", "supports"),
        ("W-001", "supports")}
    assert "Touches D-002 (Footage review tool for adjusters)" in top

    # new findings: one-to-one matches on quote spans overlapping by at least 50%, nothing missing or extra
    parsed = parse("tests/fixtures/darius-kemp.md", fixture_text("darius-kemp"))
    index = QuoteIndex(parsed)
    got = finding_quotes(findings)
    spans = [index.find(q) for q in got]
    assert all(spans), [q for q, s in zip(got, spans) if s is None]
    claims = [Claim(id=None, text="", quote=q, offset=(m.start, m.end)) for q, m in zip(got, spans)]
    want = finding_quotes(want_findings)
    matched = match_labels(parsed, [{"quotes": [{"quote": q}]} for q in want], claims)
    assert len(want) == 12 and len(got) == len(want)
    missing = [want[i] for i in set(range(len(want))) - {li for li, _ in matched}]
    assert not missing, missing
