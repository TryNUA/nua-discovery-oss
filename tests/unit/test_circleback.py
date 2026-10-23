"""Tests for discovery/ingest/circleback.py: signatures, the API client and the standard file."""
from __future__ import annotations

import hashlib
import hmac
import json

import httpx
import pytest

from discovery import config as config_mod
from discovery import frontmatter
from discovery.ingest import circleback
from discovery.ingest.circleback import (CirclebackClient, find_start, has_tag, has_transcript, payload_to_standard,
                                   verify_signature)

SECRET = "cb-signing-secret-123"


def _payload() -> dict:
    """A webhook body shaped like Circleback's documented example."""
    return {
        "id": "WRweb12iCBimU6Vo8cD7z",
        "name": "Acme x Example Mutual",
        "createdAt": "2026-09-18T23:31:57.926Z",
        "duration": 1843.2,
        "url": "https://meet.google.com/abc-defgh-xyz",
        "recordingUrl": None,
        "tags": ["discovery"],
        "icalUid": "abc123@google.com",
        "attendees": [{"name": "Alex Rivera", "email": "alex.rivera@gmail.com"},
                      {"name": "Darius Kemp", "email": "darius@example.com"},
                      {"name": "Note taker", "email": None}],
        "notes": "#### Overview\n* Darius walked through dashcam footage\n\n## Transcript\n* not a real heading",
        "actionItems": [],
        "transcript": [
            {"speaker": "Alex Rivera", "text": "Thanks for taking the time.", "timestamp": 22.4},
            {"speaker": "Darius Kemp", "text": "Of course.", "timestamp": 31.0},
            {"speaker": "Darius Kemp", "text": "We pull six dashcam clips,\nhalf of them blurry.",
             "timestamp": 33.9},
            {"speaker": "Alex Rivera", "text": "Which came first?", "timestamp": 61.2},
            {"speaker": "Darius Kemp", "text": "Arch.", "timestamp": 3725.0},
        ],
        "insights": {},
    }


@pytest.fixture
def cfg(tmp_repo):
    return config_mod.load(tmp_repo)


def _parse(rel: str, text: str):
    parse_mod = pytest.importorskip("discovery.parse")
    if not hasattr(parse_mod, "write_standard"):
        pytest.skip("discovery.parse (Task 1) not implemented yet")
    return parse_mod.parse(rel, text)


# ---------- signatures ----------

def _sig(body: bytes, secret: str = SECRET) -> str:
    return hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def test_verify_signature_valid():
    body = json.dumps(_payload()).encode()
    assert verify_signature(SECRET, body, _sig(body))
    assert verify_signature(SECRET, body, _sig(body).upper())
    assert verify_signature(SECRET, body, f" {_sig(body)}\n")


def test_verify_signature_tampered():
    body = json.dumps(_payload()).encode()
    assert not verify_signature(SECRET, body.replace(b"Mutual", b"Mutable"), _sig(body))


def test_verify_signature_wrong_secret_or_empty():
    body = json.dumps(_payload()).encode()
    assert not verify_signature("other-secret", body, _sig(body))
    assert not verify_signature(SECRET, body, "")
    assert not verify_signature(SECRET, body, None)
    assert not verify_signature("", body, _sig(body, ""))


# ---------- payload to standard ----------

def test_payload_to_standard_with_speaker_names(cfg):
    rel, text = payload_to_standard(_payload(), person="alex", config=cfg,
                                    start="2026-09-18T23:00:00.000Z", start_source="calendar")
    assert rel == "transcripts/circleback_WRweb12iCBimU6Vo8cD7z.md"
    fm, body, _ = frontmatter.split(text)
    assert fm["source"] == "circleback"
    assert fm["source_id"] == "WRweb12iCBimU6Vo8cD7z"
    assert fm["ical_uid"] == "abc123@google.com"
    assert fm["title"] == "Acme x Example Mutual"
    assert fm["start"] == "2026-09-18T16:00:00-07:00"
    assert fm["start_source"] == "calendar"
    assert fm["recorded_by"] == "alex"
    assert fm["speaker_labels"] == "named"
    assert fm["attendees"] == [{"name": "Alex Rivera", "email": "alex.rivera@gmail.com"},
                               {"name": "Darius Kemp", "email": "darius@example.com"},
                               {"name": "Note taker", "email": None}]
    assert "[0:31] **Darius Kemp**: Of course. We pull six dashcam clips, half of them blurry." in body
    assert "[1:02:05] **Darius Kemp**: Arch." in body
    assert "\u2014" not in text

    p = _parse(rel, text)
    assert p.shape == "standard" and p.origin == "live"
    assert p.source == "circleback" and p.source_id == "WRweb12iCBimU6Vo8cD7z"
    assert p.ical_uid == "abc123@google.com"
    assert p.speaker_labels == "named"
    assert p.recorded_by == "alex"
    assert p.start == "2026-09-18T16:00:00-07:00" and p.start_source == "calendar"
    assert p.date == "2026-09-18"
    assert [t.speaker for t in p.turns] == ["Alex Rivera", "Darius Kemp", "Alex Rivera", "Darius Kemp"]
    assert [t.time for t in p.turns] == ["0:22", "0:31", "1:01", "1:02:05"]
    assert "dashcam footage" in p.notes
    assert "Darius walked through" not in p.body
    assert all(p.raw[t.start:t.end] == t.text for t in p.turns)
    assert {a.email for a in p.attendees} == {"alex.rivera@gmail.com", "darius@example.com", None}


def test_ingested_at_is_written_only_when_given(cfg):
    _, text = payload_to_standard(_payload(), person="alex", config=cfg, start=None, start_source="created")
    assert "ingested_at" not in frontmatter.split(text)[0]
    _, text = payload_to_standard(_payload(), person="alex", config=cfg, start=None, start_source="created",
                                  ingested_at="2026-10-01T09:00:00-07:00")
    assert frontmatter.split(text)[0]["ingested_at"] == "2026-10-01T09:00:00-07:00"


def test_payload_to_standard_falls_back_to_created_at(cfg):
    rel, text = payload_to_standard(_payload(), person="Alex Rivera", config=cfg, start=None, start_source="calendar")
    fm, _, _ = frontmatter.split(text)
    assert fm["start"] == "2026-09-18T16:31:57-07:00"
    assert fm["start_source"] == "created"
    assert fm["recorded_by"] == "alex"


def test_notes_are_verbatim_even_with_a_transcript_heading(cfg):
    rel, text = payload_to_standard(_payload(), person="alex", config=cfg, start=None, start_source="created")
    assert _payload()["notes"] in text
    p = _parse(rel, text)
    assert "## Transcript\n* not a real heading" in p.notes
    assert len(p.turns) == 4


def test_speaker_names_cannot_break_turn_markup(cfg):
    payload = _payload()
    payload["transcript"] = [{"speaker": "**Darius**\nKemp:", "text": "Hi.", "timestamp": 1},
                             {"speaker": "Alex Rivera", "text": "Hello.", "timestamp": 2}]
    rel, text = payload_to_standard(payload, person="alex", config=cfg, start=None, start_source="created")
    assert "[0:01] **Darius Kemp**: Hi." in text
    p = _parse(rel, text)
    assert [t.speaker for t in p.turns] == ["Darius Kemp", "Alex Rivera"]


def test_null_speakers(cfg):
    payload = _payload()
    payload["transcript"][1]["speaker"] = None
    _, text = payload_to_standard(payload, person="alex", config=cfg, start=None, start_source="created")
    fm, body, _ = frontmatter.split(text)
    assert fm["speaker_labels"] == "named"
    assert "**Unknown speaker**: Of course." in body

    for seg in payload["transcript"]:
        seg["speaker"] = None
    rel, text = payload_to_standard(payload, person="alex", config=cfg, start=None, start_source="created")
    fm, _, _ = frontmatter.split(text)
    assert fm["speaker_labels"] == "single_stream"
    p = _parse(rel, text)
    assert len(p.turns) == 5


def test_missing_fields_are_tolerated(cfg):
    rel, text = payload_to_standard({"id": "abc"}, person="nobody", config=cfg, start=None, start_source="created")
    assert rel == "transcripts/circleback_abc.md"
    fm, _, _ = frontmatter.split(text)
    assert fm["recorded_by"] == "nobody"
    assert fm["ical_uid"] is None
    assert fm["start"] is None and fm["start_source"] is None
    assert fm["attendees"] == []


def test_api_meeting_shape_is_accepted(cfg):
    """A meeting from GET /meetings plus GET /meeting/{id}/transcript converts the same way."""
    meeting = {k: v for k, v in _payload().items() if k != "transcript"}
    meeting["icalUid"] = None
    meeting["calendarEvent"] = {"id": 91, "icalUid": "abc123@google.com"}
    meeting["tags"] = [{"id": 7, "name": "discovery", "description": None}]
    meeting["attendees"] = [{"profileId": 1, "name": "Darius Kemp", "email": "darius@example.com", "title": "Claims",
                             "companyName": "Example Mutual", "isCalendarEventOrganizer": False, "isCalendarInvitee": True}]
    payload = {**meeting, "transcript": _payload()["transcript"]}
    _, text = payload_to_standard(payload, person="alex", config=cfg, start=None, start_source="created")
    fm, _, _ = frontmatter.split(text)
    assert fm["ical_uid"] == "abc123@google.com"
    assert fm["attendees"] == [{"name": "Darius Kemp", "email": "darius@example.com"}]


def test_find_start():
    events = [
        {"id": "e1", "meetingId": "other", "startTime": "2026-09-18T20:00:00.000Z"},
        {"id": "e2", "startTime": "2026-09-18T21:00:00.000Z"},
        {"id": "e3", "meetingId": "WRweb12iCBimU6Vo8cD7z", "startTime": "2026-09-18T23:00:00.000Z"},
    ]
    assert find_start(events, "WRweb12iCBimU6Vo8cD7z") == "2026-09-18T23:00:00.000Z"
    assert find_start(events, "missing") is None
    assert find_start([], "WRweb12iCBimU6Vo8cD7z") is None
    assert find_start([{"meetingId": "x"}], "x") is None


def test_has_tag():
    assert has_tag({"tags": ["Discovery"]}, "discovery")
    assert has_tag({"tags": [{"id": 1, "name": "discovery"}]}, "discovery")
    assert not has_tag({"tags": ["investor"]}, "discovery")
    assert not has_tag({}, "discovery")


def test_has_transcript():
    assert has_transcript(_payload())
    assert not has_transcript({k: v for k, v in _payload().items() if k != "transcript"})
    assert not has_transcript({**_payload(), "transcript": None})
    assert not has_transcript({**_payload(), "transcript": []})
    assert not has_transcript({**_payload(), "transcript": [{"speaker": "Darius", "text": " \n "}, "junk", {}]})


# ---------- API client ----------

def _client(handler) -> CirclebackClient:
    client = CirclebackClient("cb_test", http=httpx.Client(transport=httpx.MockTransport(handler)))
    client.sleep = lambda s: None
    return client


def test_list_meetings_by_tag_follows_link_header():
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        assert request.headers["authorization"] == "Bearer cb_test"
        if request.url.path == "/api/tag":
            return httpx.Response(200, json=[{"id": 3, "name": "investor", "description": None},
                                             {"id": 7, "name": "Discovery", "description": None}])
        if request.url.path == "/api/meetings":
            if "cursor" not in request.url.params:
                return httpx.Response(
                    200, json=[{"id": "m1", "tags": [{"id": 7, "name": "Discovery"}]},
                               {"id": "m2", "tags": [{"id": 3, "name": "investor"}]}],
                    headers={"Link": '<https://circleback.ai/api/meetings?cursor=abc&tagIds=7>; rel="next"'})
            return httpx.Response(200, json=[{"id": "m3", "tags": [{"id": 7, "name": "Discovery"}]}])
        return httpx.Response(404)

    ids = [m["id"] for m in _client(handler).list_meetings(tag="discovery")]
    assert ids == ["m1", "m3"]
    first = [r for r in seen if r.url.path == "/api/meetings"][0]
    assert first.url.params.get_list("tagIds") == ["7"]
    assert first.url.params["ownership"] == "Mine"
    second = [r for r in seen if r.url.path == "/api/meetings"][1]
    assert second.url.params["cursor"] == "abc"
    assert second.url.params["ownership"] == "Mine"


def test_list_meetings_unknown_tag_lists_nothing():
    paths = []

    def handler(request):
        paths.append(request.url.path)
        if request.url.path == "/api/tag":
            return httpx.Response(200, json=[{"id": 3, "name": "investor", "description": None}])
        return httpx.Response(200, json=[{"id": "m1"}])

    assert list(_client(handler).list_meetings(tag="discovery")) == []
    assert paths == ["/api/tag"]


def test_list_meetings_without_tag():
    def handler(request):
        assert "tagIds" not in request.url.params
        return httpx.Response(200, json=[{"id": "m1", "tags": []}, {"id": "m2", "tags": ["discovery"]}])

    assert [m["id"] for m in _client(handler).list_meetings()] == ["m1", "m2"]


def test_get_transcript():
    segments = _payload()["transcript"]

    def handler(request):
        assert request.url.path == "/api/meeting/WRweb12iCBimU6Vo8cD7z/transcript"
        return httpx.Response(200, json=segments)

    assert _client(handler).get_transcript("WRweb12iCBimU6Vo8cD7z") == segments


def test_get_transcript_raises_on_error():
    with pytest.raises(httpx.HTTPStatusError):
        _client(lambda r: httpx.Response(400, json={"error": "bad"})).get_transcript("x")


def test_calendar_events_lookback_and_paging():
    seen = []

    def handler(request):
        seen.append(dict(request.url.params))
        if "cursor" not in request.url.params:
            return httpx.Response(200, json=[{"id": "e1", "meetingId": "m1", "startTime": "2026-09-18T23:00:00Z"}],
                                  headers={"Link": '</api/calendar/events?cursor=p2>; rel="next"'})
        return httpx.Response(200, json=[{"id": "e2", "meetingId": "m2", "startTime": "2026-09-18T20:00:00Z"}])

    events = _client(handler).calendar_events(lookback_hours=48)
    assert [e["id"] for e in events] == ["e1", "e2"]
    assert seen[0]["startTimeLookbackHours"] == "48"
    assert seen[1]["cursor"] == "p2"
    assert seen[1]["startTimeLookbackHours"] == "48"


def test_rate_limit_is_retried():
    responses = [httpx.Response(429, headers={"Retry-After": "0.5"}), httpx.Response(200, json=[])]
    slept = []
    client = _client(lambda r: responses.pop(0))
    client.sleep = slept.append
    assert client.get_transcript("m1") == []
    assert slept == [0.5]


def test_webhook_flow_end_to_end(cfg):
    """Verify a signed body, look up the start time, write the file."""
    body = json.dumps(_payload()).encode()
    assert verify_signature(SECRET, body, _sig(body))
    payload = json.loads(body)
    client = _client(lambda r: httpx.Response(
        200, json=[{"id": "e3", "meetingId": payload["id"], "startTime": "2026-09-18T23:00:00.000Z"}]))
    start = find_start(client.calendar_events(lookback_hours=24), payload["id"])
    rel, text = payload_to_standard(payload, person="alex", config=cfg, start=start,
                                    start_source="calendar" if start else "created")
    fm, _, _ = frontmatter.split(text)
    assert fm["start"] == "2026-09-18T16:00:00-07:00" and fm["start_source"] == "calendar"
    assert rel.endswith("circleback_WRweb12iCBimU6Vo8cD7z.md")


def test_module_has_no_em_dash():
    src = open(circleback.__file__, encoding="utf-8").read()
    assert "\u2014" not in src
