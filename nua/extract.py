"""Finding extraction: every extraction model reads the numbered transcript, in parallel.

Each model returns findings (`Claim`): one point in `text`, backed by 1 to 3 verbatim quotes.
Prompt: nua/prompts/extract.py, constants SYSTEM, EXTRACT and REPAIR. The recorder
notes go in labeled as context only. Quotes are checked afterwards by nua/verify.py, one by one; a
miss comes back here once through `repair_quote`, which asks the model that produced the finding.

The prompt helpers (filling, numbering, call facts) are shared with nua/verdicts.py.
"""
from __future__ import annotations

import re
from concurrent.futures import ThreadPoolExecutor, wait
from typing import Any, Callable, Iterable

from pydantic import Field

from nua.config import Config
from nua.context import Ctx
from nua.llm import LLMError
from nua.prompts import extract as extract_prompt
from nua.schemas import (Flag, Mapping, Objects, ObjectOrNone, OptText, OptTurn, Reply, RequiredObjects, Text, Texts,
                         Turns, choice, parse_reply, strict_schema)
from nua.types import (MAX_QUOTES, Attendee, Claim, ExtractionResult, Flags, FollowUp, ParsedTranscript, Quote,
                       Scope, Turn, Who)

EXTRACT_MAX_TOKENS = 16_000
REPAIR_MAX_TOKENS = 2_000
REPAIR_RADIUS = 8          # turns either side of the quote sent with a repair request
# How long one parallel model step (extraction, verdicts) waits for its slowest model. A little over
# providers.MAX_CALL_SECONDS, so a provider normally gives up on its own first; a model that is still
# silent after this counts as down (`one_model`) instead of holding the task past its budget.
MODEL_STEP_SECONDS = 720
MAX_SUMMARY = 5
MAX_ASK_NEXT = 3
EM_DASH = chr(0x2014)      # never written in model text we store; spelled this way so no file contains one


# ---------- reply models ----------
# Each reply model gives the strict schema sent to the model (nua/schemas.py) and reads the reply.

class ScopeReply(Reply):
    line: OptText = None
    party: OptText = None
    segment: OptText = None
    seat: OptText = None


class FlagsReply(Reply):
    hedged: Flag = False
    firsthand: Flag = False
    prompted: Flag = False


class QuoteReply(Reply):
    quote: Text = ""
    turns: Turns = []
    question_turn: OptTurn = None
    speaker: OptText = None


class ClaimReply(Reply):
    text: Text = ""
    quotes: Objects[QuoteReply] = []
    scope: Mapping[ScopeReply] = Field(default_factory=ScopeReply)
    flags: Mapping[FlagsReply] = Field(default_factory=FlagsReply)


class WhoReply(Reply):
    contact: OptText = None
    firm: OptText = None
    firm_source: choice(("transcript", "attendee_email"), nullable=True) = None
    seat: OptText = None
    institution_type: OptText = None
    role_in_thesis: OptText = None      # one of the role ids in config/domain.yml; others become null


class FollowUpReply(Reply):
    direction: choice(("we_owe", "they_offered")) = None
    what: Text = ""
    who: OptText = None
    quote: Text = ""


class MentionsReply(Reply):
    people: Texts = []
    firms: Texts = []


class ExtractReply(Reply):
    who: Mapping[WhoReply] = Field(default_factory=WhoReply)
    summary: Texts = []
    findings: RequiredObjects[ClaimReply] = []
    follow_ups: Objects[FollowUpReply] = []
    mentions: Mapping[MentionsReply] = Field(default_factory=MentionsReply)
    ask_next: Texts = []


class RepairReply(Reply):
    quote: ObjectOrNone[QuoteReply] = None


CLAIM_SCHEMA = strict_schema(ClaimReply)
EXTRACT_SCHEMA = strict_schema(ExtractReply)
REPAIR_SCHEMA = strict_schema(RepairReply)


# ---------- prompt helpers (shared with verdicts.py) ----------

def fill(template: str, values: dict[str, Any]) -> str:
    """Fill `{placeholders}` with str.format_map. Literal braces in a template are written doubled."""
    return template.format_map(values)


def clean_text(value: Any) -> str:
    """Model-written text (never a quote) with em dashes replaced and whitespace trimmed."""
    s = "" if value is None else str(value)
    return re.sub(r"\s*" + EM_DASH + r"\s*", " - ", s).strip()


def turn_line(t: Turn) -> str:
    """"[T12 3:41] Jane Doe: text" on one line; the time and speaker are left out when unknown."""
    head = f"T{t.index} {t.time}" if t.time else f"T{t.index}"
    text = " ".join(t.text.split())
    return f"[{head}] {t.speaker}: {text}" if t.speaker else f"[{head}] {text}"


def numbered_transcript(parsed: ParsedTranscript) -> str:
    """Every turn as a numbered line, e.g. "[T12 03:41] Jane Doe: text"."""
    return "\n".join(turn_line(t) for t in parsed.turns)


def turns_excerpt(parsed: ParsedTranscript, indexes: Iterable[int], radius: int = 2) -> str:
    """Numbered lines for the given turns plus `radius` turns either side, with "..." where turns are skipped."""
    wanted: set[int] = set()
    for i in indexes:
        wanted.update(range(i - radius, i + radius + 1))
    lines: list[str] = []
    prev: int | None = None
    for pos, t in enumerate(parsed.turns):
        if t.index not in wanted:
            continue
        if (prev is None and pos > 0) or (prev is not None and parsed.turns[prev].index != t.index - 1):
            lines.append("...")
        lines.append(turn_line(t))
        prev = pos
    if prev is not None and prev < len(parsed.turns) - 1:
        lines.append("...")
    return "\n".join(lines)


def scope_text(scope: Scope) -> str:
    """"line: commercial auto; party: carrier", or "none"."""
    parts = [f"{k}: {v}" for k, v in scope.to_dict().items() if v]
    return "; ".join(parts) or "none"


def _attendees_text(attendees: list[Attendee]) -> str:
    out = []
    for a in attendees:
        if a.name and a.email:
            out.append(f"{a.name} <{a.email}>")
        elif a.name or a.email:
            out.append(a.name or a.email)
    return ", ".join(out)


def call_values(ctx: Ctx, parsed: ParsedTranscript | None) -> dict[str, Any]:
    """Placeholders that describe the call itself, plus the domain (config/domain.yml), shared by the extract,
    consolidate, verdict and arbiter prompts."""
    from nua.prompts import domain_values

    cfg = ctx.config
    people = ", ".join(p.name for p in cfg.people) or "(none listed)"
    if parsed is None:
        return {**domain_values(cfg), "title": "(unknown)", "date": "unknown", "recorded_by": "unknown",
                "internal_people": people, "attendees": "(unknown)", "speaker_labels": "unknown"}
    person = cfg.person(parsed.recorded_by)
    return {
        **domain_values(cfg),
        "title": parsed.title or "(none)",
        "date": parsed.date or "unknown",
        "recorded_by": person.name if person else (parsed.recorded_by or "unknown"),
        "internal_people": people,
        "attendees": _attendees_text(parsed.attendees) or "(none listed)",
        "speaker_labels": parsed.speaker_labels,
    }


def run_parallel(specs: list[str], fn: Callable[[str], Any], *,
                 timeout: float | None = None) -> list[tuple[str, Any, LLMError | None]]:
    """Run fn(spec) for every spec at once. Returns (spec, result, error) in spec order.

    Waits at most `timeout` seconds (MODEL_STEP_SECONDS when None). A spec with no answer by then gets an
    LLMError, and its thread is left to finish on its own. Only LLMError is caught; CostCapExceeded and
    bugs propagate.
    """
    limit = MODEL_STEP_SECONDS if timeout is None else timeout
    pool = ThreadPoolExecutor(max_workers=max(1, len(specs)))
    try:
        futures = [pool.submit(fn, s) for s in specs]
        wait(futures, timeout=limit)
    finally:
        pool.shutdown(wait=False, cancel_futures=True)
    out: list[tuple[str, Any, LLMError | None]] = []
    for spec, fut in zip(specs, futures):
        if not fut.done():
            fut.cancel()
            out.append((spec, None, LLMError(f"{spec}: no answer within {limit:g}s")))
            continue
        try:
            out.append((spec, fut.result(), None))
        except LLMError as e:
            out.append((spec, None, e))
    return out


# ---------- reply normalization ----------

_EMPTY = {"", "null", "none", "n/a", "na", "unknown"}


def _opt(value: Any) -> str | None:
    s = clean_text(value)
    return None if s.lower() in _EMPTY else s


def _seat(config: Config, value: Any) -> str | None:
    s = _opt(value)
    if not s or not config.seats:
        return s
    for seat in config.seats:
        if seat.lower() == s.lower():
            return seat
    return "other" if "other" in config.seats else None


def _dedupe(items: Iterable[str | None]) -> list[str]:
    seen: list[str] = []
    for i in items:
        if i and i not in seen:
            seen.append(i)
    return seen


def to_quote(q: QuoteReply) -> Quote | None:
    """A model's quote as a Quote; None when it is empty."""
    text = q.quote.strip()
    if not text:
        return None
    return Quote(quote=text, turns=list(q.turns), question_turn=q.question_turn, speaker=_opt(q.speaker))


def to_claim(config: Config, spec: str, c: ClaimReply) -> Claim | None:
    """A model's finding as a Claim tagged with `spec`, keeping its first MAX_QUOTES non-empty quotes;
    None when it has no text or no quote."""
    text = clean_text(c.text)
    quotes = [q for q in (to_quote(x) for x in c.quotes) if q][:MAX_QUOTES]
    if not text or not quotes:
        return None
    return Claim(
        id=None,
        text=text,
        quotes=quotes,
        scope=Scope(line=_opt(c.scope.line), party=_opt(c.scope.party), segment=_opt(c.scope.segment),
                    seat=_seat(config, c.scope.seat)),
        flags=Flags(hedged=c.flags.hedged, firsthand=c.flags.firsthand, prompted=c.flags.prompted),
        model=spec,
    )


def _role(config, value: str | None) -> str | None:
    """A role_in_thesis the domain knows (config/domain.yml `roles`), lower-cased; anything else is null."""
    role = (value or "").strip().lower().replace(" ", "_")
    return role if role in config.domain.roles else None


def to_result(config: Config, spec: str, data: Any) -> ExtractionResult:
    """Normalize an extraction reply: seats from the list, entity names canonical, caps applied.

    Raises LLMError when the reply does not have the expected shape.
    """
    reply = parse_reply(ExtractReply, data, f"{spec}: extraction reply")
    w = reply.who
    who = Who(contact=config.canonical(_opt(w.contact)), firm=config.canonical(_opt(w.firm)),
              firm_source=w.firm_source, seat=_seat(config, w.seat),
              institution_type=_opt(w.institution_type), role_in_thesis=_role(config, w.role_in_thesis))
    claims = [c for c in (to_claim(config, spec, x) for x in reply.findings) if c]
    follow_ups = [
        FollowUp(direction=f.direction, what=clean_text(f.what), who=_opt(f.who), quote=f.quote.strip())
        for f in reply.follow_ups if f.direction is not None and clean_text(f.what)
    ]
    mentions = {
        "people": _dedupe(config.canonical(_opt(p)) for p in reply.mentions.people),
        "firms": _dedupe(config.canonical(_opt(f)) for f in reply.mentions.firms),
    }
    summary = [s for s in (clean_text(x) for x in reply.summary) if s][:MAX_SUMMARY]
    ask_next = [s for s in (clean_text(x) for x in reply.ask_next) if s][:MAX_ASK_NEXT]
    return ExtractionResult(model=spec, who=who, summary=summary, claims=claims, follow_ups=follow_ups,
                            mentions=mentions, ask_next=ask_next)


# ---------- extract ----------

def _entities_text(config: Config) -> str:
    lines = [f"- {canon} (also heard as: {', '.join(aliases)})" if aliases else f"- {canon}"
             for canon, aliases in config.entities.items()]
    return "\n".join(lines) or "(none)"


def extract_values(ctx: Ctx, parsed: ParsedTranscript) -> dict[str, Any]:
    """Placeholders for nua/prompts/extract.py EXTRACT (and SYSTEM)."""
    cfg = ctx.config
    return {
        **call_values(ctx, parsed),
        "seats": "\n".join(f"- {s}" for s in cfg.seats) or "- other",
        "entities": _entities_text(cfg),
        "notes": parsed.notes.strip() or "(none)",
        "transcript": numbered_transcript(parsed),
        "short_answer_words": cfg.pipeline.t("short_answer_words"),
    }


def extract(ctx: Ctx, parsed: ParsedTranscript) -> tuple[list[ExtractionResult], list[str]]:
    """Run every extractor in `pipeline.extractors` in parallel over the numbered transcript.

    Returns the results in extractor order (the first is primary) and `degraded`: [] when all
    succeeded, ["one_model"] when some failed. Raises LLMError when every extractor fails.
    """
    specs = list(ctx.config.pipeline.extractors)
    if not specs:
        raise ValueError("config/pipeline.yml lists no extractors")
    values = extract_values(ctx, parsed)
    system = fill(extract_prompt.SYSTEM, values)
    prompt = fill(extract_prompt.EXTRACT, values)

    def run(spec: str) -> ExtractionResult:
        data = ctx.llms(spec).complete_json(system=system, prompt=prompt, schema=EXTRACT_SCHEMA,
                                            max_tokens=EXTRACT_MAX_TOKENS)
        return to_result(ctx.config, spec, data)

    results, errors = [], []
    for spec, result, err in run_parallel(specs, run):
        if err is None:
            results.append(result)
        else:
            errors.append(f"{spec}: {err}")
            ctx.info(f"extract: {spec} failed: {err}")
    if not results:
        raise LLMError("every extractor failed: " + "; ".join(errors))
    return results, (["one_model"] if errors else [])


def repair_quote(ctx: Ctx, parsed: ParsedTranscript, claim: Claim, quote: Quote, reason: str) -> Quote | None:
    """Ask the model that produced `claim` to fix one of its quotes the code check rejected, once.

    Sends the turns around that quote (the whole transcript when its turns are unknown). Returns the
    corrected quote, or None when the model gives up or fails.
    """
    spec = claim.model or ctx.config.pipeline.extractors[0]
    known = {t.index for t in parsed.turns}
    near = [t for t in [*quote.turns, quote.question_turn] if t in known]
    excerpt = turns_excerpt(parsed, near, radius=REPAIR_RADIUS) if near else numbered_transcript(parsed)
    values = {
        **call_values(ctx, parsed),
        "reason": reason,
        "text": claim.text,
        "quote": quote.quote,
        "turns": ", ".join(str(t) for t in quote.turns) or "none",
        "question_turn": quote.question_turn if quote.question_turn is not None else "none",
        "speaker": quote.speaker or "unknown",
        "transcript": excerpt,
        "short_answer_words": ctx.config.pipeline.t("short_answer_words"),
    }
    try:
        data = ctx.llms(spec).complete_json(system=fill(extract_prompt.SYSTEM, values),
                                            prompt=fill(extract_prompt.REPAIR, values),
                                            schema=REPAIR_SCHEMA, max_tokens=REPAIR_MAX_TOKENS)
    except LLMError as e:
        ctx.info(f"repair: {spec} failed: {e}")
        return None
    fixed = RepairReply.model_validate(data).quote if isinstance(data, dict) else None
    return to_quote(fixed) if fixed is not None else None
