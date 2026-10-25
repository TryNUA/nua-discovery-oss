"""Granola adapter: webhook signatures, the public REST API, and notes as standard transcript files.

Docs: https://docs.granola.ai/webhooks and https://docs.granola.ai/api-reference/openapi.json

A note's transcript is a list of items `{speaker: {source, attribution?, diarization_label?, name?},
text, start_time, end_time}` with absolute ISO times. The standard file shows each turn as an
`[m:ss]` offset from the first item. Consecutive items from the same speaker become one turn,
except in single-stream notes, where every item is "Me" and the item breaks are the only structure.

Some small helpers here (HTTP retry, clock format, timezone, turn merge, attendee list) are also
used by nua/ingest/circleback.py.
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import re
import time
from datetime import datetime, timezone
from typing import Any, Callable, Iterator, Mapping
from urllib.parse import quote
from zoneinfo import ZoneInfo

import httpx

from nua.config import Config

DEFAULT_BASE_URL = "https://public-api.granola.ai/v1"
NOTES_PAGE_SIZE = 30          # API maximum for GET /notes
TRANSCRIPT_PAGE_SIZE = 100    # API maximum for GET /notes/{id}/transcript
MAX_PAGES = 1000              # guard against a cursor that never ends

TurnRow = tuple[str | None, str | None, str]   # (time or None, speaker, text): what parse.write_standard takes


# ---------- webhook ----------

def verify_webhook(secret: str, headers: Mapping[str, str], body: bytes, *, now: float | None = None,
                   tolerance_s: int = 300) -> bool:
    """Check a Standard Webhooks signature from Granola.

    The signed content is `{webhook-id}.{webhook-timestamp}.{raw body}`, keyed with the base64 part of
    `secret` after `whsec_`. `webhook-signature` holds one or more space-separated `v1,<base64>` values.
    A timestamp more than `tolerance_s` seconds from `now` is rejected, so a replayed delivery fails.
    Header names are matched case-insensitively. Returns False on anything missing or malformed.
    """
    if not secret:
        return False
    h = {k.lower(): v for k, v in headers.items()}
    msg_id, ts, sigs = h.get("webhook-id"), h.get("webhook-timestamp"), h.get("webhook-signature")
    if not msg_id or not ts or not sigs:
        return False
    try:
        ts_int = int(ts)
    except ValueError:
        return False
    current = time.time() if now is None else now
    if abs(current - ts_int) > tolerance_s:
        return False
    # Decode as leniently as Granola's Node sample does: either base64 alphabet, padding optional.
    raw_secret = secret[len("whsec_"):] if secret.startswith("whsec_") else secret
    raw_secret = raw_secret.strip().replace("-", "+").replace("_", "/")
    try:
        key = base64.b64decode(raw_secret + "=" * (-len(raw_secret) % 4))
    except (binascii.Error, ValueError):
        return False
    if not key:
        return False
    signed = f"{msg_id}.{ts}.".encode() + body
    expected = base64.b64encode(hmac.new(key, signed, hashlib.sha256).digest()).decode()
    for versioned in sigs.split():
        version, _, sig = versioned.partition(",")
        if version == "v1" and hmac.compare_digest(sig.encode(), expected.encode()):
            return True
    return False


# ---------- API client ----------

class GranolaClient:
    """Read-only client for the Granola public API (workspace or personal key, `grn_...`).

    Errors surface as httpx exceptions (`HTTPStatusError` for a bad status after retries).
    A 429, a 5xx or a network error is retried twice with a short wait.
    """

    def __init__(self, api_key: str, *, base_url: str = DEFAULT_BASE_URL, http: httpx.Client | None = None):
        self.base_url = base_url.rstrip("/")
        self.http = http or httpx.Client(timeout=10.0)
        self.headers = {"Authorization": f"Bearer {api_key}", "Accept": "application/json"}
        self.sleep: Callable[[float], None] = time.sleep

    def _url(self, *parts: str) -> str:
        return self.base_url + "".join("/" + quote(p, safe="") for p in parts)

    def get_note(self, note_id: str) -> dict:
        """Fetch a note with its transcript under `note["transcript"]`.

        Asks for `include=transcript`. When Granola answers 413 (TRANSCRIPT_TOO_LARGE), fetches the
        note without it and pages `/notes/{id}/transcript` instead.
        """
        resp = _get(self.http, self._url("notes", note_id), headers=self.headers,
                    params={"include": "transcript"}, sleep=self.sleep)
        if resp.status_code != 413:
            resp.raise_for_status()
            return resp.json()
        resp = _get(self.http, self._url("notes", note_id), headers=self.headers, sleep=self.sleep)
        resp.raise_for_status()
        note = resp.json()
        note["transcript"] = self._paged_transcript(note_id)
        return note

    def _paged_transcript(self, note_id: str) -> list[dict]:
        items: list[dict] = []
        params: dict[str, Any] = {"page_size": TRANSCRIPT_PAGE_SIZE}
        seen: set[str] = set()
        for _ in range(MAX_PAGES):
            resp = _get(self.http, self._url("notes", note_id, "transcript"), headers=self.headers,
                        params=params, sleep=self.sleep)
            resp.raise_for_status()
            data = resp.json() or {}
            items.extend(data.get("transcript") or [])
            cursor = data.get("cursor")
            if not data.get("hasMore") or not cursor or cursor in seen:
                break
            seen.add(cursor)
            params = {"page_size": TRANSCRIPT_PAGE_SIZE, "cursor": cursor}
        return items

    def list_notes(self, *, created_after: str | None = None) -> Iterator[dict]:
        """Yield every note summary the key can see (id, title, owner, created_at, updated_at),
        optionally only those created after `created_after` (ISO date or datetime)."""
        base: dict[str, Any] = {"page_size": NOTES_PAGE_SIZE}
        if created_after:
            base["created_after"] = _utc_param(created_after)
        params = dict(base)
        seen: set[str] = set()
        for _ in range(MAX_PAGES):
            resp = _get(self.http, self._url("notes"), headers=self.headers, params=params, sleep=self.sleep)
            resp.raise_for_status()
            data = resp.json() or {}
            yield from data.get("notes") or []
            cursor = data.get("cursor")
            if not data.get("hasMore") or not cursor or cursor in seen:
                return
            seen.add(cursor)
            params = {**base, "cursor": cursor}


# ---------- standard transcript file ----------

def note_to_standard(note: dict, *, config: Config, version: int = 1,
                     ingested_at: str | None = None) -> tuple[str, str]:
    """Render a Granola note (as `get_note` returns it) as a standard transcript file.

    Returns `(repo-relative path, file text)`. The path is `transcripts/granola_<id>.md`, or
    `transcripts/granola_<id>_v<n>.md` for a later version of the same note (see SPEC `note.edited`).
    Front-matter: source, source_id, title, start (calendar start, else first transcript item,
    else created_at, in the pipeline timezone), start_source, recorded_by (the owner's person key,
    else the owner's email), attendees, speaker_labels and, when given, `ingested_at` (when the
    receiver or the sweep wrote the file, ISO in the configured time zone; the duplicate rule's arrival
    order).
    The notes section holds the Granola summary, which is context only. Missing fields are tolerated.
    """
    from nua import parse   # Task 1 module; imported late so this module loads on its own

    tz = config.pipeline.timezone
    items = [i for i in (note.get("transcript") or []) if isinstance(i, dict)]
    labels = speaker_labels(items)
    turns = _note_turns(items, labels)
    start, start_source = _note_start(note, items, tz)
    owner = note.get("owner") or {}
    person = config.person(owner.get("email")) or config.person(owner.get("name"))
    calendar = note.get("calendar_event") or {}
    attendees = _attendee_list(
        list(note.get("attendees") or [])
        + [{"name": None, "email": i.get("email")} for i in (calendar.get("invitees") or []) if isinstance(i, dict)]
        + ([owner] if owner else []))
    fm = {
        "source": "granola",
        "source_id": note.get("id"),
        "title": note.get("title") or calendar.get("event_title"),
        "start": start,
        "start_source": start_source,
        "recorded_by": person.key if person else (owner.get("email") or owner.get("name")),
        "attendees": attendees,
        "speaker_labels": labels,
    }
    if ingested_at:
        fm["ingested_at"] = ingested_at
    notes = note.get("summary_markdown") or note.get("summary_text") or ""
    suffix = "" if version <= 1 else f"_v{version}"
    rel = f"transcripts/granola_{_file_id(note.get('id'))}{suffix}.md"
    return rel, parse.write_standard(fm, notes, turns)


def note_time(note: dict) -> datetime | None:
    """When the call counts as having happened, for `ingest_cutoff`: the later of the note's creation and
    its calendar start. Granola creates a note when a meeting is opened ahead of time, so a call held
    after the cutoff can have a note created before it; judged by creation alone it would be skipped.
    None when the note gives neither time."""
    times = []
    for value in (note.get("created_at"), (note.get("calendar_event") or {}).get("scheduled_start_time")):
        if not value:
            continue
        try:
            t = datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
        except ValueError:
            continue
        times.append(t if t.tzinfo else t.replace(tzinfo=timezone.utc))
    return max(times) if times else None


def transcript_length(note: dict) -> int:
    """Characters of transcript text in a note, for deciding whether `note.edited` brought a longer
    transcript. Equals the summed turn text lengths of the file `note_to_standard` writes, so it can
    be compared with `sum(len(t.text) for t in parse(stored).turns)`."""
    items = [i for i in (note.get("transcript") or []) if isinstance(i, dict)]
    return sum(len(text) for _, _, text in _note_turns(items, speaker_labels(items)))


def has_transcript(note: dict) -> bool:
    """Whether a note carries at least one transcript item with text. Granola may return
    `transcript: null`; a file written then would parse to no turns and could never be repaired."""
    return transcript_length(note) > 0


def speaker_labels(items: list[dict]) -> str:
    """How far to trust who said what: `named` when every item has a resolved name; `single_stream`
    when one label covers everything (speakerphone or in-person on a laptop); `diarized` for iOS
    Speaker A/B labels; otherwise `me_them`."""
    kept = [i for i in items if _clean_text(i.get("text"))]
    if not kept:
        return "single_stream"
    speakers = [i.get("speaker") or {} for i in kept]
    if all(_clean_speaker(s.get("name")) for s in speakers):
        return "named"
    if len({_item_speaker(i) for i in kept}) == 1:
        return "single_stream"
    if any(s.get("diarization_label") for s in speakers):
        return "diarized"
    return "me_them"


def _item_speaker(item: dict) -> str:
    s = item.get("speaker") or {}
    name = _clean_speaker(s.get("name")) or _clean_speaker(s.get("diarization_label"))
    if name:
        return name
    if s.get("attribution") in ("me", "them"):
        return "Me" if s["attribution"] == "me" else "Them"
    return "Them" if s.get("source") == "speaker" else "Me"


def _note_turns(items: list[dict], labels: str) -> list[TurnRow]:
    first = next((t for t in (_parse_time(i.get("start_time")) for i in items) if t), None)
    raw: list[TurnRow] = []
    for item in items:
        text = _clean_text(item.get("text"))
        if not text:
            continue
        at = _parse_time(item.get("start_time"))
        clock = _clock((at - first).total_seconds()) if at and first else None
        raw.append((clock, _item_speaker(item), text))
    return raw if labels == "single_stream" else _merge_turns(raw)


def _note_start(note: dict, items: list[dict], tz: str) -> tuple[str | None, str | None]:
    calendar = note.get("calendar_event") or {}
    if calendar.get("scheduled_start_time"):
        return _local_iso(calendar["scheduled_start_time"], tz), "calendar"
    first = next((i.get("start_time") for i in items if i.get("start_time")), None)
    if first:
        return _local_iso(first, tz), "transcript"
    if note.get("created_at"):
        return _local_iso(note["created_at"], tz), "created"
    return None, None


# ---------- helpers shared with circleback.py ----------

_RETRY_STATUS = {408, 429, 500, 502, 503, 504}
_MAX_WAIT_S = 2.0   # stays well inside the 15s webhook budget


def _utc_param(value: str) -> str:
    """A date or datetime as Granola's list filters accept it. Granola answers 400 "Invalid date" to a
    UTC offset such as -07:00, so a datetime goes out in UTC with a Z; a date-only value goes as is."""
    text = str(value).strip()
    if "T" not in text:
        return text
    dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        return text
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _get(http: httpx.Client, url: str, *, headers: Mapping[str, str], params: Mapping[str, Any] | None = None,
         sleep: Callable[[float], None] = time.sleep, attempts: int = 3) -> httpx.Response:
    """GET with up to `attempts` tries (at least one) on 408, 429, 5xx and network errors. Returns the
    last response; the caller decides what a non-2xx status means."""
    attempt = 0
    while True:
        last = attempt >= attempts - 1
        try:
            resp = http.get(url, params=params, headers=headers)
        except httpx.TransportError:
            if last:
                raise
            sleep(_wait(attempt, None))
        else:
            if resp.status_code not in _RETRY_STATUS or last:
                return resp
            sleep(_wait(attempt, resp.headers.get("retry-after")))
        attempt += 1


def _wait(attempt: int, retry_after: str | None) -> float:
    try:
        wanted = float(retry_after) if retry_after else 0.5 * (2 ** attempt)
    except ValueError:
        wanted = 0.5 * (2 ** attempt)
    return max(0.0, min(wanted, _MAX_WAIT_S))


def _clock(seconds: float) -> str:
    """Seconds from the start of the call as `m:ss`, or `h:mm:ss` from one hour on."""
    s = max(0, int(seconds))
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    return f"{h}:{m:02d}:{sec:02d}" if h else f"{m}:{sec:02d}"


def _parse_time(value: Any) -> datetime | None:
    if not value or not isinstance(value, str):
        return None
    try:
        dt = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _local_iso(value: Any, tz: str) -> str | None:
    """An ISO datetime converted to `tz`, with offset and whole seconds. Unparseable input is kept as is."""
    dt = _parse_time(value)
    if dt is None:
        return str(value) if value else None
    return dt.astimezone(ZoneInfo(tz)).isoformat(timespec="seconds")


def _clean_text(text: Any) -> str:
    """One line of text: every run of whitespace (newlines included) becomes one space."""
    return " ".join(str(text).split()) if text else ""


def _clean_speaker(name: Any) -> str:
    """A speaker label that fits the `**Name**:` turn markup: one line, no asterisks or colons at the end."""
    return _clean_text(str(name).replace("*", "")).rstrip(":").strip() if name else ""


def _merge_turns(turns: list[TurnRow]) -> list[TurnRow]:
    """Join consecutive turns by the same speaker, keeping the first one's time."""
    out: list[TurnRow] = []
    for clock, speaker, text in turns:
        if out and out[-1][1] == speaker:
            prev_clock, _, prev_text = out[-1]
            out[-1] = (prev_clock or clock, speaker, f"{prev_text} {text}")
        else:
            out.append((clock, speaker, text))
    return out


def _attendee_list(people: list[Any]) -> list[dict]:
    """`[{name, email}]` without duplicates (by email, else by name), filling a missing name from a later copy."""
    out: list[dict] = []
    for p in people:
        if not isinstance(p, dict):
            continue
        name = (p.get("name") or "").strip() or None
        email = (p.get("email") or "").strip() or None
        if not name and not email:
            continue
        if email:
            match = next((a for a in out if (a["email"] or "").lower() == email.lower()), None)
        else:
            match = next((a for a in out if a["name"] == name), None)
        if match is None:
            out.append({"name": name, "email": email})
        elif not match["name"] and name:
            match["name"] = name
    return out


def _file_id(source_id: Any) -> str:
    """A recorder id made safe for a file name."""
    return re.sub(r"[^A-Za-z0-9_-]", "-", str(source_id or "unknown"))
