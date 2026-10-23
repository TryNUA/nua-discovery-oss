"""Belief changes: the `apply_belief_change` task.

A change arrives three ways, all ending in `apply_change`:
- a request file in `requests/` (an agent, the MCP `request_change` tool, or a clone), ref
  `requests/<file>@<blob sha>`, via `agent`;
- a Slack click or form (a finding menu, Wrong read, Keep / Reword / Split / Retire on a belief under
  pressure), ref `<message ts>:<block_id>`, via `slack`;
- a roll-up proposal approved or rejected in Slack (`proposal_id` set), read from `config/proposals.yml`.

Rules (SPEC "Applying a change", "Change requests", "History lines", "Under pressure"):
- Skip when the ref is already recorded (a history line in beliefs.md, `config/rejected.yml` or
  `config/verdict_overrides.yml`), but still repair the Slack side.
- An add or watch whose source is already in a history line of a current entry is a duplicate, matched
  by span overlap: each held quote is placed on its digest claim (held_spans), so two claims that only
  share a phrase ("I don't think so.") are different sources. A history line keeps only the quote, so
  identical quotes (two "Exactly." answers) are placed one claim per change: at the span a finding
  item's ref names, else in the order the changes were made. discovery/indexes.py and discovery/rollup.py place
  held sources the same way. Retired entries do not block.
- A proposal whose target's version changed since the roll-up is stale.
- A Wrong read records the entry version its read was checked against (the call's `checked`), so it
  never hides a read of a later wording. A click carrying the version its brief showed is stale once
  the call has been re-checked against another version.
- Source quotes are checked against the transcript body and stored as the raw transcript text. A
  source may also be a span key `<start>-<end>` (a brief's finding menu or Wrong read), resolved through
  the digest to the claim it overlaps by half or more. A click also carries the quote it showed; when
  the claim at the span no longer says it (the call was re-digested from a richer copy since), the
  quote is looked up as text, and the change is invalid when it is gone.
- Files are written, the request file deleted, and everything committed in one commit; then a
  backcheck is scheduled (for changes that give an entry a new version, which also record themselves
  in config/backcheck.yml so an approval session runs one backcheck); then Slack: the clicked item
  is updated and a one-line notice is posted in #discovery-calls with the ref as message metadata.
- A request file that is already gone is looked up by its recorded ref, so a Slack failure after the
  commit is still repaired on retry. A duplicate, stale or invalid outcome records no ref; its reply is
  repaired from the message of the commit that deleted the file (`Remote.last_commit_message`).
"""
from __future__ import annotations

import copy
import hashlib
import re
from dataclasses import dataclass, field, replace
from typing import Any, NamedTuple

import yaml

from discovery import frontmatter
from discovery.beliefs import EMPTY_BELIEFS, VIAS, BeliefsDoc, HeldSource, parse_beliefs, parse_scope, render_beliefs
from discovery.context import Ctx
from discovery.merge import span_overlap
from discovery.types import SCOPE_KEYS, VERDICTS, VERSION_ACTIONS, Claim, Digest, Entry, HistoryLine, Quote, Scope
from discovery.verify import contains, normalize, quote_overlap

ACTIONS = ("add", "watch", "promote", "reword", "split", "merge", "retire", "keep", "dismiss_verdict", "reject")
OUTCOMES = ("applied", "duplicate", "stale", "invalid", "skipped")
PROPOSAL_VERBS = ("approve", "edit")     # also accepted as the action when proposal_id is set: both approve

BELIEFS = "beliefs.md"
PROPOSALS = "config/proposals.yml"
REJECTED = "config/rejected.yml"
VERDICT_OVERRIDES = "config/verdict_overrides.yml"
PROCESSOR_HEADER = "# Processor-written. Do not edit by hand.\n"

NOTICE_EVENT = "discovery_ref"
EM_DASH = chr(0x2014)                   # never in generated text
BOOTSTRAP_KEY = "rollup:bootstrap"      # config/proposals.yml `rollup` key of the bootstrap roll-up
SOURCE_OVERLAP = 0.5          # two quotes from the same call are the same source at this overlap

_ENTRY_ID = re.compile(r"\s*([BbWw])-?0*(\d+)\s*")
_SPAN = re.compile(r"\s*(\d+)\s*-\s*(\d+)\s*")
_FINDING_REF = re.compile(r":finding:(\d+)-(\d+)$")     # a finding item's ref names its claim's span
_OPEN = ("open", "", None)


# ---------- data ----------

@dataclass
class ChangeRequest:
    """One requested change. `sources` are (call_id, quote) pairs; a quote may be a span key "<start>-<end>".

    With `proposal_id` set, the proposal in config/proposals.yml supplies the action and fields:
    `action == "reject"` rejects it, any other action approves it, and a non-empty `statement`,
    `scope` or `statements` here (an Edit form) replaces the proposal's; `part_edits` (the Slack Edit
    form of a split proposal) edits its parts one by one. An empty scope keeps the proposal's.
    """
    action: str
    requested_by: str
    target: str | None = None
    targets: list[str] = field(default_factory=list)
    statement: str | None = None
    statements: list[dict] = field(default_factory=list)      # split: [{statement, scope}]
    scope: Scope = field(default_factory=Scope)
    sources: list[tuple[str, str]] = field(default_factory=list)
    reason: str | None = None
    proposal_id: str | None = None
    verdict: str | None = None          # dismiss_verdict only: the verdict being dismissed (optional)
    # dismiss_verdict only: the entry version the dismissed read was checked against (the digest's
    # `checked` when the brief was posted, carried by its Wrong read button); optional
    version: str | None = None
    # (call_id, span key) -> the quote Slack showed next to that span when it was clicked, so a span
    # that now lands on another claim (the call was re-digested from a richer copy) is caught
    shown: dict[tuple[str, str], str] = field(default_factory=dict)
    # Slack Edit of a split proposal: one edit per form field, merged into the proposal's parts
    # (see _edited_parts): {"statement": text, "" drops the part, None keeps it; "scope": Scope or None}
    part_edits: list[dict] = field(default_factory=list)


@dataclass
class ApplyResult:
    """What happened to one change. `message` is the one-line notice posted in #discovery-calls."""
    outcome: str      # applied | duplicate | stale | invalid | skipped
    message: str
    entry_ids: list[str] = field(default_factory=list)


class _Invalid(Exception):
    """A request that can never apply: answered once, never retried."""


# ---------- parsing requests ----------

def _text(value: Any) -> str | None:
    """Human- or model-written text (never a quote) on one line, em dashes replaced; None when empty."""
    if value is None:
        return None
    s = " ".join(str(value).replace(EM_DASH, " - ").split())
    return s or None


def _entry_id(value: Any) -> str | None:
    """"b-3", "B003" or "B-003" -> "B-003". None when empty. ValueError when it is not an entry id."""
    if value is None or str(value).strip() == "":
        return None
    m = _ENTRY_ID.fullmatch(str(value))
    if not m:
        raise ValueError(f"{value!r} is not a B- or W- id")
    return f"{m.group(1).upper()}-{int(m.group(2)):03d}"


def _scope(value: Any) -> Scope:
    """A scope from a dict, a Scope, a "key: value; key: value" string or None."""
    if value is None or value == "":
        return Scope()
    if isinstance(value, Scope):
        value = value.to_dict()
    if isinstance(value, dict):
        return Scope(**{k: _text(value.get(k)) for k in SCOPE_KEYS})
    if isinstance(value, str):
        return parse_scope(value)
    raise ValueError(f"scope must be a mapping or 'key: value' text, got {type(value).__name__}")


def _source_parts(value: Any) -> tuple[str, str, str | None]:
    """(call_id, quote or span key, quote shown with the span or None) from {call_id, quote},
    {call_id, span}, {call_id, span, quote} (a Slack click: the span plus the quote it showed) or a
    [call_id, quote] pair."""
    shown = None
    if isinstance(value, dict):
        call_id = _text(value.get("call_id"))
        quote = value.get("quote")
        span = value.get("span")
        if span is not None and (quote is None or _SPAN.fullmatch(str(span))):
            if quote is not None:
                shown = " ".join(str(quote).split()) or None
            quote = str(span)
    elif isinstance(value, (list, tuple)) and len(value) == 2:
        call_id, quote = _text(value[0]), value[1]
    else:
        raise ValueError(f"a source must be {{call_id, quote}}, got {value!r}")
    if not call_id or quote is None or not str(quote).strip():
        raise ValueError(f"a source needs a call_id and a quote, got {value!r}")
    return call_id, str(quote).strip(), shown


def _source(value: Any) -> tuple[str, str]:
    """(call_id, quote) from {call_id, quote}, {call_id, span} or a [call_id, quote] pair."""
    call_id, quote, _ = _source_parts(value)
    return call_id, quote


def _sources(data: dict) -> tuple[list[tuple[str, str]], dict[tuple[str, str], str]]:
    """(sources, shown quotes by (call_id, span key))."""
    raw = data.get("sources") or []
    if not isinstance(raw, list):
        raise ValueError("sources must be a list")
    parts = ([_source_parts(data["source"])] if data.get("source") else []) + [_source_parts(s) for s in raw]
    shown = {(c, q): s for c, q, s in parts if s}
    return [(c, q) for c, q, _ in parts], shown


def _targets(value: Any) -> list[str]:
    """Merge targets: a list of B- or W- ids. ValueError for anything else (a scalar `targets: 3`)."""
    raw = value or []
    if not isinstance(raw, list):
        raise ValueError("targets must be a list of B- or W- ids")
    return [t for t in (_entry_id(x) for x in raw) if t]


def _statements(value: Any) -> list[dict]:
    if not value:
        return []
    if not isinstance(value, list):
        raise ValueError("statements must be a list of {statement, scope}")
    out = []
    for item in value:
        if isinstance(item, str):
            item = {"statement": item}
        if not isinstance(item, dict):
            raise ValueError(f"bad split part {item!r}")
        out.append({"statement": _text(item.get("statement")), "scope": _scope(item.get("scope"))})
    return out


def _part_edits(value: Any) -> list[dict]:
    """`part_edits` from a Slack Edit form: [{"statement": str | None, "scope": dict | None}, ...].
    A statement of None keeps the part's, "" drops it; a scope of None (or an empty one) keeps the part's."""
    if not value:
        return []
    if not isinstance(value, list):
        raise ValueError("part_edits must be a list of {statement, scope}")
    out = []
    for item in value:
        if not isinstance(item, dict):
            raise ValueError(f"bad part edit {item!r}")
        statement = item.get("statement")
        scope = _scope(item.get("scope"))
        out.append({"statement": None if statement is None else (_text(statement) or ""),
                    "scope": None if scope.is_empty() else scope})
    return out


def _edited_parts(original: list[dict], edits: list[dict]) -> list[dict]:
    """A split proposal's parts after a Slack Edit: field i edits part i (new text, dropped, or kept as
    stored; a new scope or the part's own). Parts past the form's fields are kept. Raises _Invalid when
    fewer than two parts remain."""
    parts: list[dict] = []
    for i, edit in enumerate(edits):
        base = original[i] if i < len(original) else {"statement": None, "scope": Scope()}
        statement = base["statement"] if edit["statement"] is None else edit["statement"]
        if not statement:
            continue
        parts.append({"statement": statement, "scope": edit["scope"] or base["scope"]})
    parts += original[len(edits):]
    if len(parts) < 2:
        raise _Invalid("split needs at least two statements")
    return parts


def _check_shape(req: ChangeRequest) -> None:
    """Fields each action needs. Raises ValueError. Proposal-based requests take their fields from the proposal."""
    if req.action not in ACTIONS and not (req.proposal_id and req.action in PROPOSAL_VERBS):
        raise ValueError(f"unknown action {req.action!r}; expected one of {', '.join(ACTIONS)}")
    if not _text(req.requested_by):
        raise ValueError("requested_by is required")
    if req.action == "reject" and not req.proposal_id:
        raise ValueError("reject needs a proposal_id")
    if req.proposal_id:
        return
    a = req.action
    if a in ("add", "watch") and not (_text(req.statement) or req.sources):
        raise ValueError(f"{a} needs a statement or a source")
    if a in ("promote", "reword", "split", "retire", "keep", "dismiss_verdict") and not req.target:
        raise ValueError(f"{a} needs a target")
    if a == "reword" and not (_text(req.statement) or not req.scope.is_empty()):
        raise ValueError("reword needs a new statement or scope")
    if a == "split" and (len(req.statements) < 2 or not all(s.get("statement") for s in req.statements)):
        raise ValueError("split needs at least two statements")
    if a == "merge" and len(set(req.targets)) < 2:
        raise ValueError("merge needs at least two targets")
    if a == "dismiss_verdict" and not req.sources:
        raise ValueError("dismiss_verdict needs the verdict's source (call_id and quote)")
    if req.verdict is not None and req.verdict not in VERDICTS:
        raise ValueError(f"verdict must be one of {', '.join(VERDICTS)}, got {req.verdict!r}")


def request_from_dict(data: Any) -> ChangeRequest:
    """Build and check a ChangeRequest from request-file YAML or a task payload. Raises ValueError.

    Accepts the SPEC request fields (`source: {call_id, quote}`) plus `sources: [...]` (each
    `{call_id, quote}`, `{call_id, span}` or `[call_id, quote]`) and an optional `proposal_id`.
    """
    if not isinstance(data, dict):
        raise ValueError("a change request must be a mapping")
    sources, shown = _sources(data)
    req = ChangeRequest(
        action=str(data.get("action") or "").strip(),
        requested_by=_text(data.get("requested_by")) or "",
        target=_entry_id(data.get("target")),
        targets=_targets(data.get("targets")),
        statement=_text(data.get("statement")),
        statements=_statements(data.get("statements")),
        scope=_scope(data.get("scope")),
        sources=sources,
        reason=_text(data.get("reason")),
        proposal_id=_text(data.get("proposal_id")),
        verdict=_text(data.get("verdict")),
        version=_text(data.get("version")),
        shown=shown,
        part_edits=_part_edits(data.get("part_edits")),
    )
    _check_shape(req)
    return req


def _git_blob_sha(text: str) -> str:
    data = text.encode("utf-8")
    return hashlib.sha1(b"blob %d\x00" % len(data) + data).hexdigest()


def request_ref(ctx: Ctx, rel: str) -> str:
    """"requests/<file>@<blob sha>": the git blob sha at HEAD, or computed from the working file."""
    sha = ctx.remote.blob_sha(rel) or _git_blob_sha(ctx.repo.read(rel))
    return f"{rel}@{sha}"


def load_request(ctx: Ctx, rel: str) -> tuple[ChangeRequest, str]:
    """Read `requests/<file>.yml` into (request, ref). Raises ValueError on bad YAML or a bad shape."""
    ref = request_ref(ctx, rel)
    try:
        data = yaml.safe_load(ctx.repo.read(rel))
    except yaml.YAMLError as e:
        raise ValueError(f"bad YAML in {rel}: {e}".replace("\n", " ")) from None
    return request_from_dict(data), ref


# ---------- processor-written YAML files ----------

def _read_yaml(ctx: Ctx, rel: str) -> dict:
    """A processor-written YAML file as a mapping ({} when missing or empty). Bad YAML raises."""
    data = yaml.safe_load(ctx.repo.read_or(rel, ""))
    if data is None:
        return {}
    if isinstance(data, list):          # tolerate a bare list
        key = {REJECTED: "rejected", VERDICT_OVERRIDES: "overrides", PROPOSALS: "proposals"}.get(rel, "items")
        return {key: data}
    if not isinstance(data, dict):
        raise ValueError(f"{rel} must hold a mapping")
    return data


def _dump(data: dict) -> str:
    return PROCESSOR_HEADER + frontmatter.dump_yaml(data)


def _records(ctx: Ctx, rel: str, key: str) -> list[dict]:
    data = _read_yaml(ctx, rel)
    items = data.get(key)
    if items is None and rel == VERDICT_OVERRIDES:
        items = data.get("verdict_overrides")
    return [i for i in (items or []) if isinstance(i, dict)]


def _load_beliefs(ctx: Ctx) -> BeliefsDoc:
    return parse_beliefs(ctx.repo.read_or(BELIEFS, EMPTY_BELIEFS))


# ---------- recorded refs ----------

def ref_recorded(ctx: Ctx, ref: str) -> bool:
    """True when `ref` is already in a beliefs.md history line, config/rejected.yml or config/verdict_overrides.yml."""
    return _describe_recorded(ctx, ref) is not None


def _describe_recorded(ctx: Ctx, ref: str, doc: BeliefsDoc | None = None) -> tuple[str, list[str]] | None:
    """(notice text, entry ids) for a recorded ref, or None when the ref is not recorded anywhere."""
    doc = doc or _load_beliefs(ctx)
    hits = [(e, h) for e in doc.entries for h in e.history if h.ref == ref]
    if hits:
        return _describe_lines(hits)
    for r in _records(ctx, VERDICT_OVERRIDES, "overrides"):
        if r.get("ref") == ref:
            return (f"{r.get('who')} dismissed the {r.get('target')} read on {r.get('call_id')}",
                    [str(r.get("target"))])
    for r in _records(ctx, REJECTED, "rejected"):
        if r.get("ref") == ref:
            what = f"proposal {r['proposal']}" if r.get("proposal") else "a proposal"
            kind = f" ({r.get('type')}{' ' + r['target'] if r.get('target') else ''})" if r.get("type") else ""
            return f"{r.get('who')} rejected {what}{kind}", [r["target"]] if r.get("target") else []
    return None


def _describe_lines(hits: list[tuple[Entry, HistoryLine]]) -> tuple[str, list[str]]:
    """The notice for the history lines one change wrote, e.g. "Sam added B-017 via agent: <statement>"."""
    by_action: dict[str, tuple[Entry, HistoryLine]] = {}
    for e, h in hits:
        by_action.setdefault(h.action, (e, h))
    order = ("add", "watch", "promote", "split", "merge", "reword", "retire", "dropped", "keep")
    action = next((a for a in order if a in by_action), hits[0][1].action)
    e, h = by_action.get(action, hits[0])
    who, via = h.who, h.via
    ids = list(dict.fromkeys(x.id for x, _ in hits))
    if action == "add":
        return f"{who} added {e.id} via {via}: {e.statement}", ids
    if action == "watch":
        return f"{who} added {e.id} to Watching via {via}: {e.statement}", ids
    if action == "promote":
        old = (h.text or "").removeprefix("from ").strip()
        return f"{who} promoted {old} to {e.id} via {via}: {e.statement}", [e.id, *[i for i in ids if i != e.id]]
    if action == "split":
        head = next(((x, y) for x, y in hits if y.action == "split" and (y.text or "").startswith("into ")), (e, h))
        return f"{who} split {head[0].id} {head[1].text} via {via}", ids
    if action == "merge":
        others = (h.text or "").removeprefix("with ").strip()
        return f"{who} merged {others} into {e.id} via {via}: {e.statement}", [e.id, *[i for i in ids if i != e.id]]
    if action == "reword":
        return f"{who} reworded {e.id} via {via}: {e.statement}", ids
    if action == "retire":
        return f"{who} retired {e.id} via {via}", ids
    if action == "dropped":
        return f"{who} dropped {e.id} via {via}", ids
    if action == "keep":
        return f"{who} kept {e.id} via {via}", ids
    return f"{who} changed {', '.join(ids)} via {via}", ids


# ---------- sources ----------

class _Resolved(NamedTuple):
    """A request source checked against the digest and transcript."""
    source: tuple[str, str]               # (call_id, verbatim quote)
    claim: Claim | None                   # the digest claim at that quote, if any
    span: tuple[int, int] | None          # where the quote sits in the digest's transcript


def _same_span(new: _Resolved, span: tuple[int, int] | None, quote: str) -> bool:
    """Is a held source (`quote`, placed at `span` in the same call) the same source as `new`?
    By span overlap when both sides have a span (SPEC "Applying a change"), else by the longest shared
    run of words (verify.quote_overlap), both at SOURCE_OVERLAP."""
    if new.span and span:
        return span_overlap(new.span, span) >= SOURCE_OVERLAP
    return quote_overlap(new.source[1], quote) >= SOURCE_OVERLAP


def quote_spans(d: Digest) -> list[tuple[Claim, Quote]]:
    """Every verified quote of every finding in `d`, with its finding, in finding then quote order.
    A history source is one quote, so sources are placed against these, not against whole findings."""
    return [(c, q) for c in d.claims for q in c.quotes if q.offset]


def claims_span(d: Digest, quote: str) -> tuple[int, int] | None:
    """Where a stored quote that is not exactly one finding quote sits among `d`'s quotes: the span
    covered by the quotes it contains (a quote running over several), else the closest quote that
    contains it. None when no quote fits."""
    key = normalize(quote)
    if not key:
        return None
    inside = [q for _, q in quote_spans(d) if contains(quote, q.quote)]
    if inside:
        return min(q.span[0] for q in inside), max(q.span[1] for q in inside)
    around = [q for _, q in quote_spans(d) if contains(q.quote, quote)]
    best = min(around, key=lambda q: len(normalize(q.quote)), default=None)
    return best.span if best else None


def ref_span(ref: str | None) -> tuple[int, int] | None:
    """The span a finding item's ref names (`<message ts>:finding:<start>-<end>`, a Make belief or
    Watch from a brief or a roll-up), else None. The call it points into is never superseded once a
    history line cites it (SPEC "Supersede"), so the span stays valid."""
    m = _FINDING_REF.search(ref or "")
    return (int(m.group(1)), int(m.group(2))) if m else None


def held_spans(d: Digest | None, held: list[tuple[str, ...]], index: Any = None) -> list[tuple[int, int] | None]:
    """The span in `d` each held history source `(ref, quote[, date])` points at, or None when it
    cannot be placed. discovery/apply.py (duplicates), discovery/indexes.py (findings) and discovery/rollup.py (fresh
    sources) all place held sources with this, so they agree.

    A quote goes to the finding quote with the same normalized text. A history line keeps only the
    quote, so identical quotes (two "Exactly." answers) are placed one claim per change (`ref`):
    - a change whose ref names a span (ref_span: a finding item's Make belief or Watch) takes the
      claim at that span;
    - the other changes take the claims left, in claim order, in the order they were made (date,
      then ref), since each was accepted only while its claim was free. Past the last claim they
      share the last one.
    A quote that is no claim's quote goes where it occurs in the transcript (`index`, a
    verify.QuoteIndex), else where the claims put it (claims_span).
    """
    if d is None:
        return [None] * len(held)
    by_text: dict[str, list[tuple[int, int]]] = {}
    for _, q in quote_spans(d):
        by_text.setdefault(normalize(q.quote), []).append(q.span)
    items = [(str(h[0] or ""), str(h[1] or ""), str(h[2] or "") if len(h) > 2 else "") for h in held]
    keys = [normalize(quote) for _, quote, _ in items]
    changes: dict[str, dict[str, str]] = {}                     # normalized quote -> {ref: date}
    for (ref, _, day), key in zip(items, keys):
        if key in by_text:
            changes.setdefault(key, {}).setdefault(ref, day)
    placed: dict[tuple[str, str], tuple[int, int]] = {}         # (normalized quote, ref) -> span
    for key, refs in changes.items():
        spans = by_text[key]
        free = list(spans)
        for ref in refs:
            named = ref_span(ref)
            if named is None:
                continue
            score, span = max(((span_overlap(s, named), s) for s in spans), key=lambda x: x[0])
            if score >= SOURCE_OVERLAP:
                placed[(key, ref)] = span
                if span in free:
                    free.remove(span)
        for ref in sorted((r for r in refs if (key, r) not in placed), key=lambda r: (refs[r], r)):
            placed[(key, ref)] = free.pop(0) if free else spans[-1]
    out: list[tuple[int, int] | None] = []
    for (ref, quote, _), key in zip(items, keys):
        if (key, ref) in placed:
            out.append(placed[(key, ref)])
            continue
        m = index.find(quote) if index is not None and key else None
        out.append((m.start, m.end) if m is not None else claims_span(d, quote))
    return out


def placed_sources(d: Digest | None, held: list[HeldSource], index: Any = None
                   ) -> list[tuple[HeldSource, tuple[int, int] | None]]:
    """Each source of `held` (BeliefsDoc.held_sources) from `d`'s call, with the span it points at in
    `d` (held_spans). Sources from other calls are left out."""
    mine = [h for h in held if d is not None and h.call_id == d.call_id]
    return list(zip(mine, held_spans(d, [(h.ref, h.quote, h.date) for h in mine], index)))


def _dedupe_sources(sources: list[tuple[str, str]]) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    for s in sources:
        if not any(s[0] == o[0] and normalize(s[1]) == normalize(o[1]) for o in out):
            out.append(s)
    return out


class _Sources:
    """Resolves request sources against the digests and transcripts, with caches for one task."""

    def __init__(self, ctx: Ctx):
        self.ctx = ctx
        self._digests: dict[str, Digest] | None = None
        self._indexes: dict[str, Any] = {}

    def digest(self, call_id: str) -> Digest | None:
        if self._digests is None:
            from discovery.digest import load_digests
            self._digests = {d.call_id: d for d in load_digests(self.ctx.repo)}
        return self._digests.get(call_id)

    def index(self, d: Digest):
        """QuoteIndex over the digest's transcript, or None when the file is missing or does not parse."""
        if d.transcript not in self._indexes:
            index = None
            if self.ctx.repo.exists(d.transcript):
                from discovery.parse import ParseError, parse
                from discovery.verify import QuoteIndex
                try:
                    index = QuoteIndex(parse(d.transcript, self.ctx.repo.read(d.transcript)))
                except ParseError:
                    index = None
            self._indexes[d.transcript] = index
        return self._indexes[d.transcript]

    def resolve(self, call_id: str, quote: str, shown: str | None = None) -> _Resolved:
        """Check one source. Raises _Invalid when it cannot be checked.

        A span key `<start>-<end>` (a Slack click) resolves to the claim it overlaps by SOURCE_OVERLAP
        or more. Offsets belong to one transcript file, so when the click also carried the quote it
        `shown`, that claim's quote must match it too; if it does not (the call was re-digested from a
        richer copy since the click), the shown quote is looked up as text instead.
        """
        d = self.digest(call_id)
        if d is None or d.status != "digested":
            raise _Invalid(f"no digested call {call_id}")
        span = _SPAN.fullmatch(quote)
        if span:
            want = (int(span.group(1)), int(span.group(2)))
            scored = sorted(((span_overlap(q.span, want), n, c, q) for n, (c, q) in enumerate(quote_spans(d))),
                            key=lambda x: (-x[0], x[1]))
            for score, _, c, q in scored:
                if score < SOURCE_OVERLAP:
                    break
                if not shown or quote_overlap(q.quote, shown) >= SOURCE_OVERLAP:
                    return _Resolved((call_id, q.quote), c, q.span)
            if not shown:
                raise _Invalid(f"no claim at {quote} in {call_id}")
            hit = self._claim_for_text(d, shown, near=want[0])
            if hit is None:
                raise _Invalid(f"the call was re-digested since this was posted ({call_id})")
            c, q = hit
            return _Resolved((call_id, q.quote), c, q.span)
        index = self.index(d)
        if index is not None:
            m = index.find(quote)
            if m is None:
                raise _Invalid(f'quote not found in the {call_id} transcript: "{_short(quote)}"')
            claim = self._claim_at(d, (m.start, m.end))
            return _Resolved((call_id, m.raw_text), claim, (m.start, m.end))
        for c in d.claims:
            for q in c.quotes:
                if contains(q.quote, quote):
                    return _Resolved((call_id, quote), c, q.span)
        raise _Invalid(f'quote not found in {call_id} (transcript unavailable): "{_short(quote)}"')

    def _claim_for_text(self, d: Digest, text: str, *, near: int) -> tuple[Claim, Quote] | None:
        """The finding and quote at the place `text` (a quote Slack showed) occurs in the digest's
        transcript, the occurrence nearest `near` when it occurs more than once; by the findings' own
        quotes when the transcript is unavailable. None when it is not found."""
        if text.endswith("..."):                    # clipped for display: drop the cut-off last word
            text = text[:-3].rsplit(" ", 1)[0]
        index = self.index(d)
        if index is not None:
            hits = [(abs(m.start - near), hit) for m in index.find_all(text)
                    for hit in [self._quote_at(d, (m.start, m.end))] if hit is not None]
            return min(hits, key=lambda x: x[0])[1] if hits else None
        key = normalize(text)
        pairs = quote_spans(d)
        exact = [p for p in pairs if key and normalize(p[1].quote) == key]
        return exact[0] if exact else next((p for p in pairs if contains(p[1].quote, text)), None)

    @staticmethod
    def _quote_at(d: Digest, span: tuple[int, int]) -> tuple[Claim, Quote] | None:
        scored = [(span_overlap(q.span, span), (c, q)) for c, q in quote_spans(d)]
        best = max(scored, key=lambda x: x[0], default=(0.0, None))
        return best[1] if best[0] >= SOURCE_OVERLAP else None

    @classmethod
    def _claim_at(cls, d: Digest, span: tuple[int, int]) -> Claim | None:
        hit = cls._quote_at(d, span)
        return hit[0] if hit else None


def _short(text: str, limit: int = 80) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 3].rstrip() + "..."


# ---------- applying ----------

class _Change:
    """One change being applied: the beliefs doc, files to write, and what to report."""

    def __init__(self, ctx: Ctx, req: ChangeRequest, *, ref: str, via: str):
        self.ctx = ctx
        try:   # requests built in code (the runner) skip request_from_dict: clean the written text here too
            self.req = replace(req, statement=_text(req.statement), reason=_text(req.reason), scope=_scope(req.scope))
        except (TypeError, ValueError) as e:
            raise _Invalid(str(e)) from None
        self.ref = ref
        self.via = via
        self.doc = _load_beliefs(ctx)
        self.sources = _Sources(ctx)
        self.writes: dict[str, str] = {}
        self.new_version = False
        self.history_action: str | None = None     # "dropped" for an approved drop proposal
        self.date = ctx.now().astimezone(ctx.tz).date().isoformat()
        # A Slack click may come from anyone in the channel and names them by Slack handle; a request
        # file must name a teammate in config/people.yml.
        person = ctx.config.person(req.requested_by)
        if person is not None:
            self.who = (person.name or person.key).split()[0]
        elif via == "slack" and _text(req.requested_by):
            self.who = _text(req.requested_by).replace(" by ", " ")
        else:
            raise _Invalid(f"{req.requested_by or 'nobody'} is not a teammate in config/people.yml")

    # -- helpers --

    def line(self, action: str, *, sources=(), text: str | None = None, reason: str | None = None) -> HistoryLine:
        return HistoryLine(date=self.date, action=action, who=self.who, via=self.via, ref=self.ref,
                           sources=list(sources), reason=_text(reason), text=_text(text))

    def current(self, eid: str | None) -> Entry:
        e = self.doc.get(eid) if eid else None
        if e is None:
            raise _Invalid(f"{eid or 'the target'} does not exist")
        if not e.is_current:
            raise _Invalid(f"{eid} is retired")
        return e

    def checked(self, sources: list[tuple[str, str]]) -> list[_Resolved]:
        """Each source resolved, with the quote Slack showed for a span key when the click carried it."""
        return [self.sources.resolve(call_id, quote, self.req.shown.get((call_id, quote)))
                for call_id, quote in sources]

    def resolve(self, sources: list[tuple[str, str]]) -> tuple[list[tuple[str, str]], list[Claim | None]]:
        found = self.checked(sources)
        return _dedupe_sources([r.source for r in found]), [r.claim for r in found]

    def held_by(self, new: _Resolved) -> str | None:
        """The current entry whose history already holds the source `new`, or None. Held sources
        (BeliefsDoc.held_sources) are placed on the digest claims (held_spans) and compared by span
        overlap."""
        call_id = new.source[0]
        held = [h for h in self.doc.held_sources() if h.call_id == call_id]
        if not held:
            return None
        d = self.sources.digest(call_id)
        spans = held_spans(d, [(h.ref, h.quote, h.date) for h in held], self.sources.index(d) if d else None)
        return next((h.entry_id for h, span in zip(held, spans) if _same_span(new, span, h.quote)), None)

    def retire(self, e: Entry, line: HistoryLine) -> None:
        """Move `e` to Retired (last in the list, so the newest retired entry reads last)."""
        e.history.append(line)
        e.section = "retired"
        self.doc.entries.remove(e)
        self.doc.entries.append(e)

    def save_beliefs(self) -> None:
        self.writes[BELIEFS] = render_beliefs(self.doc)

    def applied(self) -> ApplyResult:
        self.save_beliefs()
        message, ids = _describe_lines([(e, h) for e in self.doc.entries for h in e.history if h.ref == self.ref])
        return ApplyResult("applied", message, ids)

    # -- actions --

    def add(self, section: str) -> ApplyResult:
        found = self.checked(self.req.sources)
        for r in found:
            hit = self.held_by(r)
            if hit:
                return ApplyResult("duplicate", f"Not added: already exists as {hit}", [hit])
        sources = _dedupe_sources([r.source for r in found])
        first = next((r.claim for r in found if r.claim is not None), None)
        statement = _text(self.req.statement) or (_text(first.text) if first else None)
        if not statement:
            raise _Invalid("no statement given and no claim to take it from")
        scope = self.req.scope if not self.req.scope.is_empty() else (copy.copy(first.scope) if first else Scope())
        eid = self.doc.next_id("B" if section == "beliefs" else "W")
        action = "add" if section == "beliefs" else "watch"
        self.doc.entries.append(Entry(id=eid, statement=statement, scope=scope, section=section,
                                      history=[self.line(action, sources=sources, reason=self.req.reason)]))
        self.new_version = True
        return self.applied()

    def promote(self) -> ApplyResult:
        w = self.current(self.req.target)
        if w.section != "watching":
            raise _Invalid(f"{w.id} is not a Watching item")
        sources, _ = self.resolve(self.req.sources)
        carried = [s for h in w.history for s in h.sources]
        eid = self.doc.next_id("B")
        new = Entry(id=eid, statement=_text(self.req.statement) or w.statement,
                    scope=self.req.scope if not self.req.scope.is_empty() else copy.copy(w.scope),
                    section="beliefs",
                    history=[self.line("promote", sources=_dedupe_sources(carried + sources), text=f"from {w.id}",
                                       reason=self.req.reason)])
        self.doc.entries.append(new)
        self.retire(w, self.line("promoted", text=f"to {eid}"))
        self.new_version = True
        return self.applied()

    def reword(self) -> ApplyResult:
        e = self.current(self.req.target)
        sources, _ = self.resolve(self.req.sources)
        if _text(self.req.statement):
            e.statement = _text(self.req.statement)
        if not self.req.scope.is_empty():
            e.scope = self.req.scope
        e.history.append(self.line("reword", sources=sources, reason=self.req.reason))
        self.new_version = True
        return self.applied()

    def split(self) -> ApplyResult:
        e = self.current(self.req.target)
        try:
            parts = [p for p in _statements(self.req.statements) if p["statement"]]
        except ValueError as err:
            raise _Invalid(str(err)) from None
        if len(parts) < 2:
            raise _Invalid("split needs at least two statements")
        sources, _ = self.resolve(self.req.sources)
        prefix = "B" if e.section == "beliefs" else "W"
        new_ids: list[str] = []
        for part in parts[1:]:
            eid = self.doc.next_id(prefix)
            new_ids.append(eid)
            scope = part["scope"]
            self.doc.entries.append(Entry(
                id=eid, statement=part["statement"], scope=scope if not scope.is_empty() else copy.copy(e.scope),
                section=e.section, history=[self.line("split", sources=sources, text=f"from {e.id}",
                                                      reason=self.req.reason)]))
        first_scope = parts[0]["scope"]
        e.statement = parts[0]["statement"]
        if not first_scope.is_empty():
            e.scope = first_scope
        e.history.append(self.line("split", sources=sources, text=f"into {', '.join([e.id, *new_ids])}",
                                   reason=self.req.reason))
        self.new_version = True
        return self.applied()

    def merge(self) -> ApplyResult:
        ids = list(dict.fromkeys(self.req.targets))
        if len(ids) < 2:
            raise _Invalid("merge needs at least two targets")
        entries = [self.current(i) for i in ids]
        keep = next((e for e in entries if e.section == "beliefs"), entries[0])
        others = [e for e in entries if e is not keep]
        sources, _ = self.resolve(self.req.sources)
        carried = [s for o in others for h in o.history for s in h.sources]
        if _text(self.req.statement):
            keep.statement = _text(self.req.statement)
        if not self.req.scope.is_empty():
            keep.scope = self.req.scope
        keep.history.append(self.line("merge", sources=_dedupe_sources(carried + sources),
                                      text=f"with {', '.join(o.id for o in others)}", reason=self.req.reason))
        for o in others:
            self.retire(o, self.line("merged", text=f"into {keep.id}"))
        self.new_version = True
        return self.applied()

    def retire_entry(self) -> ApplyResult:
        e = self.current(self.req.target)
        self.retire(e, self.line(self.history_action or "retire", reason=self.req.reason))
        return self.applied()

    def keep(self) -> ApplyResult:
        e = self.current(self.req.target)
        if e.section != "beliefs":
            raise _Invalid(f"{e.id} is a Watching item; only beliefs go under pressure")
        e.history.append(self.line("keep", reason=self.req.reason))
        return self.applied()

    def dismiss_verdict(self) -> ApplyResult:
        """Record a Wrong read in config/verdict_overrides.yml. Each record carries the claim's span
        (`offset`), the dismissed `verdict` when known and the entry version the dismissed read was
        checked against, so it hides only that verdict on that span, and not a verdict against a later
        wording (discovery.indexes.is_dismissed, SPEC "Under pressure").

        That version is the call's `checked` version for the target (the entry's current version when
        the call has none): after a Reword the old read stays in the digest until the backcheck runs,
        and that is the read a Wrong read clicked then dismisses. A Wrong read button also carries the
        version its read was checked against when the brief was posted (`req.version`); when the call
        has been re-checked against another version since, the click would hide a read the clicker never
        saw, so it is stale and nothing is recorded."""
        eid = self.req.target
        entry = self.doc.get(eid) if eid else None
        if entry is None:
            raise _Invalid(f"{eid or 'the target'} does not exist")
        verdict = self.req.verdict
        if verdict is not None and verdict not in VERDICTS:
            raise _Invalid(f"unknown verdict {verdict!r}")
        found = self.checked(self.req.sources)
        for (call_id, quote), r in zip(self.req.sources, found):
            d = self.sources.digest(call_id)
            if self.req.version and (d is None or d.checked.get(eid) != self.req.version):
                return ApplyResult("stale", f"Not applied: {call_id} was re-checked against the new wording "
                                            f"of {eid} since this was posted", [eid])
            # A Wrong read click names its claim by span: that claim must carry a read on the target,
            # or the span landed elsewhere (the call was re-digested since the brief was posted).
            if _SPAN.fullmatch(quote) and not (r.claim is not None and d is not None and any(
                    b.target == eid and r.claim.id in b.claims for b in d.belief_checks)):
                raise _Invalid(f"no {eid} read on that claim in {call_id} (re-digested since this was posted?)")
        data = _read_yaml(self.ctx, VERDICT_OVERRIDES)
        items = list(data.get("overrides") or data.pop("verdict_overrides", None) or [])
        sources: list[tuple[str, str]] = []
        for (call_id, quote), claim, at in ((r.source, r.claim, r.span) for r in found):
            if any(call_id == c and normalize(quote) == normalize(q) for c, q in sources):
                continue
            sources.append((call_id, quote))
            record: dict[str, Any] = {"call_id": call_id, "target": eid, "quote": quote}
            if claim is not None and at is not None:
                record["offset"] = [int(at[0]), int(at[1])]
            if verdict:
                record["verdict"] = verdict
            d = self.sources.digest(call_id)
            version = (d.checked.get(eid) if d is not None else None) or entry.version
            if version:
                record["version"] = version
            record.update({"who": self.who, "reason": _text(self.req.reason), "ref": self.ref})
            items.append(record)
        data["overrides"] = items
        self.writes[VERDICT_OVERRIDES] = _dump(data)
        call_ids = ", ".join(dict.fromkeys(c for c, _ in sources))
        return ApplyResult("applied", f"{self.who} dismissed the {eid} read on {call_ids}", [eid])

    # -- proposals --

    def proposal(self) -> ApplyResult:
        data = _read_yaml(self.ctx, PROPOSALS)
        proposals = [p for p in (data.get("proposals") or []) if isinstance(p, dict)]
        p = next((p for p in proposals if str(p.get("id")) == self.req.proposal_id), None)
        if p is None:
            raise _Invalid(f"no proposal {self.req.proposal_id} in {PROPOSALS}")
        pid = self.req.proposal_id
        if p.get("status") not in _OPEN:
            return ApplyResult("stale", f"Not applied: proposal {pid} is already {p.get('status')}",
                               _proposal_targets(p))

        if self.req.action == "reject":
            result = self.reject(p)
            p["status"] = "rejected"
            self.writes[PROPOSALS] = _dump(data)
            return result

        stale = self.stale_target(p)
        if stale:
            p["status"] = "stale"
            self.writes[PROPOSALS] = _dump(data)
            return ApplyResult("stale", f"Not applied: {stale} changed since proposal {pid}", _proposal_targets(p))

        if self.via == "slack" and _from_bootstrap(data, p):
            self.via = "bootstrap"
        kind = str(p.get("type") or "")
        if kind == "drop":
            kind, self.history_action = "retire", "dropped"
        if kind not in ACTIONS or kind in ("reject", "dismiss_verdict", "keep"):
            raise _Invalid(f"proposal {pid} has an unknown type {p.get('type')!r}")
        try:
            statements = self.req.statements or _statements(p.get("statements"))
            if self.req.part_edits:
                statements = _edited_parts(_statements(p.get("statements")), self.req.part_edits)
            self.req = ChangeRequest(
                action=kind, requested_by=self.req.requested_by,
                target=_entry_id(p.get("target")),
                targets=_targets(p.get("targets")),
                statement=_text(self.req.statement) or _text(p.get("statement")),
                statements=statements,
                scope=self.req.scope if not self.req.scope.is_empty() else _scope(p.get("scope")),
                sources=[_source(s) for s in (p.get("sources") or [])] + list(self.req.sources),
                reason=self.req.reason, proposal_id=pid, shown=dict(self.req.shown))
        except (TypeError, ValueError) as e:
            raise _Invalid(f"proposal {pid}: {e}") from None
        result = self.run_action()
        if result.outcome == "applied":
            p["status"] = "applied"
        elif result.outcome == "duplicate":
            p["status"] = "duplicate"
        self.writes[PROPOSALS] = _dump(data)
        return result

    def stale_target(self, p: dict) -> str | None:
        """The target id whose version moved since the proposal was written, or None.

        `target_version` belongs to `target`, or to the first of `targets` for a merge proposal. Any
        target that is gone or retired also makes the proposal stale.
        """
        versions = p.get("target_versions") if isinstance(p.get("target_versions"), dict) else {}
        targets = p.get("targets") or []
        anchor = _entry_id(p.get("target")) or (_entry_id(targets[0]) if targets else None)
        if anchor and p.get("target_version") is not None:
            versions = {**versions, anchor: p.get("target_version")}
        for raw_id, version in versions.items():
            eid = _entry_id(raw_id)
            e = self.doc.get(eid)
            if e is None or not e.is_current or e.version != version:
                return eid
        for raw_id in [p.get("target"), *(p.get("targets") or [])]:
            eid = _entry_id(raw_id)
            if eid and (self.doc.get(eid) is None or not self.doc.get(eid).is_current):
                return eid
        return None

    def reject(self, p: dict) -> ApplyResult:
        claims = []
        for s in p.get("sources") or []:
            try:
                call_id, quote = _source(s)
            except ValueError:
                continue
            claims.append({"call_id": call_id, "quote": quote})
        data = _read_yaml(self.ctx, REJECTED)
        items = list(data.get("rejected") or [])
        target = _entry_id(p.get("target")) if p.get("target") else None
        record: dict[str, Any] = {"type": p.get("type"), "target": target}
        # discovery.rollup.rejection_suppressed matches a merge on its target set and an add or watch on its
        # statement or shared evidence, so those are recorded too
        targets = _proposal_targets({"targets": p.get("targets") if isinstance(p.get("targets"), list) else []})
        if targets:
            record["targets"] = targets
        if _text(p.get("statement")):
            record["statement"] = _text(p.get("statement"))
        record.update({"claims": claims, "who": self.who, "reason": _text(self.req.reason), "ref": self.ref,
                       "proposal": self.req.proposal_id})
        items.append(record)
        data["rejected"] = items
        self.writes[REJECTED] = _dump(data)
        kind = f" ({p.get('type')}{' ' + target if target else ''})"
        return ApplyResult("applied", f"{self.who} rejected proposal {self.req.proposal_id}{kind}",
                           [target] if target else [])

    # -- dispatch --

    def run(self) -> ApplyResult:
        if self.via not in VIAS:
            raise _Invalid(f"unknown via {self.via!r}")
        if self.req.proposal_id:
            return self.proposal()
        if self.req.action == "reject":
            raise _Invalid("reject needs a proposal")
        return self.run_action()

    def run_action(self) -> ApplyResult:
        a = self.req.action
        if a == "add":
            return self.add("beliefs")
        if a == "watch":
            return self.add("watching")
        if a == "promote":
            return self.promote()
        if a == "reword":
            if not (_text(self.req.statement) or not self.req.scope.is_empty()):
                raise _Invalid("reword needs a new statement or scope")
            return self.reword()
        if a == "split":
            return self.split()
        if a == "merge":
            return self.merge()
        if a == "retire":
            return self.retire_entry()
        if a == "keep":
            return self.keep()
        if a == "dismiss_verdict":
            if not self.req.sources:
                raise _Invalid("dismiss_verdict needs the verdict's source")
            return self.dismiss_verdict()
        raise _Invalid(f"unknown action {a!r}")


def _from_bootstrap(data: dict, p: dict) -> bool:
    """True for a proposal from the bootstrap roll-up (its `rollup` key, or the file's, is "rollup:bootstrap")."""
    marker = p.get("rollup") or data.get("rollup")
    return marker == BOOTSTRAP_KEY or p.get("bootstrap") is True or data.get("bootstrap") is True


def _proposal_targets(p: dict) -> list[str]:
    ids = [p.get("target"), *(p.get("targets") or [])]
    out = []
    for raw in ids:
        try:
            eid = _entry_id(raw)
        except ValueError:
            eid = None
        if eid and eid not in out:
            out.append(eid)
    return out


def apply_change(ctx: Ctx, req: ChangeRequest, *, ref: str, via: str,
                 request_file: str | None = None, slack_item: dict | None = None) -> ApplyResult:
    """Apply one change and record it under `ref`. See the module docstring for the rules.

    `via` is slack | agent | bootstrap | system. `request_file` is deleted in the same commit as the
    outcome, whatever it is. `slack_item` = {"channel", "ts", "block_id"} (optionally "thread_ts",
    and "blocks" as seen at click time) is the clicked item to mark with the outcome after the commit.
    Invalid requests return outcome "invalid" instead of raising, so the task is not retried.
    """
    doc = _load_beliefs(ctx)
    recorded = _describe_recorded(ctx, ref, doc)
    if recorded is not None:
        return _repair(ctx, ref, recorded, doc, request_file=request_file, slack_item=slack_item)
    writes: dict[str, str] = {}
    new_version = False
    try:
        change = _Change(ctx, req, ref=ref, via=via)
        result = change.run()
        writes = change.writes
        new_version = change.new_version and result.outcome == "applied"
    except _Invalid as e:
        result = ApplyResult("invalid", f"Not applied: {e}", [])
    if new_version:
        from discovery import backcheck
        writes[backcheck.STATE_FILE] = backcheck.change_state(ref, ctx.now())
    _finish(ctx, result, ref=ref, writes=writes, request_file=request_file, slack_item=slack_item,
            regenerate=result.outcome == "applied", backcheck=new_version)
    return result


def _repair(ctx: Ctx, ref: str, recorded: tuple[str, list[str]], doc: BeliefsDoc, *, request_file: str | None,
            slack_item: dict | None) -> ApplyResult:
    """A change already recorded under `ref`: repair only what may have been lost after its commit (the
    backcheck task, which is named so it cannot run twice, and the Slack side)."""
    message, ids = recorded
    result = ApplyResult("skipped", message, ids)
    new_version = any(h.ref == ref and h.action in VERSION_ACTIONS for e in doc.current() for h in e.history)
    _finish(ctx, result, ref=ref, writes={}, request_file=request_file, slack_item=slack_item,
            regenerate=False, backcheck=new_version)
    return result


def _recorded_request_ref(ctx: Ctx, rel: str, doc: BeliefsDoc) -> str | None:
    """A ref `<rel>@<blob sha>` recorded in beliefs.md, config/verdict_overrides.yml or
    config/rejected.yml (the last one found), or None: how a retry finds the change a request file,
    deleted in the commit of its outcome, made."""
    prefix = f"{rel}@"
    found = [h.ref for e in doc.entries for h in e.history if h.ref and h.ref.startswith(prefix)]
    for path, key in ((VERDICT_OVERRIDES, "overrides"), (REJECTED, "rejected")):
        found += [str(r["ref"]) for r in _records(ctx, path, key) if str(r.get("ref") or "").startswith(prefix)]
    return found[-1] if found else None


def _finish(ctx: Ctx, result: ApplyResult, *, ref: str, writes: dict[str, str], request_file: str | None,
            slack_item: dict | None, regenerate: bool, backcheck: bool) -> None:
    """Write, delete the request file, commit, schedule the backcheck, then the Slack side."""
    paths: list[str] = []
    for rel, text in writes.items():
        ctx.repo.write(rel, text)
        paths.append(rel)
    if request_file and ctx.repo.exists(request_file):
        ctx.repo.delete(request_file)
        paths.append(request_file)
    if paths and regenerate:
        paths += [p for p in _regenerate(ctx) if p not in paths]
    if paths and not ctx.dry_run:
        ctx.remote.commit_and_push(f"{result.message} [{ref}]", paths,
                                   regenerate=(lambda: _regenerate(ctx)) if regenerate else None)
    ctx.info(f"apply_belief_change {ref}: {result.outcome}: {result.message}")
    if backcheck and ctx.queue is not None and not ctx.dry_run:
        from discovery.backcheck import backcheck_task_name
        # {"after": ref} lets the backcheck wait for a later change in the same session (discovery/backcheck.py)
        ctx.queue.enqueue("backcheck", {"after": ref}, name=backcheck_task_name(ref),
                          delay_seconds=int(ctx.config.pipeline.t("backcheck_delay_seconds")))
    _slack_side(ctx, result, ref=ref, request_file=request_file, slack_item=slack_item)


def _regenerate(ctx: Ctx) -> list[str]:
    """Rebuild the generated index files (Task 11); returns their paths."""
    from discovery import indexes
    return list(indexes.regenerate(ctx))


# ---------- Slack ----------

def _notice_text(result: ApplyResult, request_file: str | None) -> str:
    if result.outcome in ("applied", "skipped") or not request_file:
        return result.message
    name = request_file.rsplit("/", 1)[-1]
    return f"{name}: {result.message[0].lower()}{result.message[1:]}"


def _slack_side(ctx: Ctx, result: ApplyResult, *, ref: str, request_file: str | None,
                slack_item: dict | None) -> None:
    """Mark the clicked item and post the notice unless a message already carries this ref."""
    if ctx.dry_run or ctx.slack is None:
        return
    text = _notice_text(result, request_file).replace(EM_DASH, "-")
    if slack_item:
        _update_item(ctx, slack_item, text)
    channel = ctx.channel("calls")
    if ctx.slack.find_by_metadata(channel, NOTICE_EVENT, ref) is None:
        blocks = [{"type": "section", "text": {"type": "mrkdwn", "text": _mrkdwn(text)}}]
        ctx.slack.post(channel, blocks, text, metadata={"event_type": NOTICE_EVENT, "event_payload": {"key": ref}})


def _mrkdwn(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def mark_block(blocks: list[dict], block_id: str, text: str) -> list[dict] | None:
    """Copy of `blocks` with the item `block_id` marked with `text`, or None when no block has that id.

    Buttons (an actions block) or an earlier lock (a context block) are replaced by a context line.
    A section keeps its text, loses its accessory menu, and gets a context line `<block_id>:status`
    right after it (replacing an earlier one).
    """
    if not any(b.get("block_id") == block_id for b in blocks):
        return None
    status = f"{block_id}:status"
    note = [{"type": "mrkdwn", "text": _mrkdwn(text)[:3000]}]
    out: list[dict] = []
    for b in blocks:
        if b.get("block_id") == status:
            continue
        if b.get("block_id") != block_id:
            out.append(b)
            continue
        if b.get("type") == "section":
            out.append({k: v for k, v in b.items() if k != "accessory"})
            out.append({"type": "context", "block_id": status, "elements": note})
        else:
            out.append({"type": "context", "block_id": block_id, "elements": note})
    return out


def _fresh_message(ctx: Ctx, channel: str, ts: str) -> dict | None:
    """One top-level message as it is now ({"ts", "blocks", "text"}), or None when it cannot be read.

    Uses the Slack port's `message(channel, ts)` (conversations.history on that one ts) when the port
    has it; `replies` cannot serve here because it leaves out the parent."""
    fetch = getattr(ctx.slack, "message", None)
    if not callable(fetch):
        return None
    try:
        return fetch(channel, ts)
    except Exception as e:           # fall back to the blocks carried from click time
        ctx.info(f"could not read slack message {ts}: {e}")
        return None


def _update_item(ctx: Ctx, item: dict, text: str) -> None:
    """Replace the clicked item's lock with the outcome, on the message as it is now: a thread reply is
    read from its thread, a top-level message by its ts, so items locked or resolved since the click
    keep their state. The blocks carried from click time are only a fallback."""
    channel, ts, block_id = item.get("channel"), item.get("ts"), item.get("block_id")
    if not (channel and ts and block_id):
        ctx.info(f"slack item incomplete: {item}")
        return
    blocks, fallback = None, None
    thread_ts = item.get("thread_ts")
    if thread_ts and thread_ts != ts:
        for m in ctx.slack.replies(channel, thread_ts):
            if m.get("ts") == ts:
                blocks, fallback = m.get("blocks"), m.get("text")
                break
    else:
        m = _fresh_message(ctx, channel, ts)
        if m is not None and m.get("blocks") is not None:
            blocks, fallback = m.get("blocks"), m.get("text")
    if blocks is None:
        blocks, fallback = item.get("blocks"), item.get("text")
    marked = mark_block(blocks or [], block_id, text)
    if marked is None:
        ctx.info(f"slack item {ts} has no block {block_id}; not updated")
        return
    ctx.slack.update(channel, ts, marked, fallback or text)


# ---------- task entry point ----------

def apply_task(ctx: Ctx, payload: dict) -> dict:
    """The `apply_belief_change` task.

    Payloads:
    - `{"request_file": "requests/<file>.yml"}` for a request file, via agent (the keys `request` and
      `path` work too). A file that is already gone is skipped; bad YAML or a bad shape is answered as
      invalid and the file deleted.
    - `{"ref": "<message ts>:<block_id>", "change": {<ChangeRequest fields>}, "via": "slack",
       "slack_item": {"channel", "ts", "block_id", "thread_ts"?, "blocks"?}}` for Slack clicks and forms.
    Returns {"ok", "outcome", "message", "entry_ids"}.
    """
    rel = payload.get("request_file") or payload.get("request") or payload.get("path")
    if rel:
        if not ctx.repo.exists(rel):
            # Deleted in the commit of its outcome. If that change is recorded, a failure after the
            # commit may have lost the notice or the backcheck: repair them.
            doc = _load_beliefs(ctx)
            ref = _recorded_request_ref(ctx, rel, doc)
            recorded = _describe_recorded(ctx, ref, doc) if ref else None
            if recorded is not None:
                return _task_result(_repair(ctx, ref, recorded, doc, request_file=rel, slack_item=None))
            # A duplicate, stale or invalid outcome is recorded only in the message of the commit that
            # deleted the file: repair its one-line reply from there.
            lost = _deleting_commit(ctx, rel)
            if lost is not None:
                ref, message = lost
                _slack_side(ctx, ApplyResult("invalid", message), ref=ref, request_file=rel, slack_item=None)
                ctx.info(f"apply_belief_change: {rel} is already gone ({message})")
                return _task_result(ApplyResult("skipped", message))
            result = ApplyResult("skipped", f"Already gone: {rel}")
            ctx.info(f"apply_belief_change: {rel} is already gone")
            return _task_result(result)
        ref = request_ref(ctx, rel)
        try:
            req, ref = load_request(ctx, rel)
        except (TypeError, ValueError) as e:         # TypeError: extra guard for a shape _check_shape missed
            result = ApplyResult("invalid", f"Not applied: {e}")
            if not ref_recorded(ctx, ref):
                _finish(ctx, result, ref=ref, writes={}, request_file=rel, slack_item=None,
                        regenerate=False, backcheck=False)
            return _task_result(result)
        return _task_result(apply_change(ctx, req, ref=ref, via=payload.get("via") or "agent", request_file=rel))

    ref = payload.get("ref")
    if not ref:
        raise ValueError("apply_belief_change needs a request path or a ref")
    item = payload.get("slack_item")
    try:
        req = request_from_dict(payload.get("change") or {})
    except (TypeError, ValueError) as e:
        result = ApplyResult("invalid", f"Not applied: {e}")
        _finish(ctx, result, ref=ref, writes={}, request_file=None, slack_item=item, regenerate=False,
                backcheck=False)
        return _task_result(result)
    return _task_result(apply_change(ctx, req, ref=ref, via=payload.get("via") or "slack", slack_item=item))


def _deleting_commit(ctx: Ctx, rel: str) -> tuple[str, str] | None:
    """(ref, message) of a duplicate, stale or invalid outcome for the gone request file `rel`, read from
    the last commit that touched it: `_finish` commits "<message> [<rel>@<blob sha>]", and those
    messages start with "Not ". None when the remote cannot tell (it has no `last_commit_message(rel)`,
    or the lookup fails) or the last commit on `rel` is not such an outcome (deleted by hand)."""
    fetch = getattr(ctx.remote, "last_commit_message", None)
    if not callable(fetch):
        return None
    try:
        text = fetch(rel)
    except Exception as e:            # best effort: the file is gone either way
        ctx.info(f"could not read the commit that deleted {rel}: {e}")
        return None
    m = re.fullmatch(rf"\s*(Not .+?) \[({re.escape(rel)}@[0-9a-f]{{7,64}})\]\s*", str(text or ""), re.S)
    return (m.group(2), " ".join(m.group(1).split())) if m else None


def _task_result(result: ApplyResult) -> dict:
    return {"ok": True, "outcome": result.outcome, "message": result.message, "entry_ids": result.entry_ids}
