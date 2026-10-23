"""Nightly sweep: the safety net behind the webhooks (SPEC "Nightly sweep").

The `sweep` task runs these steps in order, each on its own so one failure never blocks the rest:

1. Recorders: every Granola note visible to the workspace key and every Circleback meeting tagged
   `discovery` (per person's key), created on or after `ingest_cutoff`, whose `source_id` no transcript
   in the repo carries, is written through the create-only helper (stamped `ingested_at`, like the
   receiver's files) and gets a `process_call`.
2. Transcripts: `process_call` for any transcript no digest points to. Files in a subfolder of
   `transcripts/` are never processed there (only direct children are); they are named in the ops
   summary every night until someone moves them up. A digest file that cannot be read still owns the
   transcript it names (no new digest is made), and its path and reason are named in the ops summary
   every night until someone fixes it by hand.
3. Briefs: re-post the brief of any `live`, `digested` call with no brief that is dated in the last
   48 hours (`brief_repost_hours`) or, at any age, lists a live copy under `duplicates` (a supersede
   whose brief update failed; post_brief finds the old top message through that copy's metadata).
4. Requests: `apply_belief_change` for every request file still in `requests/`.
5. Reindex and backcheck: enqueue both (backcheck skips itself when nothing is missing).
6. Reminders: Circleback meetings created since the last sweep with an invitee from outside the team and no
   `discovery` tag are posted to `#discovery-ops`, once per meeting.
7. Stale digests: with `reprocess_stale: true`, `process_call` for up to 40 digests at an old
   `prompt_version`, skipping stubs and any marked `reprocess_failed` at the current version.

Then the backfill completion check runs for every `config/batches/*.yml` without a `summary_ts`, so a
lost summary is recovered. Once a batch's summary is in Slack, its ts is written to the batch file as
`summary_ts` and later sweeps skip that batch: the check looks for the summary only among the channel's
latest messages, so an old summary would otherwise scroll out of reach and be posted again.
`granola_only` runs step 1 for Granola only (the hourly `sweep-granola` job).

Recorder steps 1 and 6 only run once `ingest_cutoff` is set: before go-live, dropped exports are the
only source, and ingesting every note the keys can see would re-ingest the backfill as live calls.

The sweep posts to `#discovery-ops` only for reminders, for things the webhooks missed (steps 1 to 4),
for transcripts left in a subfolder, for digest files that cannot be read, and for step errors. Step
errors and the summary also go to the `discovery.sweep` logger (Cloud Logging), so they leave a trace even
when Slack cannot be posted to. A dry run writes transcript files locally and still enqueues, but
touches neither Slack nor the remote; its report lists what it would have posted.

Task payloads: `process_call` {"transcript": path, "batch": id (backfill files only)},
`apply_belief_change` {"request_file": path}, `reindex` {}, `backcheck` {}.
"""
from __future__ import annotations

import logging
import math
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import PurePosixPath
from typing import Any, Callable

import yaml

from discovery import frontmatter, parse, rules
from discovery.context import Ctx
from discovery.digest import Unreadable, digest_for_transcript, load_digests, write_digest
from discovery.ingest.circleback import CirclebackClient, find_start, has_tag, payload_to_standard
from discovery.ingest.circleback import has_transcript as meeting_has_transcript
from discovery.ingest.granola import GranolaClient, note_time, note_to_standard
from discovery.ingest.granola import has_transcript as note_has_transcript
from discovery.rules import as_datetime as _as_datetime
from discovery.ports import RepoFS
from discovery.tasks import task_name
from discovery.types import Digest

DISCOVERY_TAG = "discovery"
REMINDER_LOOKBACK_HOURS = 48          # reminder window when the caller does not say when the last sweep ran
CALENDAR_LOOKBACK_MIN_HOURS = 24
CALENDAR_LOOKBACK_MAX_HOURS = 24 * 30
MAX_NAMED = 10                        # items named per line of the ops summary
UNREADABLE_REASON_LIMIT = 200         # characters of a digest file's read error shown in the ops summary
SLACK_SECTION_LIMIT = 3000
BATCH_EVENT = "discovery_batch"             # process.BATCH_EVENT: Slack metadata on a backfill summary, key "batch:<id>"
SUMMARY_TS = "summary_ts"             # batch file key: ts of the batch's summary in the calls channel

log = logging.getLogger("discovery.sweep")

# A Granola note created this many days before ingest_cutoff still counts when its call is after it.
PRECREATED_LOOKBACK_DAYS = 30


@dataclass
class SweepReport:
    """What one sweep did (or, in a dry run, would have done)."""
    ingested: list[str] = field(default_factory=list)        # transcript paths created in step 1
    enqueued: list[str] = field(default_factory=list)        # transcripts given a process_call in steps 1 and 2
    rebriefed: list[str] = field(default_factory=list)       # call ids whose brief was re-posted
    requests: list[str] = field(default_factory=list)        # request files given an apply_belief_change
    reminders: list[str] = field(default_factory=list)       # Circleback meeting ids reminded about
    stale: list[str] = field(default_factory=list)           # call ids re-run for an old prompt_version
    batches_checked: list[str] = field(default_factory=list)  # backfill batch ids whose completion was checked
    nested: list[str] = field(default_factory=list)          # files in a subfolder of transcripts/, never processed
    unreadable: list[str] = field(default_factory=list)      # digest files load_digests could not read (step 2)
    errors: list[str] = field(default_factory=list)          # "<step>: <error>" for steps or items that failed


def run_sweep(ctx: Ctx, *, granola: GranolaClient | None = None,
              circleback: dict[str, CirclebackClient] | None = None, granola_only: bool = False,
              last_sweep: str | None = None) -> SweepReport:
    """Run the nightly sweep (SPEC "Nightly sweep" steps 1 to 7, then batch completion checks).

    `granola` is a client on the workspace key; `circleback` maps a person key to a client on that
    person's key. A missing client skips that recorder. `last_sweep` (ISO datetime) starts the
    reminder window; without it (or when it cannot be read) the window is the last 48 hours, and the
    once-per-meeting check keeps overlapping windows from repeating a reminder. Errors in one step
    are recorded in the report and posted to ops; they never stop the other steps.
    Raises RuntimeError when `ctx.queue` is None.
    """
    if ctx.queue is None:
        raise RuntimeError("the sweep needs a task queue (ctx.queue is None)")
    sweep = _Sweep(ctx)
    if granola is not None:
        sweep.step("granola", sweep.granola_notes, granola)
    if not granola_only:
        for person, client in (circleback or {}).items():
            sweep.step(f"circleback {person}", sweep.circleback_meetings, person, client)
        sweep.step("transcripts", sweep.transcripts)
        sweep.step("briefs", sweep.briefs)
        sweep.step("requests", sweep.requests)
        sweep.step("reindex and backcheck", sweep.reindex_and_backcheck)
        since = _as_datetime(last_sweep, ctx.tz, strict=False)
        for person, client in (circleback or {}).items():
            sweep.step(f"reminders {person}", sweep.reminders, person, client, since)
        sweep.step("stale digests", sweep.stale_digests)
        sweep.step("batches", sweep.batches)
    sweep.step("ops summary", sweep.post_summary)
    return sweep.report


class _Sweep:
    """State for one sweep run: the report, the transcripts already queued, what the webhooks missed."""

    def __init__(self, ctx: Ctx):
        self.ctx = ctx
        self.config = ctx.config
        self.repo: RepoFS = ctx.repo
        self.report = SweepReport()
        self.now = ctx.now().astimezone(ctx.tz)
        self.nonce = f"sweep-{self.now.date().isoformat()}"   # fresh task names each night, deduped within one
        self.queued: set[str] = set()                           # transcripts given a process_call in this run
        self.missed_transcripts: list[str] = []                 # step 2 finds
        self.unreadable: list[Unreadable] = []                  # step 2: digest files that cannot be read
        self._known: set[tuple[str, str]] | None = None

    # ---------- plumbing ----------

    def step(self, name: str, fn: Callable[..., Any], *args: Any) -> None:
        """Run one step or item, recording an exception as an error instead of raising."""
        try:
            fn(*args)
        except Exception as e:   # the safety net keeps going; the error goes in the report and to ops
            msg = f"{name}: {type(e).__name__}: {e}"
            self.report.errors.append(msg)
            self.ctx.info(f"sweep error, {msg}")
            log.warning("sweep error, %s", msg, exc_info=e)

    def enqueue(self, kind: str, payload: dict, key: str, *, nonce: str | None = None,
                delay_seconds: int = 0) -> bool:
        return self.ctx.queue.enqueue(kind, payload, name=task_name(kind, key, nonce=nonce),
                                      delay_seconds=delay_seconds)

    def process_call(self, rel: str, *, batch: str | None = None, nonce: str | None = None,
                     delay_seconds: int = 0) -> None:
        payload: dict[str, Any] = {"transcript": rel}
        if batch:
            payload["batch"] = batch
        self.enqueue("process_call", payload, rel, nonce=nonce, delay_seconds=delay_seconds)
        self.queued.add(rel)

    def slack_ready(self, what: str) -> bool:
        """True when the sweep may post to Slack; logs why not otherwise."""
        if self.ctx.dry_run:
            self.ctx.info(f"sweep dry run: {what} not posted")
            return False
        if self.ctx.slack is None:
            self.ctx.info(f"sweep: no Slack client, {what} not posted")
            log.warning("sweep: no Slack client, %s not posted", what)
            return False
        return True

    def cutoff(self) -> datetime | None:
        """The ingest cutoff as an aware datetime, or None when the recorders are not live yet."""
        cutoff = _as_datetime(self.config.pipeline.ingest_cutoff, self.ctx.tz)
        if cutoff is None:
            self.ctx.info("sweep: no ingest_cutoff in config/pipeline.yml, recorder steps skipped")
        return cutoff

    @property
    def known(self) -> set[tuple[str, str]]:
        """(source, source_id) of every transcript in the repo, read from front-matter."""
        if self._known is None:
            self._known = _known_source_ids(self.repo, skipped=self.ctx.info)
        return self._known

    # ---------- step 1: recorders ----------

    def granola_notes(self, client: GranolaClient) -> None:
        cutoff = self.cutoff()
        if cutoff is None:
            return
        lookback = cutoff - timedelta(days=PRECREATED_LOOKBACK_DAYS)
        for note in client.list_notes(created_after=lookback.isoformat()):
            note_id = note.get("id")
            if not note_id or ("granola", str(note_id)) in self.known:
                continue
            created = _as_datetime(note.get("created_at"), self.ctx.tz, strict=False)
            updated = _as_datetime(note.get("updated_at"), self.ctx.tz, strict=False)
            if created is not None and created >= cutoff:
                self.step(f"granola note {note_id}", self._ingest_note, client, note_id)
            elif updated is not None and updated >= cutoff:
                # created ahead of the call, used after the cutoff: ingest only if the call was after it
                self.step(f"granola note {note_id}", self._ingest_note, client, note_id, cutoff)

    def _ingest_note(self, client: GranolaClient, note_id: str, cutoff: datetime | None = None) -> None:
        note = client.get_note(note_id)
        if cutoff is not None:
            t = note_time(note)
            if t is None or t < cutoff:
                return
        if not note_has_transcript(note):
            self.ctx.info(f"sweep: granola note {note_id} has no transcript yet, left for the next sweep")
            return
        rel, text = note_to_standard(note, config=self.config, ingested_at=self.stamp())
        self._ingest(rel, text)

    def circleback_meetings(self, person: str, client: CirclebackClient) -> None:
        cutoff = self.cutoff()
        if cutoff is None:
            return
        missing: list[tuple[dict, datetime]] = []
        for meeting in client.list_meetings(tag=DISCOVERY_TAG):
            mid = meeting.get("id")
            if mid is None or ("circleback", str(mid)) in self.known:
                continue
            created = _as_datetime(meeting.get("createdAt"), self.ctx.tz, strict=False)
            if created is None or created < cutoff:
                continue
            missing.append((meeting, created))
        if not missing:
            return
        events: list[dict] = []
        if any(not _calendar_start(m) for m, _ in missing):
            oldest = min(created for _, created in missing)
            # One lookup for the whole batch; on failure the start falls back to createdAt.
            self.step(f"circleback {person} calendar", lambda: events.extend(
                client.calendar_events(lookback_hours=_lookback_hours(self.now, oldest))))
        for meeting, _ in missing:
            self.step(f"circleback {person} meeting {meeting['id']}", self._ingest_meeting,
                      person, client, meeting, events)

    def _ingest_meeting(self, person: str, client: CirclebackClient, meeting: dict, events: list[dict]) -> None:
        mid = meeting["id"]
        start = _calendar_start(meeting) or find_start(events, mid)
        segments = client.get_transcript(str(mid))
        if not meeting_has_transcript({"transcript": segments}):
            # Still processing: a file now would become a parse_failed stub for good.
            self.ctx.info(f"sweep: circleback meeting {mid} has no transcript yet, left for the next sweep")
            return
        rel, text = payload_to_standard({**meeting, "transcript": segments}, person=person, config=self.config,
                                        start=start, start_source="calendar", ingested_at=self.stamp())
        self._ingest(rel, text)

    def stamp(self) -> str:
        """`ingested_at` for a transcript file written now (the duplicate rule's arrival order)."""
        return rules.ingest_stamp(self.ctx.now(), self.config.pipeline.timezone)

    def _ingest(self, rel: str, text: str) -> None:
        """Create the transcript file (create-only) and enqueue it, the same way the receiver does.

        A file that already exists is not written but still enqueued: process_call's own check makes
        a duplicate harmless. The task is named `task_name("process_call", path)` with no nonce, so a
        receiver naming its task the same way and the sweep queue it once. The file is also written to
        the local working tree, so later steps and local runs see it; the next sync makes it tracked.
        """
        if self._create(rel, text):
            self.report.ingested.append(rel)
        self.process_call(rel, delay_seconds=self._delay_for(rel, text))
        self.report.enqueued.append(rel)

    def _create(self, rel: str, text: str) -> bool:
        if self.repo.exists(rel):
            return False
        if not self.ctx.dry_run and not self.ctx.remote.create_file_only(rel, text, f"Add {rel} (sweep)"):
            return False
        self.repo.write(rel, text)
        return True

    def _delay_for(self, rel: str, text: str) -> int:
        """15 minutes when the invite includes another teammate, so a second recording can land first."""
        try:
            parsed = parse.parse(rel, text)
        except parse.ParseError:
            return 0
        if rules.cofounder_invited(parsed, self.config):
            return int(self.config.pipeline.t("dup_delay_minutes")) * 60
        return 0

    # ---------- step 2: transcripts no digest points to ----------

    def transcripts(self) -> None:
        self.report.nested = _nested_files(self.repo, "transcripts")
        digests = load_digests(self.repo)
        # An unreadable file still owns the transcript it names (digest_for_transcript returns a stand-in),
        # so nothing is re-digested; the ops summary names it until someone fixes it by hand.
        self.unreadable = sorted(digests.unreadable, key=lambda u: u.path)
        self.report.unreadable = [u.path for u in self.unreadable]
        batch_of = {path: batch_id for batch_id, paths in _load_batches(self.repo).items() for path in paths}
        for rel in _files(self.repo, "transcripts"):
            if rel in self.queued or digest_for_transcript(digests, rel) is not None:
                continue
            self.process_call(rel, batch=batch_of.get(rel), nonce=self.nonce)
            self.report.enqueued.append(rel)
            self.missed_transcripts.append(rel)

    # ---------- step 3: briefs ----------

    def briefs(self) -> None:
        hours = int(self.config.pipeline.t("brief_repost_hours"))
        earliest = (self.now - timedelta(hours=hours)).date().isoformat()
        today = self.now.date().isoformat()
        missing = [d for d in load_digests(self.repo)
                   if d.origin == "live" and d.status == "digested" and (d.brief is None or not d.brief.ts)]
        # A late supersede whose brief update failed clears the brief at any call age: its live copy's top
        # message is still in Slack, and post_brief finds it through the duplicates' metadata.
        due = [d for d in missing
               if (d.date and earliest <= str(d.date) <= today) or self._has_live_copy(d)]
        if not due:
            return
        if not self.slack_ready(f"{len(due)} missing brief(s)"):
            if self.ctx.dry_run:
                self.report.rebriefed.extend(d.call_id for d in due)
            return
        written: list[str] = []
        for d in due:
            self.step(f"brief {d.call_id}", self._rebrief, d, written)
        if written:
            ids = ", ".join(PurePosixPath(p).stem for p in written)
            self.ctx.remote.commit_and_push(f"Record re-posted briefs (sweep): {ids}", written)

    def _has_live_copy(self, d: Digest) -> bool:
        """True when one of the digest's duplicates is a recorder (live) transcript still in the repo."""
        for rel in d.duplicates:
            rel = _norm_path(str(rel))
            if rel == _norm_path(d.transcript) or not self.repo.exists(rel):
                continue
            try:
                fm, _, _ = frontmatter.split(self.repo.read(rel))
            except Exception as e:   # YAMLError, a plain ValueError or RecursionError from yaml, not UTF-8
                self.ctx.info(f"sweep: front-matter of {rel} unreadable ({type(e).__name__}), not a live copy")
                continue
            if str(fm.get("source") or "").strip().lower() in parse.LIVE_SOURCES:
                return True
        return False

    def _rebrief(self, d: Digest, written: list[str]) -> None:
        from discovery.process import post_brief   # Task 12; imported late so this module loads without it

        ref = post_brief(self.ctx, d)
        if not ref.ts:
            return   # nothing was posted, so there is nothing to record
        d.brief = ref
        written.append(write_digest(self.repo, d))
        self.report.rebriefed.append(d.call_id)

    # ---------- steps 4 and 5: requests, reindex, backcheck ----------

    def requests(self) -> None:
        for rel in _files(self.repo, "requests", suffixes=(".yml", ".yaml")):
            self.enqueue("apply_belief_change", {"request_file": rel}, rel, nonce=self.nonce)
            self.report.requests.append(rel)

    def reindex_and_backcheck(self) -> None:
        self.enqueue("reindex", {}, "sweep", nonce=self.nonce)
        self.enqueue("backcheck", {}, "sweep", nonce=self.nonce)

    # ---------- step 6: reminders ----------

    def reminders(self, person: str, client: CirclebackClient, since: datetime | None) -> None:
        cutoff = self.cutoff()
        if cutoff is None:
            return
        since = max(since or self.now - timedelta(hours=REMINDER_LOOKBACK_HOURS), cutoff)
        for meeting in client.list_meetings():
            mid = meeting.get("id")
            if mid is None or has_tag(meeting, DISCOVERY_TAG) or ("circleback", str(mid)) in self.known:
                continue
            created = _as_datetime(meeting.get("createdAt"), self.ctx.tz, strict=False)
            if created is None or created < since:
                continue
            external = self._external_attendees(meeting)
            if external:
                self.step(f"reminder {person} {mid}", self._remind, person, meeting, created, external)

    def _external_attendees(self, meeting: dict) -> list[dict]:
        """Attendees with an email that is neither a team domain nor a teammate's address."""
        out = []
        for a in meeting.get("attendees") or []:
            email = (a.get("email") or "").strip() if isinstance(a, dict) else ""
            if email and not self.config.is_internal_email(email) and self.config.person(email) is None:
                out.append(a)
        return out

    def _remind(self, person: str, meeting: dict, created: datetime, external: list[dict]) -> None:
        mid = str(meeting["id"])
        key = f"reminder:circleback:{mid}"
        channel = self.ctx.channel("ops")
        if self.ctx.slack is not None and self.ctx.slack.find_by_metadata(channel, "discovery_reminder", key):
            return   # already reminded
        recorder = self.config.person(person)
        text = _reminder_text(meeting, created.astimezone(self.ctx.tz), external,
                              recorder=recorder.name if recorder else person)
        if self.slack_ready(f"reminder for {mid}"):
            self.ctx.slack.post(channel, _section(text), text,
                                metadata={"event_type": "discovery_reminder", "event_payload": {"key": key}})
        elif not self.ctx.dry_run:
            return
        self.report.reminders.append(mid)

    # ---------- step 7: stale digests ----------

    def stale_digests(self) -> None:
        if not self.config.pipeline.reprocess_stale:
            return
        current = str(self.config.pipeline.prompt_version)
        limit = int(self.config.pipeline.t("reprocess_stale_limit"))
        stale = [d for d in load_digests(self.repo)
                 if d.status == "digested" and str(d.prompt_version) != current
                 and str(d.reprocess_failed or "") != current]
        for d in stale[:limit]:
            self.process_call(d.transcript, nonce=self.nonce)
            self.report.stale.append(d.call_id)

    # ---------- backfill batches ----------

    def batches(self) -> None:
        """The completion check for every batch whose summary is not recorded yet (see `summary_ts`)."""
        files = _batch_files(self.repo)
        batch_ids = sorted(bid for bid, (_, summarized) in files.items() if not summarized)
        if not batch_ids:
            return
        if self.ctx.dry_run:
            self.ctx.info(f"sweep dry run: completion checks for {', '.join(batch_ids)} not run")
            self.report.batches_checked.extend(batch_ids)
            return
        from discovery.process import batch_completion_check   # Task 12; imported late

        written: list[str] = []
        for batch_id in batch_ids:
            self.step(f"batch {batch_id}", self._check_batch, batch_completion_check, batch_id,
                      files[batch_id][0], written)
        if written:
            ids = ", ".join(PurePosixPath(p).stem for p in written)
            self.ctx.remote.commit_and_push(f"Record backfill summaries (sweep): {ids}", written)

    def _check_batch(self, check: Callable[[Ctx, str], bool], batch_id: str, rel: str, written: list[str]) -> None:
        done = check(self.ctx, batch_id)
        self.report.batches_checked.append(batch_id)
        if done and self._record_summary(batch_id, rel):
            written.append(rel)

    def _record_summary(self, batch_id: str, rel: str) -> bool:
        """Write the summary's ts into the batch file as `summary_ts`, so later sweeps skip the batch.

        Only once the summary is found in Slack; True when the file was written. A bare list of paths
        becomes the `{paths, summary_ts}` mapping.
        """
        if self.ctx.slack is None:
            return False
        data = yaml.safe_load(self.repo.read(rel))
        if isinstance(data, list):
            data = {"paths": data}
        if not isinstance(data, dict) or data.get(SUMMARY_TS):
            return False
        ts = self.ctx.slack.find_by_metadata(self.ctx.channel("calls"), BATCH_EVENT, f"batch:{batch_id}")
        if not ts:
            return False
        data[SUMMARY_TS] = ts
        self.repo.write(rel, frontmatter.dump_yaml(data))
        return True

    # ---------- ops summary ----------

    def post_summary(self) -> None:
        """One message to #discovery-ops when the sweep caught something the webhooks missed, found files
        in a subfolder of transcripts/, found digest files it cannot read, or hit errors. The text is
        logged first (WARNING when there were errors), so it reaches Cloud Logging even when the post
        fails or Slack is not set up."""
        text = _summary_text(self.report, self.missed_transcripts, self.unreadable)
        if text is None:
            return
        log.log(logging.WARNING if self.report.errors else logging.INFO, "%s", text)
        if not self.slack_ready("sweep summary"):
            self.ctx.info(text)
            return
        key = f"sweep:{self.now.isoformat(timespec='minutes')}"
        self.ctx.slack.post(self.ctx.channel("ops"), _section(text), text,
                            metadata={"event_type": "discovery_sweep", "event_payload": {"key": key}})


# ---------- helpers ----------

def _files(repo: RepoFS, folder: str, *, suffixes: tuple[str, ...] | None = None) -> list[str]:
    """Files directly in `folder`, sorted, skipping dotfiles such as .gitkeep."""
    out = []
    for rel in repo.list(f"{folder}/*"):
        name = PurePosixPath(rel).name
        if name.startswith(".") or (suffixes and not name.lower().endswith(suffixes)):
            continue
        out.append(rel)
    return out


def _nested_files(repo: RepoFS, folder: str) -> list[str]:
    """Files in a subfolder of `folder` (at any depth), sorted, skipping any path with a part that starts
    with a dot (.DS_Store, .git folders)."""
    out = []
    for rel in repo.list(f"{folder}/**/*"):
        parts = PurePosixPath(rel).relative_to(folder).parts
        if len(parts) > 1 and not any(part.startswith(".") for part in parts):
            out.append(rel)
    return out


def _known_source_ids(repo: RepoFS, *, skipped: Callable[[str], None] | None = None) -> set[tuple[str, str]]:
    """(source, source_id) from the front-matter of every transcript. Dropped files have none.

    A file whose front-matter cannot be loaded at all (invalid YAML, a value PyYAML fails on with a plain
    ValueError, nesting deep enough for a RecursionError, not UTF-8) is left out and named to `skipped`:
    transcripts are never edited after ingest, so one odd dropped file must not stop the recorder steps.
    """
    known: set[tuple[str, str]] = set()
    for rel in _files(repo, "transcripts"):
        try:
            fm, _, _ = frontmatter.split(repo.read(rel))
        except Exception as e:
            if skipped is not None:
                skipped(f"sweep: front-matter of {rel} unreadable ({type(e).__name__}), "
                        "left out of the recorder id check")
            continue
        source, source_id = fm.get("source"), fm.get("source_id")
        if source and source_id is not None:
            known.add((str(source).strip().lower(), str(source_id)))
    return known


def _load_batches(repo: RepoFS) -> dict[str, list[str]]:
    """Batch id -> transcript paths, from `config/batches/*.yml`.

    The file process.start_backfill writes is `{batch, created, paths}`; the id is its `batch` field,
    else the file stem. A bare list of paths is accepted too. An unreadable file keeps its id with no
    paths, so its completion check still runs.
    """
    return {batch_id: paths for batch_id, (_, paths, _) in _read_batches(repo).items()}


def _batch_files(repo: RepoFS) -> dict[str, tuple[str, bool]]:
    """Batch id -> (batch file path, whether the file records the summary's `summary_ts`)."""
    return {batch_id: (rel, summarized) for batch_id, (rel, _, summarized) in _read_batches(repo).items()}


def _read_batches(repo: RepoFS) -> dict[str, tuple[str, list[str], bool]]:
    """Batch id -> (batch file path, transcript paths, whether `summary_ts` is set)."""
    out: dict[str, tuple[str, list[str], bool]] = {}
    for rel in _files(repo, "config/batches", suffixes=(".yml", ".yaml")):
        batch_id = PurePosixPath(rel).stem
        try:
            data = yaml.safe_load(repo.read(rel))
        except (yaml.YAMLError, ValueError, RecursionError):   # ValueError: an impossible date, not UTF-8
            data = None
        summarized = False
        if isinstance(data, dict):
            batch_id = str(data.get("batch") or batch_id)
            paths = data.get("paths") or []
            summarized = bool(data.get(SUMMARY_TS))
        elif isinstance(data, list):
            paths = data
        else:
            paths = []
        out[batch_id] = (rel, [_norm_path(str(p)) for p in paths if p], summarized)
    return out


def _norm_path(path: str) -> str:
    return path[2:] if path.startswith("./") else path


def _calendar_start(meeting: dict) -> str | None:
    """Start time from the meeting's own calendar event, when the API included one."""
    event = meeting.get("calendarEvent")
    return (event.get("startTime") or None) if isinstance(event, dict) else None


def _lookback_hours(now: datetime, oldest: datetime) -> int:
    """Calendar lookback that reaches the oldest missing meeting, with a little margin."""
    hours = math.ceil((now - oldest).total_seconds() / 3600) + 2
    return max(CALENDAR_LOOKBACK_MIN_HOURS, min(hours, CALENDAR_LOOKBACK_MAX_HOURS))


def _one_line(text: Any) -> str:
    return " ".join(str(text).split()).replace("\u2014", ",") if text else ""


def _esc(text: str) -> str:
    """Escape the three characters Slack mrkdwn treats as control characters, so outside text such
    as a meeting title cannot ping a channel (`<!channel>`) or disguise a link (`<url|label>`)."""
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _attendee_label(a: dict) -> str:
    return _one_line(a.get("name")) or _one_line(a.get("email"))


def _reminder_text(meeting: dict, created: datetime, external: list[dict], *, recorder: str) -> str:
    """The reminder as Slack mrkdwn. The title and attendee names come from the calendar invite, which
    an outside organizer controls, so every outside value is escaped."""
    title = _esc(_one_line(meeting.get("name")) or "Untitled meeting")
    names = [_esc(_attendee_label(a)) for a in external]
    who = ", ".join(names[:3]) + (f" and {len(names) - 3} more" if len(names) > 3 else "")
    when = created.strftime("%Y-%m-%d %H:%M")
    return (f"Untagged Circleback call: {title} ({when}, {_esc(_one_line(recorder))}) with {who}. "
            f"If it was a discovery call, tag it `{DISCOVERY_TAG}` and the next sweep ingests it.")


def _named(items: list[str]) -> str:
    """Paths or ids as code spans. A dropped file keeps its original name, which may carry an em dash;
    it becomes a hyphen so the path still reads naturally."""
    shown = ", ".join(f"`{_esc(str(i).replace(chr(0x2014), '-'))}`" for i in items[:MAX_NAMED])
    return shown + (f" and {len(items) - MAX_NAMED} more" if len(items) > MAX_NAMED else "")


def _plural(n: int, word: str) -> str:
    return f"{n} {word}" if n == 1 else f"{n} {word}s"


def _summary_text(report: SweepReport, missed_transcripts: list[str],
                  unreadable: list[Unreadable] | None = None) -> str | None:
    """The ops summary, or None when there is nothing to say."""
    caught = []
    if report.ingested:
        caught.append(f"- Ingested {_plural(len(report.ingested), 'recorder call')}: {_named(report.ingested)}")
    if missed_transcripts:
        caught.append(f"- Queued {_plural(len(missed_transcripts), 'transcript')} with no digest: "
                      f"{_named(missed_transcripts)}")
    if report.rebriefed:
        caught.append(f"- Re-posted {_plural(len(report.rebriefed), 'missing brief')}: {_named(report.rebriefed)}")
    if report.requests:
        caught.append(f"- Queued {_plural(len(report.requests), 'leftover request')}: {_named(report.requests)}")
    lines = []
    if caught:
        lines += ["Sweep caught what the webhooks missed", *caught]
    if report.nested:
        lines.append(subfolder_notice(report.nested))
    if unreadable:
        lines += unreadable_notice(unreadable)
    if report.errors:
        lines.append(f"Sweep hit {_plural(len(report.errors), 'error')}")
        lines += [f"- {_esc(_one_line(e))}" for e in report.errors[:MAX_NAMED]]
        if len(report.errors) > MAX_NAMED:
            lines.append(f"- and {len(report.errors) - MAX_NAMED} more")
    return "\n".join(lines) if lines else None


def subfolder_notice(paths: list[str]) -> str:
    """The ops line for files in a subfolder of transcripts/ (Slack mrkdwn). The sweep and the /github
    route both use it."""
    n = len(paths)
    return (f"{_plural(n, 'file')} in a subfolder of `transcripts/` will not be processed: only files directly "
            f"in `transcripts/` are. Move {'it' if n == 1 else 'them'} up into `transcripts/`: {_named(paths)}")


def unreadable_notice(files: list[Unreadable]) -> list[str]:
    """Ops lines (Slack mrkdwn) for digest files that cannot be read: each path with its reason. The
    reason can quote the file's own text, so it is escaped and cut short."""
    n = len(files)
    lines = [f"{_plural(n, 'digest file')} cannot be read, so the indexes and `ask` skip "
             f"{'it' if n == 1 else 'them'} and no new digest replaces {'it' if n == 1 else 'them'}. "
             f"Fix {'it' if n == 1 else 'them'} by hand:"]
    for u in files[:MAX_NAMED]:
        reason = _one_line(u.error)
        if reason.startswith(f"{u.path}: "):
            reason = reason[len(u.path) + 2:]
        if len(reason) > UNREADABLE_REASON_LIMIT:
            reason = reason[:UNREADABLE_REASON_LIMIT - 3] + "..."
        lines.append(f"- {_named([u.path])}: {_esc(reason)}")
    if n > MAX_NAMED:
        lines.append(f"- and {n - MAX_NAMED} more")
    return lines


def _section(text: str) -> list[dict]:
    """One mrkdwn section block, trimmed to Slack's limit without splitting an escape such as `&amp;`."""
    if len(text) > SLACK_SECTION_LIMIT:
        text = re.sub(r"&[a-z]{0,3}$", "", text[:SLACK_SECTION_LIMIT - 3]) + "..."
    return [{"type": "section", "text": {"type": "mrkdwn", "text": text}}]
