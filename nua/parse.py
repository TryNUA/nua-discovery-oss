"""Parse transcript files into ParsedTranscript, and write the standard transcript format.

Three input shapes:
- standard: written by the receiver or the sweep. YAML front-matter with `source`, a
  "## Recorder notes" section, then "## Transcript" with one turn per line.
- circleback_copy: a Circleback page saved as-is. The AI overview, then
  "[m:ss] **Name**: text" turn lines.
- granola_export: a Granola text export. The AI notes, a footer (Meeting Title, Date,
  Meeting participants), a line "Transcript:", then a block with Me:/Them:/Speaker A: labels.

The recorder notes are context only. They are split off here so quote checks only ever see
the body. Every turn's offsets point into the raw file: raw[t.start:t.end] == t.text.
"""
from __future__ import annotations

import re
from datetime import date, datetime
from typing import Any
from zoneinfo import ZoneInfo

from nua import frontmatter
from nua.types import Attendee, ParsedTranscript, Turn

TIMEZONE = "America/Los_Angeles"      # the default; callers pass config.pipeline.timezone
NOTES_HEADING = "## Recorder notes (context only, not evidence)"
TRANSCRIPT_HEADING = "## Transcript"
LIVE_SOURCES = ("granola", "circleback")
SPEAKER_LABELS = ("named", "me_them", "diarized", "single_stream")
UNKNOWN_SPEAKER = "Unknown"   # written when a standard turn has neither time nor speaker
FOOTER_LINES = 12             # a Granola footer sits in the lines just above "Transcript:"

_TIME = r"\d+:\d{2}(?::\d{2})?"
_TIME_FULL = re.compile(_TIME)
_BOLD_NAME = r"\*\*(?P<{g}>[^*\r\n]+?)(?::\*\*|\*\*:)"   # **Name**: or **Name:**

# "[m:ss] **Name**: text", the Circleback copy turn line
_TIMED_TURN = re.compile(
    rf"^\[(?P<time>{_TIME})\][ \t]*{_BOLD_NAME.format(g='speaker')}[ \t]*", re.M)

# standard turn lines: "[m:ss] **Name**: text", "[m:ss] text" or "**Name**: text"
_STANDARD_TURN = re.compile(
    rf"^(?:\[(?P<time>{_TIME})\][ \t]*(?:{_BOLD_NAME.format(g='speaker')})?"
    rf"|{_BOLD_NAME.format(g='speaker2')})[ \t]*", re.M)

# Granola labels, at a line start or inline after whitespace: "Me:", "Them:", "Speaker A:"
_GRANOLA_LABEL = re.compile(r"(?<![^\s])(?P<speaker>Me|Them|Speaker (?:[A-Z]|\d{1,2})):(?=\s|$)")
_SPEAKER_X = re.compile(r"Speaker (?:[A-Z]|\d{1,2})")

_TRANSCRIPT_MARKER = re.compile(r"^Transcript:[ \t]*\r?$", re.M)
_TRANSCRIPT_HEADING = re.compile(rf"^{re.escape(TRANSCRIPT_HEADING)}[ \t]*\r?$", re.M)
_NOTES_HEADING = re.compile(r"^## Recorder notes\b[^\n]*$", re.M)

_FOOTER_KEYS = {
    "title": re.compile(r"^\s*Meeting Title:[ \t]*(.*?)\s*$"),
    "date": re.compile(r"^\s*Date:[ \t]*(.*?)\s*$"),
    "participants": re.compile(r"^\s*Meeting participants:[ \t]*(.*?)\s*$"),
}
_MONTHS = ("january", "february", "march", "april", "may", "june", "july", "august",
           "september", "october", "november", "december")
_MONTH_DAY = re.compile(
    r"\b(?P<mon>[A-Za-z]{3,9})\.?\s+(?P<day>\d{1,2})(?:st|nd|rd|th)?\b(?:,?\s+(?P<year>\d{4})\b)?")
_ISO_DATE = re.compile(r"\b(\d{4}-\d{2}-\d{2})\b")
_FILENAME_DATE = re.compile(r"(\d{4}-\d{2}-\d{2})")


class ParseError(Exception):
    """The file is not one of the three known shapes, or it has no turns."""


# ---------- public ----------

def parse(path: str, raw: str, *, commit_date: str | None = None, tz: str = TIMEZONE) -> ParsedTranscript:
    """Parse one transcript file.

    `path` is repo-relative (its filename may carry a YYYY-MM-DD date). `commit_date`
    (YYYY-MM-DD) anchors the year of a Granola footer date; it defaults to today in `tz`. `tz` is the
    configured time zone (`config.pipeline.timezone`): the call date is the date of `start` there.
    Raises ParseError for an unrecognized shape, a transcript with no turns, or metadata that cannot
    be read. It raises nothing else for any file content: callers such as the duplicate check parse
    every transcript in the repo, so one odd dropped file must never fail them.
    """
    fm, fm_end = _front_matter(raw)
    footer: dict[str, str] = {}
    if "source" in fm:
        shape = "standard"
        notes, body_start, body_end, turns = _parse_standard(raw, fm_end)
    elif _TIMED_TURN.search(raw, fm_end):
        shape = "circleback_copy"
        notes, body_start, body_end, turns = _parse_circleback_copy(raw, fm_end)
    else:
        markers = list(_TRANSCRIPT_MARKER.finditer(raw, fm_end))
        if not markers:
            raise ParseError(f"{path}: unrecognized transcript shape (no front-matter source, "
                             "no [m:ss] **Name**: turn lines, no Transcript: line)")
        shape = "granola_export"
        notes, body_start, body_end, turns, footer = _parse_granola_export(raw, fm_end, markers[-1])

    if not turns:
        raise ParseError(f"{path}: {shape} transcript has no turns")
    try:
        return _transcript(path, raw, shape, fm, footer, notes, body_start, body_end, turns, commit_date, tz)
    except (TypeError, ValueError, OverflowError, AttributeError) as e:
        # Backstop: every field below is read leniently, but a value nothing anticipated still makes
        # this one file a parse failure (it gets its parse_failed stub) instead of an error that escapes.
        raise ParseError(f"{path}: could not read the call metadata ({type(e).__name__}: {e})") from e


def _transcript(path: str, raw: str, shape: str, fm: dict[str, Any], footer: dict[str, str], notes: str,
                body_start: int, body_end: int, turns: list[Turn], commit_date: str | None,
                tz: str = TIMEZONE) -> ParsedTranscript:
    """The ParsedTranscript, with its metadata read from the front-matter and the Granola footer."""
    source = fm.get("source")
    source = str(source).strip() if source is not None else None
    start = _iso(fm.get("start"))
    day, day_source = _call_date(path, start, footer.get("date"), commit_date, tz)

    labels = fm.get("speaker_labels")
    if shape == "circleback_copy":
        labels = "named"
    elif labels not in SPEAKER_LABELS:
        labels = _infer_labels([t.speaker for t in turns])

    attendees = _attendees(fm.get("attendees"))
    if not attendees and footer.get("participants"):
        attendees = _footer_attendees(footer["participants"])

    return ParsedTranscript(
        path=path,
        raw=raw,
        shape=shape,
        front_matter=fm,
        notes=notes,
        body_start=body_start,
        body_end=body_end,
        turns=turns,
        speaker_labels=labels,
        origin="live" if (source or "").lower() in LIVE_SOURCES else "backfill",
        source=source,
        source_id=_str_or_none(fm.get("source_id")),
        ical_uid=_str_or_none(fm.get("ical_uid")),
        start=start,
        start_source=_str_or_none(fm.get("start_source")),
        date=day,
        date_source=day_source,
        recorded_by=_str_or_none(fm.get("recorded_by")),
        attendees=attendees,
        title=_str_or_none(fm.get("title")) or footer.get("title") or None,
    )


def write_standard(front_matter: dict, notes: str, turns: list[tuple[str | None, str | None, str]]) -> str:
    """Render a transcript file in the standard format.

    `turns` are (time "m:ss" or "h:mm:ss" or None, speaker or None, text). Line breaks inside a
    turn's text become single spaces so every turn is one line. A turn with neither time nor
    speaker is written with speaker "Unknown". Raises ValueError on a malformed time.
    """
    lines = [_turn_line(time, speaker, text) for time, speaker, text in turns]
    notes = (notes or "").strip()
    body = f"{NOTES_HEADING}\n\n"
    if notes:
        body += f"{notes}\n\n"
    body += f"{TRANSCRIPT_HEADING}\n\n"
    if lines:
        body += "\n\n".join(lines) + "\n"
    return frontmatter.join(front_matter, body)


def local_date(iso_datetime: str, tz: str = TIMEZONE) -> str:
    """Calendar date (YYYY-MM-DD) of an ISO datetime in `tz`, the configured time zone.

    In America/Los_Angeles, "2026-09-18T23:30:00-07:00" -> "2026-09-18" and
    "2026-09-19T05:30:00Z" -> "2026-09-18"; in Asia/Tokyo both are "2026-09-19".
    A datetime without an offset is taken as being in `tz` already. Raises ValueError when unparseable,
    or when the date in `tz` falls outside the years 1 to 9999.
    """
    dt = datetime.fromisoformat(str(iso_datetime).strip())
    if dt.tzinfo is not None:
        try:
            dt = dt.astimezone(ZoneInfo(tz))
        except OverflowError as e:          # "0001-01-01T00:00:00+14:00" is a day before year 1 in `tz`
            raise ValueError(f"{iso_datetime!r} is out of range in {tz}") from e
    return dt.date().isoformat()


# ---------- shapes ----------

def _front_matter(raw: str) -> tuple[dict[str, Any], int]:
    """(front-matter dict, offset where the rest of the file starts). Front-matter that cannot be
    loaded counts as none: invalid YAML, and also YAML that PyYAML fails on with an error that is not a
    YAMLError (a ValueError for `date: !!int 12x`, a RecursionError for very deep nesting)."""
    try:
        fm, _, offset = frontmatter.split(raw)
    except Exception:   # yaml.YAMLError, ValueError, RecursionError: a dropped file must still parse
        return {}, 0
    return fm, offset


def _parse_standard(raw: str, fm_end: int) -> tuple[str, int, int, list[Turn]]:
    headings = list(_TRANSCRIPT_HEADING.finditer(raw, fm_end))
    if not headings:
        raise ParseError(f"standard transcript has no '{TRANSCRIPT_HEADING}' heading")
    heading = headings[-1]   # the recorder notes may contain their own "## Transcript"
    notes_start = fm_end
    notes_heading = _NOTES_HEADING.search(raw, fm_end, heading.start())
    if notes_heading:
        notes_start = notes_heading.end()
    notes = raw[notes_start:heading.start()].strip()
    body_start, body_end = _strip_span(raw, heading.end(), len(raw))
    headers = [(m.start(), m.end(), m.group("time"), m.group("speaker") or m.group("speaker2"))
               for m in _STANDARD_TURN.finditer(raw, body_start, body_end)]
    return notes, body_start, body_end, _build_turns(raw, headers, body_start, body_end)


def _parse_circleback_copy(raw: str, fm_end: int) -> tuple[str, int, int, list[Turn]]:
    first = _TIMED_TURN.search(raw, fm_end)
    body_start = first.start()
    body_end = len(raw.rstrip())
    notes = raw[fm_end:body_start].strip()
    headers = [(m.start(), m.end(), m.group("time"), m.group("speaker"))
               for m in _TIMED_TURN.finditer(raw, body_start, body_end)]
    return notes, body_start, body_end, _build_turns(raw, headers, body_start, body_end)


def _parse_granola_export(raw: str, fm_end: int, marker: re.Match
                          ) -> tuple[str, int, int, list[Turn], dict[str, str]]:
    notes_region = raw[fm_end:marker.start()]
    notes = notes_region.strip()
    body_start, body_end = _strip_span(raw, marker.end(), len(raw))
    headers = [(m.start(), m.end(), None, m.group("speaker"))
               for m in _GRANOLA_LABEL.finditer(raw, body_start, body_end)]
    turns = _build_turns(raw, headers, body_start, body_end)
    return notes, body_start, body_end, turns, _footer(notes_region)


def _build_turns(raw: str, headers: list[tuple[int, int, str | None, str | None]],
                 body_start: int, body_end: int) -> list[Turn]:
    """Turns from sorted turn headers (header start, header end, time, speaker).

    A turn's text runs from its header to the next header, trimmed. Text before the first
    header becomes a turn with no time and no speaker, so no body text is left out.
    """
    if not headers:
        return []
    segments: list[tuple[int, int, str | None, str | None]] = []
    if raw[body_start:headers[0][0]].strip():
        segments.append((body_start, headers[0][0], None, None))
    for i, (_, header_end, time, speaker) in enumerate(headers):
        next_start = headers[i + 1][0] if i + 1 < len(headers) else body_end
        segments.append((header_end, next_start, time, speaker))
    turns = []
    for index, (a, b, time, speaker) in enumerate(segments, start=1):
        start, end = _strip_span(raw, a, b)
        speaker = speaker.strip() if speaker else None
        turns.append(Turn(index=index, time=time, speaker=speaker or None,
                          text=raw[start:end], start=start, end=end))
    return turns


def _strip_span(raw: str, a: int, b: int) -> tuple[int, int]:
    """Offsets of raw[a:b] with surrounding whitespace removed; (a, a) when it is all whitespace."""
    segment = raw[a:b]
    stripped = segment.strip()
    if not stripped:
        return a, a
    start = a + len(segment) - len(segment.lstrip())
    return start, start + len(stripped)


def _infer_labels(speakers: list[str | None]) -> str:
    """speaker_labels from the speaker names when the file does not say."""
    distinct = {s for s in speakers if s}
    generic = all(s in ("Me", "Them") or _SPEAKER_X.fullmatch(s) for s in distinct)
    if not generic:
        return "named"
    if len(distinct) <= 1:
        return "single_stream"
    if any(_SPEAKER_X.fullmatch(s) for s in distinct):
        return "diarized"
    return "me_them"


# ---------- Granola footer ----------

def _footer(notes_region: str) -> dict[str, str]:
    """Meeting Title / Date / Meeting participants from the lines just above "Transcript:".

    Only the last FOOTER_LINES lines count, and only those after a "---" separator when one is
    there, so a "Date:" line inside the AI notes is never read as the call date.
    """
    lines = notes_region.splitlines()[-FOOTER_LINES:]
    for i in range(len(lines) - 1, -1, -1):
        if lines[i].strip() == "---":
            lines = lines[i + 1:]
            break
    found: dict[str, str] = {}
    for line in lines:
        for key, rx in _FOOTER_KEYS.items():
            m = rx.match(line)
            if m and m.group(1):
                found[key] = m.group(1)
    return found


def _footer_attendees(participants: str) -> list[Attendee]:
    """Names from a footer "Meeting participants:" value."""
    names = [re.sub(r"\s*<[^>]*>", "", part).strip() for part in re.split(r"[,;]", participants)]
    return [Attendee(name=n) for n in names if n]


def _parse_footer_date(value: str, commit_date: str | None, tz: str = TIMEZONE) -> str | None:
    """A footer date like "Sep 18" (no year: the latest year on or before commit_date)."""
    m = _ISO_DATE.search(value)
    if m:
        try:
            return date.fromisoformat(m.group(1)).isoformat()
        except ValueError:
            return None
    for m in _MONTH_DAY.finditer(value):
        mon = m.group("mon").lower()
        month = next((i for i, name in enumerate(_MONTHS, start=1) if name.startswith(mon)), None)
        if month is None:
            continue
        day = int(m.group("day"))
        if m.group("year"):
            try:
                return date(int(m.group("year")), month, day).isoformat()
            except ValueError:
                return None
        ref = date.fromisoformat(commit_date) if commit_date else datetime.now(ZoneInfo(tz)).date()
        for year in range(ref.year, ref.year - 9, -1):
            try:
                d = date(year, month, day)
            except ValueError:
                continue
            if d <= ref:
                return d.isoformat()
        return None
    return None


# ---------- metadata ----------

def _call_date(path: str, start: str | None, footer_date: str | None,
               commit_date: str | None, tz: str = TIMEZONE) -> tuple[str | None, str]:
    """(date, date_source): front-matter start (its date in `tz`), then filename prefix, then footer,
    else unknown."""
    if start:
        try:
            return local_date(start, tz), "api"
        except ValueError:
            pass
    name = path.replace("\\", "/").rsplit("/", 1)[-1]
    m = _FILENAME_DATE.match(name)
    if m:
        try:
            return date.fromisoformat(m.group(1)).isoformat(), "filename"
        except ValueError:
            pass
    if footer_date:
        d = _parse_footer_date(footer_date, commit_date, tz)
        if d:
            return d, "footer"
    return None, "unknown"


def _attendees(value: Any) -> list[Attendee]:
    """Attendees from front-matter: a list of {name, email} dicts (plain strings tolerated).

    Anything else, such as a dropped file's `attendees: 3`, `yes` or a date, counts as none.
    """
    if isinstance(value, (str, dict)):
        value = [value]
    if not isinstance(value, (list, tuple)):
        return []
    out = []
    for a in value:
        if isinstance(a, dict):
            out.append(Attendee(name=_str_or_none(a.get("name")), email=_str_or_none(a.get("email"))))
        elif isinstance(a, str) and a.strip():
            s = a.strip()
            out.append(Attendee(email=s) if "@" in s and " " not in s else Attendee(name=s))
    return out


def _iso(value: Any) -> str | None:
    """A front-matter timestamp as an ISO string (YAML may have parsed it into a datetime)."""
    if value is None or value == "":
        return None
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    return str(value).strip()


def _str_or_none(value: Any) -> str | None:
    if value is None:
        return None
    s = str(value).strip()
    return s or None


# ---------- writing ----------

def _turn_line(time: str | None, speaker: str | None, text: str) -> str:
    text = re.sub(r"\s*[\r\n]+\s*", " ", text or "").strip()
    speaker = _clean_speaker(speaker)
    if time is not None:
        time = str(time).strip()
        if not _TIME_FULL.fullmatch(time):
            raise ValueError(f"bad turn time {time!r}: expected m:ss or h:mm:ss")
    if time and speaker:
        line = f"[{time}] **{speaker}**: {text}"
    elif time:
        line = f"[{time}] {text}"
    else:
        line = f"**{speaker or UNKNOWN_SPEAKER}**: {text}"
    return line.rstrip()


def _clean_speaker(speaker: str | None) -> str | None:
    """A speaker name safe to put inside **...**: no asterisks, no line breaks, no trailing colon."""
    if speaker is None:
        return None
    s = re.sub(r"\s+", " ", str(speaker).replace("*", "")).strip().rstrip(":").strip()
    return s or None
