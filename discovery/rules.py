"""Call-level rules: internal calls, duplicate detection, which copy wins, teammate invites.

See SPEC "Duplicates" and "Internal calls".

Arrival order: the receiver and the sweep stamp every transcript file they create with
`ingested_at` in its front-matter (`ingest_stamp`). Files are create-only, so the stamp records the
first delivery. Dropped files and files written before the stamp existed have none.
"""
from __future__ import annotations

import re
from datetime import date, datetime, tzinfo
from pathlib import PurePosixPath
from typing import Any, Iterable
from zoneinfo import ZoneInfo

from discovery.config import Config, Person
from discovery.types import Attendee, ParsedTranscript

_GRANOLA_FILE = re.compile(r"granola_(?P<note>.+?)(?:_v(?P<version>\d+))?\.md")
INGESTED_AT = "ingested_at"      # front-matter key written by the receiver and the sweep
TIMEZONE = "America/Los_Angeles"


def _emails(attendees: list[Attendee]) -> set[str]:
    """Lowercased attendee emails, blanks dropped."""
    return {a.email.strip().lower() for a in attendees if a.email and a.email.strip()}


def _cofounder_by_email(email: str | None, config: Config) -> Person | None:
    """The teammate in config/people.yml whose email is `email` (case-insensitive), else None."""
    if not email:
        return None
    e = email.strip().lower()
    return next((p for p in config.people if p.email and p.email.strip().lower() == e), None)


def _is_internal(email: str | None, config: Config) -> bool:
    """True for a team email: one on a `internal_email_domains` domain or a teammate's email from people.yml."""
    return config.is_internal_email(email) or _cofounder_by_email(email, config) is not None


def is_internal(attendees: list[Attendee], config: Config) -> bool:
    """True when the call lists two or more distinct attendees and every one has a team email.

    A team email is one on a `internal_email_domains` domain or a teammate's email from people.yml.
    A call with only the owner listed (phone, in-person, ad-hoc) is not internal, and an
    attendee with no email makes the call external.
    """
    if len(attendees) < 2:
        return False
    for a in attendees:
        if not _is_internal(a.email, config):
            return False
    return len(_emails(attendees)) >= 2


def note_version(path: str) -> tuple[str | None, int]:
    """Granola note id and version from a transcript filename.

    "transcripts/granola_abc.md" -> ("abc", 1), "transcripts/granola_abc_v2.md" -> ("abc", 2).
    Any other file -> (None, 1).
    """
    m = _GRANOLA_FILE.fullmatch(PurePosixPath(path).name)
    if not m:
        return None, 1
    return m.group("note"), int(m.group("version") or 1)


def _start(p: ParsedTranscript, tz: ZoneInfo) -> datetime | None:
    """The transcript's start as an aware datetime; a naive value is read in `tz`."""
    if not p.start:
        return None
    try:
        dt = datetime.fromisoformat(str(p.start))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=tz)


def _pacific_date(p: ParsedTranscript, tz: ZoneInfo) -> str | None:
    """Call date in `tz`, from `start` when known, else the parsed `date`. A start whose date in `tz`
    falls outside the years 1 to 9999 counts as unknown."""
    dt = _start(p, tz)
    if dt is not None:
        try:
            return dt.astimezone(tz).date().isoformat()
        except OverflowError:
            pass
    return p.date


def same_call(a: ParsedTranscript, b: ParsedTranscript, *, window_minutes: int = 10,
              tz: str = "America/Los_Angeles", config: Config | None = None) -> bool:
    """True when two transcripts record the same call.

    Same call when any of these hold:
    - they are versions of the same Granola note (by filename, or same source and source_id);
    - `ical_uid` is equal and both fall on the same date in `tz` (a recurring invite reuses its uid);
    - `start` times are within `window_minutes` and they share an attendee email from outside the team.

    A team email (see `is_internal`) never counts toward the shared email: the recorder is on every
    call they record (Granola adds the owner, Circleback lists the invitees), so two calls one
    teammate recorded minutes apart would otherwise always match. Pass `config` so team emails are
    known; without it every email counts.
    """
    note_a, note_b = note_version(a.path)[0], note_version(b.path)[0]
    if note_a is not None and note_a == note_b:
        return True
    if a.source and a.source == b.source and a.source_id and str(a.source_id) == str(b.source_id):
        return True

    zone = ZoneInfo(tz)
    if a.ical_uid and a.ical_uid == b.ical_uid:
        day_a, day_b = _pacific_date(a, zone), _pacific_date(b, zone)
        if day_a is not None and day_a == day_b:
            return True

    start_a, start_b = _start(a, zone), _start(b, zone)
    if start_a is None or start_b is None:
        return False
    if abs((start_a - start_b).total_seconds()) > window_minutes * 60:
        return False
    shared = _emails(a.attendees) & _emails(b.attendees)
    if config is not None:
        shared = {e for e in shared if not _is_internal(e, config)}
    return bool(shared)


def ingest_stamp(now: datetime, tz: str = TIMEZONE) -> str:
    """The `ingested_at` value for a file written at `now`: ISO with offset, whole seconds, in `tz`."""
    return now.astimezone(ZoneInfo(tz)).isoformat(timespec="seconds")


def as_datetime(value: Any, tz: str | tzinfo = TIMEZONE, *, strict: bool = True) -> datetime | None:
    """An aware datetime from an ISO string, a YAML datetime or a YAML date (read as midnight).
    Naive values are read in `tz` (a zone name or a tzinfo).

    None or "" gives None. Anything else that is not an ISO datetime raises ValueError when `strict`,
    else gives None. The receiver and the sweep read `ingest_cutoff` with it, so a date-only cutoff
    such as `2026-11-02`, which YAML loads as a date, means the same thing to both.
    """
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, date):
        dt = datetime(value.year, value.month, value.day)
    else:
        try:
            dt = datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
        except ValueError:
            if strict:
                raise ValueError(f"not an ISO datetime: {value!r}") from None
            return None
    if dt.tzinfo:
        return dt
    return dt.replace(tzinfo=ZoneInfo(tz) if isinstance(tz, str) else tz)


def ingested_at(p: ParsedTranscript, tz: str = TIMEZONE) -> datetime | None:
    """When the receiver or the sweep wrote this file (front-matter `ingested_at`), as an aware datetime.

    None when the file has no stamp (a dropped file, or one written before the stamp existed) or the
    value cannot be read. YAML may hand back a datetime; a naive value is read in `tz`.
    """
    return as_datetime((p.front_matter or {}).get(INGESTED_AT), tz, strict=False)


def _call_copy(path: str) -> tuple[str, str]:
    """The recording a transcript file belongs to: every version of a Granola note shares one, any
    other file is its own."""
    note = note_version(path)[0]
    return ("granola", note) if note is not None else ("file", path)


def _note_arrivals(copies: list[ParsedTranscript]) -> dict[str, datetime | None]:
    """Each copy's arrival stamp, where every version of a Granola note shares the note's earliest.

    A Resumed note's later version is the same recording as its first, so it arrived when the first
    did. A note with any unstamped version counts as unstamped (that version predates the stamp).
    """
    group: dict[tuple[str, str], datetime | None] = {}
    for c in copies:
        g, stamp = _call_copy(c.path), ingested_at(c)
        if g not in group:
            group[g] = stamp
        elif group[g] is not None:
            group[g] = None if stamp is None else min(group[g], stamp)
    return {c.path: group[_call_copy(c.path)] for c in copies}


def by_arrival(copies: list[ParsedTranscript], *, first: Iterable[str] = ()) -> list[str]:
    """Paths of `copies` in the order they arrived, as far as the repo can tell.

    Paths in `first` (copies known to have come earlier, such as ones that already have their own
    digest) lead, then the rest. Within each group copies are ordered by `ingested_at`, with unstamped
    copies ahead of stamped ones (they predate the stamp), then by path. Versions of the same Granola
    note move together: the note leads when any version is in `first`, every version carries the
    note's earliest `ingested_at`, and versions sit in version order.
    """
    lead = {_call_copy(p) for p in first}
    stamps = _note_arrivals(copies)
    head: dict[tuple[str, str], tuple[int, str]] = {}
    for c in copies:
        g, rank = _call_copy(c.path), (note_version(c.path)[1], c.path)
        head[g] = min(head.get(g, rank), rank)

    def key(c: ParsedTranscript) -> tuple:
        g, stamp = _call_copy(c.path), stamps[c.path]
        return (g not in lead, stamp is not None, stamp or datetime.min.replace(tzinfo=ZoneInfo(TIMEZONE)),
                head[g][1], note_version(c.path)[1], c.path)

    return [c.path for c in sorted(copies, key=key)]


def pick_winner(copies: list[ParsedTranscript], arrival_order: list[str]) -> ParsedTranscript:
    """The richest copy of one call.

    1. Among versions of the same Granola note, only the latest version stays in the running.
    2. A copy with `named` speaker labels beats every other label type.
    3. On a tie, the copy that arrived first wins: by `ingested_at` when every tied copy carries one,
       otherwise the earliest in `arrival_order` (copies missing from it rank after those in it, in
       path order).
    A Granola note's latest version arrived when the note's first version did: it carries the earliest
    `ingested_at` of any version in `copies` and the earliest `arrival_order` position of any version.
    So a Resumed note that won the tie before the Resume still wins it after.
    Raises ValueError when `copies` is empty.
    """
    if not copies:
        raise ValueError("pick_winner needs at least one copy")

    latest: dict[str, int] = {}
    for c in copies:
        note, version = note_version(c.path)
        if note is not None:
            latest[note] = max(latest.get(note, 0), version)
    contenders = []
    for c in copies:
        note, version = note_version(c.path)
        if note is None or version == latest[note]:
            contenders.append(c)

    best = min(c.speaker_labels != "named" for c in contenders)
    tied = [c for c in contenders if (c.speaker_labels != "named") == best]
    arrivals = _note_arrivals(copies)
    stamps = {c.path: arrivals[c.path] for c in tied}
    if all(stamps.values()):
        return min(tied, key=lambda c: (stamps[c.path], c.path))
    position: dict[tuple[str, str], int] = {}
    for i, path in enumerate(arrival_order):
        position.setdefault(_call_copy(path), i)
    return min(tied, key=lambda c: (position.get(_call_copy(c.path), len(arrival_order)), c.path))


def cofounder_invited(parsed: ParsedTranscript, config: Config) -> bool:
    """True when an attendee email belongs to a teammate other than the one who recorded.

    `recorded_by` may be a person key, name or email. When it is unknown, two or more distinct
    teammates among the attendees count as an invite (one of them is presumably the recorder).
    Teammates are matched by the email in config/people.yml only.
    """
    invited = {p.key for p in (_cofounder_by_email(a.email, config) for a in parsed.attendees) if p}
    recorder = config.person(parsed.recorded_by)
    if recorder is None:
        return len(invited) >= 2
    return bool(invited - {recorder.key})
