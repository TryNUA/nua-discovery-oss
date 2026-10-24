"""Weekly and bootstrap roll-up (SPEC "Weekly roll-up" and "Bootstrap").

One model call (`pipeline.rollup`) reads the claims from calls since the last roll-up, every
finding, the previous themes, beliefs.md, decisions.md, seat counts, config/rejected.yml and last
week's unanswered proposals. It returns themes and proposals, citing claims by ref
(`<call_id>#<claim id>`), never by retyped quotes. Code then decides what is posted: it resolves
every ref to the verbatim quote in the digest, applies the thresholds in config/pipeline.yml and
the rejection filter, and adds the promote, retire and drop proposals that need no judgment when
the model missed them.

Order of work:
1. Write config/themes.yml and config/proposals.yml, regenerate the indexes, commit and push.
2. Post the summary to #discovery-calls with metadata {"event_type": "nua_rollup",
   "event_payload": {"key": "rollup:<ISO week>"}} ("rollup:bootstrap" for the bootstrap), then one
   thread reply per item: beliefs under pressure, proposals, single-call findings.
3. Strip the buttons from the previous roll-up's unanswered items. Its open proposals and its
   single-call findings still unanswered were already carried into this one (same id or key) when they
   still pass the checks; those replies say "Rolled into", the rest "Expired".

A retry (themes.yml already records this roll-up's key) skips the model and the commit, reads the
thread and posts only the missing items. `ctx.dry_run` writes the files but skips git and Slack.

Interactive keys (read by nua/interact.py):
- Proposal: actions block `prop:<proposal id>`, buttons `prop_approve`, `prop_edit`, `prop_reject`,
  each with value `<proposal id>`.
- Under pressure: actions block `pressure:<entry id>`, buttons `pressure_keep`, `pressure_reword`,
  `pressure_split`, `pressure_retire`, each with value `<entry id>`.
- Single-call finding: the brief's finding block (`finding:<start>-<end>`, overflow `finding_menu`,
  values `mb|<call_id>|<start>-<end>` and `w|<call_id>|<start>-<end>`).
Every reply also carries a block `item:<item key>` that locking never touches, so a retry can tell
which items are already posted.

Files (both processor-written):
- config/themes.yml: {"rollup": {"key", "date", "previous", "calls", "counted", "findings"}, "themes":
  [{"name", "claims": [{"call_id", "quote"}]}]}. `calls` lists the calls this roll-up counted as new;
  `counted` every call any roll-up has counted, so a call digested late (dated before the last
  roll-up) is still new once; `findings` the item keys of the single-call findings it posted, so a
  retry posts the same ones and the next roll-up can carry the unanswered ones forward.
- config/proposals.yml: {"rollup": key, "proposals": [{"id", "type", "statement", "scope",
  "sources": [{"call_id", "quote"}], "target", "targets", "target_version", "status", "rationale",
  "statements" (split only), "rollup"}]}. The previous roll-up's records stay one more week so a
  click already queued can still find its proposal; unanswered ones are marked `expired`.
"""
from __future__ import annotations

import importlib
import re
from collections import Counter
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Annotated, Any, Iterable

import yaml
from pydantic import Field, WithJsonSchema

from nua import frontmatter
from nua.apply import claims_span, placed_sources
from nua.beliefs import EMPTY_BELIEFS, BeliefsDoc, format_scope, parse_beliefs, parse_decisions
from nua.config import Config
from nua.context import Ctx
from nua.digest import apply_overrides, load_digests
from nua.extract import ScopeReply, clean_text, fill, merge_seats
from nua.merge import span_overlap
from nua.prompts import rollup as rollup_prompt
from nua.schemas import Mapping, Objects, OptText, Reply, Text, Texts, strict_schema
from nua.types import VERSION_ACTIONS, BeliefCheck, Claim, Decision, Digest, Entry, Scope, iso_date
from nua.verify import normalize, quote_overlap

THEMES_PATH = "config/themes.yml"
PROPOSALS_PATH = "config/proposals.yml"
REJECTED_PATH = "config/rejected.yml"
EVENT_TYPE = "nua_rollup"
BOOTSTRAP_KEY = "rollup:bootstrap"
BOOTSTRAP_CAP = 40            # SPEC "Bootstrap": up to 40 proposed entries
MODEL_MAX_TOKENS = 32_000
QUOTES_SHOWN = 5              # evidence quotes shown per Slack item
QUOTE_CHARS = 300
MAX_TEXT = 3000               # Slack section text limit
SAME_SOURCE = 0.5             # quote overlap at which two sources from one call are the same claim
RESOLVE = 0.8                 # quote overlap at which a stored quote resolves to a digest claim
MERGE_OVERLAP = 0.5           # share of supporting claims two entries must share to merge

# Display order, which is also the ranking order for the bootstrap cap.
TYPES = ("add", "watch", "promote", "split", "retire", "reword", "merge", "drop")
MODEL_TYPES = {"weekly": ("add", "promote", "split", "retire", "reword", "merge", "drop"),
               "bootstrap": ("add", "watch")}
STRONG = ("agree", "arbiter")  # verdicts that count toward a threshold
EM_DASH = chr(0x2014)      # spelled this way so no file contains one
SEP = " · "
HEADER = "# Processor-written by the roll-up (nua/rollup.py). Do not edit by hand.\n"

_WEEK = re.compile(r"(?:rollup:)?(\d{4}-W\d{2})")
_ENTRY_ID = re.compile(r"\b([BW])-?(\d+)\b", re.I)


def _indexes():
    """nua.indexes, imported on use (it is built in parallel; tests swap in a stand-in)."""
    return importlib.import_module("nua.indexes")


# ---------- proposals ----------

@dataclass
class Proposal:
    id: str
    type: str                 # add | watch | promote | split | retire | reword | merge | drop
    statement: str | None
    scope: Scope
    sources: list[tuple[str, str]]          # (call_id, verbatim quote)
    target: str | None = None
    targets: list[str] = field(default_factory=list)
    target_version: str | None = None
    status: str = "open"
    rationale: str = ""
    statements: list[dict] = field(default_factory=list)   # split only: [{"statement", "scope": {...}}]


def _scope_dict(s: Scope) -> dict:
    return {k: v for k, v in s.to_dict().items() if v}


def _clean_scope(raw: Any) -> Scope:
    """A Scope from model or file output, with em dashes removed and blanks as None."""
    raw = raw if isinstance(raw, dict) else {}
    return Scope.from_dict({k: (clean_text(v) or None) if v is not None else None for k, v in raw.items()})


def proposal_to_dict(p: Proposal) -> dict:
    """The proposals.yml record for `p` (without the `rollup` key)."""
    d = {"id": p.id, "type": p.type, "statement": p.statement, "scope": _scope_dict(p.scope),
         "sources": [{"call_id": c, "quote": q} for c, q in p.sources],
         "target": p.target, "targets": list(p.targets), "target_version": p.target_version,
         "status": p.status, "rationale": p.rationale}
    if p.statements:
        d["statements"] = [{"statement": s["statement"], "scope": dict(s.get("scope") or {})} for s in p.statements]
    return d


def _source_pair(raw: Any) -> tuple[str, str] | None:
    if isinstance(raw, dict):
        call_id, quote = raw.get("call_id"), raw.get("quote")
    elif isinstance(raw, (list, tuple)) and len(raw) == 2:
        call_id, quote = raw
    else:
        return None
    return (str(call_id), str(quote or "")) if call_id else None


def proposal_from_dict(d: dict) -> Proposal:
    """Inverse of `proposal_to_dict`. Sources may be {call_id, quote} maps or [call_id, quote] pairs."""
    sources = [s for s in (_source_pair(x) for x in d.get("sources") or []) if s]
    statements = [{"statement": str(s.get("statement") or ""), "scope": _scope_dict(_clean_scope(s.get("scope")))}
                  for s in d.get("statements") or [] if isinstance(s, dict) and s.get("statement")]
    return Proposal(id=str(d.get("id") or ""), type=str(d.get("type") or ""), statement=d.get("statement"),
                    scope=_clean_scope(d.get("scope")), sources=sources, target=d.get("target"),
                    targets=[str(t) for t in d.get("targets") or []], target_version=d.get("target_version"),
                    status=str(d.get("status") or "open"), rationale=str(d.get("rationale") or ""),
                    statements=statements)


# ---------- source matching ----------

def _same_source(a: tuple[str, str], b: tuple[str, str], world: "_World | None" = None) -> bool:
    """Same call, and the same span of it: by span overlap (at least half the shorter span) when both
    quotes are a digest claim's quote (SPEC "matched by span overlap"), else by the longest shared run
    of words (verify.quote_overlap) over the shorter quote. Two claims sharing a short phrase ("I don't
    think so.") are different sources."""
    if a[0] != b[0]:
        return False
    if world is not None:
        sa, sb = world.span_of(a), world.span_of(b)
        if sa and sb:
            return span_overlap(sa, sb) >= SAME_SOURCE
    return quote_overlap(a[1], b[1]) >= SAME_SOURCE


def _dedupe_sources(sources: Iterable[tuple[str, str]], world: "_World | None" = None) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    for s in sources:
        if not any(_same_source(s, o, world) for o in out):
            out.append(s)
    return out


def rejection_suppressed(p: Proposal, rejected: list[dict], world: "_World | None" = None) -> bool:
    """True when a rejection covers `p` (SPEC "Weekly roll-up"): same type and target, a related
    proposal, and fewer than 2 of `p`'s sources outside the rejection's evidence claims.

    Adds and watches have no target, and drops no sources, so relatedness is checked: an add or watch
    must share a source with the rejected claims or have the same statement, and a merge must have the
    same target set (records written before `targets` was recorded fall back to a shared source).
    Otherwise one rejected Watch would hide every later single-source Watch.
    """
    for r in rejected or []:
        if str(r.get("type") or "") != p.type or (r.get("target") or None) != (p.target or None):
            continue
        claims = [s for s in (_source_pair(c) for c in r.get("claims") or []) if s]
        shares = any(_same_source(s, c, world) for s in p.sources for c in claims)
        if p.type in ("add", "watch"):
            statement = normalize(str(r.get("statement") or ""))
            if not (shares or (statement and statement == normalize(p.statement or ""))):
                continue
        elif p.type == "merge":
            targets = {t for t in (_entry_id(x) for x in r.get("targets") or []) if t}
            if (targets and targets != set(p.targets)) or (not targets and not shares):
                continue
        outside = [s for s in p.sources if not any(_same_source(s, c, world) for c in claims)]
        if len(outside) < 2:
            return True
    return False


# ---------- the data one roll-up reads ----------

@dataclass
class _World:
    config: Config
    today: date
    digests: list[Digest]                      # status: digested only
    beliefs: BeliefsDoc
    decisions: list[Decision]
    overrides: list[dict]                      # config/verdict_overrides.yml
    rejected: list[dict]
    findings: list[tuple[Digest, Claim]]
    by_call: dict[str, Digest] = field(default_factory=dict)
    seat_counts: Counter = field(default_factory=Counter)
    finding_keys: set[tuple[str, str]] = field(default_factory=set)
    exact: dict[str, dict[str, Claim]] = field(default_factory=dict)   # call_id -> normalized quote -> finding
    _spans: dict[tuple[str, str], tuple[int, int] | None] = field(default_factory=dict)   # span_of cache
    _held: dict[str, list[tuple[str, tuple[int, int] | None]]] | None = None   # held() cache

    def __post_init__(self) -> None:
        self.by_call = {d.call_id: d for d in self.digests}
        self.seat_counts = Counter(d.seat for d in self.digests if d.seat)
        self.finding_keys = {_key(d, c) for d, c in self.findings}
        for d in self.digests:
            quotes = self.exact.setdefault(d.call_id, {})
            for c in d.claims:
                for q in c.quote_texts:
                    quotes.setdefault(normalize(q), c)

    def t(self, name: str) -> Any:
        return self.config.pipeline.t(name)

    def held(self, call_id: str) -> list[tuple[str, tuple[int, int] | None]]:
        """(quote, span) for every source of `call_id` in the history of a current entry, placed on the
        digest claims as nua/apply.py places them (nua.apply.held_spans: two identical "Exactly."
        answers are held one per change)."""
        if self._held is None:
            held = self.beliefs.held_sources()
            self._held = {}
            for cid in dict.fromkeys(h.call_id for h in held):
                d = self.by_call.get(cid)
                self._held[cid] = ([(h.quote, span) for h, span in placed_sources(d, held)] if d is not None
                                   else [(h.quote, None) for h in held if h.call_id == cid])
        return self._held.get(call_id, [])

    def span_of(self, source: tuple[str, str]) -> tuple[int, int] | None:
        """Where `source` sits in its call: the span of the first digest claim with the same quote
        (normalized), which is where nua/apply.py resolves a proposal's quote (its first occurrence),
        else where the claims put it (nua.apply.claims_span, as apply places held sources). None when
        no claim fits."""
        key = (source[0], normalize(source[1]))
        if key not in self._spans:
            c = self.exact.get(source[0], {}).get(key[1])
            d = self.by_call.get(source[0])
            own = next((q.span for q in c.quotes if q.offset and normalize(q.quote) == key[1]), None) if c else None
            if own is not None:
                self._spans[key] = own
            else:
                self._spans[key] = claims_span(d, source[1]) if d is not None and c is None else None
        return self._spans[key]


def _read_yaml(ctx: Ctx, rel: str) -> dict:
    """A processor-written YAML map, or {} when the file is missing or empty."""
    data = yaml.safe_load(ctx.repo.read_or(rel, "")) or {}
    if not isinstance(data, dict):
        raise ValueError(f"{rel}: expected a YAML map")
    return data


def load_rejected(ctx: Ctx) -> list[dict]:
    """Rejections from config/rejected.yml: [{type, target, claims: [{call_id, quote}], who, reason, ref}]."""
    return [r for r in _read_yaml(ctx, REJECTED_PATH).get("rejected") or [] if isinstance(r, dict)]


def _iso_date(value: Any) -> str | None:
    """A digest date as YYYY-MM-DD text. YAML reads an unquoted `date: 2026-09-13` (a hand correction)
    as a date object, and the roll-up compares dates as text. Digest.from_dict already converts it;
    this guards digests built some other way."""
    return iso_date(value)


def _load_world(ctx: Ctx, today: date) -> _World:
    ix = _indexes()
    digests = [apply_overrides(d, ctx.config.overrides) for d in load_digests(ctx.repo) if d.status == "digested"]
    digests = [d if isinstance(d.date, str) or d.date is None else d.model_copy(update={"date": _iso_date(d.date)})
               for d in digests]
    beliefs = parse_beliefs(ctx.repo.read_or("beliefs.md", EMPTY_BELIEFS))
    decisions = parse_decisions(ctx.repo.read_or("decisions.md", ""))
    overrides = list(ix.load_verdict_overrides(ctx.repo) or [])
    findings = list(ix.finding_claims(digests, beliefs, overrides))
    return _World(config=ctx.config, today=today, digests=digests, beliefs=beliefs, decisions=decisions,
                  overrides=overrides, rejected=load_rejected(ctx), findings=findings)


def _key(d: Digest, c: Claim) -> tuple[str, str]:
    return (d.call_id, c.id or "")


def _ref(d: Digest, c: Claim) -> str:
    return f"{d.call_id}#{c.id}"


def _resolve_ref(world: _World, ref: Any) -> tuple[Digest, Claim] | None:
    """`<call_id>#<claim id>` to its digest and claim, or None."""
    call_id, sep, claim_id = str(ref or "").strip().strip("[]`").rpartition("#")
    d = world.by_call.get(call_id.strip()) if sep else None
    c = d.claim(claim_id.strip()) if d else None
    return (d, c) if c else None


def _resolve_quote(world: _World, call_id: str, quote: str) -> tuple[Digest, Claim] | None:
    """The claim in `call_id`'s digest whose quote best matches `quote` (overlap >= RESOLVE), or None."""
    d = world.by_call.get(call_id)
    if d is None or not quote:
        return None
    exact = world.exact.get(call_id, {}).get(normalize(quote))
    if exact is not None:
        return d, exact
    best, score = None, 0.0
    for c in d.claims:
        s = max((quote_overlap(q, quote) for q in c.quote_texts), default=0.0)
        if s > score:
            best, score = c, s
    return (d, best) if best is not None and score >= RESOLVE else None


def _as_sources(pairs: Iterable[tuple[Digest, Claim]], world: "_World | None" = None) -> list[tuple[str, str]]:
    return _dedupe_sources(((d.call_id, c.quote) for d, c in pairs), world)


def _calls_seats(world: _World, sources: Iterable[tuple[str, str]]) -> tuple[int, int]:
    """Distinct calls and distinct known seats behind a list of sources."""
    calls = {c for c, _ in sources if c in world.by_call}
    seats = {world.by_call[c].seat for c in calls if world.by_call[c].seat}
    return len(calls), len(seats)


# ---------- evidence against an entry ----------

def _keep_date(entry: Entry) -> str | None:
    """Date of a Keep that came after the entry's current version, else None."""
    for h in reversed(entry.history):
        if h.action in VERSION_ACTIONS:
            return None
        if h.action == "keep":
            return h.date
    return None


def _version_date(entry: Entry) -> str | None:
    for h in reversed(entry.history):
        if h.action in VERSION_ACTIONS:
            return h.date
    return entry.history[0].date if entry.history else None


def _counted(world: _World, entry: Entry, verdict: str, *, keep_window: bool = False,
             calls: set[str] | None = None) -> list[tuple[Digest, BeliefCheck]]:
    """Checks of `verdict` on `entry` that count toward a threshold: decided agree or arbiter, not
    dismissed, from digests checked against the entry's current version, and (with keep_window)
    from calls dated after a Keep. `calls` limits the digests looked at."""
    ix = _indexes()
    kept = _keep_date(entry) if keep_window else None
    out = []
    for d in world.digests:
        if calls is not None and d.call_id not in calls:
            continue
        if entry.version and d.checked.get(entry.id) != entry.version:
            continue
        if kept and (not d.date or d.date <= kept):
            continue
        for b in d.belief_checks:
            if (b.target == entry.id and b.verdict == verdict and b.decided_by in STRONG
                    and not ix.is_dismissed(b, d, world.overrides)):
                out.append((d, b))
    return out


def _check_claims(pairs: Iterable[tuple[Digest, BeliefCheck]]) -> list[tuple[Digest, Claim]]:
    out = []
    for d, b in pairs:
        out += [(d, c) for c in (d.claim(cid) for cid in b.claims) if c is not None]
    return out


def _support_sources(world: _World, entry: Entry) -> list[tuple[str, str]]:
    """Every source backing an entry: its counted `supports` claims and its own history sources."""
    sources = _as_sources(_check_claims(_counted(world, entry, "supports")), world)
    for h in entry.history:
        sources += [(c, q) for c, q in h.sources if c in world.by_call]
    return _dedupe_sources(sources, world)


def _drop_due(world: _World, entry: Entry) -> bool:
    """A Watching item with no belief check in any call from the last `drop_watching_weeks` weeks,
    watched (or last reworded) at least that long ago."""
    cutoff = (world.today - timedelta(weeks=int(world.t("drop_watching_weeks")))).isoformat()
    since = _version_date(entry)
    if entry.section != "watching" or not since or since > cutoff:
        return False
    return not any(d.date and d.date > cutoff and any(b.target == entry.id for b in d.belief_checks)
                   for d in world.digests)


def _fresh(world: _World, sources: list[tuple[str, str]],
           spans: list[tuple[int, int] | None] | None = None) -> list[tuple[str, str]]:
    """Sources not already in the history of a current Belief or Watching item (apply would refuse
    them), compared as nua/apply.py compares them: by span overlap with each held source where
    nua.apply.held_spans places it, else by quote overlap. `spans` gives each source's span when the
    claim is known (a single-call finding); otherwise it is world.span_of."""
    out = []
    for n, s in enumerate(sources):
        span = spans[n] if spans is not None else world.span_of(s)
        if not any((span_overlap(span, hs) if span and hs else quote_overlap(s[1], q)) >= SAME_SOURCE
                   for q, hs in world.held(s[0])):
            out.append(s)
    return out


# ---------- themes ----------

def _stored_themes(world: _World, stored: list[dict]) -> list[tuple[str, list[tuple[Digest, Claim]]]]:
    """themes.yml entries resolved to current findings. Claims that no longer resolve, or are no
    longer findings, fall out."""
    out = []
    for t in stored or []:
        name = clean_text(t.get("name")) if isinstance(t, dict) else ""
        if not name:
            continue
        members = []
        for c in t.get("claims") or []:
            pair = _source_pair(c)
            hit = _resolve_quote(world, *pair) if pair else None
            if hit and _key(*hit) in world.finding_keys and hit not in members:
                members.append(hit)
        out.append((name, members))
    return out


def _merge_themes(world: _World, previous: list[tuple[str, list[tuple[Digest, Claim]]]],
                  model_themes: list[ThemeReply]) -> list[tuple[str, list[tuple[Digest, Claim]]]]:
    """Previous themes plus the model's changes. A finding stays in its previous theme unless the
    model lists it under another; `renamed_from` renames (or folds into an existing name). Each
    finding sits in one theme; empty themes are dropped."""
    names: list[str] = []
    members: dict[str, list[tuple[str, str]]] = {}
    owner: dict[tuple[str, str], str] = {}
    lookup: dict[tuple[str, str], tuple[Digest, Claim]] = {}

    def ensure(name: str) -> str:
        low = name.lower()
        if low not in members:
            names.append(name)
            members[low] = []
        return low

    def place(k: tuple[str, str], low: str) -> None:
        old = owner.get(k)
        if old == low:
            return
        if old is not None:
            members[old].remove(k)
        owner[k] = low
        members[low].append(k)

    for name, ms in previous:
        low = ensure(name)
        for d, c in ms:
            k = _key(d, c)
            lookup[k] = (d, c)
            if k not in owner:
                place(k, low)

    for t in model_themes:
        name = clean_text(t.name)
        if not name:
            continue
        old = clean_text(t.renamed_from).lower()
        if old and old in members and old != name.lower():
            if name.lower() in members:
                for k in list(members[old]):
                    place(k, name.lower())
                names = [n for n in names if n.lower() != old]
                del members[old]
            else:
                names = [name if n.lower() == old else n for n in names]
                members[name.lower()] = members.pop(old)
                for k in members[name.lower()]:
                    owner[k] = name.lower()
        low = ensure(name)
        for ref in t.claims:
            hit = _resolve_ref(world, ref)
            if hit and _key(*hit) in world.finding_keys:
                lookup[_key(*hit)] = hit
                place(_key(*hit), low)

    out = []
    for name in names:
        ms = sorted((lookup[k] for k in members[name.lower()]),
                    key=lambda dc: (dc[0].call_id, dc[1].offset or (0, 0), dc[1].id or ""))
        if ms:
            out.append((name, ms))
    return out


def _themes_yaml(themes: list[tuple[str, list[tuple[Digest, Claim]]]]) -> list[dict]:
    return [{"name": name, "claims": [{"call_id": d.call_id, "quote": c.quote} for d, c in ms]}
            for name, ms in themes]


# ---------- choosing proposals ----------

@dataclass
class _Draft:
    """A proposal before the checks: from the model, from code, or carried from last week."""
    type: str
    statement: str | None = None
    scope: Scope = field(default_factory=Scope)
    statements: list[dict] = field(default_factory=list)
    target: str | None = None
    targets: list[str] = field(default_factory=list)
    refs: list[str] = field(default_factory=list)
    sources: list[tuple[str, str]] = field(default_factory=list)
    theme: str | None = None
    rationale: str = ""
    id: str | None = None
    target_version: str | None = None      # carried proposals: the version they were written against


def _entry_id(value: Any) -> str | None:
    m = _ENTRY_ID.search(str(value or ""))
    return f"{m.group(1).upper()}-{int(m.group(2)):03d}" if m else None


def _draft_from_model(raw: ProposalReply) -> _Draft:
    statements = []
    for s in raw.statements:
        text = clean_text(s.statement)
        if text:
            statements.append({"statement": text, "scope": _scope_dict(_clean_scope(s.scope.model_dump()))})
    return _Draft(type=raw.type.strip().lower(),
                  statement=clean_text(raw.statement) or None,
                  scope=_clean_scope(raw.scope.model_dump()), statements=statements,
                  target=_entry_id(raw.target),
                  targets=[t for t in (_entry_id(x) for x in raw.targets) if t],
                  refs=list(raw.sources),
                  theme=clean_text(raw.theme) or None, rationale=clean_text(raw.rationale))


def _draft_from_proposal(p: Proposal) -> _Draft:
    return _Draft(type=p.type, statement=p.statement, scope=p.scope, statements=list(p.statements),
                  target=p.target, targets=list(p.targets), sources=list(p.sources), rationale=p.rationale, id=p.id,
                  target_version=p.target_version)


def _mechanical(world: _World) -> list[_Draft]:
    """Proposals that need no judgment, checked like any other: retire any entry, promote or drop a Watching item."""
    out = []
    for e in world.beliefs.current():
        out.append(_Draft(type="retire", target=e.id))
        if e.section == "watching":
            out += [_Draft(type="promote", target=e.id), _Draft(type="drop", target=e.id)]
    return out


def _counts_line(calls: int, seats: int) -> str:
    return f"{calls} call{'' if calls == 1 else 's'} in {seats} seat{'' if seats == 1 else 's'}"


def _validate(world: _World, dr: _Draft, themes: dict[str, list[tuple[Digest, Claim]]], *,
              bootstrap: bool) -> Proposal | None:
    """Apply the SPEC thresholds to a draft. Returns the checked proposal, or None when it falls short."""
    kind = dr.type
    if kind not in TYPES:
        return None
    cited = _dedupe_sources(_as_sources((h for h in (_resolve_ref(world, r) for r in dr.refs) if h), world)
                            + [s for s in dr.sources if s[0] in world.by_call], world)
    add_bar = (int(world.t("add_calls")), int(world.t("add_seats")))

    if kind in ("add", "watch"):
        if not dr.statement:
            return None
        sources = cited + (_as_sources(themes.get(dr.theme.lower(), []), world) if dr.theme else [])
        sources = _fresh(world, _dedupe_sources(sources, world))
        calls, seats = _calls_seats(world, sources)
        if kind == "add" and (calls < add_bar[0] or seats < add_bar[1]):
            if not bootstrap:
                return None
            kind = "watch"
        if calls < 1:
            return None
        return Proposal(id=dr.id or "", type=kind, statement=dr.statement, scope=dr.scope, sources=sources,
                        rationale=dr.rationale or f"Recurs in {_counts_line(calls, seats)}.")

    if kind == "merge":
        entries = []
        for i in dict.fromkeys(dr.targets + ([dr.target] if dr.target else [])):
            e = world.beliefs.get(i)
            if e is None or not e.is_current:
                return None
            entries.append(e)
        if len(entries) < 2 or not dr.statement:
            return None
        if dr.target_version is not None and dr.target_version != entries[0].version:
            return None
        evidence = [_support_sources(world, e) for e in entries]
        for other in evidence[1:]:
            if not evidence[0] or not other:
                return None
            shared = sum(1 for s in other if any(_same_source(s, b, world) for b in evidence[0]))
            if shared / min(len(evidence[0]), len(other)) < MERGE_OVERLAP:
                return None
        sources = _dedupe_sources((s for ev in evidence for s in ev), world)
        return Proposal(id=dr.id or "", type="merge", statement=dr.statement, scope=dr.scope, sources=sources,
                        targets=[e.id for e in entries], target_version=entries[0].version,
                        rationale=dr.rationale or "Their supporting claims mostly overlap.")

    entry = world.beliefs.get(dr.target) if dr.target else None
    if entry is None or not entry.is_current:
        return None
    if dr.target_version is not None and dr.target_version != entry.version:
        return None                        # carried from last week, but the entry changed since
    statement, scope, rationale = entry.statement, entry.scope, dr.rationale
    statements: list[dict] = []

    if kind == "promote":
        if entry.section != "watching":
            return None
        sources = _support_sources(world, entry)
        calls, seats = _calls_seats(world, sources)
        if calls < add_bar[0] or seats < add_bar[1]:
            return None
        statement = dr.statement or entry.statement
        scope = dr.scope if not dr.scope.is_empty() else entry.scope
        rationale = rationale or f"Supported by {_counts_line(calls, seats)}."
    elif kind == "drop":
        if not _drop_due(world, entry):
            return None
        sources = []
        rationale = rationale or f"No hit in {world.t('drop_watching_weeks')} weeks."
    elif kind == "retire":
        pairs = _counted(world, entry, "contradicts", keep_window=True)
        sources = _as_sources(_check_claims(pairs), world)
        calls = len({d.call_id for d, _ in pairs})
        seats = len({d.seat for d, _ in pairs if d.seat})
        if calls < int(world.t("retire_calls")) or seats < int(world.t("retire_seats")):
            return None
        rationale = rationale or f"Contradicted by {_counts_line(calls, seats)}."
    elif kind == "split":
        if len(dr.statements) < 2:
            return None
        mismatch = _counted(world, entry, "scope_mismatch")
        contra = _counted(world, entry, "contradicts", keep_window=True)
        mismatch_calls = len({d.call_id for d, _ in mismatch})
        one_context = (len({d.call_id for d, _ in contra}) >= int(world.t("under_pressure_calls"))
                       and len({d.seat for d, _ in contra}) == 1)
        if mismatch_calls < int(world.t("split_scope_mismatch")) and not one_context:
            return None
        sources = _as_sources(_check_claims(mismatch + contra), world)
        statements = dr.statements
        rationale = rationale or (f"{mismatch_calls} scope mismatches." if not one_context
                                  else "Every contradiction comes from one seat.")
    elif kind == "reword":
        if not dr.statement or normalize(dr.statement) == normalize(entry.statement) or not cited:
            return None
        sources, statement = cited, dr.statement
        scope = dr.scope if not dr.scope.is_empty() else entry.scope
    else:
        return None
    return Proposal(id=dr.id or "", type=kind, statement=statement, scope=scope, sources=sources,
                    target=entry.id, target_version=entry.version, rationale=rationale, statements=statements)


def _same_proposal(a: Proposal, b: Proposal, world: "_World | None" = None) -> bool:
    """Two proposals for the same change: same target (or target set), or for new entries a shared source."""
    new = ("add", "watch")
    if a.type in new and b.type in new:
        return (normalize(a.statement or "") == normalize(b.statement or "")
                or any(_same_source(s, t, world) for s in a.sources for t in b.sources))
    if a.type != b.type:
        return False
    if a.type == "merge":
        return set(a.targets) == set(b.targets)
    return a.target == b.target


def _select(world: _World, drafts: list[_Draft], themes: dict[str, list[tuple[Digest, Claim]]], *,
            bootstrap: bool, prefix: str, taken_ids: set[str]) -> list[Proposal]:
    """Check, filter, dedupe, rank, cap and number the drafts (earlier drafts win a tie)."""
    kept: list[Proposal] = []
    for dr in drafts:
        p = _validate(world, dr, themes, bootstrap=bootstrap)
        if (p is None or rejection_suppressed(p, world.rejected, world)
                or any(_same_proposal(p, q, world) for q in kept)):
            continue
        kept.append(p)
    order = {t: i for i, t in enumerate(TYPES)}
    kept.sort(key=lambda p: (order[p.type], *(-n for n in _calls_seats(world, p.sources))))
    if bootstrap:
        kept = kept[:BOOTSTRAP_CAP]
    taken = set(taken_ids) | {p.id for p in kept if p.id}
    n = 0
    for p in kept:
        if p.id:
            continue
        n += 1
        while f"{prefix}-{n:02d}" in taken:
            n += 1
        p.id = f"{prefix}-{n:02d}"
        taken.add(p.id)
    return kept


# ---------- the model call ----------

class ThemeReply(Reply):
    name: Text = ""
    renamed_from: OptText = None
    claims: Texts = []              # refs: <call_id>#<claim id>


class StatementReply(Reply):
    statement: Text = ""
    scope: Mapping[ScopeReply] = Field(default_factory=ScopeReply)


class ProposalReply(Reply):
    type: Annotated[Text, WithJsonSchema({"type": "string", "enum": list(TYPES)})] = ""
    statement: OptText = None
    scope: Mapping[ScopeReply] = Field(default_factory=ScopeReply)
    statements: Objects[StatementReply] = []
    sources: Texts = []             # refs: <call_id>#<claim id>
    target: OptText = None
    targets: Texts = []
    theme: OptText = None
    rationale: Text = ""


class RollupReply(Reply):
    themes: Objects[ThemeReply] = []
    proposals: Objects[ProposalReply] = []


ROLLUP_SCHEMA = strict_schema(RollupReply)


def _one_line(text: str | None) -> str:
    return " ".join((text or "").split())


def _flags(c: Claim) -> str:
    words = [w for w, on in (("firsthand", c.flags.firsthand), ("hedged", c.flags.hedged),
                             ("prompted", c.flags.prompted)) if on]
    if not c.flags.prompted:
        words.append("unprompted")
    if c.found_by >= 2:
        words.append("found by 2 models")
    return ", ".join(words) or "no flags"


def _quotes_text(c: Claim) -> str:
    """`Quote: "..."`, or `Quotes: "..."; "..."` for a finding with several."""
    quoted = "; ".join(f'"{_one_line(q)}"' for q in c.quote_texts) or '""'
    return f"{'Quotes' if len(c.quote_texts) > 1 else 'Quote'}: {quoted}"


def _finding_line(d: Digest, c: Claim, new_calls: set[str]) -> str:
    new = " NEW" if d.call_id in new_calls else ""
    return (f"[{_ref(d, c)}]{new} {d.seat or 'seat unknown'}; {d.date or 'undated'}; {c.speaker or 'speaker unknown'}; "
            f"{_flags(c)}. {_one_line(c.text)} {_quotes_text(c)}")


def _findings_text(world: _World, themes: list[tuple[str, list[tuple[Digest, Claim]]]], new_calls: set[str]) -> str:
    if not world.findings:
        return "(no findings)"
    grouped = {_key(d, c) for _, ms in themes for d, c in ms}
    blocks = []
    for name, ms in themes:
        n_calls = len({d.call_id for d, _ in ms})
        lines = [f"#### Theme: {name} ({len(ms)} findings, {n_calls} calls)"]
        blocks.append("\n".join(lines + [_finding_line(d, c, new_calls) for d, c in ms]))
    loose = [(d, c) for d, c in world.findings if _key(d, c) not in grouped]
    if loose:
        blocks.append("\n".join(["#### In no theme"] + [_finding_line(d, c, new_calls) for d, c in loose]))
    return "\n\n".join(blocks)


def _evidence_summary(world: _World, e: Entry) -> str:
    parts = []
    for verdict in ("supports", "contradicts", "scope_mismatch"):
        pairs = _counted(world, e, verdict, keep_window=verdict == "contradicts")
        calls = {d.call_id for d, _ in pairs}
        seats = {d.seat for d, _ in pairs if d.seat}
        parts.append(f"{verdict.replace('_', ' ')} {len(calls)} calls / {len(seats)} seats")
    hits = [d.date for d in world.digests if d.date and any(b.target == e.id for b in d.belief_checks)]
    last = max(hits) if hits else "never"
    return f"{'; '.join(parts)}; last hit {last}; current wording since {_version_date(e) or 'unknown'}"


def _entries_text(world: _World) -> str:
    current = world.beliefs.current()
    if not current:
        return "(none yet)"
    out = []
    for e in current:
        kind = "belief" if e.section == "beliefs" else "watching"
        scope = format_scope(e.scope) or "none given"
        out.append(f"- {e.id} ({kind}) {_one_line(e.statement)}\n  scope: {scope}\n  evidence: {_evidence_summary(world, e)}")
    return "\n".join(out)


def _decisions_text(world: _World) -> str:
    if not world.decisions:
        return "(none)"
    return "\n".join(f"- {d.id} {_one_line(d.title)}: rests on {', '.join(d.rests_on) or 'nothing listed'}"
                     for d in world.decisions)


def _seat_counts_text(world: _World) -> str:
    if not world.seat_counts:
        return "(no calls yet)"
    lines = [f"- {seat}: {n}" for seat, n in sorted(world.seat_counts.items(), key=lambda x: (-x[1], x[0]))]
    unknown = sum(1 for d in world.digests if not d.seat)
    if unknown:
        lines.append(f"- seat unknown: {unknown}")
    return "\n".join(lines)


def _new_claims_text(world: _World, new_calls: list[Digest]) -> str:
    current = {e.id for e in world.beliefs.current()}
    lines = []
    for d in new_calls:
        for b in d.belief_checks:
            if b.target not in current:
                continue
            for c in (d.claim(cid) for cid in b.claims):
                if c is not None:
                    lines.append(f"[{_ref(d, c)}] {d.seat or 'seat unknown'}; {_one_line(c.text)} "
                                 f"{_quotes_text(c)} -> {b.target} {b.verdict.replace('_', ' ')} "
                                 f"({b.decided_by})")
    return "\n".join(lines) or "(none)"


def _sources_text(sources: list[tuple[str, str]]) -> str:
    return "; ".join(f"{c} \"{_one_line(q)}\"" for c, q in sources) or "none"


def _rejected_text(world: _World) -> str:
    if not world.rejected:
        return "(none)"
    lines = []
    for r in world.rejected:
        claims = [s for s in (_source_pair(c) for c in r.get("claims") or []) if s]
        what = r.get("target") or ", ".join(str(t) for t in r.get("targets") or []) or "(new entry)"
        said = f' "{_one_line(r.get("statement"))}"' if r.get("statement") else ""
        lines.append(f"- {r.get('type')} {what}{said}, rejected by {r.get('who') or 'a teammate'}"
                     f": {_one_line(r.get('reason')) or 'no reason given'}. Evidence: {_sources_text(claims)}")
    return "\n".join(lines)


def _open_text(proposals: list[Proposal]) -> str:
    if not proposals:
        return "(none)"
    lines = []
    for p in proposals:
        target = p.target or ", ".join(p.targets) or "(new entry)"
        lines.append(f"- {p.id} {p.type} {target}: {_one_line(p.statement) or 'no statement'}. "
                     f"Sources: {_sources_text(p.sources)}")
    return "\n".join(lines)


def _ask_model(ctx: Ctx, world: _World, *, bootstrap: bool, week: str, new_calls: list[Digest],
               themes: list[tuple[str, list[tuple[Digest, Claim]]]], carried: list[Proposal]) -> RollupReply:
    """One call to the roll-up model. Raises LLMError (the task is retried)."""
    from nua.prompts import domain_values

    t = world.t
    values = {
        **domain_values(world.config),
        "week": week, "today": world.today.isoformat(), "cap": BOOTSTRAP_CAP,
        "calls_total": len(world.digests), "calls_new": len(new_calls), "findings_total": len(world.findings),
        "entries": _entries_text(world), "decisions": _decisions_text(world),
        "seat_counts": _seat_counts_text(world), "seats": ", ".join(merge_seats(world.config.seats, (d.seat for d in world.digests))) or "(none yet)",
        "findings": _findings_text(world, themes, {d.call_id for d in new_calls}),
        "new_claims": _new_claims_text(world, new_calls), "rejected": _rejected_text(world),
        "open_proposals": _open_text(carried),
        **{k: t(k) for k in ("add_calls", "add_seats", "split_scope_mismatch", "retire_calls", "retire_seats",
                             "drop_watching_weeks", "under_pressure_calls")},
    }
    request = rollup_prompt.BOOTSTRAP if bootstrap else rollup_prompt.WEEKLY
    prompt = fill(request, values) + "\n" + fill(rollup_prompt.OUTPUT, values)
    raw = ctx.llms(ctx.config.pipeline.rollup).complete_json(system=fill(rollup_prompt.SYSTEM, values), prompt=prompt,
                                                             schema=ROLLUP_SCHEMA, max_tokens=MODEL_MAX_TOKENS)
    return RollupReply.model_validate(raw) if isinstance(raw, dict) else RollupReply()


# ---------- what the thread shows ----------

@dataclass
class _Item:
    key: str                  # prop:<id> | pressure:<entry id> | finding:<call_id>:<start>-<end>
    blocks: list[dict]
    text: str


def _show(text: str | None) -> str:
    """Display text: one line, no em dashes (stored quotes stay verbatim; this is Slack only)."""
    return _one_line((text or "").replace(EM_DASH, "-"))


def _esc(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _fit(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 3].rstrip() + "..."


def _section(block_id: str, lines: list[str]) -> dict:
    return {"type": "section", "block_id": block_id,
            "text": {"type": "mrkdwn", "text": _fit("\n".join(lines), MAX_TEXT)}}


def _button(text: str, action_id: str, value: str, style: str | None = None) -> dict:
    b = {"type": "button", "action_id": action_id, "text": {"type": "plain_text", "text": text}, "value": value}
    if style:
        b["style"] = style
    return b


def _quote_lines(world: _World, sources: list[tuple[str, str]], extra: dict[str, str] | None = None
                 ) -> tuple[list[str], list[str]]:
    """Evidence quotes as mrkdwn and plain lines, at most QUOTES_SHOWN."""
    md, plain = [], []
    for call_id, quote in sources[:QUOTES_SHOWN]:
        d = world.by_call.get(call_id)
        where = SEP.join(p for p in (call_id, d.seat if d else None, (extra or {}).get(call_id)) if p)
        q = _fit(_show(quote), QUOTE_CHARS)
        md.append(f'> "{_esc(q)}"  _({_esc(where)})_')
        plain.append(f'  "{q}" ({where})')
    more = len(sources) - QUOTES_SHOWN
    if more > 0:
        md.append(f"_and {more} more in config/proposals.yml_")
        plain.append(f"  and {more} more")
    return md, plain


def _title(p: Proposal) -> str:
    return {"add": "Add belief", "watch": "Watch", "promote": f"Promote {p.target} to a belief",
            "split": f"Split {p.target}", "retire": f"Retire {p.target}", "reword": f"Reword {p.target}",
            "merge": f"Merge {', '.join(p.targets)}", "drop": f"Drop {p.target}"}.get(p.type, p.type)


def _proposal_item(world: _World, p: Proposal) -> _Item:
    title = _title(p)
    md = [f"*{_esc(title)}*  `{p.id}`"]
    plain = [f"PROPOSAL {p.id}: {title}"]
    entry = world.beliefs.get(p.target) if p.target else None
    if p.type == "reword" and entry:
        md.append(f"Now: {_esc(_show(entry.statement))}")
        plain.append(f"Now: {_show(entry.statement)}")
        md.append(f"Proposed: *{_esc(_show(p.statement))}*")
        plain.append(f"Proposed: {_show(p.statement)}")
    elif p.type == "split":
        if entry:
            md.append(f"Now: {_esc(_show(entry.statement))}")
            plain.append(f"Now: {_show(entry.statement)}")
        for s in p.statements:
            scope = format_scope(Scope.from_dict(s.get("scope")))
            tail = f" ({scope})" if scope else ""
            md.append(f"- *{_esc(_show(s['statement']))}*{_esc(tail)}")
            plain.append(f"- {_show(s['statement'])}{tail}")
    elif p.statement:
        md.append(f"*{_esc(_show(p.statement))}*")
        plain.append(f'"{_show(p.statement)}"')
    scope = format_scope(p.scope)
    if scope and p.type != "split":
        md.append(f"Scope: {_esc(scope)}")
        plain.append(f"Scope: {scope}")
    if p.sources:
        calls, seats = _calls_seats(world, p.sources)
        md.append(f"Evidence: {_counts_line(calls, seats)}")
        plain.append(f"Evidence: {_counts_line(calls, seats)}")
        q_md, q_plain = _quote_lines(world, p.sources)
        md += q_md
        plain += q_plain
    if p.rationale:
        md.append(f"_{_esc(_show(p.rationale))}_")
        plain.append(f"Why: {_show(p.rationale)}")
    key = f"prop:{p.id}"
    buttons = [_button("Approve", "prop_approve", p.id, "primary"), _button("Edit", "prop_edit", p.id),
               _button("Reject", "prop_reject", p.id, "danger")]
    blocks = [_section(f"item:{key}", md), {"type": "actions", "block_id": key, "elements": buttons}]
    return _Item(key=key, blocks=blocks, text="\n".join(plain + ["[Approve | Edit | Reject]"]))


def _pressure_item(world: _World, entry: Entry) -> _Item:
    pairs = _counted(world, entry, "contradicts", keep_window=True)
    claims = _check_claims(pairs)
    decided = {d.call_id: f"[{'2/2 agree' if b.decided_by == 'agree' else b.decided_by}]" for d, b in pairs}
    calls = len({d.call_id for d, _ in pairs})
    seats = len({d.seat for d, _ in pairs if d.seat})
    md = [f"*Under pressure: {entry.id}* {_esc(_show(entry.statement))}",
          f"Contradicted by {_counts_line(calls, seats)}:"]
    plain = [f"UNDER PRESSURE {entry.id}: {_show(entry.statement)}", f"Contradicted by {_counts_line(calls, seats)}:"]
    q_md, q_plain = _quote_lines(world, _as_sources(claims, world), decided)
    key = f"pressure:{entry.id}"
    buttons = [_button("Keep", "pressure_keep", entry.id), _button("Reword", "pressure_reword", entry.id),
               _button("Split", "pressure_split", entry.id), _button("Retire", "pressure_retire", entry.id, "danger")]
    blocks = [_section(f"item:{key}", md + q_md), {"type": "actions", "block_id": key, "elements": buttons}]
    return _Item(key=key, blocks=blocks, text="\n".join(plain + q_plain + ["[Keep | Reword | Split | Retire]"]))


def _finding_key(d: Digest, c: Claim) -> str:
    """The item key of a single-call finding: finding:<call_id>:<start>-<end> of its first quote (the
    finding id without a span)."""
    span = f"{c.offset[0]}-{c.offset[1]}" if c.offset else (c.id or "")
    return f"finding:{d.call_id}:{span}"


def _finding_item(d: Digest, c: Claim, reasons: list[str]) -> _Item:
    from nua.brief import _finding_parts   # the brief's block, so nua/interact.py reads both the same way
    key = _finding_key(d, c)
    meta = SEP.join(p for p in (d.call_id, d.seat, ", ".join(reasons)) if p)
    blocks, plain = _finding_parts(d, c, False)
    head = {"type": "context", "block_id": f"item:{key}",
            "elements": [{"type": "mrkdwn", "text": f"*Single-call finding*{SEP}{_esc(_show(meta))}"}]}
    blocks = [head] + [_scrub(b) for b in blocks]
    return _Item(key=key, blocks=blocks, text=f"SINGLE-CALL FINDING ({_show(meta)})\n{plain.replace(EM_DASH, '-')}")


def _scrub(value: Any) -> Any:
    """No em dashes anywhere in a block structure."""
    if isinstance(value, str):
        return value.replace(EM_DASH, "-")
    if isinstance(value, list):
        return [_scrub(v) for v in value]
    if isinstance(value, dict):
        return {k: _scrub(v) for k, v in value.items()}
    return value


def _reasons(world: _World, d: Digest, c: Claim) -> list[str]:
    """Why a single-call finding is worth a look: firsthand, unprompted, from a scarce seat."""
    scarce = int(world.t("scarce_seat_max_calls"))
    return [r for r, on in (("firsthand", c.flags.firsthand), ("unprompted", not c.flags.prompted),
                            ("scarce seat", bool(d.seat) and world.seat_counts[d.seat] <= scarce)) if on]


def _single_findings(world: _World, new_calls: list[Digest], themes: list[tuple[str, list[tuple[Digest, Claim]]]],
                     proposals: list[Proposal], previous: Iterable[str] = ()) -> list[tuple[Digest, Claim, list[str]]]:
    """Single-call findings to post: findings from new calls, plus the previous roll-up's single-call
    findings (`previous`, their item keys) that are still unanswered, since unanswered items roll into
    the new roll-up. Each must lie outside any multi-call theme, any proposal and any current entry's
    history, and be firsthand, unprompted or from a scarce seat. Best first, at most `single_call_findings`."""
    new_ids = {d.call_id for d in new_calls}
    carried = set(previous or ())
    multi = {_key(d, c) for _, ms in themes if len({d.call_id for d, _ in ms}) >= 2 for d, c in ms}
    cited = [s for p in proposals for s in p.sources]
    picks = []
    for d, c in world.findings:
        if d.call_id not in new_ids and _finding_key(d, c) not in carried:
            continue
        own = [((d.call_id, q.quote), q.span) for q in c.quotes if q.quote]
        if (_key(d, c) in multi or any(_same_source(source, s, world) for source, _ in own for s in cited)
                or len(_fresh(world, [o for o, _ in own], [sp for _, sp in own])) < len(own)):
            continue
        reasons = _reasons(world, d, c)
        if reasons:
            picks.append((d, c, reasons))
    picks.sort(key=lambda x: (-len(x[2]), -x[1].found_by, x[0].call_id, x[1].offset or (0, 0)))
    return picks[: int(world.t("single_call_findings"))]


def _recorded_singles(world: _World, keys: list[str]) -> list[tuple[Digest, Claim, list[str]]]:
    """The single-call findings a roll-up recorded (a retry posts the same ones), still findings."""
    by_key = {_finding_key(d, c): (d, c) for d, c in world.findings}
    return [(d, c, _reasons(world, d, c)) for d, c in (by_key.get(str(k)) for k in keys) if d is not None]


def _pressure(world: _World, digests: list[Digest] | None = None) -> list[Entry]:
    """Beliefs under pressure (Watching items never are), by nua.indexes' rule."""
    ix = _indexes()
    ds = world.digests if digests is None else digests
    return [e for e in world.beliefs.current()
            if e.section == "beliefs" and ix.under_pressure(e, ds, world.overrides, world.config)]


def _plural(n: int, word: str) -> str:
    return f"{n} {word}" if n == 1 else f"{n} {word}s"


def _summary(world: _World, *, key: str, bootstrap: bool, new_calls: list[Digest], pressure: list[Entry],
             proposals: list[Proposal], singles: int, themes: int) -> tuple[list[dict], str]:
    lines: list[str] = []
    if bootstrap:
        lines.append("*Bootstrap roll-up*")
        lines.append(f"Read {_plural(len(world.digests), 'call')}: {_plural(len(world.findings), 'finding')} "
                     f"in {_plural(themes, 'theme')}.")
        beliefs = sum(1 for p in proposals if p.type == "add")
        watching = sum(1 for p in proposals if p.type == "watch")
        entries = "1 entry" if len(proposals) == 1 else f"{len(proposals)} entries"
        lines.append(f"Proposed {entries}: "
                     f"{_plural(beliefs, 'belief')}, {_plural(watching, 'Watching item')}. "
                     "Approve, edit or reject each one in the thread. Approvals share one backcheck.")
    else:
        new_ids = {d.call_id for d in new_calls}
        lines.append(f"*Roll-up {key.split(':', 1)[1]}*")
        contra = Counter()
        gained = []
        for e in world.beliefs.current():
            n = len({d.call_id for d, _ in _counted(world, e, "contradicts", calls=new_ids)})
            if n:
                contra[e.id] = n
            if e.section == "watching" and _counted(world, e, "supports", calls=new_ids):
                gained.append(e.id)
        week = [f"This week: {_plural(len(new_calls), 'call')}"]
        if contra:
            total = sum(contra.values())
            week.append(f"{_plural(total, 'contradiction')} ({', '.join(contra)})")
        else:
            week.append("no contradictions")
        text = ", ".join(week) + "."
        old = [d for d in world.digests if d.call_id not in new_ids]
        was = {e.id for e in _pressure(world, old)}
        went = [e.id for e in pressure if e.id not in was]
        if went:
            text += f" {', '.join(went)} went under pressure."
        if gained:
            text += f" {', '.join(gained)} gained evidence."
        lines.append(text)
    n_beliefs = sum(1 for e in world.beliefs.current() if e.section == "beliefs")
    cap = int(world.t("max_beliefs"))
    if n_beliefs > cap:
        lines.append(f"{n_beliefs} beliefs, over the cap of {cap}. Retire or merge before adding more.")
    parts = [p for p in ((_plural(len(pressure), "belief") + " under pressure") if pressure else "",
                         _plural(len(proposals), "proposal") if proposals else "",
                         _plural(singles, "single-call finding") if singles else "") if p]
    if not bootstrap:
        lines.append(f"In the thread: {', '.join(parts)}." if parts else "Nothing needs a decision this week.")
    plain = [_show(line.replace("*", "")) for line in lines]
    return [_section("rollup:summary", [_esc(_show(line)) for line in lines])], "\n".join(plain)


# ---------- Slack ----------

def _post(ctx: Ctx, key: str, summary: tuple[list[dict], str], items: list[_Item]) -> tuple[str, int, int, bool]:
    """Post the summary (unless one with this key exists) and every item not yet in its thread.
    Returns (summary ts, items posted, items already there, whether the summary was already there)."""
    ch = ctx.channel("calls")
    ts = ctx.slack.find_by_metadata(ch, EVENT_TYPE, key)
    posted_keys: set[str] = set()
    existed = ts is not None
    if ts is None:
        ts = ctx.slack.post(ch, summary[0], summary[1], metadata={"event_type": EVENT_TYPE,
                                                                  "event_payload": {"key": key}})
    else:
        for reply in ctx.slack.replies(ch, ts):
            for b in reply.get("blocks") or []:
                bid = str(b.get("block_id") or "")
                if bid.startswith("item:"):
                    posted_keys.add(bid[len("item:"):])
    posted = already = 0
    for item in items:
        if item.key in posted_keys:
            already += 1
            continue
        ctx.slack.post(ch, item.blocks, item.text, thread_ts=ts)
        posted += 1
    return ts, posted, already, existed


def _interactive(block: dict) -> bool:
    return block.get("type") == "actions" or (block.get("accessory") or {}).get("type", "image") != "image"


def _clear_previous(ctx: Ctx, previous_key: str | None, key: str, rolled: Iterable[str] = ()) -> int:
    """Remove the buttons and menus left on the previous roll-up's thread replies, each marked "No
    answer. Rolled into <this roll-up>." when this roll-up posts the same item again (its key is in
    `rolled`), else "No answer. Expired.". Returns replies changed."""
    rolled = set(rolled or ())
    if not previous_key or previous_key == key:
        return 0
    ch = ctx.channel("calls")
    ts = ctx.slack.find_by_metadata(ch, EVENT_TYPE, previous_key)
    if ts is None:
        return 0
    label = "the bootstrap roll-up" if key == BOOTSTRAP_KEY else f"the {key.split(':', 1)[1]} roll-up"
    changed = 0
    for reply in ctx.slack.replies(ch, ts):
        blocks = reply.get("blocks") or []
        if not reply.get("bot", True) or not any(_interactive(b) for b in blocks):
            continue
        kept = []
        for b in blocks:
            if b.get("type") == "actions":
                continue
            if "accessory" in b and _interactive(b):
                b = {k: v for k, v in b.items() if k != "accessory"}
            kept.append(b)
        item = next((str(b["block_id"])[len("item:"):] for b in blocks
                     if str(b.get("block_id") or "").startswith("item:")), None)
        note = f"No answer. Rolled into {label}." if item in rolled else "No answer. Expired."
        kept.append({"type": "context", "block_id": "rolled", "elements": [{"type": "mrkdwn", "text": note}]})
        ctx.slack.update(ch, reply["ts"], kept, reply.get("text") or "")
        changed += 1
    return changed


# ---------- entry point ----------

def week_key(day: date) -> str:
    """ISO week label, e.g. date(2026, 10, 1) -> "2026-W40"."""
    y, w, _ = day.isocalendar()
    return f"{y}-W{w:02d}"


def _week_before(key: str) -> str:
    """"rollup:2026-W41" -> "rollup:2026-W40"."""
    year, week = key.split(":", 1)[1].split("-W")
    return f"rollup:{week_key(date.fromisocalendar(int(year), int(week), 1) - timedelta(days=7))}"


def _new_calls(world: _World, state: dict, *, bootstrap: bool) -> list[Digest]:
    """Calls no roll-up has counted yet: every digested call missing from the last roll-up's `counted`,
    whatever its date (a call digested late still counts once). A roll-up recorded before `counted`
    existed falls back to its date: calls dated on or after it and not counted by it. The first roll-up
    counts every call."""
    if bootstrap or not state.get("key"):
        return list(world.digests)
    if isinstance(state.get("counted"), list):
        counted = {str(c) for c in state["counted"]}
        return [d for d in world.digests if d.call_id not in counted]
    since = str(state.get("date") or "")
    seen = set(state.get("calls") or [])
    return [d for d in world.digests if d.date and d.date >= since and d.call_id not in seen]


def _counted_calls(world: _World, state: dict, *, bootstrap: bool) -> list[str]:
    """The `counted` list to record: what earlier roll-ups counted plus every call digested now, since
    each is either counted already or new in this roll-up. The bootstrap starts it over."""
    before = [] if bootstrap else list(state.get("counted") or [])
    return sorted({str(c) for c in before} | {d.call_id for d in world.digests})


def _record(world: _World, p: Proposal, key: str) -> dict:
    """The proposals.yml record: the proposal, the roll-up that posted it, and for a merge the
    version of every target (nua/apply.py marks the proposal stale when any of them moves)."""
    r = {**proposal_to_dict(p), "rollup": key}
    if p.type == "merge":
        r["target_versions"] = {t: world.beliefs.get(t).version for t in p.targets if world.beliefs.get(t)}
    if key == BOOTSTRAP_KEY:
        r["bootstrap"] = True
    return r


def _write_files(ctx: Ctx, world: _World, *, key: str, previous: str | None, new_calls: list[Digest],
                 themes: list[tuple[str, list[tuple[Digest, Claim]]]], proposals: list[Proposal],
                 old_records: list[dict], counted: list[str], singles: list[str]) -> None:
    """Write config/themes.yml and config/proposals.yml. The previous roll-up's records not carried
    forward stay one more week (unanswered ones marked `expired`); older records are dropped."""
    ids = {p.id for p in proposals}
    kept_old = []
    for r in old_records:
        if r.get("rollup") != previous or r.get("id") in ids:
            continue
        r = dict(r)
        if str(r.get("status") or "open") == "open":
            r["status"] = "expired"
        kept_old.append(r)
    today = world.today.isoformat()
    themes_doc = {"updated": today,
                  "rollup": {"key": key, "date": today, "previous": previous,
                             "calls": [d.call_id for d in new_calls], "counted": counted, "findings": singles},
                  "themes": _themes_yaml(themes)}
    proposals_doc = {"rollup": key, "proposals": [_record(world, p, key) for p in proposals] + kept_old}
    ctx.repo.write(THEMES_PATH, HEADER + frontmatter.dump_yaml(themes_doc))
    ctx.repo.write(PROPOSALS_PATH, HEADER + frontmatter.dump_yaml(proposals_doc))


def run_rollup(ctx: Ctx, *, bootstrap: bool = False, week: str | None = None) -> dict:
    """Run the weekly roll-up (or the bootstrap) end to end. See the module docstring.

    `week` is an ISO week like "2026-W41" (default: this week in ctx.tz). Returns a report:
    {"key", "retry", "calls", "themes", "proposals": [ids], "pressure": [entry ids], "items": [item keys],
    "posted", "already_posted", "skipped", "cleared", "summary_ts", "commit", "text"}.
    Raises LLMError when the model call fails (nothing is written or posted then).
    """
    today = ctx.now().astimezone(ctx.tz).date()
    if bootstrap:
        key = BOOTSTRAP_KEY
    else:
        m = _WEEK.fullmatch((week or week_key(today)).strip())
        if not m:
            raise ValueError(f"week must look like 2026-W41, got {week!r}")
        key = f"rollup:{m.group(1)}"
    world = _load_world(ctx, today)
    themes_file = _read_yaml(ctx, THEMES_PATH)
    state = themes_file.get("rollup") if isinstance(themes_file.get("rollup"), dict) else {}
    records = [r for r in _read_yaml(ctx, PROPOSALS_PATH).get("proposals") or [] if isinstance(r, dict)]
    stored_themes = _stored_themes(world, themes_file.get("themes") or [])
    retry = state.get("key") == key
    commit = None

    if retry:
        previous = state.get("previous")
        new_calls = [world.by_call[c] for c in state.get("calls") or [] if c in world.by_call]
        themes = [(name, ms) for name, ms in stored_themes if ms]
        proposals = [proposal_from_dict(r) for r in records if r.get("rollup") == key]
        if bootstrap:
            singles = []
        elif isinstance(state.get("findings"), list):
            singles = _recorded_singles(world, state["findings"])
        else:                          # recorded before `findings` existed
            singles = _single_findings(world, new_calls, themes, proposals)
        ctx.info(f"rollup {key}: retry, posting missing items only")
    else:
        previous = state.get("key") or (None if bootstrap else _week_before(key))
        new_calls = _new_calls(world, state, bootstrap=bootstrap)
        carried = [proposal_from_dict(r) for r in records
                   if r.get("rollup") == previous and str(r.get("status") or "open") == "open"]
        raw = _ask_model(ctx, world, bootstrap=bootstrap, week=key.split(":", 1)[1], new_calls=new_calls,
                         themes=[(n, ms) for n, ms in stored_themes if ms], carried=carried)
        themes = _merge_themes(world, stored_themes, raw.themes)
        allowed = MODEL_TYPES["bootstrap" if bootstrap else "weekly"]
        # Carried drafts first: _select keeps the earlier of two drafts for the same change, so a
        # carried proposal that still passes the checks keeps its id, record and content (a click
        # queued on last week's item still applies). One that no longer passes gives way to a fresh
        # draft of the same change, which gets a new id.
        drafts = [_draft_from_proposal(p) for p in carried]
        drafts += [d for d in (_draft_from_model(p) for p in raw.proposals) if d.type in allowed]
        if not bootstrap:
            drafts += _mechanical(world)
        prefix = "boot" if bootstrap else key.split(":", 1)[1]
        proposals = _select(world, drafts, {n.lower(): ms for n, ms in themes}, bootstrap=bootstrap,
                            prefix=prefix, taken_ids={str(r.get("id")) for r in records})
        before = state.get("findings") if state.get("key") == previous else None
        singles = [] if bootstrap else _single_findings(world, new_calls, themes, proposals,
                                                        previous=before if isinstance(before, list) else ())
        _write_files(ctx, world, key=key, previous=previous, new_calls=new_calls, themes=themes,
                     proposals=proposals, old_records=records,
                     counted=_counted_calls(world, state, bootstrap=bootstrap),
                     singles=[_finding_key(d, c) for d, c, _ in singles])
        ix = _indexes()
        generated = list(ix.regenerate(ctx) or [])
        if not ctx.dry_run:
            label = "Bootstrap roll-up" if bootstrap else f"Roll-up {key.split(':', 1)[1]}"
            message = f"{label}: {_plural(len(themes), 'theme')}, {_plural(len(proposals), 'proposal')}"
            commit = ctx.remote.commit_and_push(message, [THEMES_PATH, PROPOSALS_PATH, *generated],
                                                regenerate=lambda: ix.regenerate(ctx))

    pressure = [] if bootstrap else _pressure(world)
    summary = _summary(world, key=key, bootstrap=bootstrap, new_calls=new_calls, pressure=pressure,
                       proposals=proposals, singles=len(singles), themes=len(themes))
    items = ([_pressure_item(world, e) for e in pressure] + [_proposal_item(world, p) for p in proposals]
             + [_finding_item(d, c, reasons) for d, c, reasons in singles])

    summary_ts, posted, already, existed, cleared = None, 0, 0, False, 0
    if not ctx.dry_run and ctx.slack is not None:
        summary_ts, posted, already, existed = _post(ctx, key, summary, items)
        cleared = _clear_previous(ctx, previous, key, rolled={item.key for item in items})
    text = "\n\n".join([summary[1]] + [item.text for item in items]) + "\n"
    ctx.info(f"rollup {key}: {len(proposals)} proposals, {len(pressure)} under pressure, posted {posted}")
    return {"key": key, "retry": retry, "calls": len(new_calls), "themes": len(themes),
            "proposals": [p.id for p in proposals], "pressure": [e.id for e in pressure],
            "items": [item.key for item in items], "posted": posted, "already_posted": already,
            "skipped": existed and posted == 0, "cleared": cleared, "summary_ts": summary_ts,
            "commit": commit, "text": text}
