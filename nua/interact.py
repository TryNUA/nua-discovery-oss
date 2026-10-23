"""Slack clicks and forms, handled by the receiver's /slack/interact route (SPEC "Slack interactions").

Slack gives the receiver 3 seconds, cold start included (the receiver scales to zero), so nothing here
reads the repo, calls GitHub, commits or calls a model: it works from an `AckCtx` (the Slack client and
the queue) and from the interaction payload alone. Anyone in the channel may act; the change is
recorded as theirs by Slack handle (apply shows a teammate's first name when the handle matches one in
config/people.yml). A click either:
- queues a change at once (Approve, Reject, Keep, Retire, Watch, Wrong read, Re-run): the task is
  enqueued under a name made from its `ref` ("<message ts>:<block_id>"), so a second click on the same
  item, including Approve then Reject, collides and gets an ephemeral "Already queued by alex". Then
  only the clicked block is locked through `response_url`; other items in the message stay live.
- or opens a form (Make belief, Edit, Reword, Split): `views.open` right away, pre-filled only from the
  clicked message's blocks and the button's value. Each statement has an optional scope field, left
  blank: blank keeps the finding's, proposal's (part's) or belief's own scope, which apply fills in.
  Nothing locks until the form is submitted; the submission is validated, queued, then locked before
  the response (nothing runs after it: the receiver has CPU only while a request is open).
  The form remembers a fingerprint of each pre-filled statement, so a statement left as shown is sent
  as "unchanged" and apply keeps the stored wording instead of the message's display text.

Queued changes use the payload `nua.apply.apply_task` takes: {"ref", "change": {ChangeRequest fields},
"via": "slack", "slack_item": {"channel", "ts", "block_id", "thread_ts"}}. A finding (and the claim a
Wrong read names) travels as a source {"call_id", "span": "<start>-<end>", "quote"}: apply resolves the
span through the digest to the verbatim quote (claim ids change on re-digest, spans do not), and takes
the claim's text and scope when none is given. An Edit of a split proposal sends `part_edits`, one per
form field ({"statement": text, "" to drop the part or None to keep it; "scope": dict or None to keep
it}); apply merges them into the proposal's parts. `quote` is the quote the clicked message showed; spans
belong to one transcript file, so apply checks the claim at the span still says it, and looks the
quote up instead when the call was re-digested from a richer copy after the click. A Wrong read value
may end with the entry version its read was checked against (`...|<verdict>|<version>`), sent as the
change's `version`: apply answers the click as stale once the call was re-checked against a new wording.

Keys come from nua/brief.py (findings, Wrong read) and nua/rollup.py (`prop:<id>` and `pressure:<id>`
action blocks). Buttons are recognised by action_id, with a prefix such as `prop_` ignored, or else by
their label. The lock line uses apply's `<block_id>:status` convention, so the outcome replaces it.
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
import threading
from collections import OrderedDict
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from nua.ports import TASK_KINDS
from nua.tasks import task_name
from nua.types import VERDICTS, Scope

if TYPE_CHECKING:
    from nua.context import Ctx

log = logging.getLogger("nua.interact")

FORM_CALLBACK_ID = "discovery_form"
LOCK_TEXT = "Queued by {who}"
NO_CLICKER = "Slack did not say who clicked, so nothing was queued."
MAX_SPLIT_PARTS = 10          # statement fields a split form shows at most
SCOPE_HINT = "key: value pairs split by ; with keys line, party, segment, seat"
MAX_STATEMENT = 500
MAX_REMEMBERED = 500
MAX_METADATA = 2900           # a modal's private_metadata (Slack allows 3000 characters)
SPLIT_MIN = "A split needs at least two statements."
NOTHING_TO_REWORD = "Change the statement or the scope, or press Cancel."

IMMEDIATE = ("approve", "reject", "keep", "retire", "watch", "wrong_read", "rerun")
FORMS = ("make_belief", "edit", "reword", "split")
_ALIASES = {"make belief": "make_belief", "wrong read": "wrong_read", "re-run": "rerun", "re run": "rerun",
            "re_run": "rerun", "dismiss": "wrong_read", "dismiss_verdict": "wrong_read"}
_PREFIXES = ("discovery_", "prop_", "proposal_", "pressure_", "finding_", "rollup_")
_TITLES = {"make_belief": "Make belief", "edit": "Edit proposal", "reword": "Reword", "split": "Split"}
_CALL_ID = re.compile(r"[A-Za-z0-9_.-]+")
_ENTRY_ID = re.compile(r"[BW]-\d+")
_LEAD_LABEL = re.compile(r"^\s*\*([^*]+)\*\s*")
_EM_DASH = chr(0x2014)
_SPLIT_PART = re.compile(r"^- \*(.+?)\*(?: \(.*\))?$")      # a split proposal's part line (nua/rollup.py)
_SCOPE_KEEPS = {"make_belief": "the finding's", "edit": "the proposal's", "reword": "the belief's",
                "split": "the belief's"}


class _Refused(Exception):
    """A click that cannot be acted on; the message goes back to the user as an ephemeral."""


# ---------- per-process memory (a hint only; task names are the real guard) ----------

_state_lock = threading.Lock()
_locks: "OrderedDict[str, str]" = OrderedDict()                        # ref -> who queued it
_messages: "OrderedDict[tuple[str, str], tuple[list, str]]" = OrderedDict()   # (channel, ts) -> (blocks, text)


def reset_state() -> None:
    """Forget remembered locks and messages (tests)."""
    with _state_lock:
        _locks.clear()
        _messages.clear()


def _remember(store: OrderedDict, key: Any, value: Any) -> None:
    with _state_lock:
        store[key] = value
        store.move_to_end(key)
        while len(store) > MAX_REMEMBERED:
            store.popitem(last=False)


def _lock_holder(ref: str) -> str | None:
    with _state_lock:
        return _locks.get(ref)


# ---------- entry point ----------

@dataclass
class AckCtx:
    """All a click or a form submission may use: the Slack client and the queue. It has no repo, so
    nothing here can read one."""
    slack: Any
    queue: Any

    @classmethod
    def from_ctx(cls, ctx: "Ctx") -> "AckCtx":
        """From a full Ctx (local runs and tests): its Slack and queue."""
        return cls(slack=ctx.slack, queue=ctx.queue)


def handle_interaction(ctx: "AckCtx | Ctx", payload: dict) -> dict | None:
    """Handle one Slack interaction payload and return the HTTP JSON body (None for an empty 200).

    Everything happens before the response: the receiver has CPU only while a request is open.
    """
    ack = ctx if isinstance(ctx, AckCtx) else AckCtx.from_ctx(ctx)
    kind = payload.get("type")
    if kind == "block_actions":
        return _block_actions(ack, payload)
    if kind == "view_submission":
        return _view_submission(ack, payload)
    return None


@dataclass
class _Click:
    channel: str
    ts: str
    thread_ts: str | None
    block_id: str
    user_id: str | None
    who: str                 # the clicker's Slack handle, for "Queued by ..."; "" when Slack gave no user
    requested_by: str        # the same handle, recorded by apply
    response_url: str | None
    trigger_id: str | None
    blocks: list[dict]
    text: str

    @property
    def ref(self) -> str:
        return f"{self.ts}:{self.block_id}"

    @property
    def in_thread(self) -> bool:
        return bool(self.thread_ts) and self.thread_ts != self.ts


def _who(user: dict) -> tuple[str, str] | None:
    """(name to show, requested_by) for the clicker: their Slack handle (user id when Slack sends no
    handle), or None when the payload names no user at all."""
    user = user or {}
    uid = user.get("id")
    if not uid:
        return None
    handle = str(user.get("username") or user.get("name") or uid).strip() or uid
    return handle, handle


def _click(ctx: AckCtx, payload: dict, action: dict) -> _Click:
    container = payload.get("container") or {}
    message = payload.get("message") or {}
    who, key = _who(payload.get("user") or {}) or ("", "")
    return _Click(
        channel=container.get("channel_id") or (payload.get("channel") or {}).get("id") or "",
        ts=container.get("message_ts") or message.get("ts") or "",
        thread_ts=container.get("thread_ts") or message.get("thread_ts"),
        block_id=action.get("block_id") or "",
        user_id=(payload.get("user") or {}).get("id"),
        who=who, requested_by=key,
        response_url=payload.get("response_url"),
        trigger_id=payload.get("trigger_id"),
        blocks=list(message.get("blocks") or []),
        text=message.get("text") or "",
    )


def _slack_item(channel: str, ts: str, block_id: str, thread_ts: str | None,
                blocks: list[dict] | None = None, text: str = "") -> dict:
    """The clicked item for apply to mark after its commit. Apply re-reads the message as it is then (a
    thread reply from its thread, a top-level message by its ts); a top-level message also carries its
    blocks as locked here, used only when that read fails."""
    item: dict[str, Any] = {"channel": channel, "ts": ts, "block_id": block_id, "thread_ts": thread_ts}
    if (not thread_ts or thread_ts == ts) and blocks is not None:
        item["blocks"] = locked_blocks(blocks, block_id, "Queued")
        item["text"] = text
    return item


def _tell(ctx: AckCtx, c: _Click, text: str) -> None:
    if ctx.slack is None or not c.channel or not c.user_id:
        return
    try:
        ctx.slack.ephemeral(c.channel, c.user_id, text)
    except Exception:
        log.exception("ephemeral failed")


# ---------- block actions ----------

def _block_actions(ctx: AckCtx, payload: dict) -> None:
    actions = payload.get("actions") or []
    if not actions:
        return None
    action = actions[0]
    c = _click(ctx, payload, action)
    verb, value = _intent(action)
    try:
        if not c.requested_by:           # Slack always names the clicker; refuse a payload that does not
            raise _Refused(NO_CLICKER)
        if verb is None:
            raise _Refused("That button is not wired up.")
        if verb in FORMS:
            _open_form(ctx, c, verb, value)
        else:
            kind, task, name = _immediate_task(c, verb, value)
            _queue_and_lock(ctx, c, kind, task, name)
    except _Refused as e:
        _tell(ctx, c, str(e))
    return None


def _intent(action: dict) -> tuple[str | None, str]:
    """(verb, value) for a clicked element. Finding menus are read from their option value."""
    option = action.get("selected_option")
    value = (option or {}).get("value", "") if option else (action.get("value") or "")
    head = value.split("|", 1)[0]
    if "|" in value and head in ("mb", "w"):
        return ("make_belief" if head == "mb" else "watch"), value
    verb = _verb(action.get("action_id") or "")
    if verb is None:
        verb = _verb(((action.get("text") or {}).get("text")) or "")
    return verb, value


def _verb(raw: str) -> str | None:
    s = raw.strip().lower().split(":", 1)[0]
    for prefix in _PREFIXES:
        if s.startswith(prefix):
            s = s[len(prefix):]
            break
    s = _ALIASES.get(s, s).replace(" ", "_").replace("-", "_")
    s = _ALIASES.get(s, s)
    return s if s in IMMEDIATE + FORMS else None


def _subject(block_id: str, value: str) -> tuple[str | None, str | None]:
    """("proposal", id) for a `prop:<id>` block, ("entry", id) for `pressure:<id>`, else a guess from the value."""
    bid = re.sub(r":\d+$", "", block_id)      # brief-style ":2" suffixes for repeated block ids
    if bid.startswith("prop:"):
        return "proposal", bid[len("prop:"):]
    if bid.startswith("pressure:"):
        return "entry", bid[len("pressure:"):]
    if _ENTRY_ID.fullmatch(value or ""):
        return "entry", value
    if value:
        return "proposal", value
    return None, None


def _finding_value(value: str) -> tuple[str, tuple[int, int]]:
    """"mb|<call_id>|<start>-<end>" -> (call_id, (start, end))."""
    parts = value.split("|")
    span = parse_span(parts[-1]) if len(parts) >= 3 else None
    call_id = parts[1] if len(parts) >= 3 else ""
    if span is None or not _CALL_ID.fullmatch(call_id) or ".." in call_id:
        raise _Refused("That finding link is malformed.")
    return call_id, span


def parse_span(text: str) -> tuple[int, int] | None:
    """"120-180" -> (120, 180); None when malformed."""
    m = re.fullmatch(r"(\d+)-(\d+)", (text or "").strip())
    return (int(m.group(1)), int(m.group(2))) if m else None


def _span_source(call_id: str, offset: tuple[int, int] | list[int], quote: str | None = None) -> dict:
    """A source by span, plus the quote the clicked message showed there (apply's check that the span
    still points at that quote after a re-digest)."""
    source = {"call_id": call_id, "span": f"{int(offset[0])}-{int(offset[1])}"}
    if quote:
        source["quote"] = quote
    return source


def _immediate_task(c: _Click, verb: str, value: str) -> tuple[str, dict, str]:
    """(task kind, payload, task name) for a click that queues at once."""
    if verb == "rerun":
        try:
            data = json.loads(value)
        except ValueError:
            raise _Refused("That Re-run button is malformed.") from None
        kind, task = data.get("kind"), dict(data.get("payload") or {})
        if kind not in TASK_KINDS:
            raise _Refused(f"Unknown task kind {kind!r}.")
        if kind == "process_call":
            task["force"] = True
        return kind, task, task_name(kind, json.dumps(task, sort_keys=True), nonce=c.ts)

    change: dict[str, Any] = {"requested_by": c.requested_by}
    if verb == "watch":
        call_id, offset = _finding_value(value)
        _, quote = _finding_texts(c.blocks, c.block_id)
        change.update(action="watch", sources=[_span_source(call_id, offset, quote)])
    elif verb == "wrong_read":
        # <call_id>|<target>|<start>-<end>[|<verdict>[|<version>]]. The brief puts Wrong read on
        # contradictions only, so a missing or empty verdict part means contradicts. The version is the
        # one the read was checked against when the brief was posted (a ref, which may hold "|"): apply
        # answers a click on a read of older wording as stale.
        parts = value.split("|", 4)
        offset = parse_span(parts[2]) if len(parts) >= 3 else None
        verdict = (parts[3] if len(parts) >= 4 else "") or "contradicts"
        version = parts[4].strip() if len(parts) == 5 else ""
        if (offset is None or not _ENTRY_ID.fullmatch(parts[1]) or not _CALL_ID.fullmatch(parts[0])
                or verdict not in VERDICTS):
            raise _Refused("That Wrong read button is malformed.")
        change.update(action="dismiss_verdict", target=parts[1], verdict=verdict,
                      sources=[_span_source(parts[0], offset, _verdict_quote(c.blocks, c.block_id))])
        if version:
            change["version"] = version
    elif verb in ("approve", "reject"):
        kind, pid = _subject(c.block_id, value)
        if kind != "proposal" or not pid:
            raise _Refused("Could not tell which proposal this is.")
        change.update(action=verb, proposal_id=pid)
    elif verb in ("keep", "retire"):
        kind, entry = _subject(c.block_id, value)
        if kind != "entry" or not entry:
            raise _Refused("Could not tell which belief this is.")
        change.update(action=verb, target=entry)
    else:
        raise _Refused("That button is not wired up.")

    task = {"ref": c.ref, "change": change, "via": "slack",
            "slack_item": _slack_item(c.channel, c.ts, c.block_id, c.thread_ts, c.blocks, c.text)}
    return "apply_belief_change", task, task_name("apply_belief_change", c.ref)


def _queue_and_lock(ctx: AckCtx, c: _Click, kind: str, task: dict, name: str) -> None:
    holder = _lock_holder(c.ref)
    if holder:
        raise _Refused(f"Already queued by {holder}.")
    if not ctx.queue.enqueue(kind, task, name=name):
        raise _Refused("Already queued.")
    _remember(_locks, c.ref, c.who)
    new_blocks = locked_blocks(c.blocks, c.block_id, LOCK_TEXT.format(who=c.who))
    try:
        _replace_message(ctx, c.response_url, c.channel, c.ts, new_blocks, c.text)
    except Exception:           # the task is queued; apply marks the item after its commit anyway
        log.exception("could not lock %s", c.ref)


def _replace_message(ctx: AckCtx, response_url: str | None, channel: str, ts: str, blocks: list[dict], text: str) -> None:
    if response_url:
        ctx.slack.respond(response_url, {"replace_original": True, "blocks": blocks, "text": text or "Queued"})
    else:
        ctx.slack.update(channel, ts, blocks, text or "Queued")


def locked_blocks(blocks: list[dict], block_id: str, text: str) -> list[dict]:
    """`blocks` with only `block_id` locked: an actions block becomes a context line with the same
    block_id; a section loses its accessory (the menu) and gets a context line `<block_id>:status`
    after it, the line nua.apply later replaces with the outcome."""
    note = [{"type": "mrkdwn", "text": text}]
    status = f"{block_id}:status"
    out: list[dict] = []
    for b in blocks:
        if b.get("block_id") == status:
            continue
        if b.get("block_id") != block_id:
            out.append(b)
        elif b.get("type") == "section":
            out.append({k: v for k, v in b.items() if k != "accessory"})
            out.append({"type": "context", "block_id": status, "elements": note})
        else:
            out.append({"type": "context", "block_id": block_id, "elements": note})
    return out


# ---------- forms ----------

def _open_form(ctx: AckCtx, c: _Click, verb: str, value: str) -> None:
    """Open the form at once (the trigger_id expires in 3 seconds), pre-filled from the payload only."""
    holder = _lock_holder(c.ref)
    if holder:
        raise _Refused(f"Already queued by {holder}.")
    meta: dict[str, Any] = {"form": verb, "channel": c.channel, "ts": c.ts, "thread_ts": c.thread_ts,
                            "block_id": c.block_id, "response_url": c.response_url}
    prefill: dict[str, Any] = {}
    if verb == "make_belief":
        call_id, offset = _finding_value(value)
        meta.update(call_id=call_id, offset=list(offset))
        text, quote = _finding_texts(c.blocks, c.block_id)
        meta["quote"] = _metadata_quote(meta, quote)
        prefill = {"statement": text, "quote": quote}
    elif verb == "edit":
        kind, pid = _subject(c.block_id, value)
        if kind != "proposal" or not pid:
            raise _Refused("Could not tell which proposal this is.")
        meta["proposal_id"] = pid
        split, statements = _proposal_shown(c.blocks, c.block_id)
        if split:
            statements = statements[:MAX_SPLIT_PARTS]
            meta.update(split=True, parts=max(2, len(statements)))
            prefill = {"statements": statements}
            meta["pre"] = [_fingerprint(t) for t in statements]
        else:
            prefill = {"statement": statements[0] if statements else ""}
            meta["pre"] = [_fingerprint(prefill["statement"])]
    else:
        kind, entry = _subject(c.block_id, value)
        if kind != "entry" or not entry:
            raise _Refused("Could not tell which belief this is.")
        meta["target"] = entry
        statement = _message_statement(c.blocks, c.block_id)
        if verb == "split":
            meta.update(split=True, parts=2)
            prefill = {"statements": [statement, ""]}
        else:
            prefill = {"statement": statement}
            meta["pre"] = [_fingerprint(statement)]

    if not c.in_thread:
        _remember(_messages, (c.channel, c.ts), (c.blocks, c.text))
    ctx.slack.open_view(c.trigger_id, form_view(meta, prefill))


def _fingerprint(text: str) -> str:
    """A short hash of a pre-filled statement, kept in the form's metadata to tell an untouched field."""
    return hashlib.sha256(_one_line(text).encode("utf-8")).hexdigest()[:10]


def _unchanged(meta: dict, index: int, typed: str) -> bool:
    pre = meta.get("pre") or []
    return index < len(pre) and pre[index] == _fingerprint(typed)


def form_view(meta: dict, prefill: dict) -> dict:
    """The modal for one form: each statement with its optional scope field, left blank."""
    form = meta["form"]
    optional = form == "edit"                    # an empty statement keeps the proposal's
    keeps = _SCOPE_KEEPS.get(form, "the current")
    blocks: list[dict] = []
    if meta.get("split"):
        count = _parts(meta)
        statements = list(prefill.get("statements") or []) + [""] * count
        part_keeps = "this part's" if form == "edit" else keeps
        for i in range(1, count + 1):
            blocks.append(_input(f"statement_{i}", f"Statement {i}", statements[i - 1], optional=optional))
            blocks.append(_input(f"scope_{i}", f"Scope {i}", "", optional=True,
                                 hint=f"Blank keeps {part_keeps} scope. {SCOPE_HINT}"))
    else:
        blocks.append(_input("statement", "Statement", prefill.get("statement"), multiline=True, optional=optional))
        if prefill.get("quote"):
            blocks.append({"type": "context", "block_id": "quote",
                           "elements": [{"type": "mrkdwn", "text": _clip(f'> "{prefill["quote"]}"', 2000)}]})
        blocks.append(_input("scope", "Scope", "", optional=True, hint=f"Blank keeps {keeps} scope. {SCOPE_HINT}"))
    blocks.append(_input("reason", "Reason", "", optional=True))

    title = _TITLES[form] + (f" {meta['target']}" if meta.get("target") else "")
    return {"type": "modal", "callback_id": FORM_CALLBACK_ID,
            "title": {"type": "plain_text", "text": title[:24]},
            "submit": {"type": "plain_text", "text": "Submit"},
            "close": {"type": "plain_text", "text": "Cancel"},
            "private_metadata": json.dumps(meta), "blocks": blocks}


def _parts(meta: dict) -> int:
    """Statement fields on a split form: one per part of the proposal (at least two)."""
    try:
        n = int(meta.get("parts") or 2)
    except (TypeError, ValueError):
        n = 2
    return min(max(2, n), MAX_SPLIT_PARTS)


def _input(block_id: str, label: str, initial: str | None, *, optional: bool = False, multiline: bool = False,
           hint: str | None = None) -> dict:
    element: dict[str, Any] = {"type": "plain_text_input", "action_id": "value", "multiline": multiline}
    if initial:
        element["initial_value"] = _clip(initial, 3000)
    block: dict[str, Any] = {"type": "input", "block_id": block_id, "optional": optional,
                             "label": {"type": "plain_text", "text": label}, "element": element}
    if hint:
        block["hint"] = {"type": "plain_text", "text": hint}
    return block


def _view_submission(ctx: AckCtx, payload: dict) -> dict | None:
    view = payload.get("view") or {}
    if view.get("callback_id") != FORM_CALLBACK_ID:
        return None
    meta = json.loads(view.get("private_metadata") or "{}")
    form = meta.get("form")
    if form not in FORMS:
        return None
    first = "statement_1" if meta.get("split") else "statement"
    ident = _who(payload.get("user") or {})
    if ident is None:
        return {"response_action": "errors", "errors": {first: NO_CLICKER}}
    who, key = ident
    values = _state_values(view)
    errors: dict[str, str] = {}
    required = form != "edit"
    change: dict[str, Any]

    if meta.get("split"):
        typed = []
        for i in range(1, _parts(meta) + 1):
            statement = _statement_field(values.get(f"statement_{i}"), f"statement_{i}", errors, required)
            typed.append((statement, _scope_field(values, f"scope_{i}", errors)))
        if form == "edit":
            change = {"action": "approve", "proposal_id": meta.get("proposal_id")}
            edits = _part_edits(meta, typed, errors)
            if edits is not None:
                change["part_edits"] = edits
        else:
            change = {"action": "split", "target": meta.get("target"),
                      "statements": [{"statement": t, "scope": (sc or Scope()).to_dict()} for t, sc in typed]}
    else:
        statement = _statement_field(values.get("statement"), "statement", errors, required)
        scope = _scope_field(values, "scope", errors)
        if form == "make_belief":
            change = {"action": "add", "statement": statement,
                      "sources": [_span_source(meta.get("call_id"), meta.get("offset") or [0, 0], meta.get("quote"))]}
        elif form == "edit":
            change = {"action": "approve", "proposal_id": meta.get("proposal_id")}
            if statement and not _unchanged(meta, 0, statement):
                change["statement"] = statement
        else:
            change = {"action": "reword", "target": meta.get("target")}
            if statement and not _unchanged(meta, 0, statement):
                change["statement"] = statement
            elif scope is None and not errors:
                errors["statement"] = NOTHING_TO_REWORD
        if scope is not None:
            change["scope"] = scope.to_dict()
    if errors:
        return {"response_action": "errors", "errors": errors}

    reason = _one_line(values.get("reason") or "")
    if reason:
        change["reason"] = reason
    change["requested_by"] = key
    ref = f"{meta.get('ts')}:{meta.get('block_id')}"
    blocks, text = _remembered(meta.get("channel"), meta.get("ts"))
    task = {"ref": ref, "change": change, "via": "slack",
            "slack_item": _slack_item(meta.get("channel"), meta.get("ts"), meta.get("block_id"),
                                      meta.get("thread_ts"), blocks, text)}

    holder = _lock_holder(ref)
    if holder:
        return {"response_action": "errors", "errors": {first: f"Already queued by {holder}."}}
    if not ctx.queue.enqueue("apply_belief_change", task, name=task_name("apply_belief_change", ref)):
        return {"response_action": "errors", "errors": {first: "Already queued."}}
    _remember(_locks, ref, who)
    _lock_after_form(ctx, meta, who)
    return None


def _part_edits(meta: dict, typed: list[tuple[str, Scope | None]], errors: dict[str, str]) -> list[dict] | None:
    """What an Edit of a split proposal changes, one entry per form field, for apply to merge into the
    proposal's parts: "statement" is the new text, "" to drop the part, or None when the field still
    holds what the form showed (apply keeps the stored wording); "scope" is the typed scope, or None to
    keep the part's. At least two parts must remain (else a form error). None when nothing changed."""
    edits: list[dict] = []
    cleared: list[str] = []
    kept = 0
    for i, (statement, scope) in enumerate(typed):
        same = _unchanged(meta, i, statement)
        if statement or same:
            kept += 1
        else:
            cleared.append(f"statement_{i + 1}")
        edits.append({"statement": None if same else statement, "scope": scope.to_dict() if scope else None})
    if kept < 2:
        for block_id in cleared or ["statement_1"]:
            errors[block_id] = SPLIT_MIN
        return None
    if all(e["statement"] is None and e["scope"] is None for e in edits):
        return None
    return edits


def _state_values(view: dict) -> dict[str, str | None]:
    """block_id -> submitted text (the first element's value in each input block)."""
    out: dict[str, str | None] = {}
    for block_id, actions in ((view.get("state") or {}).get("values") or {}).items():
        first = next(iter((actions or {}).values()), None) or {}
        out[block_id] = first.get("value")
    return out


def _statement_field(value: str | None, block_id: str, errors: dict[str, str], required: bool) -> str:
    text = _one_line(value or "")
    if not text and required:
        errors[block_id] = "Write the statement."
    elif len(text) > MAX_STATEMENT:
        errors[block_id] = f"Keep it under {MAX_STATEMENT} characters."
    return text


def _scope_field(values: dict, block_id: str, errors: dict[str, str]) -> Scope | None:
    """The typed scope; None when the field is blank (or absent), in which case apply keeps the
    claim's, proposal's (part's) or belief's own scope."""
    from nua.beliefs import parse_scope

    text = _one_line(values.get(block_id) or "")
    if not text:
        return None
    try:
        scope = parse_scope(text)
    except ValueError:
        errors[block_id] = f"Use {SCOPE_HINT}, e.g. line: onboarding; segment: mid-market"
        return None
    return None if scope.is_empty() else scope


def _lock_after_form(ctx: AckCtx, meta: dict, who: str) -> None:
    """Lock the item once its form is queued (before the response; Slack only, best effort: apply
    marks the item after its commit anyway)."""
    blocks, text = _current_message(ctx, meta)
    if blocks is None:
        return
    new_blocks = locked_blocks(blocks, meta.get("block_id"), LOCK_TEXT.format(who=who))
    try:
        _replace_message(ctx, meta.get("response_url"), meta.get("channel"), meta.get("ts"), new_blocks, text)
    except Exception:
        log.exception("could not lock %s:%s", meta.get("ts"), meta.get("block_id"))


def _remembered(channel: str | None, ts: str | None) -> tuple[list[dict] | None, str]:
    with _state_lock:
        found = _messages.get((channel, ts))
    return (list(found[0]), found[1]) if found else (None, "")


def _current_message(ctx: AckCtx, meta: dict) -> tuple[list[dict] | None, str]:
    """The item's message as it is now (another item may have been locked since the form opened): read
    from its thread when it is a reply, by its ts when it is a top-level message and the Slack port can
    read one (`message`), else as remembered when the form opened."""
    channel, ts, thread_ts = meta.get("channel"), meta.get("ts"), meta.get("thread_ts")
    if thread_ts and thread_ts != ts:
        try:
            for reply in ctx.slack.replies(channel, thread_ts):
                if reply.get("ts") == ts:
                    return list(reply.get("blocks") or []), reply.get("text") or ""
        except Exception:
            log.exception("could not read thread %s", thread_ts)
    else:
        fetch = getattr(ctx.slack, "message", None)
        if callable(fetch):
            try:
                m = fetch(channel, ts)
                if m is not None and m.get("blocks") is not None:
                    return list(m.get("blocks") or []), m.get("text") or ""
            except Exception:
                log.exception("could not read message %s", ts)
    return _remembered(channel, ts)


# ---------- reading the clicked message ----------

def _block(blocks: list[dict], block_id: str) -> tuple[int, dict | None]:
    for i, b in enumerate(blocks):
        if b.get("block_id") == block_id:
            return i, b
    return -1, None


def _finding_texts(blocks: list[dict], block_id: str) -> tuple[str, str]:
    """(claim text, quote) from a finding's section block, as the brief rendered them."""
    from nua.brief import finding_from_block

    _, block = _block(blocks, block_id)
    if block is None or block.get("type") != "section":
        return "", ""
    text, quote = finding_from_block(block)
    return _one_line(text), _shown_quote(quote)


def _verdict_quote(blocks: list[dict], block_id: str) -> str:
    """The quote a contradiction shows first (the claim its Wrong read button names), from the
    `verdict-text:<target>:<n>` section the brief puts above the `verdict:<target>:<n>` button, else the
    nearest section above the button. "" when there is none."""
    index, _ = _block(blocks, block_id)
    if index < 0:
        return ""
    section = None
    if block_id.startswith("verdict:"):
        _, section = _block(blocks, "verdict-text:" + block_id[len("verdict:"):])
    if section is None:
        section = next((b for b in reversed(blocks[:index]) if b.get("type") == "section"), None)
    for line in (((section or {}).get("text") or {}).get("text") or "").split("\n"):
        if line.startswith('> "'):
            return _shown_quote(line[2:].replace("&lt;", "<").replace("&gt;", ">").replace("&amp;", "&"))
    return ""


def _shown_quote(text: str) -> str:
    """A quote as a message showed it, on one line, without its quote marks. A quote cut off for display
    ("...") loses the cut-off word, so it stays a prefix of the real quote."""
    text = _one_line(text)
    if text.startswith('"'):
        text = text[1:]
    if text.endswith('"'):
        text = text[:-1]
    elif text.endswith("..."):
        text = text[:-3].rsplit(" ", 1)[0] if " " in text else ""
    return text.strip()


def _metadata_quote(meta: dict, quote: str) -> str:
    """`quote` cut at a word boundary so the form's private_metadata stays under MAX_METADATA."""
    room = MAX_METADATA - len(json.dumps({**meta, "quote": ""}))
    while quote and len(json.dumps(quote)) - 2 > room:
        over = len(json.dumps(quote)) - 2 - room
        cut = quote[: max(0, len(quote) - over)]
        quote = cut.rsplit(" ", 1)[0] if " " in cut else ""
    return quote


def _item_section(blocks: list[dict], block_id: str) -> dict | None:
    """The section that shows the clicked item: the clicked block itself when it is a section, the
    roll-up's `item:<block_id>` section, else the nearest section above the clicked block (else the first)."""
    index, block = _block(blocks, block_id)
    if block is not None and block.get("type") == "section":
        return block
    _, section = _block(blocks, "item:" + block_id)
    if section is not None and section.get("type") == "section":
        return section
    if index >= 0:
        section = next((b for b in reversed(blocks[:index]) if b.get("type") == "section"), None)
    return section or next((b for b in blocks if b.get("type") == "section"), None)


def _proposal_shown(blocks: list[dict], block_id: str) -> tuple[bool, list[str]]:
    """(is a split, statements) of a roll-up proposal as its message shows it: a split's "- *part*
    (scope)" lines, else the one proposed statement."""
    section = _item_section(blocks, block_id)
    lines = (((section or {}).get("text") or {}).get("text") or "").split("\n")
    parts = [_plain(m.group(1)) for m in (_SPLIT_PART.match(line.strip()) for line in lines[1:]) if m]
    if len(parts) >= 2 or (parts and re.match(r"^\*Split\b", lines[0].strip())):
        return True, parts
    return False, [_message_statement(blocks, block_id)]


def _message_statement(blocks: list[dict], block_id: str) -> str:
    """The statement shown for a roll-up item, read from the clicked message (the form's pre-fill).

    Reads the item's section: a "Proposed: *...*" line, else a fully bold line under the title, else
    the first line without its bold label ("*Under pressure: B-001* ...").
    """
    section = _item_section(blocks, block_id)
    if section is None:
        return ""
    lines = ((section.get("text") or {}).get("text") or "").split("\n")
    for line in lines:
        m = re.match(r"^Proposed:\s*\*(.+)\*\s*$", line.strip())
        if m:
            return _plain(m.group(1))
    for line in lines[1:]:
        m = re.fullmatch(r"\*([^*]+)\*", line.strip())
        if m:
            return _plain(m.group(1))
    first = lines[0]
    m = _LEAD_LABEL.match(first)
    if m:
        first = first[m.end():].strip() or m.group(1)
    first = re.sub(r"^[BW]-\d+[:.]?\s+", "", first.strip())
    return _plain(first)


def _plain(mrkdwn: str) -> str:
    text = mrkdwn.replace("*", "").replace("`", "").strip().strip("_").strip().strip('"')
    return _one_line(text.replace("&lt;", "<").replace("&gt;", ">").replace("&amp;", "&"))


def _one_line(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").replace(_EM_DASH, "-")).strip()


def _clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 3] + "..."
