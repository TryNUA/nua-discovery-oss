"""Tests for nua/slackio.py: request signatures and the WebClient-backed SlackIO (fake client)."""
from __future__ import annotations

import hashlib
import hmac
import json

import httpx
import pytest
from slack_sdk.errors import SlackApiError

from nua.slackio import MAX_FALLBACK_TEXT, SlackWebIO, fallback_text, verify_slack_signature

# Slack's documented example (api.slack.com/authentication/verifying-requests-from-slack)
DOC_SECRET = "8f742231b10e8888abcd99yyyzzz85a5"
DOC_TS = "1531420618"
DOC_BODY = (b"token=xyzz0WbapA4vBCDEFasx0q6G&team_id=T1DC2JH3J&team_domain=testteamnow&channel_id=G8PSS9T3V"
            b"&channel_name=foobar&user_id=U2CERLKJA&user_name=roadrunner&command=%2Fwebhook-collect&text="
            b"&response_url=https%3A%2F%2Fhooks.slack.com%2Fcommands%2FT1DC2JH3J%2F397700885554%2F96rGlfmibIGlgcZRskXaIFfN"
            b"&trigger_id=398738663015.47445629121.803a0bc887a14d10d2c447fce8b6703c")
DOC_SIG = "v0=a2114d57b48eac39b9ad189dd8316235a7b4a8d21a10bd27519666489c69b503"


# ---------- verify_slack_signature ----------

def test_signature_matches_slack_documented_example():
    headers = {"X-Slack-Request-Timestamp": DOC_TS, "X-Slack-Signature": DOC_SIG}
    assert verify_slack_signature(DOC_SECRET, headers, DOC_BODY, now=float(DOC_TS) + 10) is True


def test_signature_headers_are_case_insensitive():
    headers = {"x-slack-request-timestamp": DOC_TS, "x-slack-signature": DOC_SIG}
    assert verify_slack_signature(DOC_SECRET, headers, DOC_BODY, now=float(DOC_TS)) is True


def test_signature_rejects_wrong_secret_body_or_signature():
    headers = {"X-Slack-Request-Timestamp": DOC_TS, "X-Slack-Signature": DOC_SIG}
    now = float(DOC_TS)
    assert verify_slack_signature("wrong-secret", headers, DOC_BODY, now=now) is False
    assert verify_slack_signature(DOC_SECRET, headers, DOC_BODY + b"x", now=now) is False
    bad = dict(headers, **{"X-Slack-Signature": "v0=" + "0" * 64})
    assert verify_slack_signature(DOC_SECRET, bad, DOC_BODY, now=now) is False


def test_signature_rejects_stale_or_future_timestamps():
    headers = {"X-Slack-Request-Timestamp": DOC_TS, "X-Slack-Signature": DOC_SIG}
    assert verify_slack_signature(DOC_SECRET, headers, DOC_BODY, now=float(DOC_TS) + 301) is False
    assert verify_slack_signature(DOC_SECRET, headers, DOC_BODY, now=float(DOC_TS) - 301) is False
    assert verify_slack_signature(DOC_SECRET, headers, DOC_BODY, now=float(DOC_TS) + 299) is True


def test_signature_rejects_missing_or_malformed_headers_and_empty_secret():
    now = float(DOC_TS)
    assert verify_slack_signature(DOC_SECRET, {}, DOC_BODY, now=now) is False
    assert verify_slack_signature(DOC_SECRET, {"X-Slack-Signature": DOC_SIG}, DOC_BODY, now=now) is False
    assert verify_slack_signature(DOC_SECRET, {"X-Slack-Request-Timestamp": "soon", "X-Slack-Signature": DOC_SIG},
                                  DOC_BODY, now=now) is False
    assert verify_slack_signature("", {"X-Slack-Request-Timestamp": DOC_TS, "X-Slack-Signature": DOC_SIG},
                                  DOC_BODY, now=now) is False


@pytest.mark.parametrize("sig", ["v0=\u00e9" + "0" * 63, DOC_SIG[:-1] + "\u00ff", "v0=\udcff", "\u2603"])
def test_signature_with_non_ascii_characters_fails_the_check_without_raising(sig):
    # Starlette decodes headers as latin-1, so a forged byte >= 0x80 arrives as a non-ASCII str.
    headers = {"X-Slack-Request-Timestamp": DOC_TS, "X-Slack-Signature": sig}
    assert verify_slack_signature(DOC_SECRET, headers, DOC_BODY, now=float(DOC_TS)) is False


def test_signature_ignores_surrounding_whitespace():
    headers = {"X-Slack-Request-Timestamp": DOC_TS, "X-Slack-Signature": f" {DOC_SIG} "}
    assert verify_slack_signature(DOC_SECRET, headers, DOC_BODY, now=float(DOC_TS)) is True


def test_signature_uses_current_time_by_default():
    import time
    ts = str(int(time.time()))
    body = b'payload={"type":"block_actions"}'
    sig = "v0=" + hmac.new(b"s3cret", f"v0:{ts}:".encode() + body, hashlib.sha256).hexdigest()
    assert verify_slack_signature("s3cret", {"X-Slack-Request-Timestamp": ts, "X-Slack-Signature": sig}, body)


# ---------- SlackWebIO ----------

class FakeWebClient:
    """Stands in for slack_sdk.WebClient. Records calls; serves canned history and replies with paging."""

    def __init__(self):
        self.calls: list[tuple[str, dict]] = []
        self.channels = [[{"id": "C0CALLS", "name": "discovery-calls"}],
                         [{"id": "C0OPS", "name": "discovery-ops"}, {"id": "G0TEST", "name": "discovery-test"}]]
        self.history_pages: list[list[dict]] = []
        self.replies_pages: list[list[dict]] = []
        self.delete_error: str | None = None
        self._seq = 0

    def _rec(self, name, **kw):
        self.calls.append((name, kw))

    def of(self, name):
        return [kw for n, kw in self.calls if n == name]

    @staticmethod
    def _page(pages, cursor):
        i = int(cursor or 0)
        nxt = str(i + 1) if i + 1 < len(pages) else ""
        return pages[i] if pages else [], nxt

    def auth_test(self):
        self._rec("auth_test")
        return {"ok": True, "user_id": "U0BOT", "bot_id": "B0DISC"}

    def conversations_list(self, **kw):
        self._rec("conversations_list", **kw)
        page, nxt = self._page(self.channels, kw.get("cursor"))
        return {"ok": True, "channels": page, "response_metadata": {"next_cursor": nxt}}

    def chat_postMessage(self, **kw):
        self._rec("chat_postMessage", **kw)
        self._seq += 1
        return {"ok": True, "channel": "C0CALLS", "ts": f"1700000000.{self._seq:06d}"}

    def chat_update(self, **kw):
        self._rec("chat_update", **kw)
        return {"ok": True}

    def chat_delete(self, **kw):
        self._rec("chat_delete", **kw)
        if self.delete_error:
            raise SlackApiError("delete failed", {"ok": False, "error": self.delete_error})
        return {"ok": True}

    def chat_postEphemeral(self, **kw):
        self._rec("chat_postEphemeral", **kw)
        return {"ok": True}

    def conversations_history(self, **kw):
        self._rec("conversations_history", **kw)
        page, nxt = self._page(self.history_pages, kw.get("cursor"))
        return {"ok": True, "messages": page, "has_more": bool(nxt), "response_metadata": {"next_cursor": nxt}}

    def conversations_replies(self, **kw):
        self._rec("conversations_replies", **kw)
        page, nxt = self._page(self.replies_pages, kw.get("cursor"))
        return {"ok": True, "messages": page, "has_more": bool(nxt), "response_metadata": {"next_cursor": nxt}}

    def views_open(self, **kw):
        self._rec("views_open", **kw)
        return {"ok": True, "view": {"id": "V0FORM"}}

    def views_update(self, **kw):
        self._rec("views_update", **kw)
        return {"ok": True}


@pytest.fixture
def client():
    return FakeWebClient()


@pytest.fixture
def io(client):
    return SlackWebIO("xoxb-test", client=client)


def md(event_type, key):
    return {"event_type": event_type, "event_payload": {"key": key}}


def test_post_resolves_channel_name_once_and_passes_metadata(io, client):
    blocks = [{"type": "section", "text": {"type": "mrkdwn", "text": "hi"}}]
    meta = md("nua_brief", "transcripts/a.md")
    ts1 = io.post("#discovery-calls", blocks, "hi", metadata=meta)
    ts2 = io.post("#discovery-calls", blocks, "reply", thread_ts=ts1)
    assert (ts1, ts2) == ("1700000000.000001", "1700000000.000002")
    first, second = client.of("chat_postMessage")
    assert first["channel"] == "C0CALLS" and first["blocks"] == blocks and first["text"] == "hi"
    assert first["metadata"] == meta and first.get("thread_ts") is None
    assert second["thread_ts"] == ts1 and second.get("metadata") is None
    assert len(client.of("conversations_list")) == 1       # cached after the first lookup


def test_channel_lookup_pages_and_ids_pass_through(io, client):
    io.post("#discovery-test", [], "x")
    io.post("C0ALREADYID", [], "y")
    posts = client.of("chat_postMessage")
    assert posts[0]["channel"] == "G0TEST"
    assert posts[1]["channel"] == "C0ALREADYID"
    assert len(client.of("conversations_list")) == 2        # both pages read


def test_unknown_channel_name_raises(io):
    with pytest.raises(ValueError):
        io.post("#no-such-channel", [], "x")


def test_update_delete_ephemeral_and_views(io, client):
    io.update("#discovery-calls", "1.1", [{"type": "divider"}], "new")
    io.delete("#discovery-calls", "1.2")
    io.ephemeral("#discovery-ops", "U0SAM", "already queued by Alex")
    assert io.open_view("trig-1", {"type": "modal"}) == "V0FORM"
    io.update_view("V0FORM", {"type": "modal", "title": {"type": "plain_text", "text": "x"}})
    assert client.of("chat_update") == [{"channel": "C0CALLS", "ts": "1.1", "blocks": [{"type": "divider"}], "text": "new"}]
    assert client.of("chat_delete") == [{"channel": "C0CALLS", "ts": "1.2"}]
    assert client.of("chat_postEphemeral") == [{"channel": "C0OPS", "user": "U0SAM", "text": "already queued by Alex"}]
    assert client.of("views_open") == [{"trigger_id": "trig-1", "view": {"type": "modal"}}]
    assert client.of("views_update")[0]["view_id"] == "V0FORM"


def test_delete_ignores_already_deleted_but_raises_other_errors(io, client):
    client.delete_error = "message_not_found"
    io.delete("#discovery-calls", "1.2")
    client.delete_error = "cant_delete_message"
    with pytest.raises(SlackApiError):
        io.delete("#discovery-calls", "1.3")


def test_find_by_metadata_returns_most_recent_match_across_pages(io, client):
    client.history_pages = [
        [{"ts": "9.0", "bot_id": "B0DISC", "text": "newest, no metadata"},
         {"ts": "8.0", "bot_id": "B0DISC", "metadata": md("nua_brief", "transcripts/b.md")}],
        [{"ts": "7.0", "bot_id": "B0DISC", "metadata": md("nua_brief", "transcripts/a.md")},
         {"ts": "6.0", "bot_id": "B0DISC", "metadata": md("nua_brief", "transcripts/a.md")}],
    ]
    assert io.find_by_metadata("#discovery-calls", "nua_brief", "transcripts/a.md") == "7.0"
    calls = client.of("conversations_history")
    assert all(c["include_all_metadata"] is True and c["channel"] == "C0CALLS" for c in calls)
    assert io.find_by_metadata("#discovery-calls", "nua_rollup", "transcripts/a.md") is None


def test_find_by_metadata_respects_limit(io, client):
    client.history_pages = [
        [{"ts": f"{i}.0", "bot_id": "B0DISC"} for i in range(100, 0, -1)],
        [{"ts": "0.5", "bot_id": "B0DISC", "metadata": md("nua_ref", "k")}],
    ]
    assert io.find_by_metadata("#discovery-calls", "nua_ref", "k", limit=100) is None
    assert io.find_by_metadata("#discovery-calls", "nua_ref", "k", limit=1000) == "0.5"


def test_replies_excludes_parent_pages_and_marks_own_bot(io, client):
    client.replies_pages = [
        [{"ts": "5.0", "thread_ts": "5.0", "bot_id": "B0DISC", "text": "parent", "blocks": []},
         {"ts": "5.1", "thread_ts": "5.0", "bot_id": "B0DISC", "text": "findings", "blocks": [{"type": "divider"}]}],
        [{"ts": "5.2", "thread_ts": "5.0", "user": "U0ALEX", "text": "nice"},
         {"ts": "5.3", "thread_ts": "5.0", "bot_id": "B0OTHER", "text": "other app"}],
    ]
    out = io.replies("#discovery-calls", "5.0")
    assert out == [
        {"ts": "5.1", "blocks": [{"type": "divider"}], "text": "findings", "bot": True},
        {"ts": "5.2", "blocks": [], "text": "nice", "bot": False},
        {"ts": "5.3", "blocks": [], "text": "other app", "bot": False},
    ]
    assert all(c["ts"] == "5.0" and c["channel"] == "C0CALLS" for c in client.of("conversations_replies"))


def test_message_reads_one_top_level_message_by_ts(io, client):
    client.history_pages = [[{"ts": "5.0", "bot_id": "B0DISC", "text": "brief", "blocks": [{"type": "divider"}]}]]
    assert io.message("#discovery-calls", "5.0") == {"ts": "5.0", "blocks": [{"type": "divider"}], "text": "brief",
                                                     "bot": True}
    call = client.of("conversations_history")[-1]
    assert call == {"channel": "C0CALLS", "latest": "5.0", "oldest": "5.0", "inclusive": True, "limit": 1}
    assert io.message("#discovery-calls", "4.0") is None
    client.history_pages = []
    assert io.message("#discovery-calls", "5.0") is None


def test_fake_slack_message_returns_the_live_message_only():
    from nua.ports import FakeSlack
    slack = FakeSlack()
    ts = slack.post("#c", [{"type": "divider"}], "hi")
    slack.update("#c", ts, [], "edited")
    assert slack.message("#c", ts) == {"ts": ts, "blocks": [], "text": "edited"}
    assert slack.message("#other", ts) is None
    slack.delete("#c", ts)
    assert slack.message("#c", ts) is None


def test_respond_posts_json_to_response_url(io):
    seen = []

    def handler(request: httpx.Request):
        seen.append((str(request.url), json.loads(request.content)))
        return httpx.Response(200, text="ok")

    io.http = httpx.Client(transport=httpx.MockTransport(handler))
    payload = {"replace_original": True, "blocks": [{"type": "context", "elements": []}]}
    io.respond("https://hooks.slack.com/actions/T1/1/abc", payload)
    assert seen == [("https://hooks.slack.com/actions/T1/1/abc", payload)]


def test_respond_raises_on_expired_url(io):
    io.http = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(404, text="expired_url")))
    with pytest.raises(RuntimeError, match="expired_url"):
        io.respond("https://hooks.slack.com/actions/T1/1/abc", {"text": "x"})


def test_satisfies_slack_protocol_methods():
    from nua.ports import SlackIO
    for name in ("post", "update", "delete", "find_by_metadata", "replies", "message", "open_view",
                 "update_view", "respond", "ephemeral"):
        assert callable(getattr(SlackWebIO, name)), name
        assert hasattr(SlackIO, name)


def test_long_fallback_text_is_capped_on_post_and_update(io, client):
    """Slack rejects chat.update with msg_too_long past about 4,000 characters of fallback text."""
    long = "x" * 10_000
    ts = io.post("#discovery-calls", [], long)
    io.update("#discovery-calls", ts, [], long)
    for name in ("chat_postMessage", "chat_update"):
        text = client.of(name)[-1]["text"]
        assert len(text) == MAX_FALLBACK_TEXT and text.endswith("...")


def test_short_fallback_text_is_unchanged():
    assert fallback_text("Greg John brief") == "Greg John brief"
    assert fallback_text("y" * MAX_FALLBACK_TEXT) == "y" * MAX_FALLBACK_TEXT
