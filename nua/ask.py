"""Cross-call questions: map/reduce over every digested call.

1. Map: digests with `status: digested` (optionally filtered by date and seat) go in batches to the
   cheap `pipeline.ask_map` model, several batches at once. Each batch returns the ids of the claims
   that bear on the question; the code attaches the stored quotes, so the model never retypes them.
   A failed batch is retried once, then counted as unread, and the coverage line shows it.
   With a time limit (`deadline_seconds`, the MCP server sets one), batches still running when the
   map's share of it runs out are counted as unread too, and no batch is retried past it. Under a time
   limit each model request also gets a shorter timeout (MAP_CALL_TIMEOUT_SECONDS, REDUCE_CALL_TIMEOUT_SECONDS)
   and no provider retries (the one retry is ask's own, which stops at the limit), so threads left
   running past the limit stop spending soon after it. A file in digests/ that cannot be read counts
   as unread too (whatever the filters), so the coverage line never claims N/N while one is broken.
2. Reduce: the strong `pipeline.ask_reduce` model writes the answer from those claims and lists the
   quotes it rests on. When it fails, or the time limit leaves no room for it, the answer says so and
   the citations are the kept claims' own quotes.
3. Every cited quote is checked by code: against the call's claim quotes, then the call's transcript
   body through `verify.QuoteIndex`, then as a near match of one claim quote (its words in the same
   order, at most QUOTE_SKIP_BUDGET claim words skipped, never a negation, no word the claim lacks).
   A verified quote is always shown as stored text, never as the model typed it. Recorder notes can
   never match.

Prompt: nua/prompts/ask.py, constants SYSTEM, MAP and REDUCE.
"""
from __future__ import annotations

import datetime as dt
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from concurrent.futures import TimeoutError as FuturesTimeout
from dataclasses import dataclass
from typing import Annotated, Any, Callable

from pydantic import BeforeValidator, WithJsonSchema

from nua.context import Ctx
from nua.digest import apply_overrides, is_stub, load_digests, unreadable_of
from nua.extract import clean_text, fill, scope_text
from nua.llm import LLM, LLMError
from nua.parse import parse
from nua.prompts import ask as ask_prompt
from nua.schemas import Objects, Reply, RequiredObjects, Text, parse_reply, strict_schema
from nua.types import Claim, Digest
from nua.verify import QuoteIndex, normalize

MAP_MAX_TOKENS = 4_000
REDUCE_MAX_TOKENS = 6_000
REDUCE_RESERVE_SECONDS = 90   # of a time limit, kept back from the map for the answer step...
REDUCE_RESERVE_SHARE = 0.4    # ...but never more than this share of it
MAP_CALL_TIMEOUT_SECONDS = 75       # per model request under a time limit (map)
REDUCE_CALL_TIMEOUT_SECONDS = REDUCE_RESERVE_SECONDS   # per model request under a time limit (reduce)
LIMITED_RETRY_DELAYS: tuple[float, ...] = ()   # no provider retries under a time limit; ask retries once itself
QUOTE_SKIP_BUDGET = 2      # claim words a near-match citation may leave out between its first and last word
# Words whose loss flips a quote's meaning, so a near match may never skip them (plus any "...n't").
NEGATIONS = frozenset({"not", "no", "never", "nor", "neither", "none", "nothing", "nobody", "nowhere", "without",
                       "cannot", "cant", "dont", "doesnt", "didnt", "isnt", "wasnt", "arent", "werent", "wont",
                       "wouldnt", "shouldnt", "couldnt", "havent", "hasnt", "hadnt", "aint"})


# ---------- reply models (nua/schemas.py) ----------

class MapItem(Reply):
    call_id: Text = ""
    claim_id: Text = ""
    why: Text = ""


class MapReply(Reply):
    relevant: RequiredObjects[MapItem]     # no list: the batch failed


class Citation(Reply):
    call_id: Text = ""
    quote: Text = ""


class ReduceReply(Reply):
    # Anything but text (null, a number, a list) means no answer, like an empty one.
    answer: Annotated[str | None, BeforeValidator(lambda v: v if isinstance(v, str) else None),
                      WithJsonSchema({"type": "string"})] = None
    citations: Objects[Citation] = []


MAP_SCHEMA = strict_schema(MapReply)
REDUCE_SCHEMA = strict_schema(ReduceReply)


# ---------- result ----------

@dataclass
class AskResult:
    answer: str
    citations: list[dict]      # {"call_id", "quote", "verified": bool}
    read: int                  # digests whose map batch succeeded
    total: int                 # digests that matched the filters

    @property
    def coverage(self) -> str:
        """"read 70/70 digests"."""
        return f"read {self.read}/{self.total} digests"


@dataclass
class _Evidence:
    digest: Digest
    claim: Claim
    why: str


_now: Callable[[], float] = time.monotonic      # replaced in tests


# ---------- selection ----------

def _norm_seat(seat: str | None) -> str:
    return " ".join((seat or "").split()).lower()


def _check_since(since: str | None) -> str | None:
    if since is None:
        return None
    try:
        return dt.date.fromisoformat(since.strip()).isoformat()
    except ValueError:
        raise ValueError(f"since must be a date like 2026-09-01, got {since!r}") from None


def select_digests(ctx: Ctx, *, since: str | None = None, seat: str | None = None,
                   digests: list[Digest] | None = None) -> list[Digest]:
    """Digested calls (stubs skipped) matching the filters, with config/overrides.yml applied, by call_id.

    `since` keeps calls dated on or after it; a call with no date is left out when `since` is given.
    `seat` matches the digest's seat, ignoring case and extra spaces. `digests` is an already loaded
    list to select from; by default the repo's digests are loaded.
    """
    since = _check_since(since)
    want_seat = _norm_seat(seat) if seat and seat.strip() else None
    out = []
    for d in (load_digests(ctx.repo) if digests is None else digests):
        if is_stub(d):
            continue
        d = apply_overrides(d, ctx.config.overrides)
        if since and not (d.date and str(d.date) >= since):
            continue
        if want_seat and _norm_seat(d.seat) != want_seat:
            continue
        out.append(d)
    return out


# ---------- prompts ----------

def _internal_people(ctx: Ctx) -> str:
    return ", ".join(p.name for p in ctx.config.people) or "(none listed)"


def _flags_text(claim: Claim) -> str:
    on = [name for name in ("hedged", "firsthand", "prompted") if getattr(claim.flags, name)]
    return ", ".join(on) or "none"


def digest_block(d: Digest) -> str:
    """One digest as the map model sees it: header line, who, summary, then every finding with its quotes."""
    who = [f"date: {d.date or 'unknown'}", f"contact: {d.contact or 'unknown'}", f"firm: {d.firm or 'unknown'}",
           f"seat: {d.seat or 'unknown'}", f"institution: {d.institution_type or 'unknown'}",
           f"role: {d.role_in_thesis or 'unknown'}", f"speaker labels: {d.speaker_labels or 'unknown'}"]
    lines = [f"=== {d.call_id}", "; ".join(who)]
    if d.summary:
        lines.append("summary (context only):")
        lines.extend(f"- {s}" for s in d.summary)
    lines.append("claims:" if d.claims else "claims: none")
    for c in d.claims:
        lines.append(f"[{c.id}] scope: {scope_text(c.scope)}; flags: {_flags_text(c)}")
        lines.append(f"  claim: {c.text}")
        for q in c.quotes:
            lines.append(f'  quote ({q.speaker or "speaker unknown"}, {q.attribution}): "{q.quote}"')
    return "\n".join(lines)


def _evidence_block(n: int, e: _Evidence) -> str:
    d, c = e.digest, e.claim
    who = ", ".join(x for x in (d.contact, d.firm) if x) or "unknown"
    attribution = c.quotes[0].attribution if c.quotes else "span"
    head = (f"[E{n}] {d.call_id} | {d.date or 'unknown date'} | {who} | seat: {d.seat or 'unknown'} | "
            f"speaker: {c.speaker or 'unknown'} ({attribution}, labels {d.speaker_labels or 'unknown'})")
    return "\n".join([
        head,
        f"claim: {c.text}",
        f"scope: {scope_text(c.scope)}; flags: {_flags_text(c)}",
        f"why kept: {e.why or '(no reason given)'}",
        *(f'quote: "{q}"' for q in c.quote_texts),
    ])


def _filters_text(since: str | None, seat: str | None) -> str:
    """"since 2026-09-01, seat underwriter", or "" with no filters."""
    parts = []
    if since:
        parts.append(f"since {since}")
    if seat and seat.strip():
        parts.append(f"seat {seat.strip()}")
    return ", ".join(parts)


# ---------- map ----------

def _map_batch(llm: LLM, system: str, template: str, values: dict[str, Any], batch: list[Digest]) -> list[_Evidence]:
    """One map call. Returns the kept claims; ids not in this batch are dropped. Raises LLMError on a bad reply."""
    prompt = fill(template, {**values, "count": len(batch), "digests": "\n\n".join(digest_block(d) for d in batch)})
    out = llm.complete_json(system=system, prompt=prompt, schema=MAP_SCHEMA, max_tokens=MAP_MAX_TOKENS)
    reply = parse_reply(MapReply, out, f"{llm.spec}: map reply")
    by_id = {d.call_id: d for d in batch}
    found: list[_Evidence] = []
    for item in reply.relevant:
        d = by_id.get(item.call_id.strip())
        claim_id = item.claim_id.strip().strip("[]").strip()
        c = d.claim(claim_id) if d else None
        if c is not None and c.quote:
            found.append(_Evidence(digest=d, claim=c, why=clean_text(item.why)))
    return found


def _past(stop_at: float | None) -> bool:
    return stop_at is not None and _now() >= stop_at


def _call_options(stop_at: float | None, timeout: float) -> dict[str, Any]:
    """LLMFactory options for one model use: under a time limit, `timeout` per request and no provider
    retries; without one, the provider defaults."""
    if stop_at is None:
        return {}
    return {"timeout": timeout, "delays": LIMITED_RETRY_DELAYS}


def _map_with_retry(llm: LLM | None, system: str, template: str, values: dict[str, Any],
                    batch: list[Digest], stop_at: float | None = None) -> list[_Evidence] | None:
    """Map one batch, retrying once unless `stop_at` has passed. None when it fails (the batch counts
    as unread)."""
    if llm is None or _past(stop_at):
        return None
    for attempt in range(2):
        if attempt and _past(stop_at):
            return None
        try:
            return _map_batch(llm, system, template, values, batch)
        except LLMError:
            continue
    return None


def _run_map(ctx: Ctx, digests: list[Digest], values: dict[str, Any], system: str, template: str, *,
             batch_size: int, concurrency: int, progress: Callable[[int, int], None] | None,
             stop_at: float | None = None) -> tuple[int, list[_Evidence], bool]:
    """Map every batch, `concurrency` at a time, until `stop_at` (a `_now()` time; None waits for all).

    Returns (digests read, kept claims in call order, whether the map stopped for time). Batches not
    finished by `stop_at` count as unread; their threads are left to finish on their own.
    """
    try:
        llm: LLM | None = ctx.llms(ctx.config.pipeline.ask_map, **_call_options(stop_at, MAP_CALL_TIMEOUT_SECONDS))
    except LLMError as e:
        ctx.info(f"ask: map model {ctx.config.pipeline.ask_map} unavailable: {e}")
        llm = None
    batches = [digests[i:i + batch_size] for i in range(0, len(digests), batch_size)]
    total, done, read = len(digests), 0, 0
    evidence: list[_Evidence] = []
    timed_out = False
    pool = ThreadPoolExecutor(max_workers=max(1, min(concurrency, len(batches))))
    try:
        futures = {pool.submit(_map_with_retry, llm, system, template, values, b, stop_at): b for b in batches}
        wait = None if stop_at is None else max(0.0, stop_at - _now())
        try:
            for fut in as_completed(futures, timeout=wait):
                batch = futures[fut]
                found = fut.result()          # CostCapExceeded and bugs propagate
                done += len(batch)
                if found is not None:
                    read += len(batch)
                    evidence.extend(found)
                if progress is not None:
                    progress(done, total)
        except FuturesTimeout:
            timed_out = True
            ctx.info(f"ask: map ran out of time, {total - done} of {total} digests unread")
            if progress is not None and done < total:
                progress(total, total)
    finally:
        # Never wait on a batch still running past the time limit: that is the wait the limit prevents.
        pool.shutdown(wait=not timed_out, cancel_futures=True)
    seen: set[tuple[str, str]] = set()
    unique: list[_Evidence] = []
    for e in sorted(evidence, key=lambda e: (e.digest.call_id, e.claim.id or "")):
        key = (e.digest.call_id, e.claim.id or "")
        if key not in seen:
            seen.add(key)
            unique.append(e)
    return read, unique, timed_out or _past(stop_at)


# ---------- reduce ----------

_OUT_OF_TIME = object()


def _reduce(ctx: Ctx, system: str, template: str, values: dict[str, Any],
            evidence: list[_Evidence], stop_at: float | None = None) -> tuple[str, list[dict]] | None:
    """(answer, raw citations) from the reduce model, retried once unless `stop_at` has passed. None
    when it fails."""
    prompt = fill(template, {**values, "count": len(evidence),
                             "evidence": "\n\n".join(_evidence_block(i, e) for i, e in enumerate(evidence, 1))})
    for attempt in range(2):
        if attempt and _past(stop_at):
            break
        try:
            llm = ctx.llms(ctx.config.pipeline.ask_reduce, **_call_options(stop_at, REDUCE_CALL_TIMEOUT_SECONDS))
            out = llm.complete_json(system=system, prompt=prompt, schema=REDUCE_SCHEMA, max_tokens=REDUCE_MAX_TOKENS)
        except LLMError as e:
            ctx.info(f"ask: reduce failed: {e}")
            continue
        reply = ReduceReply.model_validate(out) if isinstance(out, dict) else None
        if reply is None or reply.answer is None or not reply.answer.strip():
            ctx.info("ask: reduce reply has no answer")
            continue
        raw = [{"call_id": c.call_id.strip(), "quote": c.quote.strip()} for c in reply.citations]
        return reply.answer, [c for c in raw if c["call_id"] and c["quote"]]
    return None


def _reduce_by(ctx: Ctx, system: str, template: str, values: dict[str, Any], evidence: list[_Evidence],
               stop_at: float | None) -> Any:
    """`_reduce`, given up on at `stop_at`: returns _OUT_OF_TIME then (the model call is left to
    finish in a daemon thread). Without `stop_at` it simply runs `_reduce`."""
    if stop_at is None:
        return _reduce(ctx, system, template, values, evidence)
    left = stop_at - _now()
    if left <= 0:
        return _OUT_OF_TIME
    box: dict[str, Any] = {}

    def work() -> None:
        try:
            box["result"] = _reduce(ctx, system, template, values, evidence, stop_at)
        except BaseException as e:           # CostCapExceeded and bugs are raised in the caller
            box["error"] = e

    worker = threading.Thread(target=work, name="discovery-ask-reduce", daemon=True)
    worker.start()
    worker.join(left)
    if worker.is_alive():
        ctx.info("ask: the answer step ran out of time")
        return _OUT_OF_TIME
    if "error" in box:
        raise box["error"]
    return box.get("result")


# ---------- quote checks ----------

def _is_negation(word: str) -> bool:
    return word in NEGATIONS or word.endswith("n't")


def quote_in_order(quote: str, source: str, *, budget: int = QUOTE_SKIP_BUDGET) -> bool:
    """True when every word of `quote` appears in `source` in the same order, with at most `budget`
    words of `source` skipped between the first and last matched word and no skipped negation.

    Words are compared after `verify.normalize`. A reordered quote, one with a word the source lacks,
    or one that drops a "not" never passes. An empty quote never passes.
    """
    q, src = normalize(quote).split(), normalize(source).split()
    if not q or len(q) > len(src):
        return False
    for start, word in enumerate(src):
        if word != q[0]:
            continue
        skipped, pos, ok = 0, start + 1, True
        for want in q[1:]:              # earliest next match: the fewest skips for this start
            while pos < len(src) and src[pos] != want:
                if _is_negation(src[pos]):
                    ok = False
                    break
                skipped += 1
                pos += 1
            if not ok or pos >= len(src) or skipped > budget:
                ok = False
                break
            pos += 1
        if ok:
            return True
    return False


class _QuoteChecker:
    """Checks cited quotes against digested calls, parsing each call's transcript at most once."""

    def __init__(self, ctx: Ctx, digests: list[Digest]):
        self.ctx = ctx
        self.by_id = {d.call_id: d for d in digests}
        self._index: dict[str, QuoteIndex | None] = {}

    def _transcript(self, d: Digest) -> QuoteIndex | None:
        if d.call_id not in self._index:
            index = None
            try:
                raw = self.ctx.repo.read_or(d.transcript, "") if d.transcript else ""
            except (OSError, ValueError) as e:
                # ports.UnsafePathError (a symlink, an absolute or `..` path), a folder, or not UTF-8:
                # the file is never read, and the check falls back to the claim quotes.
                self.ctx.info(f"ask: cannot read {d.transcript} of {d.call_id} to check quotes "
                              f"({type(e).__name__}: {e})")
                raw = ""
            if raw:
                try:
                    index = QuoteIndex(parse(d.transcript, raw, commit_date=d.date,
                                             tz=self.ctx.config.pipeline.timezone))
                except Exception as e:   # ParseError or a broken front-matter: fall back to claim quotes
                    self.ctx.info(f"ask: cannot parse {d.transcript} to check quotes: {e}")
                    index = None
            self._index[d.call_id] = index
        return self._index[d.call_id]

    def check(self, call_id: str, quote: str) -> tuple[bool, str]:
        """(verified, quote to show). A verified quote is shown as the stored or raw transcript text."""
        d = self.by_id.get(call_id)
        target = normalize(quote)
        if d is None or not target:
            return False, quote
        for c in d.claims:
            for q in c.quote_texts:
                if normalize(q) == target:
                    return True, q
        index = self._transcript(d)
        match = index.find(quote) if index is not None else None
        if match is not None:
            return True, match.raw_text
        for c in d.claims:
            for q in c.quote_texts:
                if quote_in_order(quote, q):
                    return True, q
        return False, quote


def _check_citations(checker: _QuoteChecker, raw: list[dict]) -> list[dict]:
    """Verified citations in the model's order, one per call and quote."""
    out: list[dict] = []
    seen: set[tuple[str, str]] = set()
    for c in raw:
        key = (c["call_id"], normalize(c["quote"]))
        if key in seen:
            continue
        seen.add(key)
        verified, shown = checker.check(c["call_id"], c["quote"])
        out.append({"call_id": c["call_id"], "quote": shown, "verified": verified})
    return out


# ---------- public ----------

def _unread_note(read: int, total: int) -> str:
    missing = total - read
    return f" {missing} of {total} digests could not be read, so this may miss evidence." if missing else ""


def ask(ctx: Ctx, question: str, *, since: str | None = None, seat: str | None = None,
        batch_size: int = 20, concurrency: int = 8,
        progress: Callable[[int, int], None] | None = None,
        deadline_seconds: float | None = None) -> AskResult:
    """Answer `question` from every digested call that matches the filters.

    `since` (YYYY-MM-DD) and `seat` narrow the calls read. Digests are mapped `batch_size` at a time,
    `concurrency` batches at once; `progress(done, total)` is called after each batch with the number
    of digests finished (read or failed) and the total. `read` counts digests whose batch succeeded.
    `deadline_seconds` bounds the whole ask (None, the default, waits for every batch): the map gets
    the limit less REDUCE_RESERVE_SECONDS (at most REDUCE_RESERVE_SHARE of it), batches unfinished by
    then count as unread, and an answer step that cannot finish in time falls back to the kept claims.
    Each citation is {"call_id", "quote", "verified"}; an unverified quote was not found in the call's
    claims or transcript. Raises ValueError for a bad `since`, `batch_size` or `deadline_seconds`;
    CostCapExceeded propagates.
    """
    if batch_size < 1:
        raise ValueError(f"batch_size must be at least 1, got {batch_size}")
    if deadline_seconds is not None and deadline_seconds <= 0:
        raise ValueError(f"deadline_seconds must be positive, got {deadline_seconds}")
    end = map_end = None
    if deadline_seconds is not None:
        end = _now() + deadline_seconds
        map_end = end - min(REDUCE_RESERVE_SECONDS, deadline_seconds * REDUCE_RESERVE_SHARE)
    # Loaded once: the quote check below reuses this list rather than reading the tree again.
    everything = load_digests(ctx.repo)
    loaded = [d for d in everything if not is_stub(d)]
    digests = select_digests(ctx, since=since, seat=seat, digests=loaded)
    # A digest file that cannot be read is counted as unread: its call may match the filters, so the
    # coverage line never claims every digest was read while one is broken.
    unreadable = len(unreadable_of(everything))
    total = len(digests) + unreadable
    filters = _filters_text(_check_since(since), seat)
    scope = f"digested calls {filters}" if filters else "all digested calls"
    if not digests:
        if unreadable:
            answer = (f"No readable digested calls to read ({filters})." if filters
                      else "No readable digested calls to read.") + _unread_note(0, total)
        else:
            answer = f"No digested calls to read ({filters})." if filters else "No digested calls to read yet."
        return AskResult(answer=answer, citations=[], read=0, total=total)

    from nua.prompts import domain_values

    values = {**domain_values(ctx.config), "question": question.strip(), "internal_people": _internal_people(ctx),
              "scope": scope}
    system = fill(ask_prompt.SYSTEM, values)
    read, evidence, out_of_time = _run_map(ctx, digests, values, system, ask_prompt.MAP,
                                           batch_size=batch_size, concurrency=concurrency, progress=progress,
                                           stop_at=map_end)
    ctx.info(f"ask: read {read}/{total} digests, kept {len(evidence)} claims")

    if read == 0:
        why = "every map batch failed or ran out of time" if out_of_time else "every map batch failed"
        return AskResult(answer=f"Could not read any of the {total} digests: {why}.",
                         citations=[], read=0, total=total)
    if not evidence:
        return AskResult(answer=f"None of the {read} digests read has a claim that bears on this question."
                                + _unread_note(read, total), citations=[], read=read, total=total)

    checker = _QuoteChecker(ctx, loaded)
    reduced = _reduce_by(ctx, system, ask_prompt.REDUCE, {**values, "read": read, "total": total},
                         evidence, end)
    if reduced is None or reduced is _OUT_OF_TIME:
        what = "ran out of time" if reduced is _OUT_OF_TIME else "failed"
        answer = (f"The answer step {what}, so there is no written answer. These are the {len(evidence)} claims "
                  f"the map step found relevant." + _unread_note(read, total))
        raw = [{"call_id": e.digest.call_id, "quote": e.claim.quote} for e in evidence]
    else:
        answer, raw = reduced
    citations = _check_citations(checker, raw)
    unverified = sum(1 for c in citations if not c["verified"])
    ctx.info(f"ask: {len(citations)} citations, {unverified} unverified")
    return AskResult(answer=clean_text(answer), citations=citations, read=read, total=total)
