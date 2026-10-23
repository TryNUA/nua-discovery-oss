"""Per-call Slack brief: Block Kit messages plus a plain-text rendering (SPEC "Per-call Slack brief").

The top message leads with what the call changed against beliefs. Details go in thread replies:
the summary with a verification footer first, then new findings (with Make belief / Watch menus),
then prep questions. Follow-ups and suggested next questions stay in the digest, not the brief.

Every message is built twice in step: Slack mrkdwn blocks and a plain fallback text. The fallback
text doubles as the dry-run output (`render_text`), so the two never drift apart.

Interactive keys (read by discovery/interact.py):
- Wrong read: actions block `verdict:<target>:<n>`, button action_id `wrong_read`,
  value `<call_id>|<target>|<start>-<end>|<verdict>|<version>`, where `<version>` is the entry version
  the read was checked against (the digest's `checked`, empty when unknown). When the value would not
  fit, the verdict is left empty first (discovery/interact.py then reads it as `contradicts`, the only
  verdict that gets the button), then both are dropped.
- Finding menu: section block `finding:<start>-<end>`, overflow action_id `finding_menu`,
  option values `mb|<call_id>|<start>-<end>` (Make belief) and `w|<call_id>|<start>-<end>` (Watch).
  A finding carries 1 to 3 quotes; the span is its first quote's, and the block shows that quote first.
A span can name another claim once the call is superseded, so a click also carries the quote its
block showed (`shown_quote`), which the applier checks the resolved claim against.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from discovery.merge import span_overlap
from discovery.types import BeliefCheck, Claim, Decision, Digest, Quote
from discovery.verify import contains, token_overlap

if TYPE_CHECKING:
    from discovery.beliefs import BeliefsDoc

MAX_BLOCKS = 50          # Slack's per-message block limit
MAX_TEXT = 3000          # section text limit
MAX_CONTEXT = 2000       # context element text limit
MAX_VALUE = 150          # option and button value limit (values must stay under it)
TOP_FINDINGS = 3         # findings shown in the top message when no belief is touched
QUOTES_PER_CHECK = 2     # quotes shown per verdict line in the top message
SHORT_SECOND = 140       # a finding's second quote is shown in full when it is at most this long
MAX_QUESTION = 200       # characters of the question turn shown after a short answer

EM_DASH = "\u2014"
SEP = " · "

VERDICT_WORDS = {"supports": "supports", "contradicts": "contradicts", "scope_mismatch": "scope mismatch"}
DEGRADED_WORDS = {"one_model": "one extraction model down", "no_arbiter": "arbiter down",
                  "no_consolidation": "findings merged by quote overlap only"}
COVERAGE_TITLES = (("answered", "Answered"), ("dodged", "Dodged"), ("never_asked", "Never asked"))
LOCKED_NOTE = "Already in beliefs.md"


@dataclass
class BriefMessages:
    top_blocks: list[dict]
    top_text: str
    thread: list[tuple[list[dict], str]] = field(default_factory=list)   # (blocks, fallback text) per reply


# ---------- text helpers ----------

def _clean(text: str) -> str:
    """No em dashes in anything the brief produces, quotes included (display only; digests keep the raw text)."""
    return text.replace(EM_DASH, "-")


def _esc(text: str) -> str:
    """Escape the three characters Slack mrkdwn treats as control characters."""
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _unesc(text: str) -> str:
    return text.replace("&lt;", "<").replace("&gt;", ">").replace("&amp;", "&")


def _oneline(text: str | None) -> str:
    """Collapse whitespace so a quote or claim fits on one line."""
    return " ".join((text or "").split())


def _fit(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 3].rstrip() + "..."


def _plural(n: int, word: str) -> str:
    return f"{n} {word}" if n == 1 else f"{n} {word}s"


def _same_quote(a: str, b: str) -> bool:
    """True when one quote contains the other after normalization, or they share 80% of their words."""
    return contains(a, b) or contains(b, a) or token_overlap(a, b) >= 0.8


def _model_name(spec: str) -> str:
    """'anthropic:claude-sonnet-5' -> 'claude-sonnet-5'."""
    return spec.split(":", 1)[-1]


# ---------- one Slack message ----------

class _Message:
    """Collects the blocks and the matching plain text of one Slack message. Keeps block_ids unique."""

    def __init__(self) -> None:
        self.blocks: list[dict] = []
        self.lines: list[str] = []
        self._ids: set[str] = set()

    def _id(self, block_id: str) -> str:
        unique, n = block_id, 2
        while unique in self._ids:
            unique, n = f"{block_id}:{n}", n + 1
        self._ids.add(unique)
        return unique

    def section(self, block_id: str, mrkdwn: str, plain: str) -> None:
        self.blocks.append({"type": "section", "block_id": self._id(block_id),
                            "text": {"type": "mrkdwn", "text": _fit(mrkdwn, MAX_TEXT)}})
        self.lines.append(plain)

    def sections(self, block_id: str, mrkdwn_lines: list[str], plain: str) -> None:
        """One or more sections holding `mrkdwn_lines`, split at line breaks to stay under MAX_TEXT."""
        chunks, current = [], ""
        for line in mrkdwn_lines:
            candidate = f"{current}\n{line}" if current else line
            if current and len(candidate) > MAX_TEXT:
                chunks.append(current)
                candidate = line
            current = candidate
        chunks.append(current)
        for i, chunk in enumerate(chunks):
            self.section(block_id if i == 0 else f"{block_id}:{i + 1}", chunk, plain if i == 0 else "")

    def context(self, block_id: str, mrkdwn: str, plain: str | None) -> None:
        self.blocks.append({"type": "context", "block_id": self._id(block_id),
                            "elements": [{"type": "mrkdwn", "text": _fit(mrkdwn, MAX_CONTEXT)}]})
        if plain is not None:
            self.lines.append(plain)

    def actions(self, block_id: str, elements: list[dict]) -> None:
        self.blocks.append({"type": "actions", "block_id": self._id(block_id), "elements": elements})

    def add(self, blocks: list[dict], plain: str) -> None:
        """Append prebuilt blocks, renaming any block_id already used in this message."""
        for block in blocks:
            self.blocks.append({**block, "block_id": self._id(block["block_id"])})
        self.lines.append(plain)

    def done(self) -> tuple[list[dict], str]:
        """Blocks and fallback text, trimmed to MAX_BLOCKS with a pointer to the digest if needed."""
        blocks = self.blocks
        if len(blocks) > MAX_BLOCKS:
            blocks = blocks[: MAX_BLOCKS - 1] + [{
                "type": "context", "block_id": self._id("truncated"),
                "elements": [{"type": "mrkdwn", "text": "More in the digest."}]}]
        text = "\n".join(line for line in self.lines if line != "")
        return blocks, text


# ---------- pieces shared by several sections ----------

def _span(claim: Claim | None) -> str | None:
    """'<start>-<end>' for a verified claim, else None."""
    if claim is None or not claim.offset:
        return None
    return f"{claim.offset[0]}-{claim.offset[1]}"


def _decided_label(check: BeliefCheck) -> str:
    if check.decided_by == "agree":
        return "[2/2 agree]"
    if check.decided_by == "arbiter":
        return "[arbiter]"
    if check.decided_by == "single":
        return "[1 model]"
    votes = ", ".join(f"{_model_name(m)} says {VERDICT_WORDS.get(v, 'no link')}" for m, v in check.votes.items())
    return f"[unarbitrated: {votes}]" if votes else "[unarbitrated]"


def _statement(beliefs: "BeliefsDoc | None", entry_id: str) -> str | None:
    entry = beliefs.get(entry_id) if beliefs is not None else None
    return _oneline(entry.statement) if entry else None


def _is_watching(beliefs: "BeliefsDoc | None", entry_id: str) -> bool:
    entry = beliefs.get(entry_id) if beliefs is not None else None
    return entry.section == "watching" if entry else entry_id.startswith("W-")


def _counts_phrase(count: dict | None) -> str:
    if not count or not count.get("calls"):
        return ""
    return f"now {_plural(int(count['calls']), 'call')}, {_plural(int(count.get('seats', 0)), 'seat')}"


def _seat_is_new(seat: str | None, count: dict | None) -> bool:
    """Whether this call's seat is new evidence for an entry. `count` includes this call.

    Uses `seat_calls` ({seat: calls}) when the caller provides it. Otherwise: a seat missing from
    `seat_list` is new, and a seat in it is new only when every counted call has its own seat.
    """
    if not seat or not count:
        return False
    seat_calls = count.get("seat_calls")
    if isinstance(seat_calls, dict):
        return int(seat_calls.get(seat, 0)) <= 1
    seat_list = count.get("seat_list") or []
    if seat not in seat_list:
        return True
    return int(count.get("calls", 0)) <= int(count.get("seats", len(seat_list)))


def _new_findings(d: Digest) -> list[Claim]:
    """The digest's new-finding snapshot, or every claim no belief check cites when there is none."""
    if d.new_findings:
        return [c for c in (d.claim(cid) for cid in d.new_findings) if c is not None]
    cited = {cid for check in d.belief_checks for cid in check.claims}
    return [c for c in d.claims if c.id not in cited]


def _strongest(claims: list[Claim], n: int) -> list[Claim]:
    """Rank firsthand, then unprompted, then found by both models; ties keep transcript order."""
    return sorted(claims, key=lambda c: (not c.flags.firsthand, c.flags.prompted, c.found_by < 2))[:n]


_SENTENCE_END = re.compile(r"(?<=[.?!])\s+")


def _question_text(turn: str | None) -> str:
    """A question turn on one line, at most MAX_QUESTION characters. A longer turn keeps its end, where
    the question usually is: its last whole sentences that fit, else its last words, after "... "."""
    text = _oneline(turn)
    if len(text) <= MAX_QUESTION:
        return text
    room = MAX_QUESTION - 4
    tail = ""
    for sentence in reversed(_SENTENCE_END.split(text)):
        candidate = f"{sentence} {tail}".strip()
        if len(candidate) > room:
            break
        tail = candidate
    if not tail:
        tail = text[-room:]
        tail = tail.split(" ", 1)[1] if " " in tail else tail
    return f"... {tail}"


def _question_of(claim: Claim, quote: Quote, questions: dict[int | str, str] | None) -> str | None:
    """The question a short answer answers, from the `questions` map: keyed by finding id when it was asked
    in the answer's own turn (a one-block Granola export, whose turn text runs to the end of the call),
    else by the question turn. None when it is not known."""
    questions, q = questions or {}, quote.question_turn
    if isinstance(q, int) and quote.turns and q >= quote.turns[0]:
        return questions.get(claim.id) if claim.id else None
    return questions.get(q)


def _quote_lines(claim: Claim, *, with_claim_text: bool,
                 questions: dict[int | str, str] | None = None) -> tuple[list[str], list[str]]:
    """(mrkdwn lines, plain lines) for one cited finding: its first quote, and for a short answer (one with
    a question_turn) the question it answers, `"<quote>"  (answering: <question>)`, or the finding text
    when the question is not known (see _question_of). In mrkdwn the question goes on its own line, so the
    quote line stays `> "<quote>"`, which shown_quote and discovery/interact.py read back for Wrong read."""
    first = claim.quotes[0] if claim.quotes else Quote(quote="")
    quote = _oneline(first.quote)
    md, plain = [f'> "{_esc(quote)}"'], [f'  "{quote}"']
    if with_claim_text and first.question_turn is not None:
        question = _question_text(_question_of(claim, first, questions))
        if question:
            md.append(f"> _(answering: {_esc(question)})_")
            plain[-1] += f"  (answering: {question})"
        else:
            text = _oneline(claim.text)
            md.append(f"> Claim: {_esc(text)}")
            plain.append(f"  Claim: {text}")
    return md, plain


# ---------- top message ----------

def _header(d: Digest) -> str:
    parts = [
        d.contact, d.firm, d.seat,
        f"{d.duration_min}m" if d.duration_min else None,
        d.recorded_by.title() if d.recorded_by else None,
        d.source.title() if d.source else None,
    ]
    return SEP.join(_oneline(str(p)) for p in parts if p) or d.call_id


def _touched_decisions(d: Digest, decisions: list[Decision]) -> list[Decision]:
    hit = {c.target for c in d.belief_checks if c.verdict in ("contradicts", "scope_mismatch")}
    return [dec for dec in decisions if hit & set(dec.rests_on)]


def _verdict_item(msg: _Message, d: Digest, check: BeliefCheck, headline: str, n: int,
                  beliefs: "BeliefsDoc | None", questions: dict[int | str, str] | None = None) -> None:
    """A contradiction or scope mismatch: headline, label, quotes, note, and Wrong read for contradictions."""
    statement = _statement(beliefs, check.target)
    title = f'{headline} "{statement}"' if statement else headline
    label = _decided_label(check)
    claims = [c for c in (d.claim(cid) for cid in check.claims) if c is not None]
    button = None
    span = _span(claims[0]) if claims else None
    # The version the read was checked against rides along, so apply answers a click on a read of older
    # wording as stale. When the value would not fit, the verdict goes first, then the version.
    version = d.checked.get(check.target) or ""
    base = f"{d.call_id}|{check.target}|{span}"
    value = next((v for v in (f"{base}|{check.verdict}|{version}", f"{base}||{version}", base)
                  if len(v) < MAX_VALUE), None)
    if check.verdict == "contradicts" and span and value is not None:
        button = {"type": "button", "action_id": "wrong_read",
                  "text": {"type": "plain_text", "text": "Wrong read"}, "value": value}

    md = [f"*{_esc(headline)}*" + (f' "{_esc(statement)}"' if statement else "") + f"  {_esc(label)}"]
    plain = [f"{title}  {label}" + ("  [Wrong read]" if button else "")]
    for claim in claims[:QUOTES_PER_CHECK]:
        q_md, q_plain = _quote_lines(claim, with_claim_text=True, questions=questions)
        md += q_md
        plain += q_plain
    if len(claims) > QUOTES_PER_CHECK:
        more = len(claims) - QUOTES_PER_CHECK
        md.append(f"_{_plural(more, 'more quote')} in the digest_")
        plain.append(f"  {_plural(more, 'more quote')} in the digest")
    if check.note:
        md.append(f"_{_esc(_oneline(check.note))}_")
        plain.append(f"  {_oneline(check.note)}")

    kind = "verdict-text" if check.verdict == "contradicts" else "mismatch"
    msg.section(f"{kind}:{check.target}:{n}", "\n".join(md), "\n".join(plain))
    if button:
        msg.actions(f"verdict:{check.target}:{n}", [button])


def _what_changed(msg: _Message, d: Digest, beliefs: "BeliefsDoc | None", decisions: list[Decision],
                  counts: dict[str, dict], questions: dict[int | str, str] | None = None) -> None:
    linked = {b for dec in decisions for b in dec.rests_on}
    watching = [c for c in d.belief_checks if _is_watching(beliefs, c.target)]
    on_beliefs = [c for c in d.belief_checks if not _is_watching(beliefs, c.target)]
    contradicts = sorted((c for c in on_beliefs if c.verdict == "contradicts"),
                         key=lambda c: (c.target not in linked, c.target))
    mismatches = sorted((c for c in on_beliefs if c.verdict == "scope_mismatch"), key=lambda c: c.target)
    supported = sorted({c.target for c in on_beliefs if c.verdict == "supports"})

    seen: dict[str, int] = {}

    def nth(target: str) -> int:
        seen[target] = seen.get(target, 0) + 1
        return seen[target]

    for check in contradicts:
        _verdict_item(msg, d, check, f"Contradicts {check.target}", nth(check.target), beliefs, questions)
    for check in mismatches:
        _verdict_item(msg, d, check, f"Scope mismatch {check.target}", nth(check.target), beliefs, questions)

    collapsed: list[str] = []
    for target in supported:
        count = counts.get(target)
        if not _seat_is_new(d.seat, count):
            collapsed.append(target)
            continue
        statement = _statement(beliefs, target)
        extra = "; ".join(p for p in (_counts_phrase(count), f"new seat: {_oneline(d.seat)}") if p)
        md = f"*Supports {target}*" + (f' "{_esc(statement)}"' if statement else "") + f"  ({_esc(extra)})"
        plain = f"Supports {target}" + (f' "{statement}"' if statement else "") + f"  ({extra})"
        msg.section(f"supports:{target}", md, plain)
    if collapsed:
        line = "Supports " + ", ".join(collapsed)
        msg.section("supports:collapsed", f"*{line}*", line)

    for check in sorted(watching, key=lambda c: c.target):
        word = VERDICT_WORDS.get(check.verdict, check.verdict)
        headline = f"Watching {check.target}, {word}"
        if check.verdict == "supports":
            statement = _statement(beliefs, check.target)
            phrase = _counts_phrase(counts.get(check.target))
            md = f"*{headline}*" + (f' "{_esc(statement)}"' if statement else "") + (f"  ({phrase})" if phrase else "")
            plain = headline + (f' "{statement}"' if statement else "") + (f"  ({phrase})" if phrase else "")
            msg.section(f"watching:{check.target}:{nth(check.target)}", md, plain)
        else:
            _verdict_item(msg, d, check, headline, nth(check.target), beliefs, questions)


def _no_belief_touched(msg: _Message, findings: list[Claim]) -> None:
    strongest = _strongest(findings, TOP_FINDINGS)
    lead = "No belief touched." + (" Strongest new findings:" if strongest else "")
    msg.section("changed:none", lead, lead)
    for i, claim in enumerate(strongest, 1):
        text, quote = _oneline(claim.text), _oneline(claim.quote)
        msg.section(f"top-finding:{i}", f'*{_esc(text)}*\n> "{_esc(quote)}"', f'- {text}\n  "{quote}"')


def _count_line(msg: _Message, d: Digest, n_findings: int, digest_url: str | None) -> None:
    dodged = sum(1 for q in d.question_coverage if q.status == "dodged")
    parts = [_plural(n_findings, "new finding"), _plural(dodged, "dodged question")]
    md, plain = SEP.join(parts), SEP.join(parts)
    if digest_url:
        md += f"{SEP}<{digest_url}|digest on GitHub>"
        plain += f"{SEP}digest: {digest_url}"
    msg.context("counts", md, "\n" + plain)


def _top(d: Digest, beliefs: "BeliefsDoc | None", decisions: list[Decision], counts: dict[str, dict],
         findings: list[Claim], digest_url: str | None,
         questions: dict[int | str, str] | None = None) -> tuple[list[dict], str]:
    msg = _Message()
    header = _header(d)
    msg.section("header", f"*{_esc(header)}*", header)
    touched = _touched_decisions(d, decisions)
    if touched:
        line = "Touches " + ", ".join(f"{dec.id} ({_oneline(dec.title)})" for dec in touched)
        msg.section("decisions", f"*{_esc(line)}*", line)
    msg.section("changed", "*WHAT CHANGED*", "\nWHAT CHANGED")
    if d.belief_checks:
        _what_changed(msg, d, beliefs, decisions, counts, questions)
    else:
        _no_belief_touched(msg, findings)
    _count_line(msg, d, len(findings), digest_url)
    return msg.done()


# ---------- thread replies ----------

def _more_quotes(claim: Claim) -> tuple[list[str], int]:
    """(further quotes shown in full, number left for the digest) for a finding's quotes after the first:
    the second is shown when it is at most SHORT_SECOND characters, the rest are counted."""
    rest = [_oneline(q.quote) for q in claim.quotes[1:] if q.quote]
    shown = rest[:1] if rest and len(rest[0]) <= SHORT_SECOND else []
    return shown, len(rest) - len(shown)


def _finding_parts(d: Digest, claim: Claim, locked: bool) -> tuple[list[dict], str]:
    """Blocks and plain text for one new finding: its text, first quote (and a short second one, or
    "+N more quotes"), meta line; the menu when open, a context line when locked."""
    text, quote = _oneline(claim.text), _oneline(claim.quote)
    shown, more = _more_quotes(claim)
    quote_md = "\n".join([f'> "{_esc(quote)}"', *(f'> "{_esc(q)}"' for q in shown)])
    quote_plain = "\n".join([f'  "{quote}"', *(f'  "{q}"' for q in shown)])
    if more:
        quote_md += f"\n_+{_plural(more, 'more quote')}_"
        quote_plain += f"\n  +{_plural(more, 'more quote')}"
    meta = [claim.speaker, "found by 2 models" if claim.found_by >= 2 else "found by 1 model"]
    if claim.flags.firsthand:
        meta.append("firsthand")
    if not claim.flags.prompted:
        meta.append("unprompted")
    meta_text = SEP.join(_oneline(m) for m in meta if m)
    span = _span(claim)
    key = f"{span}" if span else claim.id
    block = {"type": "section", "block_id": f"finding:{key}",
             "text": {"type": "mrkdwn", "text": _fit(f'*{_esc(text)}*\n{quote_md}\n_{_esc(meta_text)}_', MAX_TEXT)}}
    plain = f'- {text}\n{quote_plain}\n  {meta_text}'
    values = [f"mb|{d.call_id}|{span}", f"w|{d.call_id}|{span}"]
    if locked:
        lock = {"type": "context", "block_id": f"finding-locked:{key}",
                "elements": [{"type": "mrkdwn", "text": LOCKED_NOTE}]}
        return [block, lock], f"{plain}{SEP}{LOCKED_NOTE}"
    if span and all(len(v) < MAX_VALUE for v in values):
        block["accessory"] = {"type": "overflow", "action_id": "finding_menu", "options": [
            {"text": {"type": "plain_text", "text": "Make belief"}, "value": values[0]},
            {"text": {"type": "plain_text", "text": "Watch"}, "value": values[1]},
        ]}
        plain += "  [Make belief | Watch]"
    return [block], plain


def finding_from_block(block: dict) -> tuple[str, str]:
    """(claim text, quote) from a finding section block, for pre-filling the Make belief form."""
    lines = (block.get("text") or {}).get("text", "").split("\n")
    text = lines[0].strip("*") if lines else ""
    quote = ""
    if len(lines) > 1 and lines[1].startswith("> "):
        quote = lines[1][2:].strip()
        if len(quote) >= 2 and quote[0] == '"' and quote[-1] == '"':
            quote = quote[1:-1]
    return _unesc(text), _unesc(quote)


def shown_quote(blocks: list[dict] | None, block_id: str | None) -> str | None:
    """The quote the clicked brief block showed, or None when that block is not in `blocks`.

    A click names its claim by a span (`<start>-<end>`) into the call's transcript. If the call is
    superseded (re-digested from a richer copy) between the click and its apply, the span points into
    another file and can land on a different claim, so the click also carries this quote and
    discovery/apply.py checks the claim it resolves against it. A finding block (`finding:<span>`) shows one
    quote. A Wrong read button (`verdict:<target>:<n>`) sits under the section
    `verdict-text:<target>:<n>`, whose first quote is the claim the button's span names.
    """
    if not blocks or not block_id:
        return None
    if block_id.startswith("verdict:"):
        block_id = "verdict-text:" + block_id[len("verdict:"):]
    block = next((b for b in blocks if isinstance(b, dict) and b.get("block_id") == block_id), None)
    if block is None:
        return None
    if block_id.startswith("finding:"):
        return finding_from_block(block)[1] or None
    lines = (block.get("text") or {}).get("text", "").split("\n")
    first = next((line for line in lines if line.startswith('> "') and line.endswith('"') and len(line) > 4), "")
    return _unesc(first[3:-1]) if first else None


def acted_on(d: Digest, beliefs: "BeliefsDoc") -> tuple[list[tuple[int, int]], list[tuple[str, str]]]:
    """(acted_spans, acted_sources) for render_brief: where the history sources of current entries
    from `d`'s call sit in `d`, placed as discovery/apply.py and findings.md place them (held_spans), so
    two identical "Exactly." answers lock one finding per change, not every "Exactly.". A source
    that cannot be placed on any claim comes back as its (call_id, quote), matched by quote text."""
    from discovery.apply import placed_sources   # imported lazily: discovery.apply pulls in the applier's modules

    spans: list[tuple[int, int]] = []
    quotes: list[tuple[str, str]] = []
    for held, span in placed_sources(d, beliefs.held_sources()):
        if span is not None:
            spans.append((int(span[0]), int(span[1])))
        else:
            quotes.append((held.call_id, held.quote))
    return spans, quotes


def _locked(claim: Claim, spans: list[tuple[int, int]], quotes: list[str]) -> bool:
    """True when a placed history source overlaps any of the finding's quote spans by SOURCE_OVERLAP (the
    rule apply, findings.md and the roll-up use to hold a finding), or an unplaced one matches any quote."""
    from discovery.apply import SOURCE_OVERLAP   # imported lazily: discovery.apply pulls in the applier's modules

    if any(span_overlap(span, own) >= SOURCE_OVERLAP for own in claim.spans for span in spans):
        return True
    return any(_same_quote(mine, q) for mine in claim.quote_texts for q in quotes)


def _findings_replies(d: Digest, findings: list[Claim], acted_sources: list[tuple[str, str]],
                      acted_spans: list[tuple[int, int]] | None = None) -> list[tuple[list[dict], str]]:
    """New findings, packed into as many replies as needed so none exceeds MAX_BLOCKS."""
    acted = [q for call_id, q in acted_sources if call_id == d.call_id]
    parts = [_finding_parts(d, c, _locked(c, acted_spans or [], acted)) for c in findings]

    chunks: list[list[tuple[list[dict], str]]] = [[]]
    used = 1                                   # the title block
    for blocks, plain in parts:
        if used + len(blocks) > MAX_BLOCKS and chunks[-1]:
            chunks.append([])
            used = 1
        chunks[-1].append((blocks, plain))
        used += len(blocks)

    replies = []
    for i, chunk in enumerate(chunks, 1):
        suffix = f", part {i} of {len(chunks)}" if len(chunks) > 1 else ""
        count = f"({len(findings)}{suffix})"
        msg = _Message()
        msg.section("findings:title", f"*New findings* {count}", f"NEW FINDINGS {count}")
        for blocks, plain in chunk:
            msg.add(blocks, plain)
        replies.append(msg.done())
    return replies


def _questions_reply(d: Digest) -> tuple[list[dict], str]:
    msg = _Message()
    msg.section("questions:title", "*Questions from prep*", "QUESTIONS FROM PREP")
    for status, title in COVERAGE_TITLES:
        items = [q for q in d.question_coverage if q.status == status]
        if not items:
            continue
        md, plain = [f"*{title}*"], [title]
        for q in items:
            question = _oneline(q.question)
            md.append(f"• {_esc(question)}")
            plain.append(f"- {question}")
            claim = d.claim(q.claims[0]) if q.claims else None
            if claim is not None and status != "never_asked":
                q_md, q_plain = _quote_lines(claim, with_claim_text=False)
                md += q_md
                plain += [f"  {line}" for line in q_plain]
        msg.sections(f"questions:{status}", md, "\n".join(plain))
    return msg.done()


def _footer(d: Digest) -> str:
    v = d.verification
    matched = f"Quotes matched first pass {v.matched_first_pass}/{v.quotes}"
    fixes = [p for p in (f"{v.repaired} repaired" if v.repaired else "",
                         f"{len(v.dropped)} dropped" if v.dropped else "") if p]
    if fixes:
        matched += f" ({', '.join(fixes)})"
    by_two = sum(1 for c in d.claims if c.found_by >= 2)
    parts = [matched, f"Findings found by 2 models: {by_two}, by 1 model: {len(d.claims) - by_two}"]
    arbiter = sum(1 for c in d.belief_checks if c.decided_by == "arbiter") + \
        sum(1 for q in d.question_coverage if q.decided_by == "arbiter")
    parts.append(f"{_plural(arbiter, 'verdict')} by arbiter")
    if d.degraded:
        parts.append("Degraded: " + ", ".join(DEGRADED_WORDS.get(m, m) for m in d.degraded))
    if d.prep is None and d.origin == "live":
        parts.append("no prep matched")
    return SEP.join(parts)


def _summary_reply(d: Digest) -> tuple[list[dict], str]:
    msg = _Message()
    items = [_oneline(s) for s in d.summary]
    msg.sections("summary", ["*Summary*"] + [f"• {_esc(s)}" for s in items],
                 "\n".join(["SUMMARY"] + [f"- {s}" for s in items]))
    footer = _footer(d)
    msg.context("footer", _esc(footer), footer)
    return msg.done()


# ---------- entry points ----------

def _scrub(value):
    """Apply `_clean` to every string in a nested block structure."""
    if isinstance(value, str):
        return _clean(value)
    if isinstance(value, list):
        return [_scrub(v) for v in value]
    if isinstance(value, dict):
        return {k: _scrub(v) for k, v in value.items()}
    return value


def render_brief(d: Digest, beliefs: "BeliefsDoc", decisions: list[Decision], *,
                 counts: dict[str, dict], digest_url: str | None = None,
                 acted_sources: list[tuple[str, str]] | None = None,
                 acted_spans: list[tuple[int, int]] | None = None,
                 questions: dict[int | str, str] | None = None) -> BriefMessages:
    """Build the brief for one digest: a top message plus thread replies.

    counts: entry_id -> {"calls": int, "seats": int, "seat_list": [str]} including this call
    (optionally "seat_calls": {seat: calls} for an exact new-seat check).
    acted_spans: spans in `d`'s transcript that history lines already hold (acted_on); a finding whose
    span lies inside one renders locked.
    acted_sources: (call_id, quote) pairs already in history lines that could not be placed by span;
    findings of this call with a matching quote render locked.
    questions: {question_turn: text of that turn} for the claims the verdicts cite, so a short answer
    shows the question it answers (process.build_brief reads them from the transcript). A short answer
    asked in its own turn is keyed by claim id instead, with the text of that turn before it. A short
    answer whose question is missing shows its claim text instead.
    """
    findings = _new_findings(d)
    top_blocks, top_text = _top(d, beliefs, decisions, counts or {}, findings, digest_url, questions)
    thread: list[tuple[list[dict], str]] = [_summary_reply(d)]
    if findings:
        thread += _findings_replies(d, findings, acted_sources or [], acted_spans)
    if d.question_coverage:
        thread.append(_questions_reply(d))
    return BriefMessages(top_blocks=_scrub(top_blocks), top_text=_clean(top_text),
                         thread=[(_scrub(blocks), _clean(text)) for blocks, text in thread])


def render_text(msgs: BriefMessages) -> str:
    """Plain text of the whole brief, for --dry-run output and golden comparisons."""
    parts = [msgs.top_text]
    for i, (_, text) in enumerate(msgs.thread, 1):
        parts.append(f"--- thread reply {i} ---\n{text}")
    return "\n\n".join(parts) + "\n"
