"""Questions asked in Slack: "@<bot> <question>" (SPEC "Asking in Slack").

The receiver's /slack/events route gets Slack's `app_mention` event. Slack wants an answer within 3
seconds and an ask takes a minute or two, so the receiver only reads the message (`parse_mention`) and
queues an `ask` task named after the event id (Slack resends an event it thinks was missed; the name
makes the copy a no-op). A bare mention gets the help text at once, with no task.

The processor runs `run`. First one cheap call (`route`, prompt nua/prompts/route.py, the `ask_map`
model) decides what the message is, reading the thread so far when the mention is a reply in one:
- a question the calls could answer: rewritten to stand alone (so "what about brokers?" works as a
  follow-up), then "On it" in the thread, nua.ask over every digest, and one reply with the answer,
  its verified quotes and the coverage line (`answer_blocks`);
- a request to list the beliefs (or show one, "B-007") or the findings: read straight from
  beliefs-status.md or findings.md (`beliefs_blocks`, `findings_blocks`), no search;
- anything else (a greeting, thanks, a test, a question about the bot, a request it cannot carry
  out): the router's short reply, and no search.
When the router call fails, a plain rule stands in (`_fallback_route`): a "?" or at least
MIN_QUESTION_WORDS words is asked, anything else gets the help text.

Filters go anywhere in the message: `since:2026-09-01` and `seat:"<seat from config/seats.yml>"` (quotes
needed only when the seat has spaces). Anyone in a channel the bot is in may ask; each answer costs
model calls (SPEC "Costs").
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from nua.schemas import Reply, Text, parse_reply, strict_schema

if TYPE_CHECKING:
    from nua.ask import AskResult
    from nua.context import Ctx

MAX_QUESTION = 1000
MAX_SECTION = 2900             # Slack's limit for a section's text is 3000 characters
MAX_BLOCKS = 48                # and 50 blocks per message
MAX_QUOTE = 300                # a citation's quote is cut to this many characters
MIN_QUESTION_WORDS = 3         # without the router, shorter messages with no "?" get the help text
THREAD_MESSAGES = 8            # earlier thread messages the router reads, newest kept
THREAD_MESSAGE_CHARS = 600     # each cut to this many characters
ROUTE_MAX_TOKENS = 600
ROUTE_TIMEOUT_SECONDS = 30

ON_IT = "On it: reading every call. This takes a minute or two."
HELP = ('Mention me with a question and I read every call to answer it, e.g. "what do customers say '
        'about pricing?" I can also list the beliefs ("list beliefs", "show B-001") and findings ("list findings"). '
        'Optional filters: `since:2026-09-01` and `seat:"<seat>"`.')

_MENTION = re.compile(r"<@[A-Z0-9]+(?:\|[^>]*)?>")
_SINCE = re.compile(r"(?i)\bsince:\s*(\d{4}-\d{2}-\d{2})\b")
_SEAT = re.compile(r'(?i)\bseat:\s*(?:"([^"]+)"|“([^”]+)”|(\S+))')
_LINK = re.compile(r"<(?:mailto:)?([^>|]+)\|([^>]+)>|<([^>|@#!][^>]*)>")


@dataclass
class Question:
    text: str
    since: str | None = None
    seat: str | None = None


def parse_mention(text: str) -> Question:
    """The question in an app_mention's text: mentions removed, Slack link markup unwrapped, and the
    `since:` and `seat:` filters taken out. An empty `text` means there was no question."""
    s = _MENTION.sub(" ", text or "")
    since = seat = None
    m = _SINCE.search(s)
    if m:
        since = m.group(1)
        s = s[:m.start()] + " " + s[m.end():]
    m = _SEAT.search(s)
    if m:
        seat = (m.group(1) or m.group(2) or m.group(3) or "").strip() or None
        s = s[:m.start()] + " " + s[m.end():]
    s = _LINK.sub(lambda l: l.group(2) or l.group(3) or l.group(1) or "", s)
    s = s.replace("&lt;", "<").replace("&gt;", ">").replace("&amp;", "&")
    s = " ".join(s.split())[:MAX_QUESTION]
    return Question(text=s, since=since, seat=seat)


# ---------- routing ----------

class RouteReply(Reply):
    route: Text = ""
    question: Text = ""
    reply: Text = ""
    target: Text = ""          # for "beliefs": one B-/W- id to show, or ""


ROUTE_SCHEMA = strict_schema(RouteReply)


@dataclass
class Route:
    kind: str                  # "ask" | "reply" | "beliefs" | "findings"
    question: str = ""         # for "ask": the question to search for
    reply: str = ""            # for "reply": the text to post
    target: str = ""           # for "beliefs": one B-/W- id, or "" for all


def _fallback_route(text: str) -> Route:
    if "?" in text or len(text.split()) >= MIN_QUESTION_WORDS:
        return Route("ask", question=text)
    return Route("reply", reply=HELP)


def _thread_text(ctx: "Ctx", channel: str, thread_ts: str, mention_ts: str | None) -> str:
    """The thread before this mention, oldest first, one line per message ("bot:" or "person:"). Empty
    when the mention starts the thread or Slack cannot be read."""
    if not mention_ts or mention_ts == thread_ts:
        return ""
    try:
        parent = ctx.slack.message(channel, thread_ts)
        replies = ctx.slack.replies(channel, thread_ts)
    except Exception:
        return ""
    messages = ([parent] if parent else []) + list(replies or [])
    lines = []
    for m in messages:
        if m.get("ts") == mention_ts:
            continue
        text = " ".join(_MENTION.sub("@bot", str(m.get("text") or "")).split())
        if text:
            lines.append(f"{'bot' if m.get('bot') else 'person'}: {text[:THREAD_MESSAGE_CHARS]}")
    return "\n".join(lines[-THREAD_MESSAGES:])


def route(ctx: "Ctx", text: str, thread: str = "") -> Route:
    """Decide what a mention is (see the module docstring). Never raises: a failed call falls back."""
    from nua.extract import fill
    from nua.llm import LLMError
    from nua.prompts import route as prompt

    try:
        llm = ctx.llms(ctx.config.pipeline.ask_map, timeout=ROUTE_TIMEOUT_SECONDS, delays=(2.0,))
        from nua.prompts import domain_values

        values = {**domain_values(ctx.config), "thread": thread or "(none)", "message": text}
        out = llm.complete_json(system=fill(prompt.SYSTEM, values), schema=ROUTE_SCHEMA, max_tokens=ROUTE_MAX_TOKENS,
                                prompt=fill(prompt.ROUTE, values))
        reply = parse_reply(RouteReply, out, f"{llm.spec}: route reply")
    except (LLMError, ValueError) as e:
        ctx.info(f"ask route failed ({e}); using the fallback rule")
        return _fallback_route(text)
    kind = reply.route.strip().lower()
    if kind == "ask":
        return Route("ask", question=" ".join(reply.question.split())[:MAX_QUESTION] or text)
    if kind == "reply" and reply.reply.strip():
        return Route("reply", reply=reply.reply.strip())
    if kind == "beliefs":
        m = _ENTRY_ID.search(reply.target or "")
        return Route("beliefs", target=m.group(0).upper() if m else "")
    if kind == "findings":
        return Route("findings")
    return _fallback_route(text)


# ---------- beliefs and findings, straight from the generated files ----------

_ENTRY_ID = re.compile(r"(?i)\b[BW]-\d{3}\b")
_ENTRY = re.compile(r"(?m)^### ([BW]-\d{3}) (.+)$")
_COUNT = re.compile(r"\*\*(For|Against|Scope mismatch)\*\*(?::\s*none| \((\d+) calls?)")
MAX_FINDING_ITEMS = 8


def _repo_link(path: str) -> str:
    import os

    slug = os.environ.get("REPO_SLUG") or ""
    branch = os.environ.get("REPO_BRANCH") or "main"
    return f"<https://github.com/{slug}/blob/{branch}/{path}|{path}>" if slug else f"`{path}`"


def _status_entries(text: str) -> list[dict]:
    """Each B-/W- entry in beliefs-status.md: id, statement, for/against/mismatch call counts, body."""
    out = []
    matches = list(_ENTRY.finditer(text))
    for i, m in enumerate(matches):
        body = text[m.end():matches[i + 1].start() if i + 1 < len(matches) else len(text)]
        body = body.split("\n## ", 1)[0]
        counts = {k: int(n) if n else 0 for k, n in _COUNT.findall(body)}
        out.append({"id": m.group(1), "statement": m.group(2).strip(), "for": counts.get("For", 0),
                    "against": counts.get("Against", 0), "mismatch": counts.get("Scope mismatch", 0),
                    "body": body.strip()})
    return out


def beliefs_blocks(ctx: "Ctx", target: str = "") -> tuple[list[dict], str]:
    """Every Belief and Watching item with its evidence counts, or one entry's evidence when `target`."""
    text = ctx.repo.read_or("beliefs-status.md", "")
    entries = _status_entries(text)
    if not entries:
        msg = "There are no beliefs yet."
        return [_section(msg)], msg
    if target:
        e = next((x for x in entries if x["id"] == target), None)
        if e is None:
            msg = f"There is no {target}. Ask me to list the beliefs to see the ids."
            return [_section(msg)], msg
        body = re.sub(r"(?m)^\*\*(.+?)\*\*", r"*\1*", e["body"])
        body = re.sub(r"(?m)^(\s*)- ", r"\1• ", body)
        blocks = [_section(c) for c in _chunks(f"*{e['id']}* {e['statement']}\n\n{body}")][:MAX_BLOCKS]
        blocks.append({"type": "context", "elements": [{"type": "mrkdwn", "text": f"From {_repo_link('beliefs-status.md')}"}]})
        return blocks, f"{e['id']} {e['statement']}"
    pressure = next((l for l in text.splitlines() if l.startswith("Under pressure:")), "")
    lines = []
    for kind, label in (("B", "Beliefs"), ("W", "Watching")):
        group = [e for e in entries if e["id"].startswith(kind)]
        if not group:
            continue
        lines.append(f"*{label}* ({len(group)})")
        for e in group:
            lines.append(f"• *{e['id']}* {e['statement']}  _{e['for']} for · {e['against']} against · "
                         f"{e['mismatch']} scope mismatch_")
        lines.append("")
    blocks = [_section(c) for c in _chunks("\n".join(lines).strip())]
    note = (pressure + "  " if pressure else "") + "Counts are calls. Ask about one, e.g. \"show B-007\", for its quotes."
    blocks.append({"type": "context", "elements": [{"type": "mrkdwn", "text": f"{note} {_repo_link('beliefs-status.md')}"}]})
    return blocks, "\n".join(lines)[:2000]


def findings_blocks(ctx: "Ctx") -> tuple[list[dict], str]:
    """The themes in findings.md (claims no belief covers yet) and the newest ungrouped findings."""
    text = ctx.repo.read_or("findings.md", "")
    if not text.strip():
        msg = "There are no findings yet."
        return [_section(msg)], msg
    total = next((l for l in text.splitlines() if l.startswith("Claims no current belief")), "")
    themes, ungrouped_head, newest = [], "", []
    section = None
    for line in text.splitlines():
        if line.startswith("## "):
            section = line[3:].strip()
            if section.lower().startswith("ungrouped"):
                ungrouped_head = section
            else:
                themes.append(section)
        elif section and section == ungrouped_head and line.startswith("- ") and len(newest) < MAX_FINDING_ITEMS:
            newest.append(line[2:].strip())
    count = total.split(":", 1)[1].split(".", 1)[0].strip() if ":" in total else ""
    out = [f"*Findings no belief covers yet*  {count}".rstrip()]
    if themes:
        out += ["", f"*Themes* ({len(themes)}, from the weekly roll-up)"] + [f"• {t}" for t in themes]
    if newest:
        out += ["", f"*Newest ungrouped* ({ungrouped_head.lower()})"]
        for item in newest:
            who, _, claim = item.partition(": ")
            out.append(f"• {claim[:220] + ('...' if len(claim) > 220 else '')}  _{who}_")
    blocks = [_section(c) for c in _chunks("\n".join(out))][:MAX_BLOCKS]
    blocks.append({"type": "context", "elements": [{"type": "mrkdwn", "text": f"Quotes for each in {_repo_link('findings.md')}"}]})
    return blocks, "\n".join(out)[:2000]


# ---------- the answer ----------

def _mrkdwn(text: str) -> str:
    """Markdown from the model as Slack mrkdwn: **bold** to *bold*, list stars and dashes to bullets."""
    text = re.sub(r"\*\*(.+?)\*\*", r"*\1*", text)
    text = re.sub(r"(?m)^(\s*)[*-] ", r"\1• ", text)
    return re.sub(r"(?m)^#{1,6} +(.+)$", r"*\1*", text)


def _chunks(text: str, size: int = MAX_SECTION) -> list[str]:
    """Split on paragraph, then line, then word boundaries so no piece is longer than `size`."""
    out: list[str] = []
    cur = ""
    for para in text.split("\n"):
        while len(para) > size:
            cut = para.rfind(" ", 0, size)
            cut = cut if cut > 0 else size
            if cur:
                out.append(cur)
                cur = ""
            out.append(para[:cut])
            para = para[cut:].lstrip()
        nxt = f"{cur}\n{para}" if cur else para
        if len(nxt) > size:
            out.append(cur)
            cur = para
        else:
            cur = nxt
    if cur.strip():
        out.append(cur)
    return [c for c in out if c.strip()]


def _section(text: str) -> dict:
    return {"type": "section", "text": {"type": "mrkdwn", "text": text}}


def answer_blocks(q: Question, res: "AskResult") -> tuple[list[dict], str]:
    """The thread reply: the answer, then the quotes it rests on (call id and verbatim quote; one that
    could not be found in the call is marked), then the coverage line. Returns (blocks, fallback text)."""
    blocks: list[dict] = [_section(c) for c in _chunks(_mrkdwn(res.answer.strip()) or "No answer.")]
    if res.citations:
        lines = []
        for c in res.citations:
            quote = " ".join(str(c.get("quote") or "").split())
            if len(quote) > MAX_QUOTE:
                quote = quote[:MAX_QUOTE].rsplit(" ", 1)[0] + "..."
            mark = "" if c.get("verified") else " _(not verified)_"
            lines.append(f"• `{c.get('call_id')}`: “{quote}”{mark}")
        blocks.append({"type": "divider"})
        blocks.extend(_section(c) for c in _chunks("*Quotes*\n" + "\n".join(lines)))
    filters = []
    if q.since:
        filters.append(f"since {q.since}")
    if q.seat:
        filters.append(f"seat {q.seat}")
    context = res.coverage + (f" ({', '.join(filters)})" if filters else "")
    if len(blocks) > MAX_BLOCKS:
        blocks = blocks[:MAX_BLOCKS] + [_section("_Cut short: the full answer is longer than one Slack "
                                                  "message. Run `uv run python -m nua ask` for all of it._")]
    blocks.append({"type": "context", "elements": [{"type": "mrkdwn", "text": context}]})
    return blocks, f"{res.answer.strip()[:2000]}\n\n{context}"


def run(ctx: "Ctx", payload: dict) -> dict:
    """The `ask` task: route the message, then answer it in its thread (see the module docstring). A bad
    `since` gets a reply saying so instead of a retry."""
    from nua.ask import ask

    channel, thread_ts = payload.get("channel"), payload.get("thread_ts")
    text = str(payload.get("question") or "")
    if not channel or not thread_ts or not text:
        from nua.runner import InvalidTask
        raise InvalidTask("ask needs a channel, a thread_ts and a question")
    r = route(ctx, text, _thread_text(ctx, channel, thread_ts, payload.get("ts")))
    if r.kind == "reply":
        ctx.slack.post(channel, [_section(r.reply)], r.reply, thread_ts=thread_ts)
        return {"answered": False, "routed": "reply"}
    if r.kind in ("beliefs", "findings"):
        blocks, fallback = beliefs_blocks(ctx, r.target) if r.kind == "beliefs" else findings_blocks(ctx)
        ctx.slack.post(channel, blocks, fallback, thread_ts=thread_ts)
        return {"answered": True, "routed": r.kind, "target": r.target}
    q = Question(text=r.question, since=payload.get("since"), seat=payload.get("seat"))
    on_it = ON_IT if q.text == text else f"On it: _{q.text}_\nReading every call. This takes a minute or two."
    ctx.slack.post(channel, [_section(on_it)], on_it, thread_ts=thread_ts)
    try:
        res = ask(ctx, q.text, since=q.since, seat=q.seat)
    except ValueError as e:
        msg = f"I could not run that: {e}"
        ctx.slack.post(channel, [_section(msg)], msg, thread_ts=thread_ts)
        return {"answered": False, "error": str(e)}
    blocks, fallback = answer_blocks(q, res)
    ctx.slack.post(channel, blocks, fallback, thread_ts=thread_ts)
    return {"answered": True, "routed": "ask", "question": q.text, "coverage": res.coverage,
            "citations": len(res.citations)}


def queue_from_event(ack: Any, event: dict, event_id: str) -> dict[str, Any]:
    """Receiver side, repo-free: reply in the thread and queue the task. Returns what was done."""
    from nua.tasks import task_name

    channel = event.get("channel")
    thread_ts = event.get("thread_ts") or event.get("ts")
    if not channel or not thread_ts:
        return {"ignored": "no channel or ts"}
    q = parse_mention(event.get("text") or "")
    if not q.text:
        ack.slack.post(channel, [_section(HELP)], HELP, thread_ts=thread_ts)
        return {"ignored": "no question"}
    payload = {"channel": channel, "thread_ts": thread_ts, "ts": event.get("ts"), "question": q.text,
               "since": q.since, "seat": q.seat, "user": event.get("user")}
    if not ack.queue.enqueue("ask", payload, name=task_name("ask", event_id or f"{channel}:{event.get('ts')}")):
        return {"ignored": "already queued"}
    return {"queued": "ask"}
