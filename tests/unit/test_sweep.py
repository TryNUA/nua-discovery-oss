"""Tests for nua/sweep.py: the nightly sweep with fake recorder clients, a RecordingQueue and FakeSlack.

nua/process.py (Task 12) is stubbed through sys.modules, so these tests never depend on it.
"""
from __future__ import annotations

import sys
import types

import pytest
import yaml

from nua import frontmatter
from nua.digest import read_digest, write_digest
from nua.ingest.circleback import has_tag
from nua.ports import NullRemote
from nua.sweep import SweepReport, run_sweep
from nua.types import BriefRef, Digest

CUTOFF = "2026-09-01T00:00:00-07:00"
OWNER = {"name": "Alex Rivera", "email": "alex.rivera@gmail.com"}
DARIUS = {"name": "Darius Kemp", "email": "darius@examplemutual.com"}


# ---------- fakes ----------

class FakeGranola:
    """GranolaClient stand-in. `list_notes` ignores `created_after`, so the sweep must filter too."""

    def __init__(self, notes: list[dict], *, fail_list: bool = False):
        self.notes = {n["id"]: n for n in notes}
        self.list_calls: list[str | None] = []
        self.fetched: list[str] = []
        self.fail_list = fail_list

    def list_notes(self, *, created_after=None):
        self.list_calls.append(created_after)
        if self.fail_list:
            raise RuntimeError("granola is down")
        for n in self.notes.values():
            yield {k: n[k] for k in ("id", "title", "owner", "created_at", "updated_at") if k in n}

    def get_note(self, note_id):
        self.fetched.append(note_id)
        return self.notes[note_id]


class FakeCircleback:
    def __init__(self, meetings: list[dict], *, events: list[dict] | None = None):
        self.meetings = meetings
        self.events = events or []
        self.list_calls: list[str | None] = []
        self.transcripts: list[str] = []
        self.event_calls: list[int] = []

    def list_meetings(self, *, tag=None):
        self.list_calls.append(tag)
        for m in self.meetings:
            if tag is None or has_tag(m, tag):
                yield m

    def get_transcript(self, meeting_id):
        self.transcripts.append(meeting_id)
        return [{"speaker": "Alex Rivera", "text": "Thanks for the time.", "timestamp": 1.0},
                {"speaker": "Darius Kemp", "text": "Happy to help.", "timestamp": 4.0}]

    def calendar_events(self, *, lookback_hours=24):
        self.event_calls.append(lookback_hours)
        return self.events


class ExistingRemote(NullRemote):
    """A remote where every file already exists."""

    def create_file_only(self, rel, content, message):
        return False


@pytest.fixture
def proc(monkeypatch):
    """A stand-in for nua.process with post_brief and batch_completion_check."""
    mod = types.ModuleType("nua.process")
    mod.briefed = []
    mod.checked = []

    def post_brief(ctx, d):
        mod.briefed.append(d.call_id)
        channel = ctx.channel("calls")
        ts = ctx.slack.post(channel, [], f"brief for {d.call_id}",
                            metadata={"event_type": "nua_brief", "event_payload": {"key": d.transcript}})
        return BriefRef(channel=channel, ts=ts, thread=[])

    def batch_completion_check(ctx, batch_id):
        mod.checked.append(batch_id)
        return False

    mod.post_brief = post_brief
    mod.batch_completion_check = batch_completion_check
    monkeypatch.setitem(sys.modules, "nua.process", mod)
    return mod


@pytest.fixture
def live(ctx):
    """The test ctx with the recorders live (an ingest cutoff set)."""
    ctx.config.pipeline.ingest_cutoff = CUTOFF
    return ctx


# ---------- builders ----------

def _item(text: str, at: str, **speaker) -> dict:
    return {"speaker": speaker, "text": text, "start_time": at, "end_time": at}


def _note(note_id: str, created_at: str, attendees: list[dict] | None = None) -> dict:
    return {
        "id": note_id, "title": "Acme x Example Mutual", "owner": OWNER, "created_at": created_at,
        "calendar_event": None, "attendees": attendees if attendees is not None else [DARIUS],
        "summary_markdown": "### Overview\n\n- Darius handles forty open claims",
        "transcript": [
            _item("Thanks for taking the time.", "2026-09-30T20:00:00Z", source="microphone", attribution="me"),
            _item("Of course.", "2026-09-30T20:00:04Z", source="speaker", attribution="them"),
        ],
    }


def _meeting(mid, created_at: str, *, tags=("discovery",), attendees: list[dict] | None = None,
             name: str = "Acme x Example Mutual") -> dict:
    return {"id": mid, "name": name, "createdAt": created_at,
            "tags": [{"id": i, "name": t} for i, t in enumerate(tags)],
            "attendees": attendees if attendees is not None else [DARIUS, OWNER],
            "notes": "#### Overview\n* Darius walked through store footage requests", "icalUid": None}


def _write(ctx, rel: str, text: str) -> None:
    ctx.repo.write(rel, text)


def _transcript(ctx, rel: str, **fm) -> None:
    body = "## Transcript\n\n[0:01] **Darius Kemp**: Hello.\n"
    _write(ctx, rel, frontmatter.join(fm, body) if fm else body)


def _digest(ctx, call_id: str, transcript: str, **kw) -> Digest:
    d = Digest(call_id=call_id, transcript=transcript, status=kw.pop("status", "digested"),
               origin=kw.pop("origin", "live"), prompt_version=kw.pop("prompt_version", "v1"), **kw)
    write_digest(ctx.repo, d)
    return d


def _ops(ctx) -> list[dict]:
    return ctx.slack.live(ctx.channel("ops"))


def _no_em_dash(ctx) -> None:
    for m in ctx.slack.messages:
        assert "\u2014" not in m["text"]
        assert "\u2014" not in str(m["blocks"])


# ---------- report ----------

def test_report_fields(ctx, proc):
    report = run_sweep(ctx)
    assert isinstance(report, SweepReport)
    for name in ("ingested", "enqueued", "rebriefed", "requests", "reminders", "stale", "batches_checked",
                 "errors"):
        assert getattr(report, name) == []
    assert ctx.slack.messages == []


def test_needs_a_queue(ctx, proc):
    ctx.queue = None
    with pytest.raises(RuntimeError):
        run_sweep(ctx)


# ---------- step 1: recorders ----------

def test_granola_missing_note_ingested_and_enqueued(live, proc):
    _transcript(live, "transcripts/granola_not_old.md", source="granola", source_id="not_old")
    _digest(live, "2026-09-20_old-call", "transcripts/granola_not_old.md", date="2026-09-20")
    granola = FakeGranola([_note("not_old", "2026-09-20T17:00:00Z"), _note("not_new", "2026-09-30T20:00:00Z")])

    report = run_sweep(live, granola=granola)

    rel = "transcripts/granola_not_new.md"
    assert granola.fetched == ["not_new"]
    assert report.ingested == [rel]
    assert report.enqueued == [rel]
    assert live.remote.created == [rel]
    fm, _, _ = frontmatter.split(live.repo.read(rel))
    assert fm["source"] == "granola" and fm["source_id"] == "not_new"
    assert fm["ingested_at"] == "2026-10-01T09:00:00-07:00"          # ctx.now(), Pacific: the duplicate rule's arrival order
    calls = live.queue.of("process_call")
    assert [t["payload"] for t in calls] == [{"transcript": rel}]
    assert calls[0]["delay_seconds"] == 0
    # the webhook missed it, so ops hears about it
    ops = _ops(live)
    assert len(ops) == 1 and rel in ops[0]["text"]
    _no_em_dash(live)


def test_cutoff_respected(live, proc):
    granola = FakeGranola([_note("not_before", "2026-08-15T17:00:00Z"), _note("not_after", "2026-09-30T20:00:00Z")])
    cb = FakeCircleback([_meeting("m_before", "2026-08-20T17:00:00Z"), _meeting("m_after", "2026-09-30T20:00:00Z")])

    report = run_sweep(live, granola=granola, circleback={"sam": cb})

    assert granola.list_calls == ["2026-08-02T00:00:00-07:00"]       # PRECREATED_LOOKBACK_DAYS before it
    assert granola.fetched == ["not_after"]
    assert cb.transcripts == ["m_after"]
    assert sorted(report.ingested) == ["transcripts/circleback_m_after.md", "transcripts/granola_not_after.md"]


def test_a_note_created_ahead_of_a_call_after_the_cutoff_is_ingested(live, proc):
    """Granola pre-creates notes for opened meetings: created before the cutoff, used (updated) after it."""
    ahead = {**_note("not_ahead", "2026-08-30T17:00:00Z"), "updated_at": "2026-09-02T18:00:00Z",
             "calendar_event": {"scheduled_start_time": "2026-09-02T17:00:00Z", "event_title": "Acme x Example"}}
    old_edit = {**_note("not_old", "2026-08-25T17:00:00Z"), "updated_at": "2026-09-03T18:00:00Z",
                "calendar_event": {"scheduled_start_time": "2026-08-25T17:00:00Z", "event_title": "Old call"}}
    stale = {**_note("not_stale", "2026-08-20T17:00:00Z"), "updated_at": "2026-08-20T18:00:00Z"}
    granola = FakeGranola([ahead, old_edit, stale])
    report = run_sweep(live, granola=granola, circleback=None)
    assert granola.fetched == ["not_ahead", "not_old"]     # never the note untouched since before the cutoff
    assert report.ingested == ["transcripts/granola_not_ahead.md"]


def test_cutoff_from_yaml_datetime(live, proc):
    live.config.pipeline.ingest_cutoff = yaml.safe_load(f"c: {CUTOFF}")["c"]
    granola = FakeGranola([_note("not_before", "2026-08-15T17:00:00Z"), _note("not_after", "2026-09-30T20:00:00Z")])
    report = run_sweep(live, granola=granola)
    assert report.ingested == ["transcripts/granola_not_after.md"]


def test_no_cutoff_means_recorders_not_live(ctx, proc):
    assert ctx.config.pipeline.ingest_cutoff is None
    granola = FakeGranola([_note("not_new", "2026-09-30T20:00:00Z")])
    cb = FakeCircleback([_meeting("m1", "2026-09-30T20:00:00Z", tags=())])
    report = run_sweep(ctx, granola=granola, circleback={"sam": cb})
    assert granola.list_calls == [] and cb.list_calls == []
    assert report.ingested == [] and report.reminders == []


def test_circleback_missing_meeting_ingested(live, proc):
    _transcript(live, "transcripts/circleback_12345.md", source="circleback", source_id=12345)
    _digest(live, "2026-09-20_old-call", "transcripts/circleback_12345.md", date="2026-09-20")
    events = [{"meetingId": "m_new", "startTime": "2026-09-30T19:55:00Z"}]
    cb = FakeCircleback([_meeting(12345, "2026-09-20T17:00:00Z"), _meeting("m_new", "2026-09-30T20:00:00Z"),
                         _meeting("m_untagged", "2026-09-30T20:00:00Z", tags=("sales",))], events=events)

    report = run_sweep(live, circleback={"sam": cb})

    rel = "transcripts/circleback_m_new.md"
    assert cb.transcripts == ["m_new"]
    assert report.ingested == [rel]
    assert len(cb.event_calls) == 1 and cb.event_calls[0] >= 24
    fm, _, _ = frontmatter.split(live.repo.read(rel))
    assert fm["source"] == "circleback" and fm["source_id"] == "m_new"
    assert fm["recorded_by"] == "sam"
    assert fm["start"] == "2026-09-30T12:55:00-07:00" and fm["start_source"] == "calendar"
    assert fm["ingested_at"] == "2026-10-01T09:00:00-07:00"
    assert [t["payload"] for t in live.queue.of("process_call")] == [{"transcript": rel}]


def test_circleback_without_calendar_event_falls_back_to_created(live, proc):
    cb = FakeCircleback([_meeting("m_new", "2026-09-30T20:00:00Z")], events=[])
    run_sweep(live, circleback={"alex": cb})
    fm, _, _ = frontmatter.split(live.repo.read("transcripts/circleback_m_new.md"))
    assert fm["start_source"] == "created" and fm["recorded_by"] == "alex"


def test_file_already_on_remote_still_enqueues(live, proc):
    live.remote = ExistingRemote()
    granola = FakeGranola([_note("not_new", "2026-09-30T20:00:00Z")])
    report = run_sweep(live, granola=granola)
    rel = "transcripts/granola_not_new.md"
    assert report.ingested == []
    assert [t["payload"] for t in live.queue.of("process_call")] == [{"transcript": rel}]


def test_cofounder_invited_delays_processing(live, proc):
    sam = live.config.person("sam")
    sam.email = "sam@acme.dev"
    granola = FakeGranola([_note("not_new", "2026-09-30T20:00:00Z",
                                 attendees=[DARIUS, {"name": "Sam", "email": "sam@acme.dev"}])])
    run_sweep(live, granola=granola)
    (task,) = live.queue.of("process_call")
    assert task["delay_seconds"] == 15 * 60


def test_granola_only_runs_step_one_for_granola(live, proc):
    _transcript(live, "transcripts/dropped.md")
    _write(live, "requests/2026-10-01_sam_x.yml", "action: add\n")
    granola = FakeGranola([_note("not_new", "2026-09-30T20:00:00Z")])
    cb = FakeCircleback([_meeting("m_new", "2026-09-30T20:00:00Z")])

    report = run_sweep(live, granola=granola, circleback={"sam": cb}, granola_only=True)

    assert report.ingested == ["transcripts/granola_not_new.md"]
    assert cb.list_calls == []
    assert [t["kind"] for t in live.queue.tasks] == ["process_call"]
    assert report.requests == [] and proc.checked == []


def test_recorder_failure_does_not_stop_other_steps(live, proc):
    _write(live, "requests/2026-10-01_sam_x.yml", "action: add\n")
    report = run_sweep(live, granola=FakeGranola([], fail_list=True))
    assert report.requests == ["requests/2026-10-01_sam_x.yml"]
    assert report.errors and "granola" in report.errors[0].lower()
    ops = _ops(live)
    assert len(ops) == 1 and "granola is down" in ops[0]["text"]
    _no_em_dash(live)


def test_dry_run_writes_locally_but_skips_remote_and_slack(live, proc):
    live.dry_run = True
    granola = FakeGranola([_note("not_new", "2026-09-30T20:00:00Z")])
    report = run_sweep(live, granola=granola)
    rel = "transcripts/granola_not_new.md"
    assert report.ingested == [rel]
    assert live.remote.created == []
    assert live.repo.exists(rel)
    assert live.slack.messages == []


# ---------- step 2: transcripts ----------

def test_transcripts_without_digest_enqueued(ctx, proc):
    for name in ("a", "b", "c", "d", "e"):
        _transcript(ctx, f"transcripts/{name}.md")
    _write(ctx, "transcripts/.gitkeep", "")
    _digest(ctx, "2026-09-18_darius-kemp", "transcripts/a.md", duplicates=["transcripts/b.md"])
    _digest(ctx, "c", "transcripts/c.md", status="failed", reason="boom", prompt_version=None)
    _write(ctx, "config/batches/abc123.yml", yaml.safe_dump({"paths": ["transcripts/e.md", "transcripts/c.md"]}))

    report = run_sweep(ctx)

    assert report.enqueued == ["transcripts/d.md", "transcripts/e.md"]
    assert [t["payload"] for t in ctx.queue.of("process_call")] == [
        {"transcript": "transcripts/d.md"}, {"transcript": "transcripts/e.md", "batch": "abc123"}]
    ops = _ops(ctx)
    assert len(ops) == 1 and "transcripts/d.md" in ops[0]["text"]


def test_dropped_filenames_with_an_em_dash_never_reach_ops(ctx, proc):
    # Dropped files keep their original names, and AI meeting titles often carry an em dash.
    _transcript(ctx, "transcripts/Claims footage review \u2014 Lena.md")
    _write(ctx, "requests/2026-10-06_sam_footage \u2014 retention.yml", "action: add\n")

    run_sweep(ctx)

    [m] = _ops(ctx)
    assert "`transcripts/Claims footage review - Lena.md`" in m["text"]
    assert "`requests/2026-10-06_sam_footage - retention.yml`" in m["text"]
    _no_em_dash(ctx)


def test_nothing_missed_posts_nothing(ctx, proc):
    _transcript(ctx, "transcripts/a.md")
    _digest(ctx, "2026-09-18_darius-kemp", "transcripts/a.md", date="2026-09-18")
    report = run_sweep(ctx)
    assert report.enqueued == []
    assert ctx.slack.messages == []


# ---------- step 3: briefs ----------

def test_rebrief_recent_live_call_without_brief(ctx, proc):
    _digest(ctx, "2026-09-30_darius-kemp", "transcripts/g.md", date="2026-09-30")
    _digest(ctx, "2026-09-30_backfill", "transcripts/b.md", date="2026-09-30", origin="backfill")
    _digest(ctx, "2026-09-20_old", "transcripts/o.md", date="2026-09-20")
    _digest(ctx, "2026-10-01_has-brief", "transcripts/h.md", date="2026-10-01",
            brief=BriefRef(channel="#discovery-calls", ts="1.2", thread=[]))
    _digest(ctx, "internal_stub", "transcripts/i.md", date="2026-10-01", status="internal", reason="internal",
            prompt_version=None)
    _digest(ctx, "undated_someone", "transcripts/u.md")

    report = run_sweep(ctx)

    assert proc.briefed == ["2026-09-30_darius-kemp"]
    assert report.rebriefed == ["2026-09-30_darius-kemp"]
    d = read_digest(ctx.repo, "digests/2026-09-30_darius-kemp.md")
    assert d.brief is not None and d.brief.ts and d.brief.channel == "#discovery-calls"
    assert ctx.remote.commits and ctx.remote.commits[-1]["paths"] == ["digests/2026-09-30_darius-kemp.md"]
    ops = _ops(ctx)
    assert len(ops) == 1 and "2026-09-30_darius-kemp" in ops[0]["text"]


def test_rebrief_late_supersede_outside_the_window(ctx, proc):
    # An old call superseded by a richer copy whose brief update failed: the digest records no brief and
    # lists the live copy that held the brief. The sweep re-posts it whatever the call's age.
    _transcript(ctx, "transcripts/granola_abc.md", source="granola", source_id="abc")
    _transcript(ctx, "transcripts/dropped.md")
    _transcript(ctx, "transcripts/old_backfill.md")
    _digest(ctx, "2026-08-01_darius-kemp", "transcripts/circleback_1.md", date="2026-08-01",
            duplicates=["transcripts/granola_abc.md"])
    _digest(ctx, "2026-08-02_backfill-copy", "transcripts/circleback_2.md", date="2026-08-02",
            duplicates=["transcripts/dropped.md"])                 # its only copy is a dropped file
    _digest(ctx, "2026-08-03_copy-gone", "transcripts/circleback_3.md", date="2026-08-03",
            duplicates=["transcripts/granola_gone.md"])            # the copy's file is not in the repo
    _digest(ctx, "2026-08-04_backfill", "transcripts/old_backfill.md", date="2026-08-04", origin="backfill",
            duplicates=["transcripts/granola_abc.md"])             # backfill calls are never briefed

    report = run_sweep(ctx)

    assert proc.briefed == ["2026-08-01_darius-kemp"]
    assert report.rebriefed == ["2026-08-01_darius-kemp"]
    assert read_digest(ctx.repo, "digests/2026-08-01_darius-kemp.md").brief.ts
    assert ctx.remote.commits[-1]["paths"] == ["digests/2026-08-01_darius-kemp.md"]


def test_rebrief_skipped_in_dry_run(ctx, proc):
    ctx.dry_run = True
    _digest(ctx, "2026-09-30_darius-kemp", "transcripts/g.md", date="2026-09-30")
    report = run_sweep(ctx)
    assert proc.briefed == []
    assert report.rebriefed == ["2026-09-30_darius-kemp"]
    assert ctx.remote.commits == []


# ---------- steps 4 and 5: requests, reindex, backcheck ----------

def test_leftover_requests_enqueued(ctx, proc):
    _write(ctx, "requests/2026-10-01_sam_footage.yml", "action: add\n")
    _write(ctx, "requests/2026-10-02_alex_x.yaml", "action: keep\n")
    _write(ctx, "requests/.gitkeep", "")
    _write(ctx, "requests/README.md", "how to write a request\n")

    report = run_sweep(ctx)

    expected = ["requests/2026-10-01_sam_footage.yml", "requests/2026-10-02_alex_x.yaml"]
    assert report.requests == expected
    assert [t["payload"] for t in ctx.queue.of("apply_belief_change")] == [{"request_file": p} for p in expected]


def test_reindex_and_backcheck_enqueued(ctx, proc):
    run_sweep(ctx)
    assert [t["payload"] for t in ctx.queue.of("reindex")] == [{}]
    assert [t["payload"] for t in ctx.queue.of("backcheck")] == [{}]


# ---------- step 6: reminders ----------

def test_untagged_reminder_posted_once_per_meeting(live, proc):
    meetings = [
        _meeting("m_remind", "2026-09-30T20:00:00Z", tags=(), name="Coffee with Darius"),
        _meeting("m_tagged", "2026-09-30T20:00:00Z"),
        _meeting("m_internal", "2026-09-30T20:00:00Z", tags=(),
                 attendees=[OWNER, {"name": "Sam", "email": "sam@acme.dev"}]),
        _meeting("m_earlier", "2026-09-29T20:00:00Z", tags=()),
        _meeting("m_no_email", "2026-09-30T20:00:00Z", tags=(), attendees=[{"name": "Darius Kemp", "email": None}]),
    ]
    cb = FakeCircleback(meetings)

    first = run_sweep(live, circleback={"sam": cb}, last_sweep="2026-09-30T02:00:00-07:00")
    second = run_sweep(live, circleback={"sam": cb}, last_sweep="2026-09-30T02:00:00-07:00")

    assert first.reminders == ["m_remind"]
    assert second.reminders == []
    reminders = [m for m in _ops(live) if (m["metadata"] or {}).get("event_type") == "nua_reminder"]
    assert len(reminders) == 1
    text = reminders[0]["text"]
    assert "Coffee with Darius" in text and "discovery" in text and "Sam" in text
    _no_em_dash(live)


# ---------- step 7: stale digests ----------

def test_stale_digests_limited_to_40(ctx, proc):
    ctx.config.pipeline.reprocess_stale = True
    for i in range(45):
        _digest(ctx, f"2026-09-{1 + i % 28:02d}_person-{i:02d}", f"transcripts/s{i:02d}.md", prompt_version="v0")
    _digest(ctx, "2026-09-10_failed-now", "transcripts/f.md", prompt_version="v0", reprocess_failed="v1")
    _digest(ctx, "stub", "transcripts/stub.md", status="parse_failed", reason="bad", prompt_version=None)
    _digest(ctx, "2026-09-11_current", "transcripts/cur.md", prompt_version="v1")

    report = run_sweep(ctx)

    assert len(report.stale) == 40
    assert len(ctx.queue.of("process_call")) == 40
    assert "2026-09-10_failed-now" not in report.stale
    assert "stub" not in report.stale and "2026-09-11_current" not in report.stale
    assert report.enqueued == []


def test_stale_retries_a_failure_from_an_older_version(ctx, proc):
    ctx.config.pipeline.reprocess_stale = True
    _digest(ctx, "2026-09-10_failed-before", "transcripts/f.md", prompt_version="v0", reprocess_failed="v0")
    report = run_sweep(ctx)
    assert report.stale == ["2026-09-10_failed-before"]
    assert [t["payload"] for t in ctx.queue.of("process_call")] == [{"transcript": "transcripts/f.md"}]


def test_stale_off_by_default(ctx, proc):
    _digest(ctx, "2026-09-10_old-prompt", "transcripts/f.md", prompt_version="v0")
    report = run_sweep(ctx)
    assert report.stale == [] and ctx.queue.of("process_call") == []


# ---------- batch completion ----------

def test_every_batch_file_checked(ctx, proc):
    # the shape process.start_backfill writes, where the id lives in the file
    _write(ctx, "config/batches/abc-123.yml", yaml.safe_dump(
        {"batch": "abc/123", "created": "2026-09-30T10:00:00-07:00", "paths": ["transcripts/a.md"]}))
    _write(ctx, "config/batches/def456.yml", yaml.safe_dump(["transcripts/b.md"]))
    report = run_sweep(ctx)
    assert proc.checked == ["abc/123", "def456"]
    assert report.batches_checked == ["abc/123", "def456"]


def test_one_bad_note_does_not_block_the_next(live, proc):
    good = _note("not_good", "2026-09-30T20:00:00Z")
    bad = _note("not_bad", "2026-09-30T19:00:00Z")
    granola = FakeGranola([bad, good])
    real_get = granola.get_note

    def get_note(note_id):
        if note_id == "not_bad":
            raise RuntimeError("413 paging failed")
        return real_get(note_id)

    granola.get_note = get_note
    report = run_sweep(live, granola=granola)
    assert report.ingested == ["transcripts/granola_not_good.md"]
    assert len(report.errors) == 1 and "not_bad" in report.errors[0]


def test_malformed_cutoff_is_an_error_not_a_silent_skip(live, proc):
    live.config.pipeline.ingest_cutoff = "go-live"
    report = run_sweep(live, granola=FakeGranola([_note("not_new", "2026-09-30T20:00:00Z")]))
    assert report.ingested == []
    assert report.errors and "go-live" in report.errors[0]


def test_meeting_still_processing_is_left_for_the_next_sweep(live, proc):
    cb = FakeCircleback([_meeting("m_busy", "2026-09-30T20:00:00Z"), _meeting("m_done", "2026-09-30T20:30:00Z")])
    real = cb.get_transcript
    cb.get_transcript = lambda mid: [] if mid == "m_busy" else real(mid)
    report = run_sweep(live, circleback={"sam": cb})
    assert report.ingested == ["transcripts/circleback_m_done.md"]
    assert not live.repo.exists("transcripts/circleback_m_busy.md")
    assert report.errors == []


def test_bad_last_sweep_falls_back_to_default_window(live, proc):
    cb = FakeCircleback([_meeting("m_remind", "2026-09-30T20:00:00Z", tags=())])
    report = run_sweep(live, circleback={"sam": cb}, last_sweep="yesterday")
    assert report.reminders == ["m_remind"]


def test_blank_transcripts_are_left_for_the_next_sweep(live, proc):
    note = _note("not_blank", "2026-09-30T20:00:00Z")
    note["transcript"] = [_item("   ", "2026-09-30T20:00:00Z", source="speaker")]
    cb = FakeCircleback([_meeting("m_blank", "2026-09-30T20:00:00Z")])
    cb.get_transcript = lambda mid: [{"speaker": "Darius Kemp", "text": " \n", "timestamp": 1.0}]
    report = run_sweep(live, granola=FakeGranola([note]), circleback={"sam": cb})
    assert report.ingested == [] and report.errors == []
    assert live.repo.list("transcripts/*") == []


# ---------- backfill summary recorded ----------

def _summary_check(proc):
    """A batch_completion_check like process.py's: the batch is done, the summary goes to Slack once."""
    def check(ctx, batch_id):
        proc.checked.append(batch_id)
        channel, key = ctx.channel("calls"), f"batch:{batch_id}"
        if ctx.slack.find_by_metadata(channel, "nua_batch", key) is None:
            ctx.slack.post(channel, [], f"Backfill batch {batch_id} done",
                           metadata={"event_type": "nua_batch", "event_payload": {"key": key}})
        return True
    proc.batch_completion_check = check


def test_batch_summary_ts_is_recorded_and_the_batch_never_checked_again(ctx, proc, monkeypatch):
    _summary_check(proc)
    _write(ctx, "config/batches/abc123.yml", frontmatter.dump_yaml(
        {"batch": "abc123", "created": "2026-09-30T10:00:00-07:00", "paths": ["transcripts/a.md"]}))
    _write(ctx, "config/batches/def456.yml", yaml.safe_dump(["transcripts/b.md"]))

    first = run_sweep(ctx)
    [s1, s2] = [m for m in ctx.slack.messages if (m["metadata"] or {}).get("event_type") == "nua_batch"]
    assert first.batches_checked == ["abc123", "def456"]
    assert yaml.safe_load(ctx.repo.read("config/batches/abc123.yml")) == {
        "batch": "abc123", "created": "2026-09-30T10:00:00-07:00", "paths": ["transcripts/a.md"],
        "summary_ts": s1["ts"]}
    assert yaml.safe_load(ctx.repo.read("config/batches/def456.yml")) == {
        "paths": ["transcripts/b.md"], "summary_ts": s2["ts"]}
    assert [c["paths"] for c in ctx.remote.commits] == [["config/batches/abc123.yml", "config/batches/def456.yml"]]

    # Months later both summaries have scrolled out of the channel window the check can scan.
    monkeypatch.setattr(ctx.slack, "find_by_metadata", lambda *a, **kw: None)
    second = run_sweep(ctx)
    assert second.batches_checked == []
    assert proc.checked == ["abc123", "def456"]
    assert len([m for m in ctx.slack.messages if (m["metadata"] or {}).get("event_type") == "nua_batch"]) == 2
    assert len(ctx.remote.commits) == 1


def test_unfinished_batch_records_nothing(ctx, proc):
    _write(ctx, "config/batches/abc123.yml", yaml.safe_dump({"batch": "abc123", "paths": ["transcripts/a.md"]}))
    run_sweep(ctx)
    run_sweep(ctx)
    assert proc.checked == ["abc123", "abc123"]
    assert "summary_ts" not in yaml.safe_load(ctx.repo.read("config/batches/abc123.yml"))
    assert ctx.remote.commits == []


def test_batch_event_matches_process():
    import importlib

    from nua import sweep

    real = importlib.import_module("nua.process")
    assert sweep.BATCH_EVENT == real.BATCH_EVENT
    assert sweep.SUMMARY_TS == real.SUMMARY_TS


# ---------- Slack escaping ----------

def test_reminder_escapes_slack_control_characters(live, proc):
    meeting = _meeting("m_evil", "2026-09-30T20:00:00Z", tags=(), name="<!channel> see <https://x.io|Circleback login>",
                       attendees=[{"name": "Jane <!here> & Co", "email": "jane@fund.com"}, OWNER])
    run_sweep(live, circleback={"sam": FakeCircleback([meeting])}, last_sweep="2026-09-30T02:00:00-07:00")
    [m] = [m for m in _ops(live) if (m["metadata"] or {}).get("event_type") == "nua_reminder"]
    for text in (m["text"], m["blocks"][0]["text"]["text"]):
        assert "<" not in text and ">" not in text
        assert "&lt;!channel&gt; see &lt;https://x.io|Circleback login&gt;" in text
        assert "Jane &lt;!here&gt; &amp; Co" in text


def test_summary_escapes_errors(live, proc):
    granola = FakeGranola([])

    def boom(*, created_after=None):
        raise RuntimeError("<!here> down, see <https://x.io|status> & retry")

    granola.list_notes = boom
    run_sweep(live, granola=granola)
    [m] = [m for m in _ops(live) if (m["metadata"] or {}).get("event_type") == "discovery_sweep"]
    for text in (m["text"], m["blocks"][0]["text"]["text"]):
        assert "<" not in text and ">" not in text
        assert "&lt;!here&gt; down, see &lt;https://x.io|status&gt; &amp; retry" in text


def test_section_trim_never_splits_an_entity():
    from nua.sweep import _section

    [block] = _section("x" * 2994 + "&amp;" + "y" * 50)
    text = block["text"]["text"]
    assert len(text) <= 3000 and text.endswith("x...")


# ---------- odd front-matter in a dropped transcript ----------

_UNLOADABLE = (pytest.param("date: !!int 12x", id="bad-int-tag"),                # yaml.safe_load: ValueError
               pytest.param("tags: " + "[" * 3000 + "]" * 3000, id="deep-nesting"))  # RecursionError


def _recorder_items() -> tuple[FakeGranola, FakeCircleback]:
    granola = FakeGranola([_note("not_new1", "2026-09-30T17:00:00Z")])
    cb = FakeCircleback([_meeting(12345, "2026-09-30T17:00:00Z"), _meeting("m_new", "2026-09-30T20:00:00Z"),
                         _meeting("m_untagged", "2026-09-30T20:00:00Z", tags=("sales",),
                                  attendees=[{"name": "Jane", "email": "jane@fund.com"}, OWNER])])
    return granola, cb


@pytest.mark.parametrize("fm", ["date: 2026-09-31", "date: 0000-00-00", "created: 2026-13-01 10:00:00",
                                *_UNLOADABLE])
def test_odd_front_matter_in_a_dropped_file_never_stops_the_recorder_catch_up(live, proc, fm):
    # The file cannot be fixed (transcripts are never edited after ingest), so it must not block
    # step 1 or the reminders every night.
    _write(live, "transcripts/darius-notes.md", f"---\ntitle: Darius call\n{fm}\n---\n\nTranscript:\nMe: hello\nThem: hi\n")
    _transcript(live, "transcripts/circleback_12345.md", source="circleback", source_id=12345)
    granola, cb = _recorder_items()

    report = run_sweep(live, granola=granola, circleback={"sam": cb}, last_sweep="2026-09-30T02:00:00-07:00")

    assert report.errors == []
    assert sorted(report.ingested) == ["transcripts/circleback_m_new.md", "transcripts/granola_not_new1.md"]
    assert cb.transcripts == ["m_new"]                         # circleback_12345 is still known
    assert report.reminders == ["m_untagged"]
    assert "transcripts/darius-notes.md" in report.enqueued      # step 2 still hands it to process_call


@pytest.mark.parametrize("fm", _UNLOADABLE)
def test_unloadable_front_matter_is_skipped_and_named(live, proc, fm):
    _write(live, "transcripts/darius-notes.md", f"---\n{fm}\n---\n\nTranscript:\nMe: hello\nThem: hi\n")
    report = run_sweep(live, granola=FakeGranola([_note("not_new1", "2026-09-30T17:00:00Z")]))
    assert report.errors == [] and report.ingested == ["transcripts/granola_not_new1.md"]
    assert any("transcripts/darius-notes.md" in line for line in live.log)


@pytest.mark.parametrize("fm", _UNLOADABLE)
def test_skipped_front_matter_reaches_the_log(live, proc, fm, caplog):
    caplog.set_level("INFO", logger="nua")
    _write(live, "transcripts/darius-notes.md", f"---\n{fm}\n---\n\nTranscript:\nMe: hello\nThem: hi\n")
    run_sweep(live, granola=FakeGranola([_note("not_new1", "2026-09-30T17:00:00Z")]))
    assert any("transcripts/darius-notes.md" in r.getMessage()
               for r in caplog.records if r.name.startswith("nua") and r.levelname == "INFO")


@pytest.mark.parametrize("fm", ["date: 2026-09-31", *_UNLOADABLE])
def test_odd_front_matter_in_a_duplicate_never_breaks_the_brief_step(ctx, proc, fm):
    _write(ctx, "transcripts/bad.md", f"---\n{fm}\n---\n\nTranscript:\nMe: hello\nThem: hi\n")
    _transcript(ctx, "transcripts/a.md")
    _digest(ctx, "2026-09-01_darius-kemp", "transcripts/a.md", date="2026-09-01", duplicates=["transcripts/bad.md"])
    report = run_sweep(ctx)
    assert report.errors == []


# ---------- Cloud Logging ----------

class _DownSlack:
    """Slack that fails on every call, like an outage or a missing bot token."""

    def __getattr__(self, name):
        def fail(*args, **kwargs):
            raise RuntimeError("slack 503")
        return fail


def test_step_errors_and_the_summary_reach_the_log_when_slack_is_down(live, proc, caplog):
    live.slack = _DownSlack()
    caplog.set_level("INFO", logger="nua")
    granola = FakeGranola([], fail_list=True)

    report = run_sweep(live, granola=granola)

    assert report.errors == ["granola: RuntimeError: granola is down", "ops summary: RuntimeError: slack 503"]
    warnings = [r.getMessage() for r in caplog.records if r.levelname == "WARNING" and r.name.startswith("nua")]
    assert any("granola: RuntimeError: granola is down" in w for w in warnings)
    assert any("ops summary: RuntimeError: slack 503" in w for w in warnings)
    # the summary itself is logged before the post is tried, so it survives the failed post
    assert any(w.startswith("Sweep hit 1 error") and "granola is down" in w for w in warnings)


def test_summary_without_errors_is_logged_at_info(ctx, proc, caplog):
    caplog.set_level("INFO", logger="nua")
    _transcript(ctx, "transcripts/d.md")
    run_sweep(ctx)
    [m] = _ops(ctx)
    infos = [r.getMessage() for r in caplog.records if r.name.startswith("nua") and r.levelname == "INFO"]
    assert m["text"] in infos
    assert not [r for r in caplog.records if r.name.startswith("nua") and r.levelname == "WARNING"]


def test_summary_is_logged_when_there_is_no_slack_client(ctx, proc, caplog):
    ctx.slack = None
    caplog.set_level("INFO", logger="nua")
    _transcript(ctx, "transcripts/d.md")
    run_sweep(ctx)
    assert any("transcripts/d.md" in r.getMessage() for r in caplog.records if r.name.startswith("nua"))
    assert any("no Slack client, sweep summary not posted" in r.getMessage()
               for r in caplog.records if r.levelname == "WARNING")


# ---------- transcripts dropped into a subfolder ----------

def test_transcripts_in_a_subfolder_are_named_in_the_ops_summary(ctx, proc):
    _transcript(ctx, "transcripts/a.md")
    _digest(ctx, "2026-09-18_darius-kemp", "transcripts/a.md", date="2026-09-18")
    _transcript(ctx, "transcripts/Granola exports/wade-pruitt.md")
    _transcript(ctx, "transcripts/Granola exports/older/viktor \u2014 lang.md")
    _write(ctx, "transcripts/Granola exports/.DS_Store", "x")
    _write(ctx, "transcripts/.hidden/x.md", "x")

    report = run_sweep(ctx)

    assert report.enqueued == []                          # never processed from a subfolder
    assert ctx.queue.of("process_call") == []
    [m] = _ops(ctx)
    assert "`transcripts/Granola exports/wade-pruitt.md`" in m["text"]
    assert "`transcripts/Granola exports/older/viktor - lang.md`" in m["text"]
    assert ".DS_Store" not in m["text"] and ".hidden" not in m["text"]
    assert "move" in m["text"].lower()
    _no_em_dash(ctx)


# ---------- digest files that cannot be read ----------

BROKEN_DIGEST = ("---\ncall_id: 2026-09-01_jane-doe\ntranscript: transcripts/b.md\nstatus: digested\n"
                 "origin: live\nseat: [unclosed <!channel>\n---\n")


def test_unreadable_digests_are_named_in_one_ops_post_per_sweep(ctx, proc):
    _transcript(ctx, "transcripts/a.md")
    _digest(ctx, "2026-09-18_darius-kemp", "transcripts/a.md", date="2026-09-18")
    _transcript(ctx, "transcripts/b.md")
    _write(ctx, "digests/2026-09-01_jane-doe.md", BROKEN_DIGEST)
    _write(ctx, "digests/notes.md", "Stray notes, not a digest.\n")

    report = run_sweep(ctx)

    assert report.unreadable == ["digests/2026-09-01_jane-doe.md", "digests/notes.md"]
    assert report.enqueued == [] and ctx.queue.of("process_call") == []   # its transcript counts as digested
    [m] = _ops(ctx)
    assert "`digests/2026-09-01_jane-doe.md`: front-matter is not valid YAML" in m["text"]
    assert "`digests/notes.md`: not a digest" in m["text"]
    assert "by hand" in m["text"]
    assert "<!channel>" not in m["text"] and "<!channel>" not in str(m["blocks"])
    _no_em_dash(ctx)

    run_sweep(ctx)                                 # still broken the next night: named again, once
    assert len(_ops(ctx)) == 2


def test_readable_digests_add_nothing_to_the_ops_summary(ctx, proc):
    _transcript(ctx, "transcripts/a.md")
    _digest(ctx, "2026-09-18_darius-kemp", "transcripts/a.md", date="2026-09-18")
    assert run_sweep(ctx).unreadable == []
    assert ctx.slack.messages == []


def test_batch_file_with_an_impossible_date_is_read_as_unreadable(ctx, proc):
    _transcript(ctx, "transcripts/a.md")
    _write(ctx, "config/batches/abc123.yml", "batch: abc123\ncreated: 2026-09-31\npaths: [transcripts/a.md]\n")
    report = run_sweep(ctx)
    assert report.errors == []
    assert report.enqueued == ["transcripts/a.md"]
    assert proc.checked == ["abc123"]
