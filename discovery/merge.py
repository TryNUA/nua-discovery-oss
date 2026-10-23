"""Combine verified findings from several extraction models into one list.

`consolidate` is the main path: one model call (the arbiter's model spec) reads every model's
findings and groups the ones that make the same point, writing one text, scope and flags per group.
Code then builds each consolidated finding from its members' verified quotes (`pick_quotes`), so no
quote is ever written by the consolidating model. Two models often quote different sentences for
the same point, which a span check cannot see; the grouping can.

`merge_claims` is the fallback when the consolidation call fails: two findings from different
models are the same when any of their quote spans overlap. Findings from the same model never merge
there. The result is marked degraded `no_consolidation`.
"""
from __future__ import annotations

from pydantic import Field

from discovery.config import Config
from discovery.context import Ctx
from discovery.llm import LLMError
from discovery.prompts import consolidate as consolidate_prompt
from discovery.schemas import Flag, Reply, RequiredObjects, Text, Texts, parse_reply, strict_schema
from discovery.types import MAX_QUOTES, Claim, Flags, ParsedTranscript, Quote, Scope

CONSOLIDATE_MAX_TOKENS = 16_000
SAME_SPAN = 0.5            # two quotes of one finding sharing this much of the shorter span are one quote


def span_overlap(a: tuple[int, int], b: tuple[int, int]) -> float:
    """Intersection of two [start, end) spans over the shorter span's length. 0.0 if either is empty."""
    shorter = min(a[1] - a[0], b[1] - b[0])
    if shorter <= 0:
        return 0.0
    inter = min(a[1], b[1]) - max(a[0], b[0])
    return max(0, inter) / shorter


def best_overlap(a: list[tuple[int, int]], b: list[tuple[int, int]]) -> float:
    """The largest span_overlap between any span in `a` and any span in `b`; 0.0 when either is empty."""
    return max((span_overlap(x, y) for x in a for y in b), default=0.0)


def _intersection(a: tuple[int, int], b: tuple[int, int]) -> int:
    return max(0, min(a[1], b[1]) - max(a[0], b[0]))


def _scope_fields(claim: Claim) -> int:
    """Number of non-empty scope fields."""
    return sum(1 for v in claim.scope.model_dump().values() if v)


def pick_quotes(quotes: list[Quote], limit: int = MAX_QUOTES) -> list[Quote]:
    """At most `limit` quotes from `quotes` (best first), in transcript order.

    A quote sharing at least SAME_SPAN of the shorter span with one already kept is a repeat and is
    skipped. The first quote is always kept; each further pick is the candidate farthest from the
    quotes already kept, so the result spreads over different parts of the call. Ties go to the
    earlier candidate. Quotes without a span are kept only when there is room after the spanned ones.
    """
    unique: list[Quote] = []
    for q in quotes:
        if q.span is not None and any(u.span is not None and span_overlap(q.span, u.span) >= SAME_SPAN
                                      for u in unique):
            continue
        if q.span is None and any(u.span is None and u.quote == q.quote for u in unique):
            continue
        unique.append(q)
    spanned = [q for q in unique if q.span is not None]
    chosen: list[Quote] = spanned[:1]
    rest = spanned[1:]
    while rest and len(chosen) < limit:
        def distance(q: Quote) -> int:
            return min(abs(q.span[0] - c.span[0]) for c in chosen)
        pick = max(rest, key=lambda q: (distance(q), -rest.index(q)))
        chosen.append(pick)
        rest.remove(pick)
    chosen.sort(key=lambda q: q.span)
    chosen += [q for q in unique if q.span is None][:max(0, limit - len(chosen))]
    return [q.model_copy() for q in chosen]


def _finish(claims: list[Claim]) -> list[Claim]:
    """Sorted by first quote offset (findings without one last), ids c01, c02, ..., `model` cleared."""
    claims = sorted(claims, key=lambda c: (c.offset is None, c.offset or (0, 0)))
    return [c.model_copy(update={"id": f"c{n:02d}", "model": None}) for n, c in enumerate(claims, start=1)]


def _build(members: list[tuple[int, Claim]], *, text: str | None = None, scope: Scope | None = None,
           flags: Flags | None = None) -> Claim:
    """One finding from a group of (model index, finding) members, best member first: its text, scope and
    flags unless given, the members' quotes through pick_quotes, found_by = distinct models among them."""
    best = members[0][1]
    quotes = [q for _, m in members for q in m.quotes]
    return best.model_copy(update={
        "text": text or best.text,
        "quotes": pick_quotes(quotes),
        "scope": (scope if scope is not None and not scope.is_empty() else best.scope).model_copy(),
        "flags": (flags or best.flags).model_copy(),
        "found_by": len({li for li, _ in members}),
    })


def merge_claims(per_model: list[list[Claim]]) -> list[Claim]:
    """One list of findings from one list per model, in extractor order, by quote overlap.

    Each finding pairs with at most one group per model: the group whose quote spans it overlaps most
    (by `span_overlap` over any pair of spans, then by shared characters). A group keeps the member with
    the most non-empty scope fields (the earlier list winning ties) for text, scope and flags, and the
    union of its members' quotes (pick_quotes). `found_by` is the number of lists in the group. Output
    is sorted by first offset, ids `c01`, `c02`, ..., `model` cleared. Findings without a verified
    quote never merge and sort last. Inputs are not mutated.
    """
    groups: list[list[tuple[int, Claim]]] = []
    for li, claims in enumerate(per_model):
        pairs = []
        for ci, c in enumerate(claims):
            if not c.spans:
                continue
            for gi, group in enumerate(groups):
                spans = [s for _, m in group for s in m.spans]
                best = best_overlap(c.spans, spans)
                if best > 0:
                    shared = max(_intersection(a, b) for a in c.spans for b in spans)
                    pairs.append((-best, -shared, gi, ci))
        pairs.sort()
        taken_groups: set[int] = set()
        assigned: dict[int, int] = {}
        for _, _, gi, ci in pairs:
            if gi in taken_groups or ci in assigned:
                continue
            taken_groups.add(gi)
            assigned[ci] = gi
        fresh = []
        for ci, c in enumerate(claims):
            if ci in assigned:
                groups[assigned[ci]].append((li, c))
            else:
                fresh.append([(li, c)])
        groups.extend(fresh)

    merged = []
    for group in groups:
        ordered = sorted(group, key=lambda m: (-_scope_fields(m[1]), m[0]))
        merged.append(_build(ordered))
    return _finish(merged)


# ---------- consolidation ----------

class ScopeChoice(Reply):
    line: Text = ""
    party: Text = ""
    segment: Text = ""
    seat: Text = ""


class FlagsChoice(Reply):
    hedged: Flag = False
    firsthand: Flag = False
    prompted: Flag = False


class GroupReply(Reply):
    ids: Texts = []
    text: Text = ""
    scope: ScopeChoice = Field(default_factory=ScopeChoice)
    flags: FlagsChoice = Field(default_factory=FlagsChoice)


class ConsolidateReply(Reply):
    groups: RequiredObjects[GroupReply] = []     # not a list: the reply fails and the overlap merge runs


CONSOLIDATE_SCHEMA = strict_schema(ConsolidateReply)


def finding_ids(per_model: list[list[Claim]]) -> dict[str, tuple[int, Claim]]:
    """Input ids for the consolidation prompt: `a1`, `a2`, ... for the first model, `b1`, ... for the next."""
    out: dict[str, tuple[int, Claim]] = {}
    for li, claims in enumerate(per_model):
        letter = chr(ord("a") + li) if li < 26 else f"m{li}_"
        for n, c in enumerate(claims, start=1):
            out[f"{letter}{n}"] = (li, c)
    return out


def _scope_line(scope: Scope) -> str:
    parts = [f"{k}: {v}" for k, v in scope.to_dict().items() if v]
    return "; ".join(parts) or "none"


def findings_text(ids: dict[str, tuple[int, Claim]]) -> str:
    """Every input finding as a `<finding>` block: id, reader, text, scope, flags and its quotes."""
    blocks = []
    for fid, (li, c) in ids.items():
        lines = [f'<finding id="{fid}" reader="{li + 1}">', f"text: {c.text}", f"scope: {_scope_line(c.scope)}"]
        flags = [n for n in ("hedged", "firsthand", "prompted") if getattr(c.flags, n)]
        if flags:
            lines.append(f"flags: {', '.join(flags)}")
        for q in c.quotes:
            where = ", ".join(f"T{t}" for t in q.turns) or "turn unknown"
            lines.append(f'quote ({where}): "{q.quote}"')
        lines.append("</finding>")
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks)


def _opt(value: str) -> str | None:
    s = " ".join((value or "").split())
    return None if s.lower() in ("", "null", "none", "n/a", "unknown") else s


def read_groups(reply: ConsolidateReply, ids: dict[str, tuple[int, Claim]],
                config: Config | None = None) -> list[tuple[list[str], str, Scope | None, Flags | None]]:
    """The model's groups, validated: unknown ids dropped, an id already placed in an earlier group
    dropped from a later one, empty groups skipped, and every input id the model left out added as a
    group of its own (with no text, scope or flags, so its own are kept). Groups come in reply order,
    the left-out ones after."""
    from discovery.extract import clean_text, _seat
    placed: set[str] = set()
    out = []
    for g in reply.groups:
        members = []
        for raw in g.ids:
            fid = str(raw).strip()
            if fid in ids and fid not in placed and fid not in members:
                members.append(fid)
        if not members:
            continue
        placed.update(members)
        seat = _opt(g.scope.seat)
        if config is not None:
            seat = _seat(config, seat)
        scope = Scope(line=_opt(g.scope.line), party=_opt(g.scope.party), segment=_opt(g.scope.segment), seat=seat)
        flags = Flags(hedged=g.flags.hedged, firsthand=g.flags.firsthand, prompted=g.flags.prompted)
        out.append((members, clean_text(g.text), scope, flags))
    out += [([fid], "", None, None) for fid in ids if fid not in placed]
    return out


def build_groups(groups: list[tuple[list[str], str, Scope | None, Flags | None]],
                 ids: dict[str, tuple[int, Claim]]) -> list[Claim]:
    """Consolidated findings from validated groups: members ordered by model, then input order, so the
    primary extractor's finding leads; quotes from pick_quotes; found_by counts distinct models."""
    order = list(ids)
    out = []
    for members, text, scope, flags in groups:
        ranked = sorted(members, key=lambda fid: (ids[fid][0], order.index(fid)))
        out.append(_build([ids[fid] for fid in ranked], text=text or None, scope=scope, flags=flags))
    return _finish(out)


def consolidate(ctx: Ctx, parsed: ParsedTranscript | None,
                per_model: list[list[Claim]]) -> tuple[list[Claim], list[str]]:
    """Group every model's verified findings by the point they make, with one call to the arbiter's model.

    Returns (findings with ids c01..., degraded). When the call fails (LLMError or a reply of the wrong
    shape) or no arbiter is configured, falls back to `merge_claims` and returns degraded
    ["no_consolidation"]. With fewer than two findings in all there is nothing to group and no call is
    made. CostCapExceeded propagates.
    """
    ids = finding_ids(per_model)
    if len(ids) < 2:
        return build_groups([([fid], "", None, None) for fid in ids], ids), []
    spec = ctx.config.pipeline.arbiter
    if not spec:
        return merge_claims(per_model), ["no_consolidation"]
    from discovery.extract import call_values, fill
    values = {**call_values(ctx, parsed), "findings": findings_text(ids), "count": len(ids),
              "readers": len([c for c in per_model if c])}
    try:
        raw = ctx.llms(spec).complete_json(system=fill(consolidate_prompt.SYSTEM, values),
                                           prompt=fill(consolidate_prompt.CONSOLIDATE, values),
                                           schema=CONSOLIDATE_SCHEMA, max_tokens=CONSOLIDATE_MAX_TOKENS)
        reply = parse_reply(ConsolidateReply, raw, f"{spec}: consolidation reply")
    except LLMError as e:
        ctx.info(f"consolidate: {spec} failed, merging by quote overlap instead: {e}")
        return merge_claims(per_model), ["no_consolidation"]
    return build_groups(read_groups(reply, ids, ctx.config), ids), []
