"""Circleback adapter: webhook signatures, the public REST API, and meetings as standard transcript files.

Docs: https://circleback.ai/docs/support/automations/export-meeting-data-with-webhooks and
https://circleback.ai/docs/api.

The webhook body carries the whole meeting, transcript included, as a list of segments
`{speaker, text, timestamp}` where `timestamp` is seconds from the start. It has no owner field,
so the receiver URL (`/ingest/circleback/<person>`) says who recorded it. The API returns the same
meeting without the transcript (`GET /meetings`, `GET /meeting/{id}`) plus the segments from
`GET /meeting/{id}/transcript`; merge them as `{**meeting, "transcript": segments}` before
calling `payload_to_standard`.
"""
from __future__ import annotations

import hashlib
import hmac
import time
from typing import Any, Callable, Iterator
from urllib.parse import parse_qs, quote, urljoin, urlparse

import httpx

from discovery.config import Config
from discovery.ingest.granola import (MAX_PAGES, TurnRow, _attendee_list, _clean_speaker, _clean_text, _clock, _file_id,
                                _get, _local_iso, _merge_turns)

DEFAULT_BASE_URL = "https://circleback.ai/api"
UNKNOWN_SPEAKER = "Unknown speaker"


# ---------- webhook ----------

def verify_signature(secret: str, body: bytes, signature: str) -> bool:
    """Check Circleback's `x-signature` header: the hex HMAC-SHA256 of the raw body, keyed with the
    automation's signing secret. Case and surrounding whitespace in the header are ignored.
    Circleback signs no timestamp, so this cannot detect a replay; idempotent writes cover that."""
    if not secret or not signature:
        return False
    expected = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(signature.strip().lower().encode(), expected.encode())


# ---------- API client ----------

class CirclebackClient:
    """Read-only client for the Circleback public API, with one person's key (`cb_...`).

    Lists pass `ownership=Mine`, so every meeting returned was recorded by the key's owner.
    Pagination follows the RFC 8288 `Link: <...>; rel="next"` header. Errors surface as httpx
    exceptions; a 429, a 5xx or a network error is retried twice with a short wait.
    """

    def __init__(self, api_key: str, *, base_url: str = DEFAULT_BASE_URL, http: httpx.Client | None = None):
        self.base_url = base_url.rstrip("/")
        self.http = http or httpx.Client(timeout=10.0)
        self.headers = {"Authorization": f"Bearer {api_key}", "Accept": "application/json"}
        self.sleep: Callable[[float], None] = time.sleep

    def _url(self, *parts: str) -> str:
        return self.base_url + "".join("/" + quote(p, safe="") for p in parts)

    def _json(self, url: str, params: dict[str, Any] | None = None) -> httpx.Response:
        resp = _get(self.http, url, headers=self.headers, params=params, sleep=self.sleep)
        resp.raise_for_status()
        return resp

    def _pages(self, url: str, params: dict[str, Any]) -> Iterator[list]:
        """Yield each page's list, following `rel="next"` links. A next link's cursor is re-sent
        with the original filters, in case the link itself leaves them out."""
        seen: set[str] = set()
        current_url, current_params = url, dict(params)
        for _ in range(MAX_PAGES):
            resp = self._json(current_url, current_params)
            yield _as_list(resp.json())
            nxt = (resp.links.get("next") or {}).get("url")
            if not nxt or nxt in seen:
                return
            seen.add(nxt)
            absolute = urljoin(str(resp.url), nxt)
            cursor = parse_qs(urlparse(absolute).query).get("cursor")
            if cursor:
                current_url, current_params = url, {**params, "cursor": cursor[0]}
            else:
                current_url, current_params = absolute, {}

    def tag_ids(self, name: str) -> list[int]:
        """Ids of the tags called `name` (case-insensitive)."""
        tags = _as_list(self._json(self._url("tag")).json())
        return [t["id"] for t in tags if isinstance(t, dict) and str(t.get("name", "")).lower() == name.lower()]

    def list_meetings(self, *, tag: str | None = None) -> Iterator[dict]:
        """Yield the key owner's meetings (full meeting objects without transcripts), in the
        API's order, which is undocumented. With `tag`, only meetings carrying that tag name;
        an unknown tag yields nothing."""
        params: dict[str, Any] = {"ownership": "Mine"}
        if tag:
            ids = self.tag_ids(tag)
            if not ids:
                return
            params["tagIds"] = ids
        for page in self._pages(self._url("meetings"), params):
            for meeting in page:
                if isinstance(meeting, dict) and (not tag or has_tag(meeting, tag)):
                    yield meeting

    def get_transcript(self, meeting_id: str) -> list[dict]:
        """Transcript segments `[{speaker, text, timestamp}]` in time order."""
        return _as_list(self._json(self._url("meeting", meeting_id, "transcript")).json(), key="transcript")

    def calendar_events(self, *, lookback_hours: int = 24) -> list[dict]:
        """Calendar events that started up to `lookback_hours` ago (and upcoming ones), each with
        `startTime` and, once recorded, `meetingId`. Events without a link or with one attendee
        are included, so in-person meetings can still be matched."""
        params = {"startTimeLookbackHours": lookback_hours, "includeOfflineSingleAttendee": "true"}
        events: list[dict] = []
        for page in self._pages(self._url("calendar", "events"), params):
            events.extend(e for e in page if isinstance(e, dict))
        return events


# ---------- standard transcript file ----------

def payload_to_standard(payload: dict, *, person: str, config: Config, start: str | None,
                        start_source: str, ingested_at: str | None = None) -> tuple[str, str]:
    """Render a Circleback meeting (webhook body, or API meeting plus transcript) as a standard file.

    Returns `(repo-relative path, file text)`; the path is `transcripts/circleback_<id>.md`.
    `person` is who recorded it (from the receiver URL or the API key's owner) and becomes
    `recorded_by` as a person key when `config` knows it. `start` is the calendar start from
    `find_start`; when it is None the meeting's `createdAt` is used with `start_source: created`.
    Speakers are the names Circleback gives (`named`); a missing name shows as "Unknown speaker".
    The Circleback notes go in the context-only section. `ingested_at` (when the receiver or the
    sweep wrote the file, ISO in Pacific time) goes in the front-matter when given; it is the arrival
    order the duplicate rule uses. Missing fields are tolerated.
    """
    from discovery import parse   # Task 1 module; imported late so this module loads on its own

    tz = config.pipeline.timezone
    segments = [s for s in (payload.get("transcript") or []) if isinstance(s, dict)]
    named = any(_clean_speaker(s.get("speaker")) for s in segments)
    labels = "named" if named or not segments else "single_stream"
    turns = _segment_turns(segments, labels)
    if start:
        start_iso, source = _local_iso(start, tz), start_source
    elif payload.get("createdAt"):
        start_iso, source = _local_iso(payload["createdAt"], tz), "created"
    else:
        start_iso, source = None, None
    known = config.person(person)
    calendar = payload.get("calendarEvent") or {}
    fm = {
        "source": "circleback",
        "source_id": payload.get("id"),
        "ical_uid": payload.get("icalUid") or calendar.get("icalUid") or None,
        "title": payload.get("name"),
        "start": start_iso,
        "start_source": source,
        "recorded_by": known.key if known else person,
        "attendees": _attendee_list(list(payload.get("attendees") or [])),
        "speaker_labels": labels,
    }
    if ingested_at:
        fm["ingested_at"] = ingested_at
    rel = f"transcripts/circleback_{_file_id(payload.get('id'))}.md"
    return rel, parse.write_standard(fm, payload.get("notes") or "", turns)


def has_transcript(payload: dict) -> bool:
    """Whether a meeting carries at least one transcript segment with text. The webhook sends the
    transcript only when the automation selected it, and an API meeting still processing has none;
    a file written without one would parse to no turns and could never be repaired."""
    return any(_clean_text(s.get("text")) for s in (payload.get("transcript") or []) if isinstance(s, dict))


def find_start(events: list[dict], meeting_id: str) -> str | None:
    """`startTime` of the calendar event linked to `meeting_id`, or None when no event matches."""
    for event in events or []:
        if isinstance(event, dict) and event.get("meetingId") == meeting_id:
            return event.get("startTime") or None
    return None


def has_tag(meeting: dict, tag: str) -> bool:
    """Whether a meeting carries `tag` (case-insensitive). Webhook tags are strings; API tags are
    `{id, name, description}` objects."""
    want = tag.lower()
    for t in meeting.get("tags") or []:
        name = t.get("name") if isinstance(t, dict) else t
        if isinstance(name, str) and name.lower() == want:
            return True
    return False


def _segment_turns(segments: list[dict], labels: str) -> list[TurnRow]:
    turns: list[TurnRow] = []
    for seg in segments:
        text = _clean_text(seg.get("text"))
        if not text:
            continue
        ts = seg.get("timestamp")
        clock = _clock(float(ts)) if isinstance(ts, (int, float)) else None
        turns.append((clock, _clean_speaker(seg.get("speaker")) or UNKNOWN_SPEAKER, text))
    return turns if labels == "single_stream" else _merge_turns(turns)


def _as_list(data: Any, key: str = "data") -> list:
    """A JSON array response, tolerating an object that wraps the array."""
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for k in (key, "data", "items", "meetings", "events"):
            if isinstance(data.get(k), list):
                return data[k]
    return []
