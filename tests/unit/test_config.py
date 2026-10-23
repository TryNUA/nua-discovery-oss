"""Tests for discovery/config.py, including how a date-only `ingest_cutoff` from config/pipeline.yml is read
by both of its readers, the receiver (service/main.py) and the nightly sweep (discovery/sweep.py). Offline."""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from datetime import date, datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from discovery import config as config_mod
from discovery.context import Ctx
from discovery.rules import as_datetime
from discovery.sweep import run_sweep
from service import main

GRANOLA_SECRET = "whsec_" + base64.b64encode(b"config-test-key").decode()
CB_SECRET = "config-test-circleback"
DARIUS = {"name": "Darius Kemp", "email": "darius@example.com"}
OWNER = {"name": "Alex Rivera", "email": "alex.rivera@gmail.com"}

# A date-only cutoff means midnight Pacific: 2026-10-01T00:00-07:00, which is 07:00 UTC.
BEFORE = "2026-10-01T05:00:00Z"     # 22:00 PT on Sep 30: before the cutoff, though after midnight UTC
AFTER = "2026-10-01T08:00:00Z"      # 01:00 PT on Oct 1: after the cutoff however it is read


def _set_cutoff_text(root: Path, value: str) -> None:
    """Write `ingest_cutoff: <value>` into config/pipeline.yml as literal YAML text, as Alex would."""
    path = root / "config" / "pipeline.yml"
    lines = [line for line in path.read_text(encoding="utf-8").splitlines() if not line.startswith("ingest_cutoff:")]
    path.write_text("\n".join([*lines, f"ingest_cutoff: {value}", ""]), encoding="utf-8")


# ---------- loading ----------

def test_date_only_cutoff_loads_as_a_date(tmp_repo):
    _set_cutoff_text(tmp_repo, "2026-10-01")
    cutoff = config_mod.load(tmp_repo).pipeline.ingest_cutoff
    assert isinstance(cutoff, date) and not isinstance(cutoff, datetime)
    assert cutoff == date(2026, 10, 1)


def test_unset_cutoff_loads_as_none(tmp_repo):
    assert config_mod.load(tmp_repo).pipeline.ingest_cutoff is None


@pytest.mark.parametrize("value", ["2026-11-31", "2026-02-30T09:00:00-08:00", "2026-11-02T25:00:00-08:00"])
def test_impossible_cutoff_loads_as_its_text_and_reads_as_unreadable(tmp_repo, value):
    # a date typo at go-live must not stop config/ from loading (every Ctx is built from it)
    _set_cutoff_text(tmp_repo, value)
    cutoff = config_mod.load(tmp_repo).pipeline.ingest_cutoff
    assert cutoff == value
    with pytest.raises(ValueError):
        as_datetime(cutoff)


def test_valid_datetime_cutoff_still_loads_as_a_datetime(tmp_repo):
    _set_cutoff_text(tmp_repo, "2026-11-02T09:00:00-08:00")
    cutoff = config_mod.load(tmp_repo).pipeline.ingest_cutoff
    assert isinstance(cutoff, datetime) and cutoff.utcoffset() is not None
    assert as_datetime(cutoff) == as_datetime("2026-11-02T17:00:00Z")


def test_symlinked_config_files_are_never_read(tmp_repo, tmp_path):
    # a committed link such as config/people.yml -> /proc/self/environ could put the service's
    # secrets into model prompts; config/ is read like any repo file, never through a link
    secret = tmp_path / "environ"
    secret.write_text("ANTHROPIC_API_KEY=sk-ant-xyz\n", encoding="utf-8")
    real = config_mod.load(tmp_repo)
    assert real.people and real.seats

    (tmp_repo / "config" / "people.yml").unlink()
    (tmp_repo / "config" / "people.yml").symlink_to(secret)
    (tmp_repo / "config" / "seats.yml").unlink()
    (tmp_repo / "config" / "seats.yml").symlink_to(secret)
    cfg = config_mod.load(tmp_repo)
    assert cfg.people == []
    assert cfg.seats == []


# ---------- the date-only cutoff at both call sites ----------

def _note(note_id: str, created_at: str) -> dict:
    return {
        "id": note_id, "title": "Darius / Alex", "created_at": created_at, "owner": OWNER,
        "calendar_event": None, "attendees": [DARIUS],
        "summary_markdown": "### Overview\nDarius talked about dashcam footage.",
        "transcript": [
            {"speaker": {"source": "microphone"}, "text": "Hi Darius, thanks for the time.", "start_time": created_at},
            {"speaker": {"source": "speaker"}, "text": "Happy to. We pull three dashcam clips.",
             "start_time": created_at},
        ],
    }


def _meeting(mid: int, created_at: str) -> dict:
    return {"id": mid, "name": "Darius / Alex", "createdAt": created_at, "tags": ["discovery"], "icalUid": None,
            "attendees": [DARIUS, OWNER], "notes": "#### Overview\nDarius pulls dashcam clips.",
            "transcript": [{"speaker": "Alex Rivera", "text": "Hi Darius.", "timestamp": 1.0},
                           {"speaker": "Darius Kemp", "text": "Hello, we pull three dashcam clips.", "timestamp": 4.5}]}


class _Granola:
    """GranolaClient stand-in for both readers. `list_notes` ignores `created_after`, so each reader must filter."""

    def __init__(self, notes: list[dict]):
        self.notes = {n["id"]: n for n in notes}

    def list_notes(self, *, created_after=None):
        for n in self.notes.values():
            yield {k: n[k] for k in ("id", "title", "owner", "created_at")}

    def get_note(self, note_id):
        return self.notes[note_id]


class _Circleback:
    def __init__(self, meetings: list[dict]):
        self.meetings = meetings

    def list_meetings(self, *, tag=None):
        yield from ({k: v for k, v in m.items() if k != "transcript"} for m in self.meetings)

    def get_transcript(self, meeting_id):
        return next(m["transcript"] for m in self.meetings if str(m["id"]) == str(meeting_id))

    def calendar_events(self, *, lookback_hours=24):
        return []


def _granola_headers(body: bytes) -> dict:
    msg_id, ts = "evt_1", str(int(time.time()))
    key = base64.b64decode(GRANOLA_SECRET[len("whsec_"):])
    sig = base64.b64encode(hmac.new(key, f"{msg_id}.{ts}.".encode() + body, hashlib.sha256).digest()).decode()
    return {"webhook-id": msg_id, "webhook-timestamp": ts, "webhook-signature": f"v1,{sig}",
            "content-type": "application/json"}


@pytest.fixture
def date_cutoff(tmp_repo, monkeypatch) -> Path:
    """The temp repo with `ingest_cutoff: 2026-10-01` in pipeline.yml and the receiver's secrets set."""
    _set_cutoff_text(tmp_repo, "2026-10-01")
    monkeypatch.setenv("GRANOLA_WEBHOOK_SECRET", GRANOLA_SECRET)
    monkeypatch.setenv("CIRCLEBACK_SIGNING_SECRET_ALEX", CB_SECRET)
    return tmp_repo


def test_receiver_reads_a_date_only_cutoff_as_midnight_pacific(date_cutoff):
    ctx = Ctx.for_tests(date_cutoff)
    assert type(ctx.config.pipeline.ingest_cutoff) is date
    notes = [_note("not_before", BEFORE), _note("not_after", AFTER)]
    meetings = [_meeting(1001, BEFORE), _meeting(1002, AFTER)]
    client = TestClient(main.create_receiver_app(lambda: ctx, granola=lambda: _Granola(notes),
                                                 circleback=lambda person: _Circleback(meetings)))

    def granola(note_id: str) -> dict:
        body = json.dumps({"event_id": f"evt_{note_id}", "event_type": "note.generated",
                           "note_id": note_id}).encode()
        r = client.post("/ingest/granola", content=body, headers=_granola_headers(body))
        assert r.status_code == 200
        return r.json()

    def circleback(meeting: dict) -> dict:
        body = json.dumps(meeting).encode()
        sig = hmac.new(CB_SECRET.encode(), body, hashlib.sha256).hexdigest()
        r = client.post("/ingest/circleback/alex", content=body, headers={"x-signature": sig})
        assert r.status_code == 200
        return r.json()

    # 22:00 PT on Sep 30 is before midnight Pacific, though it is after midnight UTC
    assert granola("not_before").get("ignored") == "call before ingest_cutoff"
    assert circleback(meetings[0]).get("ignored") == "created before ingest_cutoff"
    assert ctx.remote.created == [] and ctx.queue.tasks == []

    # 01:00 PT on Oct 1 is after the cutoff
    assert "ignored" not in granola("not_after")
    assert "ignored" not in circleback(meetings[1])
    assert ctx.remote.created == ["transcripts/granola_not_after.md", "transcripts/circleback_1002.md"]


def test_sweep_reads_a_date_only_cutoff_as_midnight_pacific(date_cutoff):
    ctx = Ctx.for_tests(date_cutoff)
    assert type(ctx.config.pipeline.ingest_cutoff) is date
    granola = _Granola([_note("not_before", BEFORE), _note("not_after", AFTER)])
    circleback = _Circleback([_meeting(1001, BEFORE), _meeting(1002, AFTER)])

    report = run_sweep(ctx, granola=granola, circleback={"alex": circleback})

    assert sorted(report.ingested) == ["transcripts/circleback_1002.md", "transcripts/granola_not_after.md"]
    assert not ctx.repo.exists("transcripts/granola_not_before.md")
    assert not ctx.repo.exists("transcripts/circleback_1001.md")
    assert not [e for e in report.errors if "cutoff" in e]


# ---------- an unreadable cutoff at both call sites ----------

@pytest.fixture(params=["2026-11-31", "go-live"])
def bad_cutoff(request, tmp_repo, monkeypatch) -> Path:
    """The temp repo with an unreadable `ingest_cutoff` written as literal YAML text."""
    _set_cutoff_text(tmp_repo, request.param)
    monkeypatch.setenv("GRANOLA_WEBHOOK_SECRET", GRANOLA_SECRET)
    monkeypatch.setenv("CIRCLEBACK_SIGNING_SECRET_ALEX", CB_SECRET)
    return tmp_repo


def test_receiver_acknowledges_and_ignores_recorder_items_with_an_unreadable_cutoff(bad_cutoff):
    ctx = Ctx.for_tests(bad_cutoff)
    notes, meetings = [_note("not_after", AFTER)], [_meeting(1002, AFTER)]
    client = TestClient(main.create_receiver_app(lambda: ctx, granola=lambda: _Granola(notes),
                                                 circleback=lambda person: _Circleback(meetings)))
    body = json.dumps({"event_id": "evt_1", "event_type": "note.generated", "note_id": "not_after"}).encode()
    g = client.post("/ingest/granola", content=body, headers=_granola_headers(body))
    body = json.dumps(meetings[0]).encode()
    sig = hmac.new(CB_SECRET.encode(), body, hashlib.sha256).hexdigest()
    c = client.post("/ingest/circleback/alex", content=body, headers={"x-signature": sig})
    for r in (g, c):
        assert r.status_code == 200 and r.json()["ignored"] == "ingest_cutoff not set"
    assert ctx.remote.created == [] and ctx.queue.tasks == []


def test_sweep_skips_recorder_steps_with_an_unreadable_cutoff(bad_cutoff):
    ctx = Ctx.for_tests(bad_cutoff)
    report = run_sweep(ctx, granola=_Granola([_note("not_after", AFTER)]),
                       circleback={"alex": _Circleback([_meeting(1002, AFTER)])})
    assert report.ingested == []
    assert any(str(ctx.config.pipeline.ingest_cutoff) in e for e in report.errors)
