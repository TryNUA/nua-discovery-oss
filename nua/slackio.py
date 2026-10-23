"""Slack: the real ports.SlackIO on slack_sdk's WebClient, and request signature checks.

Channels may be given as names ("#discovery-calls", as in config/pipeline.yml) or ids ("C0123ABC").
Names are resolved once with conversations.list and cached, because chat.update, chat.delete and
conversations.history only take ids.

Bot scopes this needs: chat:write, channels:read, groups:read, channels:history, groups:history
(metadata lookups and thread replies), plus interactivity for views.open and views.update.
"""
from __future__ import annotations

import hashlib
import hmac
import re
import time
from typing import Mapping

import httpx

SIGNATURE_MAX_AGE_SECONDS = 300
# Slack rejects chat.update with msg_too_long when the plain-text fallback passes about 4,000
# characters. The blocks carry the real content, so the fallback is only a notification preview.
MAX_FALLBACK_TEXT = 3000
_CHANNEL_ID = re.compile(r"^[CGD][A-Z0-9]{2,}$")


def _header(headers: Mapping[str, str], name: str) -> str | None:
    """Case-insensitive header lookup that works for plain dicts and Starlette headers alike."""
    value = headers.get(name)
    if value is not None:
        return value
    lower = name.lower()
    for k, v in headers.items():
        if k.lower() == lower:
            return v
    return None


def verify_slack_signature(signing_secret: str, headers: Mapping[str, str], body: bytes, *,
                           now: float | None = None) -> bool:
    """True when `X-Slack-Signature` is the v0 HMAC-SHA256 of `v0:<timestamp>:<raw body>` and
    `X-Slack-Request-Timestamp` is within 5 minutes of `now` (replay protection)."""
    if not signing_secret:
        return False
    ts = _header(headers, "X-Slack-Request-Timestamp")
    sig = _header(headers, "X-Slack-Signature")
    if not ts or not sig:
        return False
    try:
        ts_int = int(ts)
    except ValueError:
        return False
    current = time.time() if now is None else now
    if abs(current - ts_int) > SIGNATURE_MAX_AGE_SECONDS:
        return False
    base = f"v0:{ts}:".encode("utf-8") + body
    expected = "v0=" + hmac.new(signing_secret.encode("utf-8"), base, hashlib.sha256).hexdigest()
    # Bytes, not str: compare_digest raises TypeError on a str with non-ASCII characters, and Starlette
    # decodes headers as latin-1, so a forged header byte >= 0x80 must fail the check, not raise.
    return hmac.compare_digest(expected.encode("ascii"), sig.strip().encode("utf-8", "surrogateescape"))


def _next_cursor(resp) -> str:
    return ((resp.get("response_metadata") or {}).get("next_cursor") or "").strip()


def fallback_text(text: str) -> str:
    """Cap a message's plain-text fallback at MAX_FALLBACK_TEXT characters, ending in an ellipsis."""
    if len(text) <= MAX_FALLBACK_TEXT:
        return text
    return text[:MAX_FALLBACK_TEXT - 3].rstrip() + "..."


class SlackWebIO:
    """ports.SlackIO backed by `slack_sdk.WebClient`. `client` lets tests pass a fake with the same methods.

    `respond` posts to an interaction's `response_url` with `self.http` (an httpx.Client).
    """

    def __init__(self, token: str, *, client=None):
        if client is None:
            from slack_sdk import WebClient
            from slack_sdk.http_retry.builtin_handlers import RateLimitErrorRetryHandler
            client = WebClient(token=token)
            client.retry_handlers.append(RateLimitErrorRetryHandler(max_retry_count=3))
        self.client = client
        self.http = httpx.Client(timeout=10.0)
        self._channel_ids: dict[str, str] = {}
        self._identity: dict[str, str | None] | None = None

    # ---------- helpers ----------

    def channel_id(self, channel: str) -> str:
        """The id for a channel name or id. Raises ValueError when no channel has that name."""
        if _CHANNEL_ID.match(channel):
            return channel
        name = channel.lstrip("#")
        if name in self._channel_ids:
            return self._channel_ids[name]
        cursor = None
        while True:
            resp = self.client.conversations_list(types="public_channel,private_channel",
                                                  exclude_archived=True, limit=1000, cursor=cursor)
            for ch in resp.get("channels") or []:
                self._channel_ids[ch["name"]] = ch["id"]
            if name in self._channel_ids:
                return self._channel_ids[name]
            cursor = _next_cursor(resp)
            if not cursor:
                raise ValueError(f"Slack channel {channel!r} not found (is the bot invited to it?)")

    def _is_own(self, message: dict) -> bool:
        """True when this app's bot posted `message` (auth.test is called once for the bot's ids)."""
        if self._identity is None:
            resp = self.client.auth_test()
            self._identity = {"bot_id": resp.get("bot_id"), "user_id": resp.get("user_id")}
        bot_id = message.get("bot_id")
        if bot_id and bot_id == self._identity["bot_id"]:
            return True
        return bool(self._identity["user_id"]) and message.get("user") == self._identity["user_id"]

    # ---------- ports.SlackIO ----------

    def post(self, channel: str, blocks: list[dict], text: str, *, thread_ts: str | None = None,
             metadata: dict | None = None) -> str:
        """Post a message (a thread reply when `thread_ts` is set); returns its ts."""
        kwargs = {"channel": self.channel_id(channel), "blocks": blocks, "text": fallback_text(text)}
        if thread_ts:
            kwargs["thread_ts"] = thread_ts
        if metadata:
            kwargs["metadata"] = metadata
        return self.client.chat_postMessage(**kwargs)["ts"]

    def update(self, channel: str, ts: str, blocks: list[dict], text: str) -> None:
        """Replace a message's blocks and text in place."""
        self.client.chat_update(channel=self.channel_id(channel), ts=ts, blocks=blocks, text=fallback_text(text))

    def delete(self, channel: str, ts: str) -> None:
        """Delete a message. A message that is already gone counts as deleted."""
        from slack_sdk.errors import SlackApiError

        try:
            self.client.chat_delete(channel=self.channel_id(channel), ts=ts)
        except SlackApiError as e:
            if (e.response or {}).get("error") != "message_not_found":
                raise

    def find_by_metadata(self, channel: str, event_type: str, key: str, *, limit: int = 1000) -> str | None:
        """ts of the newest message among the latest `limit` in `channel` whose metadata has this
        `event_type` and `event_payload["key"] == key`, or None."""
        cid = self.channel_id(channel)
        seen = 0
        cursor = None
        while seen < limit:
            resp = self.client.conversations_history(channel=cid, include_all_metadata=True,
                                                     limit=min(200, limit - seen), cursor=cursor)
            for m in resp.get("messages") or []:
                seen += 1
                if seen > limit:
                    return None
                md = m.get("metadata") or {}
                if md.get("event_type") == event_type and (md.get("event_payload") or {}).get("key") == key:
                    return m["ts"]
            cursor = _next_cursor(resp)
            if not cursor:
                return None
        return None

    def replies(self, channel: str, ts: str) -> list[dict]:
        """Thread replies under `ts`, oldest first, without the parent. `bot` is True for this app's own."""
        cid = self.channel_id(channel)
        out: list[dict] = []
        cursor = None
        while True:
            resp = self.client.conversations_replies(channel=cid, ts=ts, limit=200, cursor=cursor)
            for m in resp.get("messages") or []:
                if m.get("ts") == ts:
                    continue
                out.append({"ts": m["ts"], "blocks": m.get("blocks") or [], "text": m.get("text") or "",
                            "bot": self._is_own(m)})
            cursor = _next_cursor(resp)
            if not cursor:
                return out

    def message(self, channel: str, ts: str) -> dict | None:
        """The message at `ts` as it is now, {"ts", "blocks", "text", "bot"}, or None when it is gone.

        conversations.history bounded to that one ts (thread replies are not returned; use `replies`)."""
        resp = self.client.conversations_history(channel=self.channel_id(channel), latest=ts, oldest=ts,
                                                 inclusive=True, limit=1)
        for m in resp.get("messages") or []:
            if m.get("ts") == ts:
                return {"ts": m["ts"], "blocks": m.get("blocks") or [], "text": m.get("text") or "",
                        "bot": self._is_own(m)}
        return None

    def open_view(self, trigger_id: str, view: dict) -> str:
        """Open a modal (views.open); returns the view id for a later update_view."""
        return self.client.views_open(trigger_id=trigger_id, view=view)["view"]["id"]

    def update_view(self, view_id: str, view: dict) -> None:
        """Replace an open modal (views.update)."""
        self.client.views_update(view_id=view_id, view=view)

    def respond(self, response_url: str, payload: dict) -> None:
        """POST `payload` to an interaction's response_url. Raises RuntimeError if Slack refuses it."""
        r = self.http.post(response_url, json=payload)
        if r.status_code != 200:
            raise RuntimeError(f"Slack response_url returned {r.status_code}: {r.text[:200]}")

    def ephemeral(self, channel: str, user: str, text: str) -> None:
        """A message only `user` sees in `channel`."""
        self.client.chat_postEphemeral(channel=self.channel_id(channel), user=user, text=text)
