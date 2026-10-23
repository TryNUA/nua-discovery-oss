"""Tests for service/main.py (receiver and processor apps) and nua/settings.py. All offline."""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import time
from datetime import date
from pathlib import Path
from urllib.parse import urlencode

import httpx
import pytest
import yaml
from fastapi.testclient import TestClient

from nua import interact, runner, settings
from nua.context import Ctx
from nua.digest import write_digest
from nua.ingest.circleback import CirclebackClient
from nua.ingest.granola import GranolaClient, note_to_standard
from nua.ports import NullRemote
from nua.tasks import InlineQueue, task_name
from nua.types import Digest
from service import main

GRANOLA_SECRET = "whsec_" + base64.b64encode(b"granola-test-key").decode()
CB_SECRET = "circleback-secret"
GH_SECRET = "github-secret"
SLACK_SECRET = "slack-secret"


# ---------- helpers ----------

class WritingRemote(NullRemote):
    """NullRemote that also writes created files into the repo, like a push the next sync would pull."""

    def __init__(self, root: Path, existing: set[str] | None = None):
        super().__init__()
        self.root = root
        self.existing = existing or set()   # on the remote, never pulled into the clone
        self.unsynced: dict[str, str] = {}  # on the remote, pulled into the clone by the next sync
        self.syncs = 0

    def create_file_only(self, rel, content, message):
        if rel in self.existing or rel in self.unsynced or (self.root / rel).exists():
            return False
        (self.root / rel).parent.mkdir(parents=True, exist_ok=True)
        (self.root / rel).write_text(content, encoding="utf-8")
        self.created.append(rel)
        return True

    def sync(self):
        self.syncs += 1
        for rel, content in self.unsynced.items():
            (self.root / rel).parent.mkdir(parents=True, exist_ok=True)
            (self.root / rel).write_text(content, encoding="utf-8")
        self.unsynced = {}


def edit_config(root: Path, name: str, update) -> None:
    path = root / "config" / name
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    update(data)
    path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")


CUTOFF = "2026-09-01T00:00:00-07:00"   # go-live before every fixture item (ctx.now() is 2026-10-01)


@pytest.fixture
def sctx(tmp_repo, monkeypatch) -> Ctx:
    edit_config(tmp_repo, "people.yml", lambda p: p["sam"].update(email="sam@acme.dev", slack_id="U_SAM"))
    edit_config(tmp_repo, "pipeline.yml", lambda p: p.update(ingest_cutoff=CUTOFF))
    for name, value in {"GRANOLA_WEBHOOK_SECRET": GRANOLA_SECRET, "GRANOLA_API_KEY": "grn_test",
                        "CIRCLEBACK_SIGNING_SECRET_ALEX": CB_SECRET, "CIRCLEBACK_API_KEY_ALEX": "cb_test",
                        "GITHUB_WEBHOOK_SECRET": GH_SECRET, "GITHUB_APP_SLUG": "nua-discovery",
                        "SLACK_SIGNING_SECRET": SLACK_SECRET, "REPO_BRANCH": "main"}.items():
        monkeypatch.setenv(name, value)
    for name in ("NUA_DEV", "RECEIVER_URL", "SCHEDULER_SERVICE_ACCOUNT"):
        monkeypatch.delenv(name, raising=False)
    ctx = Ctx.for_tests(tmp_repo)
    ctx.remote = WritingRemote(tmp_repo)
    return ctx


def note(**over) -> dict:
    n = {
        "id": "not_abc123",
        "title": "Darius / Alex",
        "created_at": "2026-10-01T16:00:00Z",
        "owner": {"name": "Alex Rivera", "email": "alex.rivera@gmail.com"},
        "calendar_event": {"scheduled_start_time": "2026-10-01T16:00:00Z", "invitees": [{"email": "darius@example.com"}]},
        "attendees": [{"name": "Darius Kemp", "email": "darius@example.com"}],
        "summary_markdown": "### Overview\nDarius talked about store footage.",
        "transcript": [
            {"speaker": {"source": "microphone"}, "text": "Hi Darius, thanks for the time.",
             "start_time": "2026-10-01T16:00:05Z"},
            {"speaker": {"source": "speaker"}, "text": "Happy to. We handle three store claims.",
             "start_time": "2026-10-01T16:00:09Z"},
        ],
    }
    n.update(over)
    return n


def granola_http(n: dict | None = None, *, status: int = 200) -> httpx.Client:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == f"/v1/notes/{(n or note())['id']}"
        if status != 200:
            return httpx.Response(status, json={"error": "nope"})
        return httpx.Response(200, json=n or note())
    return httpx.Client(transport=httpx.MockTransport(handler))


def granola_headers(body: bytes, *, secret: str = GRANOLA_SECRET) -> dict:
    msg_id, ts = "evt_1", str(int(time.time()))
    key = base64.b64decode(secret[len("whsec_"):])
    sig = base64.b64encode(hmac.new(key, f"{msg_id}.{ts}.".encode() + body, hashlib.sha256).digest()).decode()
    return {"webhook-id": msg_id, "webhook-timestamp": ts, "webhook-signature": f"v1,{sig}",
            "content-type": "application/json"}


def granola_event(event_type: str = "note.generated") -> bytes:
    return json.dumps({"event_id": "evt_1", "event_type": event_type, "note_id": "not_abc123",
                       "occurred_at": "2026-10-01T17:00:00Z"}).encode()


def receiver(ctx: Ctx, *, granola_note: dict | None = None, granola_status: int = 200,
             events: list | None = None) -> TestClient:
    app = main.create_receiver_app(
        lambda: ctx, ack=lambda: interact.AckCtx.from_ctx(ctx),
        granola=lambda: GranolaClient("grn_test", http=granola_http(granola_note, status=granola_status)),
        circleback=lambda person: CirclebackClient("cb_test", http=circleback_http(events)))
    return TestClient(app)


CB_PAYLOAD = {
    "id": 4242, "name": "Darius / Alex", "createdAt": "2026-10-01T16:30:00Z", "tags": ["discovery"],
    "icalUid": "abc@google.com", "attendees": [{"name": "Darius Kemp", "email": "darius@example.com"}],
    "notes": "#### Overview\nDarius handles store claims.",
    "transcript": [{"speaker": "Alex Rivera", "text": "Hi Darius.", "timestamp": 1.0},
                   {"speaker": "Darius Kemp", "text": "Hello, we handle three store claims.", "timestamp": 4.5}],
}


def circleback_http(events: list | None) -> httpx.Client:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/calendar/events")
        if events is None:
            return httpx.Response(500, json={})
        return httpx.Response(200, json=events)
    return httpx.Client(transport=httpx.MockTransport(handler))


def cb_sig(body: bytes, secret: str = CB_SECRET) -> str:
    return hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def gh_headers(body: bytes, event: str = "push", secret: str = GH_SECRET) -> dict:
    sig = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return {"X-GitHub-Event": event, "X-Hub-Signature-256": sig, "content-type": "application/json"}


def push(added: list[str], *, modified: list[str] | None = None, sender: str = "alexrivera",
         ref: str = "refs/heads/main") -> bytes:
    return json.dumps({"ref": ref, "after": "abc123def", "sender": {"login": sender, "type": "User"},
                       "commits": [{"id": "abc123def", "added": added, "removed": [],
                                    "modified": modified or []}]}).encode()


def slack_headers(body: bytes, secret: str = SLACK_SECRET) -> dict:
    ts = str(int(time.time()))
    sig = "v0=" + hmac.new(secret.encode(), f"v0:{ts}:".encode() + body, hashlib.sha256).hexdigest()
    return {"X-Slack-Request-Timestamp": ts, "X-Slack-Signature": sig,
            "content-type": "application/x-www-form-urlencoded"}


# ---------- /ingest/granola ----------

def test_granola_webhook_creates_file_and_enqueues(sctx):
    client = receiver(sctx)
    body = granola_event()
    r = client.post("/ingest/granola", content=body, headers=granola_headers(body))
    assert r.status_code == 200
    rel = "transcripts/granola_not_abc123.md"
    assert sctx.remote.created == [rel]
    text = (sctx.repo.root / rel).read_text(encoding="utf-8")
    assert "source: granola" in text and "Happy to. We handle three store claims." in text
    assert yaml.safe_load(text.split("---")[1])["ingested_at"] == "2026-10-01T09:00:00-07:00"   # ctx.now(), Pacific
    [task] = sctx.queue.tasks
    assert task["kind"] == "process_call"
    assert task["payload"] == {"transcript": rel}
    assert task["name"] == task_name("process_call", rel)
    assert task["delay_seconds"] == 0


def test_granola_redelivery_skips_the_write_but_still_enqueues(sctx):
    sctx.remote.existing.add("transcripts/granola_not_abc123.md")
    client = receiver(sctx)
    body = granola_event("note.access_granted")
    assert client.post("/ingest/granola", content=body, headers=granola_headers(body)).status_code == 200
    assert sctx.remote.created == []
    assert [t["kind"] for t in sctx.queue.tasks] == ["process_call"]


def test_granola_bad_signature_is_503_never_4xx(sctx):
    client = receiver(sctx)
    body = granola_event()
    headers = granola_headers(body, secret="whsec_" + base64.b64encode(b"wrong").decode())
    r = client.post("/ingest/granola", content=body, headers=headers)
    assert r.status_code == 503
    assert sctx.queue.tasks == [] and sctx.remote.created == []


def test_granola_fetch_failure_is_503(sctx):
    client = receiver(sctx, granola_status=404)
    body = granola_event()
    assert client.post("/ingest/granola", content=body, headers=granola_headers(body)).status_code == 503
    assert sctx.queue.tasks == []


def test_granola_note_before_cutoff_is_acknowledged_and_ignored(tmp_repo, sctx):
    edit_config(tmp_repo, "pipeline.yml", lambda p: p.update(ingest_cutoff="2026-11-02T09:00:00-08:00"))
    ctx = Ctx.for_tests(tmp_repo)
    ctx.remote = WritingRemote(tmp_repo)
    client = receiver(ctx)
    body = granola_event()
    assert client.post("/ingest/granola", content=body, headers=granola_headers(body)).status_code == 200
    assert ctx.remote.created == [] and ctx.queue.tasks == []


def test_granola_call_with_another_cofounder_is_delayed(sctx):
    n = note(attendees=[{"name": "Darius Kemp", "email": "darius@example.com"}, {"name": "Sam", "email": "sam@acme.dev"}])
    client = receiver(sctx, granola_note=n)
    body = granola_event()
    client.post("/ingest/granola", content=body, headers=granola_headers(body))
    assert sctx.queue.tasks[0]["delay_seconds"] == 15 * 60


def test_granola_note_edited_longer_transcript_creates_next_version(sctx):
    short = note(transcript=note()["transcript"][:1])
    receiver(sctx, granola_note=short).post("/ingest/granola", content=granola_event(),
                                            headers=granola_headers(granola_event()))
    body = granola_event("note.edited")
    r = receiver(sctx).post("/ingest/granola", content=body, headers=granola_headers(body))
    assert r.status_code == 200
    assert sctx.remote.created == ["transcripts/granola_not_abc123.md", "transcripts/granola_not_abc123_v2.md"]
    assert sctx.queue.tasks[-1]["payload"] == {"transcript": "transcripts/granola_not_abc123_v2.md"}
    v2 = (sctx.repo.root / "transcripts/granola_not_abc123_v2.md").read_text(encoding="utf-8")
    assert yaml.safe_load(v2.split("---")[1])["ingested_at"] == "2026-10-01T09:00:00-07:00"


def test_granola_note_edited_same_length_is_ignored_once_a_digest_points_to_it(sctx):
    """A notes-only edit of a digested call: nothing to write and nothing to queue."""
    rel = "transcripts/granola_not_abc123.md"
    client = receiver(sctx)
    client.post("/ingest/granola", content=granola_event(), headers=granola_headers(granola_event()))
    write_digest(sctx.repo, Digest(call_id="2026-10-01_darius-kemp", transcript=rel, status="digested",
                                   origin="live", prompt_version="v1"))
    sctx.queue.tasks.clear()             # the first task has run; its name no longer dedupes
    body = granola_event("note.edited")
    r = client.post("/ingest/granola", content=body, headers=granola_headers(body))
    assert r.status_code == 200 and "ignored" in r.json()
    assert sctx.remote.created == [rel]
    assert sctx.queue.tasks == []


def test_granola_note_edited_shorter_transcript_is_ignored(sctx):
    receiver(sctx, granola_note=note(transcript=_items(3))).post(
        "/ingest/granola", content=granola_event(), headers=granola_headers(granola_event()))
    sctx.queue.tasks.clear()
    body = granola_event("note.edited")
    r = receiver(sctx, granola_note=note(transcript=_items(2))).post(
        "/ingest/granola", content=body, headers=granola_headers(body))
    assert r.status_code == 200 and "ignored" in r.json()
    assert sctx.remote.created == ["transcripts/granola_not_abc123.md"]
    assert sctx.queue.tasks == []


def test_granola_note_edited_same_length_still_enqueues_an_undigested_version(sctx):
    """The stored version no digest points to yet may never have been queued: queue it, like any redelivery."""
    rel = "transcripts/granola_not_abc123.md"
    client = receiver(sctx)
    client.post("/ingest/granola", content=granola_event(), headers=granola_headers(granola_event()))
    sctx.queue.tasks.clear()             # the note.generated enqueue was lost
    body = granola_event("note.edited")
    r = client.post("/ingest/granola", content=body, headers=granola_headers(body))
    assert r.status_code == 200
    assert r.json()["transcript"] == rel and r.json()["created"] is False and r.json()["queued"] is True
    assert sctx.remote.created == [rel]
    assert [t["payload"] for t in sctx.queue.tasks] == [{"transcript": rel}]


def test_granola_note_edited_redelivery_enqueues_the_version_the_failed_delivery_wrote(sctx):
    """The first note.edited delivery writes _v2, then the enqueue fails (503). Granola redelivers:
    the transcript is no longer longer than the stored one, and _v2 still has to be queued."""
    receiver(sctx, granola_note=note(transcript=_items(2))).post(
        "/ingest/granola", content=granola_event(), headers=granola_headers(granola_event()))
    v2 = "transcripts/granola_not_abc123_v2.md"
    enqueue, failures = sctx.queue.enqueue, [RuntimeError("queue down")]

    def flaky(*args, **kwargs):
        if failures:
            raise failures.pop()
        return enqueue(*args, **kwargs)

    sctx.queue.enqueue = flaky
    body = granola_event("note.edited")
    client = receiver(sctx, granola_note=note(transcript=_items(4)))
    assert client.post("/ingest/granola", content=body, headers=granola_headers(body)).status_code == 503
    assert sctx.remote.created == ["transcripts/granola_not_abc123.md", v2]
    r = client.post("/ingest/granola", content=body, headers=granola_headers(body))
    assert r.status_code == 200
    assert r.json()["transcript"] == v2 and r.json()["created"] is False and r.json()["queued"] is True
    assert sctx.remote.created == ["transcripts/granola_not_abc123.md", v2]
    assert sctx.queue.tasks[-1]["payload"] == {"transcript": v2}
    assert sctx.queue.tasks[-1]["name"] == task_name("process_call", v2)


class LostResponseRemote(WritingRemote):
    """The PUT of `lost` lands on the remote but its response is lost: create_file_only returns False
    (as gitops does after the retry gets 422), and the next sync pulls the file into the clone."""

    def __init__(self, root: Path, lost: str):
        super().__init__(root)
        self.lost = lost

    def create_file_only(self, rel, content, message):
        if rel == self.lost and rel not in self.unsynced and not (self.root / rel).exists():
            self.unsynced[rel] = content
            return False
        return super().create_file_only(rel, content, message)


def test_granola_note_edited_enqueues_the_version_whose_create_response_was_lost(sctx):
    v2 = "transcripts/granola_not_abc123_v2.md"
    sctx.remote = LostResponseRemote(sctx.repo.root, v2)
    receiver(sctx, granola_note=note(transcript=_items(2))).post(
        "/ingest/granola", content=granola_event(), headers=granola_headers(granola_event()))
    body = granola_event("note.edited")
    r = receiver(sctx, granola_note=note(transcript=_items(4))).post(
        "/ingest/granola", content=body, headers=granola_headers(body))
    assert r.status_code == 200
    assert r.json()["transcript"] == v2 and r.json()["created"] is False and r.json()["queued"] is True
    assert (sctx.repo.root / v2).exists()
    assert sctx.remote.created == ["transcripts/granola_not_abc123.md"]
    assert [t["payload"] for t in sctx.queue.tasks] == [{"transcript": "transcripts/granola_not_abc123.md"},
                                                        {"transcript": v2}]


def test_granola_note_edited_enqueues_version_one_when_it_is_the_first_event_and_the_response_was_lost(sctx):
    v1 = "transcripts/granola_not_abc123.md"
    sctx.remote = LostResponseRemote(sctx.repo.root, v1)
    body = granola_event("note.edited")
    r = receiver(sctx).post("/ingest/granola", content=body, headers=granola_headers(body))
    assert r.status_code == 200
    assert r.json()["transcript"] == v1 and r.json()["created"] is False
    assert [t["payload"] for t in sctx.queue.tasks] == [{"transcript": v1}]


def test_granola_note_edited_with_nothing_stored_ingests_version_one(sctx):
    body = granola_event("note.edited")
    receiver(sctx).post("/ingest/granola", content=body, headers=granola_headers(body))
    assert sctx.remote.syncs == 1
    assert sctx.remote.created == ["transcripts/granola_not_abc123.md"]


def _items(n: int) -> list[dict]:
    lines = ["Hi Darius, thanks for the time.", "Happy to. We handle three store claims.",
             "Who pulls the footage?", "The store manager, and the camera system keeps it for thirty days at most."]
    return [{"speaker": {"source": "microphone" if i % 2 == 0 else "speaker"}, "text": lines[i],
             "start_time": f"2026-10-01T16:00:{5 + 4 * i:02d}Z"} for i in range(n)]


def test_granola_note_edited_syncs_when_the_next_version_landed_after_the_last_sync(sctx):
    receiver(sctx, granola_note=note(transcript=_items(2))).post(
        "/ingest/granola", content=granola_event(), headers=granola_headers(granola_event()))
    v2 = "transcripts/granola_not_abc123_v2.md"
    sctx.remote.unsynced[v2] = note_to_standard(note(transcript=_items(3)), config=sctx.config, version=2)[1]
    body = granola_event("note.edited")
    r = receiver(sctx, granola_note=note(transcript=_items(4))).post(
        "/ingest/granola", content=body, headers=granola_headers(body))
    assert r.status_code == 200 and r.json()["created"] is True
    assert sctx.remote.syncs == 1
    assert sctx.remote.created == ["transcripts/granola_not_abc123.md", "transcripts/granola_not_abc123_v3.md"]
    assert sctx.queue.tasks[-1]["payload"] == {"transcript": "transcripts/granola_not_abc123_v3.md"}


def test_granola_note_edited_is_503_when_the_next_version_never_shows_up(sctx):
    receiver(sctx, granola_note=note(transcript=_items(2))).post(
        "/ingest/granola", content=granola_event(), headers=granola_headers(granola_event()))
    sctx.remote.existing.add("transcripts/granola_not_abc123_v2.md")
    body = granola_event("note.edited")
    r = receiver(sctx, granola_note=note(transcript=_items(4))).post(
        "/ingest/granola", content=body, headers=granola_headers(body))
    assert r.status_code == 503
    assert sctx.remote.syncs == 2
    assert sctx.remote.created == ["transcripts/granola_not_abc123.md"]
    assert len(sctx.queue.tasks) == 1


@pytest.mark.parametrize("event_type", ["note.generated", "note.edited"])
@pytest.mark.parametrize("transcript", [None, [], [{"speaker": {"source": "speaker"}, "text": "  "}]])
def test_granola_note_without_transcript_creates_no_file(sctx, event_type, transcript):
    body = granola_event(event_type)
    r = receiver(sctx, granola_note=note(transcript=transcript)).post(
        "/ingest/granola", content=body, headers=granola_headers(body))
    assert r.status_code == 200 and r.json()["ignored"] == "no transcript in note"
    assert sctx.remote.created == [] and sctx.queue.tasks == []
    assert list((sctx.repo.root / "transcripts").iterdir()) == []


def _with_cutoff(tmp_repo, value) -> Ctx:
    edit_config(tmp_repo, "pipeline.yml", lambda p: p.update(ingest_cutoff=value))
    ctx = Ctx.for_tests(tmp_repo)
    ctx.remote = WritingRemote(tmp_repo)
    return ctx


def _post_both(ctx: Ctx) -> list:
    client = receiver(ctx, events=[])
    g = client.post("/ingest/granola", content=granola_event(), headers=granola_headers(granola_event()))
    body = json.dumps(CB_PAYLOAD).encode()
    c = client.post("/ingest/circleback/alex", content=body, headers={"x-signature": cb_sig(body)})
    return [g, c]


@pytest.mark.parametrize("value", [None, "", "go-live", "2026-11-31"])   # an impossible date: see test_config.py for the unquoted text
def test_recorder_webhooks_ingest_nothing_without_a_readable_cutoff(tmp_repo, sctx, value):
    ctx = _with_cutoff(tmp_repo, value)
    for r in _post_both(ctx):
        assert r.status_code == 200 and r.json()["ignored"] == "ingest_cutoff not set"
    assert ctx.remote.created == [] and ctx.queue.tasks == []


def test_date_only_cutoff_is_read_as_midnight_pacific(tmp_repo, sctx):
    # These payloads (16:00Z) cannot tell Pacific from UTC midnight; tests/unit/test_config.py posts items
    # at 05:00Z and 08:00Z on the cutoff day, which can.
    ctx = _with_cutoff(tmp_repo, date(2026, 11, 2))
    assert isinstance(ctx.config.pipeline.ingest_cutoff, date)   # YAML reads `2026-11-02` as a date
    for r in _post_both(ctx):
        assert r.status_code == 200 and r.json()["ignored"] in ("call before ingest_cutoff",       # granola
                                                                "created before ingest_cutoff")    # circleback
    assert ctx.remote.created == [] and ctx.queue.tasks == []

    ctx = _with_cutoff(tmp_repo, date(2026, 9, 1))
    assert [r.status_code for r in _post_both(ctx)] == [200, 200]
    assert ctx.remote.created == ["transcripts/granola_not_abc123.md", "transcripts/circleback_4242.md"]


def test_a_note_created_before_the_cutoff_for_a_call_after_it_is_ingested(tmp_repo, sctx):
    """Granola creates a note when a meeting is opened ahead of time: the call's start decides."""
    edit_config(tmp_repo, "pipeline.yml", lambda p: p.update(ingest_cutoff="2026-10-01T06:00:00-04:00"))
    ctx = Ctx.for_tests(tmp_repo)
    ctx.remote = WritingRemote(tmp_repo)
    early = note(created_at="2026-09-30T20:00:00Z",
                 calendar_event={"scheduled_start_time": "2026-10-01T16:30:00Z", "event_title": "Darius / Alex"})
    client = receiver(ctx, granola_note=early)
    body = granola_event()
    r = client.post("/ingest/granola", content=body, headers=granola_headers(body))
    assert r.status_code == 200 and "ignored" not in r.json()
    assert ctx.remote.created == ["transcripts/granola_not_abc123.md"]


# ---------- /ingest/circleback/<person> ----------

def test_circleback_webhook_uses_calendar_start(sctx):
    client = receiver(sctx, events=[{"meetingId": 4242, "startTime": "2026-10-01T16:00:00Z"}])
    body = json.dumps(CB_PAYLOAD).encode()
    r = client.post("/ingest/circleback/alex", content=body, headers={"x-signature": cb_sig(body)})
    assert r.status_code == 200
    rel = "transcripts/circleback_4242.md"
    assert sctx.remote.created == [rel]
    fm = yaml.safe_load((sctx.repo.root / rel).read_text(encoding="utf-8").split("---")[1])
    assert fm["start_source"] == "calendar" and fm["recorded_by"] == "alex"
    assert fm["ingested_at"] == "2026-10-01T09:00:00-07:00"
    assert sctx.queue.tasks[0]["payload"] == {"transcript": rel}


def test_circleback_calendar_failure_falls_back_to_created_at(sctx):
    client = receiver(sctx, events=None)
    body = json.dumps(CB_PAYLOAD).encode()
    assert client.post("/ingest/circleback/alex", content=body,
                       headers={"x-signature": cb_sig(body)}).status_code == 200
    text = (sctx.repo.root / "transcripts/circleback_4242.md").read_text(encoding="utf-8")
    assert "start_source: created" in text


def test_circleback_bad_signature_or_unknown_person_is_503(sctx):
    client = receiver(sctx, events=[])
    body = json.dumps(CB_PAYLOAD).encode()
    assert client.post("/ingest/circleback/alex", content=body,
                       headers={"x-signature": cb_sig(body, "wrong")}).status_code == 503
    assert client.post("/ingest/circleback/nobody", content=body,
                       headers={"x-signature": cb_sig(body)}).status_code == 503
    assert sctx.queue.tasks == []


def test_circleback_meeting_without_discovery_tag_is_ignored(sctx):
    client = receiver(sctx, events=[])
    body = json.dumps({**CB_PAYLOAD, "tags": ["investor"]}).encode()
    assert client.post("/ingest/circleback/alex", content=body,
                       headers={"x-signature": cb_sig(body)}).status_code == 200
    assert sctx.remote.created == [] and sctx.queue.tasks == []


@pytest.mark.parametrize("tags", [[], None, "missing"])
def test_circleback_untagged_meeting_is_ignored(sctx, tags):
    payload = {**CB_PAYLOAD, "name": "Seed update with Jane (investor)", "tags": tags}
    if tags == "missing":
        del payload["tags"]
    body = json.dumps(payload).encode()
    r = receiver(sctx, events=[]).post("/ingest/circleback/alex", content=body,
                                       headers={"x-signature": cb_sig(body)})
    assert r.status_code == 200 and r.json()["ignored"] == "not tagged discovery"
    assert sctx.remote.created == [] and sctx.queue.tasks == []


@pytest.mark.parametrize("transcript", ["missing", None, [], [{"speaker": "Darius Kemp", "text": " \n"}]])
def test_circleback_payload_without_transcript_creates_no_file(sctx, transcript):
    payload = {**CB_PAYLOAD, "transcript": transcript}
    if transcript == "missing":
        del payload["transcript"]
    body = json.dumps(payload).encode()
    r = receiver(sctx, events=[]).post("/ingest/circleback/alex", content=body,
                                       headers={"x-signature": cb_sig(body)})
    assert r.status_code == 200 and r.json()["ignored"] == "no transcript in payload"
    assert sctx.remote.created == [] and sctx.queue.tasks == []
    assert list((sctx.repo.root / "transcripts").iterdir()) == []


# ---------- /github ----------

def test_github_bad_signature_is_401(sctx):
    client = receiver(sctx)
    body = push(["transcripts/a.md"])
    assert client.post("/github", content=body, headers=gh_headers(body, secret="wrong")).status_code == 401
    assert sctx.queue.tasks == []


def test_github_small_drop_enqueues_process_call_per_file(sctx):
    client = receiver(sctx)
    body = push(["transcripts/a.md", "transcripts/b.md", "transcripts/.gitkeep", "notes/x.md"])
    assert client.post("/github", content=body, headers=gh_headers(body)).status_code == 200
    assert [(t["kind"], t["payload"]) for t in sctx.queue.tasks] == [
        ("process_call", {"transcript": "transcripts/a.md"}), ("process_call", {"transcript": "transcripts/b.md"})]
    assert sctx.queue.tasks[0]["name"] == task_name("process_call", "transcripts/a.md")


def test_github_push_of_more_than_five_is_one_backfill(sctx):
    client = receiver(sctx)
    paths = [f"transcripts/2026-09-{i:02d}_x.md" for i in range(1, 7)]
    body = push(paths)
    client.post("/github", content=body, headers=gh_headers(body))
    [task] = sctx.queue.tasks
    assert task["kind"] == "backfill"
    assert task["payload"] == {"paths": paths, "batch_id": "abc123def"}


def test_github_requests_and_overrides(sctx):
    client = receiver(sctx)
    body = push(["requests/2026-10-06_sam_x.yml"], modified=["config/overrides.yml"])
    client.post("/github", content=body, headers=gh_headers(body))
    assert [(t["kind"], t["payload"]) for t in sctx.queue.tasks] == [
        ("apply_belief_change", {"request_file": "requests/2026-10-06_sam_x.yml"}), ("reindex", {})]


def test_github_form_encoded_payload(sctx):
    client = receiver(sctx)
    body = urlencode({"payload": push(["transcripts/a.md"]).decode()}).encode()
    assert client.post("/github", content=body, headers=gh_headers(body)).status_code == 200
    assert [t["payload"] for t in sctx.queue.tasks] == [{"transcript": "transcripts/a.md"}]


def test_receiver_starts_without_cloning_or_building_clients(sctx):
    """The receiver scales to zero: startup takes nothing on (no clone, no clients), and /healthz
    neither."""
    built = []

    def factory():
        built.append("repo")
        return sctx

    def ack():
        built.append("ack")
        return interact.AckCtx.from_ctx(sctx)

    with TestClient(main.create_receiver_app(factory, ack=ack)) as client:
        assert client.get("/healthz").json() == {"ok": True}
    assert built == []


def test_github_ignores_the_apps_own_pushes_other_branches_and_pings(sctx):
    client = receiver(sctx)
    body = push(["transcripts/a.md"], sender="nua-discovery[bot]")
    assert client.post("/github", content=body, headers=gh_headers(body)).status_code == 200
    body = push(["transcripts/a.md"], ref="refs/heads/feature")
    assert client.post("/github", content=body, headers=gh_headers(body)).status_code == 200
    body = b'{"zen": "hi"}'
    assert client.post("/github", content=body, headers=gh_headers(body, event="ping")).status_code == 200
    assert sctx.queue.tasks == []


def test_github_files_in_a_subfolder_of_transcripts_get_an_ops_notice(sctx):
    # The GitHub web UI keeps folder structure when a folder is dragged in.
    client = receiver(sctx)
    body = push(["transcripts/darius-kemp.md", "transcripts/Granola exports/lena-okafor.md",
                 "transcripts/Granola exports/wade \u2014 pruitt.md", "transcripts/Granola exports/.DS_Store",
                 "transcripts/.hidden/x.md"])
    r = client.post("/github", content=body, headers=gh_headers(body))
    assert r.status_code == 200
    assert r.json()["not_processed"] == ["transcripts/Granola exports/lena-okafor.md",
                                         "transcripts/Granola exports/wade \u2014 pruitt.md"]
    assert [t["payload"] for t in sctx.queue.tasks] == [{"transcript": "transcripts/darius-kemp.md"}]
    [m] = sctx.slack.live(sctx.channel("ops"))
    for text in (m["text"], m["blocks"][0]["text"]["text"]):
        assert "`transcripts/Granola exports/lena-okafor.md`" in text
        assert "`transcripts/Granola exports/wade - pruitt.md`" in text
        assert "\u2014" not in text and ".DS_Store" not in text and ".hidden" not in text
        assert "move" in text.lower()


def test_github_subfolder_only_push_still_gets_the_notice(sctx):
    client = receiver(sctx)
    body = push(["transcripts/exports/a.md"])
    assert client.post("/github", content=body, headers=gh_headers(body)).status_code == 200
    assert sctx.queue.tasks == []
    [m] = sctx.slack.live(sctx.channel("ops"))
    assert "`transcripts/exports/a.md`" in m["text"]


def test_github_subfolder_notice_failure_never_fails_the_push(sctx, monkeypatch, caplog):
    def down(*args, **kwargs):
        raise RuntimeError("slack 503")

    monkeypatch.setattr(sctx.slack, "post", down)
    client = receiver(sctx)
    body = push(["transcripts/a.md", "transcripts/exports/b.md"])
    r = client.post("/github", content=body, headers=gh_headers(body))
    assert r.status_code == 200
    assert [t["payload"] for t in sctx.queue.tasks] == [{"transcript": "transcripts/a.md"}]
    assert any("transcripts/exports/b.md" in rec.getMessage() for rec in caplog.records if rec.levelname == "WARNING")


def test_github_non_ascii_signature_is_401(sctx):
    client = receiver(sctx)
    body = push(["transcripts/a.md"])
    headers = {**gh_headers(body), "X-Hub-Signature-256": b"sha256=\xff"}
    assert client.post("/github", content=body, headers=headers).status_code == 401
    assert sctx.queue.tasks == []


# ---------- /slack/interact ----------

def test_slack_bad_signature_is_401(sctx):
    client = receiver(sctx)
    body = urlencode({"payload": json.dumps({"type": "block_actions"})}).encode()
    assert client.post("/slack/interact", content=body, headers=slack_headers(body, "wrong")).status_code == 401


def test_slack_non_ascii_signature_is_401(sctx, monkeypatch):
    seen = []
    monkeypatch.setattr(interact, "handle_interaction", lambda *a, **kw: seen.append(1))
    client = receiver(sctx)
    body = urlencode({"payload": json.dumps({"type": "block_actions"})}).encode()
    headers = {**slack_headers(body), "X-Slack-Signature": b"v0=\xff\xfe"}
    assert client.post("/slack/interact", content=body, headers=headers).status_code == 401
    assert seen == []


def test_slack_interaction_is_handled_and_returns_the_body(sctx, monkeypatch):
    seen = []

    def handle(ctx, payload):
        seen.append(payload["type"])
        return {"response_action": "errors", "errors": {"statement": "x"}} if payload["type"] == "view_submission" else None

    monkeypatch.setattr(interact, "handle_interaction", handle)
    client = receiver(sctx)
    body = urlencode({"payload": json.dumps({"type": "block_actions"})}).encode()
    r = client.post("/slack/interact", content=body, headers=slack_headers(body))
    assert r.status_code == 200 and r.content == b""
    body = urlencode({"payload": json.dumps({"type": "view_submission"})}).encode()
    r = client.post("/slack/interact", content=body, headers=slack_headers(body))
    assert r.json() == {"response_action": "errors", "errors": {"statement": "x"}}
    assert seen == ["block_actions", "view_submission"]


# ---------- the Slack path never reaches the repo ----------

PRESSURE_BLOCKS = [
    {"type": "section", "block_id": "item:pressure:B-001",
     "text": {"type": "mrkdwn", "text": "*Under pressure: B-001* Video footage is the most valuable evidence"}},
    {"type": "actions", "block_id": "pressure:B-001", "elements": [
        {"type": "button", "action_id": f"pressure_{verb}", "text": {"type": "plain_text", "text": verb.title()},
         "value": "B-001"} for verb in ("keep", "reword")]},
]


def slack_click(action_id: str, *, ts: str = "1700000000.000200") -> dict:
    return {"type": "block_actions", "user": {"id": "U_ALEX", "name": "alex"}, "trigger_id": "trigger-1",
            "response_url": "https://hooks.slack.com/actions/T1/1/abc",
            "container": {"type": "message", "message_ts": ts, "channel_id": "C123", "thread_ts": None},
            "channel": {"id": "C123"},
            "message": {"ts": ts, "text": "UNDER PRESSURE B-001", "blocks": PRESSURE_BLOCKS},
            "actions": [{"type": "button", "action_id": action_id, "block_id": "pressure:B-001", "value": "B-001",
                         "text": {"type": "plain_text", "text": action_id.split("_")[1].title()}}]}


def post_slack(client: TestClient, payload: dict):
    body = urlencode({"payload": json.dumps(payload)}).encode()
    return client.post("/slack/interact", content=body, headers=slack_headers(body))


@pytest.fixture
def repo_free(monkeypatch):
    """The production receiver (create_app) with fake Slack and queue clients, where anything that would
    clone, pull or reach GitHub fails the test."""
    from nua import gitops
    from nua.ports import FakeSlack, RecordingQueue

    def forbidden(*args, **kwargs):
        raise AssertionError("the Slack path touched the repo or GitHub")

    monkeypatch.setattr(settings, "build_ctx", forbidden)
    monkeypatch.setattr(settings, "_get_repo", forbidden)
    monkeypatch.setattr(gitops, "clone", forbidden)
    monkeypatch.setattr(gitops, "GitHubAppAuth", forbidden)
    monkeypatch.setattr(gitops, "GitRemote", forbidden)
    slack, queue = FakeSlack(), RecordingQueue()
    monkeypatch.setattr(settings, "_get_clients", lambda: settings._Clients(slack=slack, queue=queue))
    monkeypatch.setenv("SERVICE_ROLE", "receiver")
    monkeypatch.setenv("SLACK_SIGNING_SECRET", SLACK_SECRET)
    interact.reset_state()
    return TestClient(main.create_app("receiver")), slack, queue


def test_a_slack_click_is_answered_without_the_repo(repo_free):
    client, slack, queue = repo_free
    r = post_slack(client, slack_click("pressure_keep"))
    assert r.status_code == 200 and r.content == b""
    [task] = queue.tasks
    assert task["kind"] == "apply_belief_change"
    assert task["payload"]["change"] == {"action": "keep", "target": "B-001", "requested_by": "alex"}
    [resp] = slack.responses
    assert "Queued by alex" in json.dumps(resp["payload"]["blocks"])


def test_a_slack_form_opens_and_submits_without_the_repo(repo_free):
    client, slack, queue = repo_free
    r = post_slack(client, slack_click("pressure_reword"))
    assert r.status_code == 200
    [opened] = slack.views
    view = opened["view"]
    statement = next(b for b in view["blocks"] if b.get("block_id") == "statement")
    assert statement["element"]["initial_value"] == "Video footage is the most valuable evidence"
    scope = next(b for b in view["blocks"] if b.get("block_id") == "scope")
    assert "initial_value" not in scope["element"] and scope["optional"] is True
    assert queue.tasks == [] and slack.responses == []

    state = {"statement": {"value": {"type": "plain_text_input", "value": "Video is the best evidence"}}}
    submission = {"type": "view_submission", "user": {"id": "U_ALEX", "name": "alex"},
                  "view": {**view, "id": "V1", "state": {"values": state}}}
    r = post_slack(client, submission)
    assert r.status_code == 200 and r.content == b""
    [task] = queue.tasks
    assert task["payload"]["change"] == {"action": "reword", "target": "B-001",
                                         "statement": "Video is the best evidence", "requested_by": "alex"}
    assert "Queued by alex" in json.dumps(slack.responses[-1]["payload"]["blocks"])


def test_anyone_may_click_on_the_repo_free_path(repo_free):
    client, slack, queue = repo_free
    payload = slack_click("pressure_keep")
    payload["user"] = {"id": "U0GUEST", "name": "maria"}
    assert post_slack(client, payload).status_code == 200
    [task] = queue.tasks
    assert task["payload"]["change"]["requested_by"] == "maria"
    assert slack.ephemerals == []


def post_event(client: TestClient, data: dict, secret: str = SLACK_SECRET):
    body = json.dumps(data).encode()
    headers = {**slack_headers(body, secret), "content-type": "application/json"}
    return client.post("/slack/events", content=body, headers=headers)


def mention(text: str, *, event_id: str = "Ev1", thread_ts: str | None = None, bot: bool = False) -> dict:
    event = {"type": "app_mention", "user": "U_ALEX", "text": text, "ts": "1790000000.000100",
             "channel": "C_CALLS"}
    if thread_ts:
        event["thread_ts"] = thread_ts
    if bot:
        event["bot_id"] = "B1"
    return {"type": "event_callback", "event_id": event_id, "event": event}


def test_slack_events_answer_the_url_challenge_and_check_the_signature(repo_free):
    client, _, _ = repo_free
    r = post_event(client, {"type": "url_verification", "challenge": "abc"})
    assert r.status_code == 200 and r.json() == {"challenge": "abc"}
    assert post_event(client, {"type": "url_verification", "challenge": "abc"}, secret="wrong").status_code == 401


def test_a_mention_queues_an_ask_without_the_repo(repo_free):
    client, slack, queue = repo_free
    r = post_event(client, mention('<@U0BOT> what do adjusters say about video? since:2026-09-01 seat:"claims adjuster (staff)"'))
    assert r.status_code == 200
    [task] = queue.tasks
    assert task["kind"] == "ask"
    assert task["payload"] == {"channel": "C_CALLS", "thread_ts": "1790000000.000100", "ts": "1790000000.000100",
                               "question": "what do adjusters say about video?", "since": "2026-09-01",
                               "seat": "claims adjuster (staff)", "user": "U_ALEX"}
    assert slack.messages == []          # the processor routes first, then says "On it" for a real question


def test_a_mention_in_a_thread_replies_in_that_thread(repo_free):
    client, slack, queue = repo_free
    post_event(client, mention("<@U0BOT> which seats?", thread_ts="1789999999.000001"))
    payload = queue.tasks[0]["payload"]
    assert payload["thread_ts"] == "1789999999.000001" and payload["ts"] == "1790000000.000100"


def test_slack_resending_an_event_does_not_ask_twice(repo_free):
    client, slack, queue = repo_free
    post_event(client, mention("<@U0BOT> which seats?"))
    post_event(client, mention("<@U0BOT> which seats?"))
    assert len(queue.tasks) == 1


def test_an_empty_mention_gets_help_and_queues_nothing(repo_free):
    client, slack, queue = repo_free
    post_event(client, mention("<@U0BOT>"))
    assert queue.tasks == [] and "Mention me with a question" in slack.messages[0]["text"]


def test_bot_messages_and_other_events_are_ignored(repo_free):
    client, slack, queue = repo_free
    post_event(client, mention("<@U0BOT> hi", bot=True))
    post_event(client, {"type": "event_callback", "event_id": "Ev2", "event": {"type": "message", "text": "x"}})
    assert queue.tasks == [] and slack.messages == []


def test_scheduler_routes_enqueue_without_the_repo(repo_free, monkeypatch):
    client, _, queue = repo_free
    monkeypatch.setenv("NUA_DEV", "1")
    assert client.post("/sweep", json={}).status_code == 200
    assert client.post("/rollup", json={}).status_code == 200
    assert [t["kind"] for t in queue.tasks] == ["sweep", "rollup"]


def test_importing_the_receiver_stays_light():
    """Nothing heavy loads with the app: no model SDK and no git module, so a cold start is quick."""
    import subprocess
    import sys

    code = ("import sys, service.main\n"
            "heavy = ('anthropic', 'openai', 'google.genai', 'nua.gitops', 'nua.providers', 'google.cloud.tasks_v2')\n"
            "print(','.join(m for m in heavy if m in sys.modules))\n")
    env = {k: v for k, v in os.environ.items() if k != "SERVICE_ROLE"}
    env["SERVICE_ROLE"] = "receiver"
    out = subprocess.run([sys.executable, "-c", code], cwd=Path(main.__file__).parents[1], env=env,
                         capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == ""


# ---------- /sweep and /rollup ----------

def test_scheduler_routes_need_a_token(sctx):
    client = receiver(sctx)
    assert client.post("/sweep", json={}).status_code == 401
    assert client.post("/rollup", json={}).status_code == 401
    assert sctx.queue.tasks == []


def test_scheduler_routes_skip_the_check_in_dev(sctx, monkeypatch):
    monkeypatch.setenv("NUA_DEV", "1")
    client = receiver(sctx)
    assert client.post("/sweep", json={"granola_only": True}).status_code == 200
    assert client.post("/rollup", json={"bootstrap": True}).status_code == 200
    assert client.post("/sweep").status_code == 200
    assert [(t["kind"], t["payload"]) for t in sctx.queue.tasks] == [
        ("sweep", {"granola_only": True}), ("rollup", {"bootstrap": True}), ("sweep", {"granola_only": False})]


def test_scheduler_oidc_token_checked_with_google_auth(sctx, monkeypatch):
    from google.oauth2 import id_token

    monkeypatch.setenv("RECEIVER_URL", "https://receiver.example.run.app")
    monkeypatch.setenv("SCHEDULER_SERVICE_ACCOUNT", "nua-scheduler@proj.iam.gserviceaccount.com")
    seen = []

    def verify(token, request, audience=None, **kw):
        seen.append(token)
        if token != "good" and token != "wrong-sa":
            raise ValueError("bad token")
        email = "nua-scheduler@proj.iam.gserviceaccount.com" if token == "good" else "someone@else.com"
        return {"aud": "https://receiver.example.run.app", "email": email, "email_verified": True}

    monkeypatch.setattr(id_token, "verify_oauth2_token", verify)
    client = receiver(sctx)
    assert client.post("/sweep", json={}, headers={"Authorization": "Bearer good"}).status_code == 200
    assert client.post("/sweep", json={}, headers={"Authorization": "Bearer forged"}).status_code == 401
    assert client.post("/rollup", json={}, headers={"Authorization": "Bearer wrong-sa"}).status_code == 401
    assert [t["kind"] for t in sctx.queue.tasks] == ["sweep"]
    assert seen == ["good", "forged", "wrong-sa"]


def test_health(sctx):
    assert receiver(sctx).get("/healthz").json() == {"ok": True}


# ---------- processor ----------

def test_processor_passes_retry_count_and_returns_the_result(sctx, monkeypatch):
    seen = []

    def run_task(ctx, kind, payload, *, attempt=0, max_attempts=5, **kw):
        seen.append((kind, payload, attempt, max_attempts))
        return {"ok": True, "kind": kind, "result": {}}

    monkeypatch.setattr(runner, "run_task", run_task)
    client = TestClient(main.create_processor_app(lambda: sctx))
    r = client.post("/task/process_call", json={"transcript": "transcripts/a.md"},
                    headers={"X-CloudTasks-TaskRetryCount": "2"})
    assert r.status_code == 200 and r.json()["ok"] is True
    assert seen == [("process_call", {"transcript": "transcripts/a.md"}, 2, 5)]


def test_processor_failure_is_500_until_the_final_attempt(sctx, monkeypatch):
    def run_task(ctx, kind, payload, *, attempt=0, max_attempts=5, **kw):
        if attempt < max_attempts - 1:
            raise RuntimeError("try again")
        return {"ok": False, "kind": kind, "error": "gave up", "final": True}

    monkeypatch.setattr(runner, "run_task", run_task)
    client = TestClient(main.create_processor_app(lambda: sctx), raise_server_exceptions=False)
    assert client.post("/task/sweep", json={}).status_code == 500
    r = client.post("/task/sweep", json={}, headers={"X-CloudTasks-TaskRetryCount": "4"})
    assert r.status_code == 200 and r.json()["ok"] is False


@pytest.mark.parametrize("transcript, button", [("transcripts/a.md", True), ("transcripts/" + "a" * 2100 + ".md", False)])
def test_final_attempt_without_a_repo_alerts_ops_and_fits_the_rerun_button(monkeypatch, transcript, button):
    import nua.slackio

    posts = []

    class Web:
        def __init__(self, token):
            assert token == "xoxb-test"

        def post(self, channel, blocks, text, **kw):
            posts.append({"channel": channel, "blocks": blocks, "text": text})
            return "1.0"

    def no_repo():
        raise RuntimeError("GitHub is down")

    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-test")
    monkeypatch.delenv("NUA_OPS_CHANNEL", raising=False)
    monkeypatch.delenv("NUA_SLACK_TEST", raising=False)
    monkeypatch.setattr(nua.slackio, "SlackWebIO", Web)
    client = TestClient(main.create_processor_app(no_repo), raise_server_exceptions=False)
    r = client.post("/task/process_call", json={"transcript": transcript}, headers={"X-CloudTasks-TaskRetryCount": "4"})
    assert r.status_code == 200 and r.json()["final"] is True
    [post] = posts
    assert post["channel"] == "#discovery-ops" and "Could not load the repo" in post["text"]
    actions = [b for b in post["blocks"] if b.get("type") == "actions"]
    if button:
        [value] = [e["value"] for b in actions for e in b["elements"]]
        assert json.loads(value) == {"kind": "process_call", "payload": {"transcript": transcript, "force": True}}
    else:
        assert actions == []     # too big for a button; an empty payload would re-run as an invalid task


@pytest.mark.parametrize("ops_channel", [None, "#discovery-ops-prod"])
def test_final_attempt_without_a_repo_goes_to_the_test_channel_in_slack_test_mode(monkeypatch, ops_channel):
    # NUA_SLACK_TEST=1 sends every Slack post to #discovery-test, this one included (staging must never
    # reach the production ops channel).
    import nua.slackio

    posts = []

    class Web:
        def __init__(self, token):
            pass

        def post(self, channel, blocks, text, **kw):
            posts.append(channel)
            return "1.0"

    def no_repo():
        raise RuntimeError("GitHub is down")

    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-test")
    monkeypatch.setenv("NUA_SLACK_TEST", "1")
    if ops_channel:
        monkeypatch.setenv("NUA_OPS_CHANNEL", ops_channel)
    else:
        monkeypatch.delenv("NUA_OPS_CHANNEL", raising=False)
    monkeypatch.setattr(nua.slackio, "SlackWebIO", Web)
    client = TestClient(main.create_processor_app(no_repo), raise_server_exceptions=False)
    client.post("/task/process_call", json={"transcript": "transcripts/a.md"}, headers={"X-CloudTasks-TaskRetryCount": "4"})
    assert posts == ["#discovery-test"]


def test_processor_runs_the_real_runner(sctx, monkeypatch):
    import sys
    import types

    import nua

    mod = types.ModuleType("nua.backcheck")
    mod.backcheck = lambda ctx, payload=None: {"entries": ["B-001"], "digests_updated": 3}
    monkeypatch.setitem(sys.modules, "nua.backcheck", mod)
    monkeypatch.setattr(nua, "backcheck", mod, raising=False)
    client = TestClient(main.create_processor_app(lambda: sctx))
    r = client.post("/task/backcheck", content=b"")
    assert r.json() == {"ok": True, "kind": "backcheck", "result": {"entries": ["B-001"], "digests_updated": 3}}


def test_create_app_picks_the_role(monkeypatch):
    monkeypatch.setenv("SERVICE_ROLE", "processor")
    paths = {r.path for r in main.create_app().routes}
    assert "/task/{kind}" in paths and "/ingest/granola" not in paths
    monkeypatch.setenv("SERVICE_ROLE", "receiver")
    paths = {r.path for r in main.create_app().routes}
    assert "/ingest/granola" in paths and "/task/{kind}" not in paths


# ---------- settings ----------

def test_build_ctx_local_reads_the_repo_and_runs_tasks_inline(tmp_repo, monkeypatch):
    monkeypatch.setenv("NUA_REPO_ROOT", str(tmp_repo))
    ctx = settings.build_ctx("local")
    assert ctx.repo.root == tmp_repo
    assert isinstance(ctx.remote, NullRemote)
    assert isinstance(ctx.queue, InlineQueue)
    assert ctx.slack is None
    assert ctx.llms.cost.cap_usd == ctx.config.pipeline.cost_cap_usd_per_task


def test_build_ctx_rejects_unknown_roles_and_missing_settings(monkeypatch):
    with pytest.raises(ValueError):
        settings.build_ctx("worker")
    settings.reset()
    for name in ("REPO_SLUG", "GITHUB_APP_ID", "GITHUB_APP_INSTALLATION_ID", "GITHUB_APP_PRIVATE_KEY"):
        monkeypatch.delenv(name, raising=False)
    with pytest.raises(settings.SettingsError, match="REPO_SLUG"):
        settings.build_ctx("processor")


def test_receiver_pulls_the_clone_only_when_it_is_stale(tmp_repo, monkeypatch):
    """No background sync: a repo route pulls first when the clone is older than RECEIVER_SYNC_SECONDS.
    The processor pulls before every task."""
    from nua.ports import FakeSlack, RecordingQueue

    for name in ("REPO_SLUG", "GITHUB_APP_ID", "GITHUB_APP_INSTALLATION_ID", "GITHUB_APP_PRIVATE_KEY",
                 "GCP_PROJECT", "TASKS_LOCATION", "PROCESSOR_URL", "TASKS_SERVICE_ACCOUNT"):
        monkeypatch.setenv(name, "x")
    remote = NullRemote()
    syncs = []
    monkeypatch.setattr(remote, "sync", lambda: syncs.append(1))
    clock = [1000.0]
    monkeypatch.setattr(settings.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(settings, "_get_clients", lambda: settings._Clients(slack=FakeSlack(), queue=RecordingQueue()))
    repo = settings._Repo(root=tmp_repo, remote=remote, synced_at=1000.0)
    monkeypatch.setattr(settings, "_get_repo", lambda: repo)

    settings.build_ctx("receiver")
    assert syncs == []
    clock[0] += settings.RECEIVER_SYNC_SECONDS + 1
    ctx = settings.build_ctx("receiver")
    assert syncs == [1] and ctx.repo.root == tmp_repo
    settings.build_ctx("receiver")
    assert syncs == [1]
    settings.build_ctx("processor")
    assert syncs == [1, 1]
    import threading
    assert not any(t.name == "nua-repo-sync" for t in threading.enumerate())


def test_a_failed_pull_on_the_receiver_reads_the_last_good_tree(tmp_repo, monkeypatch):
    remote = NullRemote()

    def down():
        raise RuntimeError("github down")

    monkeypatch.setattr(remote, "sync", down)
    repo = settings._Repo(root=tmp_repo, remote=remote, synced_at=0.0)
    settings._sync(repo, max_age=settings.RECEIVER_SYNC_SECONDS)       # logged, not raised
    assert repo.synced_at == 0.0
    with pytest.raises(RuntimeError):
        settings._sync(repo)                                           # the processor's pull raises


def test_the_queue_is_built_on_the_first_enqueue_only(monkeypatch):
    """A click that only opens a form never loads Cloud Tasks (grpc)."""
    from nua.ports import RecordingQueue

    for name in ("GCP_PROJECT", "TASKS_LOCATION", "PROCESSOR_URL", "TASKS_SERVICE_ACCOUNT"):
        monkeypatch.setenv(name, "x")
    built = []

    def build():
        built.append(RecordingQueue())
        return built[-1]

    monkeypatch.setattr(settings, "_queue", build)
    settings.reset()
    try:
        ack = settings.ack_ctx()
        assert built == []
        assert ack.queue.enqueue("sweep", {}) is True
        ack.queue.enqueue("rollup", {})
        [queue] = built
        assert [t["kind"] for t in queue.tasks] == ["sweep", "rollup"]
    finally:
        settings.reset()


def test_slack_test_mode_sends_everything_to_the_test_channel(tmp_repo, monkeypatch):
    monkeypatch.setenv("NUA_REPO_ROOT", str(tmp_repo))
    monkeypatch.setenv("NUA_SLACK_TEST", "1")
    ctx = settings.build_ctx("local")
    assert ctx.channel("calls") == ctx.channel("ops") == ctx.channel("test") == "#discovery-test"


def test_recorder_clients_come_from_env(tmp_repo, monkeypatch):
    monkeypatch.delenv("GRANOLA_API_KEY", raising=False)
    assert settings.granola_client() is None
    monkeypatch.setenv("GRANOLA_API_KEY", "grn_x")
    assert isinstance(settings.granola_client(), GranolaClient)
    monkeypatch.setenv("CIRCLEBACK_API_KEY_ALEX", "cb_x")
    monkeypatch.delenv("CIRCLEBACK_API_KEY_SAM", raising=False)
    monkeypatch.delenv("CIRCLEBACK_API_KEY_JORDAN", raising=False)
    from nua import config
    clients = settings.circleback_clients(config.load(tmp_repo))
    assert list(clients) == ["alex"]


def test_github_signature_helper():
    body = b'{"a": 1}'
    good = "sha256=" + hmac.new(b"s", body, hashlib.sha256).hexdigest()
    assert main.verify_github_signature("s", body, good) is True
    assert main.verify_github_signature("s", body + b" ", good) is False
    assert main.verify_github_signature("", body, good) is False
    assert main.verify_github_signature("s", body, None) is False
    assert main.verify_github_signature("s", body, "sha256=\xff") is False      # latin-1 decoded forged header
    assert main.verify_github_signature("s", body, good[:-1] + "\u00e9") is False
