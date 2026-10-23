"""Tests for nua/ingest/granola.py: webhook signatures, the API client and the standard file."""
from __future__ import annotations

import base64
import hashlib
import hmac
import json

import httpx
import pytest

from nua import config as config_mod
from nua import frontmatter
from nua.ingest import granola
from nua.ingest.granola import GranolaClient, has_transcript, note_to_standard, transcript_length, verify_webhook

SECRET = "whsec_" + base64.b64encode(b"granola-test-signing-key-0123456").decode()
BODY = b'{"event_id":"evt_1","event_type":"note.generated","note_id":"not_1d3tmYTlCICgjy"}'
NOW = 1_790_000_000


def _sign(secret: str, msg_id: str, ts: int, body: bytes) -> str:
    key = base64.b64decode(secret[len("whsec_"):])
    mac = hmac.new(key, f"{msg_id}.{ts}.".encode() + body, hashlib.sha256).digest()
    return "v1," + base64.b64encode(mac).decode()


def _headers(ts: int = NOW, body: bytes = BODY, msg_id: str = "evt_1") -> dict[str, str]:
    return {"webhook-id": msg_id, "webhook-timestamp": str(ts),
            "webhook-signature": _sign(SECRET, msg_id, ts, body)}


# ---------- webhook signatures ----------

def test_verify_webhook_valid():
    assert verify_webhook(SECRET, _headers(), BODY, now=NOW)


def test_verify_webhook_tampered_body():
    assert not verify_webhook(SECRET, _headers(), BODY.replace(b"generated", b"edited"), now=NOW)


def test_verify_webhook_stale_timestamp():
    assert not verify_webhook(SECRET, _headers(ts=NOW - 301), BODY, now=NOW)
    assert verify_webhook(SECRET, _headers(ts=NOW - 299), BODY, now=NOW)


def test_verify_webhook_future_timestamp():
    assert not verify_webhook(SECRET, _headers(ts=NOW + 301), BODY, now=NOW)


def test_verify_webhook_wrong_secret():
    other = "whsec_" + base64.b64encode(b"some-other-key").decode()
    assert not verify_webhook(other, _headers(), BODY, now=NOW)


def test_verify_webhook_missing_or_bad_headers():
    h = _headers()
    for key in ("webhook-id", "webhook-timestamp", "webhook-signature"):
        partial = {k: v for k, v in h.items() if k != key}
        assert not verify_webhook(SECRET, partial, BODY, now=NOW)
    assert not verify_webhook(SECRET, {**h, "webhook-timestamp": "soon"}, BODY, now=NOW)
    assert not verify_webhook(SECRET, {**h, "webhook-signature": h["webhook-signature"].replace("v1,", "v2,")},
                              BODY, now=NOW)
    assert not verify_webhook("whsec_not base64!!", h, BODY, now=NOW)
    assert not verify_webhook("", h, BODY, now=NOW)


def test_verify_webhook_secret_without_padding_or_url_safe():
    key = b"\xfb\xff\xfe-granola-key"
    std = base64.b64encode(key).decode()
    urlsafe = base64.urlsafe_b64encode(key).decode().rstrip("=")
    body = b"{}"
    mac = hmac.new(key, f"evt_2.{NOW}.".encode() + body, hashlib.sha256).digest()
    headers = {"webhook-id": "evt_2", "webhook-timestamp": str(NOW),
               "webhook-signature": "v1," + base64.b64encode(mac).decode()}
    assert verify_webhook("whsec_" + std, headers, body, now=NOW)
    assert verify_webhook("whsec_" + urlsafe, headers, body, now=NOW)


def test_verify_webhook_several_signatures_and_header_case():
    h = _headers()
    headers = {"Webhook-Id": h["webhook-id"], "Webhook-Timestamp": h["webhook-timestamp"],
               "Webhook-Signature": "v1,AAAAbad= " + h["webhook-signature"]}
    assert verify_webhook(SECRET, headers, BODY, now=NOW)


# ---------- notes to standard files ----------

@pytest.fixture
def cfg(tmp_repo):
    return config_mod.load(tmp_repo)


OWNER = {"name": "Alex Rivera", "email": "alex.rivera@gmail.com"}
CAL = {"event_title": "Acme x Example Mutual", "invitees": [{"email": "darius@example.com"}, {"email": "alex.rivera@gmail.com"}],
       "organiser": "alex.rivera@gmail.com", "calendar_event_id": "abc_20260918T230000Z",
       "scheduled_start_time": "2026-09-18T23:00:00Z", "scheduled_end_time": "2026-09-18T23:30:00Z"}


def _item(text: str, second: int, **speaker) -> dict:
    return {"speaker": speaker or {"source": "microphone"}, "text": text,
            "start_time": f"2026-09-18T23:{2 + second // 60:02d}:{second % 60:02d}.000Z",
            "end_time": f"2026-09-18T23:{2 + second // 60:02d}:{second % 60:02d}.900Z"}


def _desktop_note() -> dict:
    return {
        "id": "not_1d3tmYTlCICgjy", "object": "note", "title": "Darius / Alex",
        "owner": OWNER, "created_at": "2026-09-18T22:58:00Z", "updated_at": "2026-09-18T23:40:00Z",
        "calendar_event": CAL,
        "attendees": [{"name": "Darius Kemp", "email": "darius@example.com"}],
        "summary_markdown": "### Dashcam footage\n\n- Darius runs six fleets",
        "transcript": [
            _item("Thanks for taking the time.", 0, source="microphone", attribution="me"),
            _item("Of course.", 4, source="speaker", attribution="them"),
            _item("Happy to walk through the dashcam footage.", 6, source="speaker", attribution="them"),
            _item("How many fleets do you run?", 15, source="microphone", attribution="me"),
            _item("We have six,\n  Brightway and Rider among them.", 75, source="speaker", attribution="them"),
        ],
    }


def _parse(rel: str, text: str):
    parse_mod = pytest.importorskip("nua.parse")
    if not hasattr(parse_mod, "write_standard"):
        pytest.skip("nua.parse (Task 1) not implemented yet")
    return parse_mod.parse(rel, text)


def test_desktop_note_me_them(cfg):
    rel, text = note_to_standard(_desktop_note(), config=cfg)
    assert rel == "transcripts/granola_not_1d3tmYTlCICgjy.md"
    fm, body, _ = frontmatter.split(text)
    assert fm["source"] == "granola"
    assert fm["source_id"] == "not_1d3tmYTlCICgjy"
    assert fm["start"] == "2026-09-18T16:00:00-07:00"
    assert fm["start_source"] == "calendar"
    assert fm["recorded_by"] == "alex"
    assert fm["speaker_labels"] == "me_them"
    assert "ical_uid" not in fm
    emails = [a["email"] for a in fm["attendees"]]
    assert emails == ["darius@example.com", "alex.rivera@gmail.com"]
    assert "[0:04] **Them**: Of course. Happy to walk through the dashcam footage." in body
    assert "[1:15] **Them**: We have six, Brightway and Rider among them." in body
    assert "\u2014" not in text

    p = _parse(rel, text)
    assert p.shape == "standard" and p.origin == "live"
    assert p.source == "granola" and p.source_id == "not_1d3tmYTlCICgjy"
    assert p.speaker_labels == "me_them"
    assert p.recorded_by == "alex"
    assert p.start == "2026-09-18T16:00:00-07:00"
    assert p.date == "2026-09-18"
    assert [t.speaker for t in p.turns] == ["Me", "Them", "Me", "Them"]
    assert [t.time for t in p.turns] == ["0:00", "0:04", "0:15", "1:15"]
    assert "six fleets" in p.notes and "six fleets" not in p.body
    assert all(p.raw[t.start:t.end] == t.text for t in p.turns)


def test_ingested_at_is_written_only_when_given(cfg):
    _, text = note_to_standard(_desktop_note(), config=cfg)
    assert "ingested_at" not in frontmatter.split(text)[0]
    rel, text = note_to_standard(_desktop_note(), config=cfg, ingested_at="2026-10-01T09:00:00-07:00")
    assert frontmatter.split(text)[0]["ingested_at"] == "2026-10-01T09:00:00-07:00"
    assert _parse(rel, text).front_matter["ingested_at"] == "2026-10-01T09:00:00-07:00"


def test_named_note(cfg):
    note = _desktop_note()
    note["transcript"] = [
        _item("Thanks for taking the time.", 0, source="microphone", attribution="me", name="Alex Rivera"),
        _item("Of course.", 4, source="speaker", attribution="them", name="Darius Kemp"),
        _item("Guidewire already does this.", 9, source="speaker", attribution="them", name="Darius Kemp"),
    ]
    rel, text = note_to_standard(note, config=cfg)
    fm, body, _ = frontmatter.split(text)
    assert fm["speaker_labels"] == "named"
    assert "[0:04] **Darius Kemp**: Of course. Guidewire already does this." in body
    p = _parse(rel, text)
    assert p.speaker_labels == "named"
    assert [t.speaker for t in p.turns] == ["Alex Rivera", "Darius Kemp"]


def test_partly_named_note_stays_me_them(cfg):
    note = _desktop_note()
    note["transcript"] = [
        _item("Thanks for taking the time.", 0, source="microphone", attribution="me"),
        _item("Of course.", 4, source="speaker", attribution="them", name="Darius Kemp"),
    ]
    _, text = note_to_standard(note, config=cfg)
    fm, body, _ = frontmatter.split(text)
    assert fm["speaker_labels"] == "me_them"
    assert "**Me**: Thanks" in body and "**Darius Kemp**: Of course." in body


def test_ios_diarized_note(cfg):
    note = {
        "id": "not_A1b2C3d4E5f6G7", "title": "Coffee with Lena", "owner": OWNER,
        "created_at": "2026-09-18T18:00:00Z", "calendar_event": None, "attendees": [OWNER],
        "summary_markdown": None, "summary_text": "Lena on claims intake.",
        "transcript": [
            _item("You're calling from Utah, right?", 0, source="microphone", diarization_label="Speaker A"),
            _item("Yes, I am.", 3, source="microphone", diarization_label="Speaker B"),
            _item("Three decades in New York.", 5, source="microphone", diarization_label="Speaker B"),
        ],
    }
    rel, text = note_to_standard(note, config=cfg)
    fm, body, _ = frontmatter.split(text)
    assert fm["speaker_labels"] == "diarized"
    assert fm["start_source"] == "transcript"
    assert fm["start"] == "2026-09-18T16:02:00-07:00"
    assert fm["attendees"] == [{"name": "Alex Rivera", "email": "alex.rivera@gmail.com"}]
    assert "[0:03] **Speaker B**: Yes, I am. Three decades in New York." in body
    p = _parse(rel, text)
    assert p.speaker_labels == "diarized"
    assert [t.speaker for t in p.turns] == ["Speaker A", "Speaker B"]
    assert "Lena on claims intake." in p.notes


def test_single_stream_keeps_items_as_turns(cfg):
    note = _desktop_note()
    note["transcript"] = [
        _item("You're calling from Utah, right?", 0, source="microphone", attribution="me"),
        _item("Yes, yes, I am.", 3, source="microphone", attribution="me"),
        _item("Close to four decades.", 8, source="microphone", attribution="me"),
    ]
    rel, text = note_to_standard(note, config=cfg)
    fm, _, _ = frontmatter.split(text)
    assert fm["speaker_labels"] == "single_stream"
    p = _parse(rel, text)
    assert p.speaker_labels == "single_stream"
    assert len(p.turns) == 3
    assert {t.speaker for t in p.turns} == {"Me"}


def test_legacy_items_without_attribution_use_source(cfg):
    note = _desktop_note()
    note["transcript"] = [_item("Hi.", 0, source="microphone"), _item("Hello.", 2, source="speaker")]
    _, text = note_to_standard(note, config=cfg)
    fm, body, _ = frontmatter.split(text)
    assert fm["speaker_labels"] == "me_them"
    assert "**Me**: Hi." in body and "**Them**: Hello." in body


def test_version_path_and_created_fallback(cfg):
    note = _desktop_note()
    note["calendar_event"] = None
    note["transcript"] = [{"speaker": {"source": "microphone"}, "text": "No times here."}]
    rel, text = note_to_standard(note, config=cfg, version=3)
    assert rel == "transcripts/granola_not_1d3tmYTlCICgjy_v3.md"
    fm, _, _ = frontmatter.split(text)
    assert fm["start_source"] == "created"
    assert fm["start"] == "2026-09-18T15:58:00-07:00"


def test_long_call_uses_hours(cfg):
    note = _desktop_note()
    note["transcript"] = [
        {"speaker": {"source": "microphone", "attribution": "me"}, "text": "Start.",
         "start_time": "2026-09-18T23:00:00Z"},
        {"speaker": {"source": "speaker", "attribution": "them"}, "text": "Much later.",
         "start_time": "2026-09-19T00:05:09Z"},
    ]
    _, text = note_to_standard(note, config=cfg)
    assert "[1:05:09] **Them**: Much later." in text


def test_missing_fields_are_tolerated(cfg):
    rel, text = note_to_standard({"id": "not_Zz9Yy8Xx7Ww6Vv"}, config=cfg)
    assert rel == "transcripts/granola_not_Zz9Yy8Xx7Ww6Vv.md"
    fm, _, _ = frontmatter.split(text)
    assert fm["source"] == "granola"
    assert fm["start"] is None and fm["start_source"] is None
    assert fm["recorded_by"] is None
    assert fm["attendees"] == []


def test_unknown_owner_keeps_email(cfg):
    note = _desktop_note()
    note["owner"] = {"name": "Guest", "email": "guest@example.com"}
    _, text = note_to_standard(note, config=cfg)
    fm, _, _ = frontmatter.split(text)
    assert fm["recorded_by"] == "guest@example.com"


def test_transcript_length_matches_parsed_turns(cfg):
    note = _desktop_note()
    n = transcript_length(note)
    assert n > 0
    rel, text = note_to_standard(note, config=cfg)
    p = _parse(rel, text)
    assert n == sum(len(t.text) for t in p.turns)


def test_transcript_length_grows_on_resume():
    note = _desktop_note()
    before = transcript_length(note)
    note["transcript"].append(_item("One more thing after resume.", 200, source="microphone", attribution="me"))
    assert transcript_length(note) > before
    assert transcript_length({"id": "x"}) == 0


def test_has_transcript():
    assert has_transcript(_desktop_note())
    assert not has_transcript({"id": "x"})
    assert not has_transcript({"id": "x", "transcript": None})
    assert not has_transcript({"id": "x", "transcript": [_item("  ", 0, source="speaker")]})


# ---------- API client ----------

def _client(handler) -> GranolaClient:
    client = GranolaClient("grn_test", http=httpx.Client(transport=httpx.MockTransport(handler)))
    client.sleep = lambda s: None
    return client


def test_get_note_inline_transcript():
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=_desktop_note())

    note = _client(handler).get_note("not_1d3tmYTlCICgjy")
    assert len(note["transcript"]) == 5
    req = seen[0]
    assert req.url.path == "/v1/notes/not_1d3tmYTlCICgjy"
    assert req.url.params["include"] == "transcript"
    assert req.headers["authorization"] == "Bearer grn_test"
    assert str(req.url).startswith("https://public-api.granola.ai/v1/")


def test_get_note_pages_transcript_on_413():
    items = _desktop_note()["transcript"]
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append((request.url.path, dict(request.url.params)))
        if request.url.path == "/v1/notes/not_1d3tmYTlCICgjy":
            if request.url.params.get("include") == "transcript":
                return httpx.Response(413, json={"code": "TRANSCRIPT_TOO_LARGE"})
            meta = {k: v for k, v in _desktop_note().items() if k != "transcript"}
            return httpx.Response(200, json=meta)
        if request.url.path == "/v1/notes/not_1d3tmYTlCICgjy/transcript":
            cursor = request.url.params.get("cursor")
            if cursor is None:
                return httpx.Response(200, json={"transcript": items[:2], "hasMore": True, "cursor": "c1"})
            if cursor == "c1":
                return httpx.Response(200, json={"transcript": items[2:4], "hasMore": True, "cursor": "c2"})
            return httpx.Response(200, json={"transcript": items[4:], "hasMore": False, "cursor": None})
        return httpx.Response(404)

    note = _client(handler).get_note("not_1d3tmYTlCICgjy")
    assert [i["text"] for i in note["transcript"]] == [i["text"] for i in items]
    assert note["title"] == "Darius / Alex"
    transcript_calls = [c for c in calls if c[0].endswith("/transcript")]
    assert len(transcript_calls) == 3
    assert all(c[1]["page_size"] == "100" for c in transcript_calls)


def test_get_note_raises_on_404():
    client = _client(lambda request: httpx.Response(404, json={"error": "not found"}))
    with pytest.raises(httpx.HTTPStatusError):
        client.get_note("not_missing000000000")


def test_get_note_retries_rate_limit():
    responses = [httpx.Response(429, headers={"Retry-After": "1"}), httpx.Response(200, json=_desktop_note())]
    slept = []

    def handler(request):
        return responses.pop(0)

    client = _client(handler)
    client.sleep = slept.append
    note = client.get_note("not_1d3tmYTlCICgjy")
    assert note["id"] == "not_1d3tmYTlCICgjy"
    assert slept == [1.0]


def test_get_note_gives_up_after_retries():
    attempts = []

    def handler(request):
        attempts.append(1)
        return httpx.Response(503)

    with pytest.raises(httpx.HTTPStatusError):
        _client(handler).get_note("not_1d3tmYTlCICgjy")
    assert len(attempts) == 3


def test_list_notes_follows_cursor():
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(dict(request.url.params))
        if "cursor" not in request.url.params:
            return httpx.Response(200, json={"notes": [{"id": "not_a"}, {"id": "not_b"}], "hasMore": True,
                                             "cursor": "next1"})
        return httpx.Response(200, json={"notes": [{"id": "not_c"}], "hasMore": False, "cursor": None})

    ids = [n["id"] for n in _client(handler).list_notes(created_after="2026-11-02T09:00:00-08:00")]
    assert ids == ["not_a", "not_b", "not_c"]
    assert seen[0]["created_after"] == "2026-11-02T17:00:00Z"       # Granola rejects a -08:00 offset
    assert seen[0]["page_size"] == "30"
    assert seen[1]["cursor"] == "next1"
    assert seen[1]["created_after"] == "2026-11-02T17:00:00Z"


@pytest.mark.parametrize("value, sent", [
    ("2026-10-05T12:00:00-07:00", "2026-10-05T19:00:00Z"),
    ("2026-10-05T19:00:00Z", "2026-10-05T19:00:00Z"),
    ("2026-10-05", "2026-10-05"),
])
def test_created_after_goes_out_in_utc(value, sent):
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(dict(request.url.params))
        return httpx.Response(200, json={"notes": [], "hasMore": False, "cursor": None})

    list(_client(handler).list_notes(created_after=value))
    assert seen[0]["created_after"] == sent


def test_list_notes_stops_on_repeated_cursor():
    def handler(request):
        return httpx.Response(200, json={"notes": [{"id": "not_a"}], "hasMore": True, "cursor": "same"})

    ids = [n["id"] for n in _client(handler).list_notes()]
    assert ids == ["not_a", "not_a"]


def test_webhook_payload_round_trip_through_verify_and_fetch():
    """A signed note.generated event leads to a fetch of the note it names."""
    body = json.dumps({"event_id": "evt_9", "event_type": "note.generated", "note_id": "not_1d3tmYTlCICgjy",
                       "occurred_at": "2026-09-18T23:40:00Z"}).encode()
    headers = _headers(body=body, msg_id="evt_9")
    assert verify_webhook(SECRET, headers, body, now=NOW)
    note_id = json.loads(body)["note_id"]
    note = _client(lambda r: httpx.Response(200, json=_desktop_note())).get_note(note_id)
    assert note["id"] == note_id


def test_module_has_no_em_dash():
    src = open(granola.__file__, encoding="utf-8").read()
    assert "\u2014" not in src
