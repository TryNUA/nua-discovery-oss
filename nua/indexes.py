"""Generated index files and belief status (SPEC "Index files", "Under pressure").

Every file written here is a pure function of the digests, `beliefs.md`, `decisions.md`,
`config/themes.yml`, `config/verdict_overrides.yml` and `config/overrides.yml`. No model calls and
no timestamps, so regenerating twice gives the same bytes and a rejected push can rebuild them
after a re-fetch.

Rules shared by every file:
- Only `status: digested` digests count; stubs appear in `calls.csv` only.
- A belief check counts for an entry only when the digest's `checked[entry id]` holds the entry's
  current version. Checks against older wording wait for the backcheck.
- A check dismissed in `config/verdict_overrides.yml` (same call and target, span overlap >= 0.5,
  and the same verdict and entry version when the override records them) is left out of the
  evidence, and a claim whose only checks were dismissed stays a finding.
- Generated text never contains an em dash; quotes get one rewritten as "-" (display only, the
  digests keep the raw text).
"""
from __future__ import annotations

import csv
import io
import re
from dataclasses import dataclass
from typing import Any

import yaml

from nua.apply import SOURCE_OVERLAP, placed_sources
from nua.beliefs import EMPTY_BELIEFS, BeliefsDoc, format_scope, parse_beliefs, parse_decisions
from nua.config import Config
from nua.context import Ctx
from nua.digest import apply_overrides, load_digests
from nua.merge import span_overlap
from nua.ports import RepoFS
from nua.types import SCOPE_KEYS, VERDICTS, VERSION_ACTIONS, BeliefCheck, Claim, Decision, Digest, Entry, iso_date
from nua.verify import normalize, quote_overlap, token_overlap

GENERATED = ["calls.csv", "claims.csv", "findings.md", "beliefs-status.md", "open-questions.md"]

CALLS_COLUMNS = ["call_id", "transcript", "status", "origin", "date", "date_source", "contact", "firm", "seat",
                 "institution_type", "role_in_thesis", "recorded_by", "source", "speaker_labels", "claims",
                 "prompt_version"]
CLAIMS_COLUMNS = ["call_id", "claim_id", "date", "speaker", "attribution", "seat", "line", "party", "segment",
                  "text", "quote", "belief_targets", "found_by"]

BELIEFS_FILE = "beliefs.md"
DECISIONS_FILE = "decisions.md"
THEMES_FILE = "config/themes.yml"
VERDICT_OVERRIDES_FILE = "config/verdict_overrides.yml"

MATCH_THRESHOLD = 0.5                        # quote overlap for dismissals, theme claims and history sources
PRESSURE_DECIDED_BY = ("agree", "arbiter")   # only these contradictions put a belief under pressure
SKIP_UNHEARD_SEATS = ("other",)              # catch-all seat, never worth listing as "not yet heard from"

MANUAL_START = "<!-- manual:start -->"
MANUAL_END = "<!-- manual:end -->"
_MANUAL = re.compile(re.escape(MANUAL_START) + r"(.*?)" + re.escape(MANUAL_END), re.S)

# Keys config/verdict_overrides.yml may keep its list under (the file may also be a bare list).
_OVERRIDE_KEYS = ("overrides", "verdict_overrides", "dismissed")
# Keys config/themes.yml may record the last roll-up under.
_ROLLUP_KEYS = ("updated", "as_of", "week")

EM_DASH = "\u2014"
SEP = " · "
VERDICT_TITLES = (("supports", "For"), ("contradicts", "Against"), ("scope_mismatch", "Scope mismatch"))
DECIDED_LABELS = {"agree": "2/2 agree", "arbiter": "arbiter", "unarbitrated": "unarbitrated", "single": "1 model"}
COVERAGE_WORDS = {"dodged": "dodged", "never_asked": "never asked"}


@dataclass
class EntryStatus:
    """Evidence for one belief or Watching item. Each evidence item is
    {"call_id", "seat", "quote", "date", "decided_by"}, one per cited claim."""
    entry_id: str
    supports: list[dict]
    contradicts: list[dict]
    scope_mismatch: list[dict]
    under_pressure: bool
    checked: int        # digested calls whose `checked` holds the entry's current version
    total: int          # digested calls


# ---------- small helpers ----------

def _clean(text: str) -> str:
    """No em dashes in generated output."""
    return text.replace(EM_DASH, "-")


def _oneline(text: Any) -> str:
    """Collapse whitespace so a value fits on one line. None gives ""."""
    return " ".join(("" if text is None else str(text)).split())


_iso = iso_date   # a date as YYYY-MM-DD text (YAML may hand back a date object)


def _plural(n: int, word: str) -> str:
    return f"{n} {word}" if n == 1 else f"{n} {word}s"


def _newest_first_key(d: Digest) -> tuple[str, str]:
    return (_iso(d.date) or "", d.call_id)


def _load_yaml(repo: RepoFS, rel: str) -> Any:
    text = repo.read_or(rel, "")
    return yaml.safe_load(text) if text.strip() else None


def _with_overrides(digests: list[Digest], config: Config) -> list[Digest]:
    """Digests with `config/overrides.yml` applied. Only overridden digests are copied."""
    fixes = config.overrides or {}
    if not fixes:
        return list(digests)
    return [apply_overrides(d, fixes) if fixes.get(d.call_id) else d for d in digests]


# ---------- loaders ----------

def load_verdict_overrides(repo: RepoFS) -> list[dict]:
    """Dismissed verdicts from `config/verdict_overrides.yml`: [{call_id, target, quote, who, reason, ref}].

    Reads a bare list or a mapping holding the list under `overrides` (also `verdict_overrides` or
    `dismissed`). A missing or empty file gives [].
    """
    data = _load_yaml(repo, VERDICT_OVERRIDES_FILE)
    if isinstance(data, dict):
        data = next((data[k] for k in _OVERRIDE_KEYS if isinstance(data.get(k), list)), [])
    if not isinstance(data, list):
        return []
    return [row for row in data if isinstance(row, dict)]


def load_themes(repo: RepoFS) -> list[dict]:
    """Finding themes from `config/themes.yml` ({"themes": [{"name", "claims": [{"call_id", "quote"}]}]}).

    Themes without a name are skipped. A missing or empty file gives [].
    """
    data = _load_yaml(repo, THEMES_FILE)
    themes = data.get("themes") if isinstance(data, dict) else data
    if not isinstance(themes, list):
        return []
    return [t for t in themes if isinstance(t, dict) and t.get("name")]


def _last_rollup(repo: RepoFS) -> str | None:
    """When the themes were last written by a roll-up (`updated`, `as_of` or `week` in themes.yml)."""
    data = _load_yaml(repo, THEMES_FILE)
    if not isinstance(data, dict):
        return None
    for key in _ROLLUP_KEYS:
        value = _iso(data.get(key))
        if value:
            return value
    return None


# ---------- quote matching ----------

def _span(value: Any) -> tuple[int, int] | None:
    """An offset pair from [start, end] or "start-end"; None when absent or malformed."""
    try:
        if isinstance(value, str):
            start, end = value.split("-", 1)
            return int(start), int(end)
        if isinstance(value, (list, tuple)) and len(value) == 2:
            return int(value[0]), int(value[1])
    except (TypeError, ValueError):
        return None
    return None


def _quote_score(quote: str | None, claim: Claim, span: tuple[int, int] | None = None) -> float:
    """How well a stored quote (and optional offset) points at `claim`: the best over the finding's quotes
    of token overlap, or span overlap when both sides carry offsets, whichever is higher."""
    score = max((token_overlap(quote or "", q) for q in claim.quote_texts), default=0.0)
    if span:
        score = max([score, *(span_overlap(span, own) for own in claim.spans)])
    return score


def _jaccard(a: str, b: str) -> float:
    wa, wb = set(normalize(a).split()), set(normalize(b).split())
    return len(wa & wb) / len(wa | wb) if wa and wb else 0.0


def _best_claim(d: Digest, quote: str | None, span: tuple[int, int] | None = None) -> Claim | None:
    """The claim in `d` a stored (call_id, quote) reference points at, or None below the threshold.

    Ranked by `_quote_score`, ties broken by word-set similarity, so a short quote contained in
    several claims goes to the closest one.
    """
    best: Claim | None = None
    best_key = (0.0, 0.0)
    for c in d.claims:
        score = _quote_score(quote, c, span)
        if score < MATCH_THRESHOLD:
            continue
        key = (score, max((_jaccard(quote or "", q) for q in c.quote_texts), default=0.0))
        if key > best_key:
            best, best_key = c, key
    return best


def _dismissal_covers(o: dict, claim: Claim) -> bool:
    """True when the override points at `claim`: by span overlap when both carry offsets (quote-span
    overlap, SPEC "Under pressure"), else by the longest shared run of words (verify.quote_overlap)."""
    span = _span(o.get("offset") or o.get("span"))
    if span and claim.spans:
        return any(span_overlap(span, own) >= MATCH_THRESHOLD for own in claim.spans)
    return any(quote_overlap(o.get("quote") or "", q) >= MATCH_THRESHOLD for q in claim.quote_texts)


def is_dismissed(check: BeliefCheck, digest: Digest, overrides: list[dict]) -> bool:
    """True when a verdict override covers this check: same call_id and target, and the override's
    span overlaps one of the check's claims by at least 0.5 (quote run overlap when either side has no
    offset). An override that names a verdict only covers that verdict, and one that records the
    entry's version only covers checks made against that version (`digest.checked`), so a dismissal
    does not carry over past a Reword or Split. Matching on the span rather than the claim id lets a
    dismissal survive a re-digest."""
    claims = [c for c in (digest.claim(cid) for cid in check.claims) if c is not None]
    if not claims:
        return False
    for o in overrides:
        if o.get("call_id") != digest.call_id or o.get("target") != check.target:
            continue
        if o.get("verdict") and o.get("verdict") != check.verdict:
            continue
        if o.get("version") and digest.checked.get(check.target) != o.get("version"):
            continue
        if any(_dismissal_covers(o, c) for c in claims):
            return True
    return False


# ---------- belief status ----------

def _checked_current(d: Digest, e: Entry) -> bool:
    """A digested call whose checks on `e` were made against its current version."""
    return d.status == "digested" and e.id in d.checked and d.checked[e.id] == e.version


def _evidence(d: Digest, check: BeliefCheck) -> list[dict]:
    """One evidence item per cited claim (one with an empty quote when no claim resolves)."""
    base = {"call_id": d.call_id, "seat": d.seat, "date": _iso(d.date), "decided_by": check.decided_by}
    claims = [c for c in (d.claim(cid) for cid in check.claims) if c is not None]
    if not claims:
        return [dict(base, quote="")]
    return [dict(base, quote=c.quote) for c in claims]


def _keep_date(e: Entry) -> str | None:
    """Date of the latest Keep after the current version, or None. A Reword or Split after a Keep
    starts a new version, so that Keep no longer applies."""
    last_version = max((i for i, h in enumerate(e.history) if h.action in VERSION_ACTIONS), default=-1)
    keeps = [h.date for h in e.history[last_version + 1:] if h.action == "keep"]
    return max(keeps) if keeps else None


def _pressure_items(e: Entry, contradicts: list[dict]) -> list[dict]:
    """The contradictions that count toward pressure: decided agree or arbiter, and after a Keep
    only those from calls dated after it (undated calls do not count then)."""
    cutoff = _keep_date(e)
    return [i for i in contradicts
            if i["decided_by"] in PRESSURE_DECIDED_BY and (cutoff is None or (i["date"] or "") > cutoff)]


def _pressure_met(items: list[dict], config: Config) -> bool:
    calls = {i["call_id"] for i in items}
    seats = {i["seat"] for i in items if i["seat"]}
    return (len(calls) >= config.pipeline.t("under_pressure_calls")
            and len(seats) >= config.pipeline.t("under_pressure_seats"))


def entry_status(entry: Entry, digests: list[Digest], overrides: list[dict], config: Config) -> EntryStatus:
    """Evidence for and against one entry, whether it is under pressure, and its coverage.

    Counts only `status: digested` digests whose `checked` holds the entry's current version, drops
    dismissed checks, and applies `config.overrides` (so a corrected seat counts as corrected).
    Only entries in the Beliefs section can be under pressure (SPEC "Under pressure").
    """
    live = [d for d in _with_overrides(digests, config) if d.status == "digested"]
    lists: dict[str, list[dict]] = {v: [] for v in VERDICTS}
    checked = 0
    for d in live:
        if not _checked_current(d, entry):
            continue
        checked += 1
        for check in d.belief_checks:
            if check.target != entry.id or check.verdict not in lists:
                continue
            if is_dismissed(check, d, overrides):
                continue
            lists[check.verdict].extend(_evidence(d, check))
    pressure = entry.section == "beliefs" and _pressure_met(_pressure_items(entry, lists["contradicts"]), config)
    return EntryStatus(entry_id=entry.id, supports=lists["supports"], contradicts=lists["contradicts"],
                       scope_mismatch=lists["scope_mismatch"], under_pressure=pressure,
                       checked=checked, total=len(live))


def under_pressure(entry: Entry, digests: list[Digest], overrides: list[dict], config: Config) -> bool:
    """True when the belief has contradictions decided agree or arbiter, not dismissed and against
    its current version, from at least `under_pressure_calls` calls in `under_pressure_seats` seats.
    After a Keep only calls dated after it count. Watching and Retired entries never qualify."""
    return entry_status(entry, digests, overrides, config).under_pressure


def evidence_counts(digests: list[Digest], beliefs: BeliefsDoc,
                    overrides: list[dict] | None = None) -> dict[str, dict]:
    """Supporting evidence per current entry, for `brief.render_brief`.

    entry_id -> {"calls", "seats", "seat_list", "seat_calls": {seat: calls}}, counting digested calls
    with a `supports` check against the entry's current version that `overrides` (the rows of
    config/verdict_overrides.yml, see load_verdict_overrides) does not dismiss, as beliefs-status.md
    counts them. Pass the digests including the call being briefed. Calls without a seat count as
    calls but not as a seat.
    """
    out: dict[str, dict] = {}
    for e in beliefs.current():
        calls: set[str] = set()
        by_seat: dict[str, set[str]] = {}
        for d in digests:
            if not _checked_current(d, e):
                continue
            if any(c.target == e.id and c.verdict == "supports"
                   and not (overrides and is_dismissed(c, d, overrides)) for c in d.belief_checks):
                calls.add(d.call_id)
                if d.seat:
                    by_seat.setdefault(d.seat, set()).add(d.call_id)
        seat_list = sorted(by_seat)
        out[e.id] = {"calls": len(calls), "seats": len(seat_list), "seat_list": seat_list,
                     "seat_calls": {s: len(by_seat[s]) for s in seat_list}}
    return out


def finding_claims(digests: list[Digest], beliefs: BeliefsDoc,
                   overrides: list[dict] | None = None) -> list[tuple[Digest, Claim]]:
    """Claims no current check touches, in digest then claim order.

    A claim is touched when a belief check on a current Belief or Watching item cites it (against
    that entry's current version, and not dismissed by `overrides`, the rows of
    config/verdict_overrides.yml), or when it is a history source of a current entry. Each source is
    placed as nua/apply.py places it for duplicates (held_spans: two identical "Exactly." answers are
    held one per change, a quote running over several claims covers their span), and every claim its
    span overlaps by SOURCE_OVERLAP is held, since apply refuses a new source there as a duplicate. A
    source that cannot be placed goes to its closest claim by quote overlap. Checks on Retired entries,
    on old wording or judged a Wrong read leave a claim a finding. Only `status: digested` digests count.
    """
    current = {e.id: e for e in beliefs.current()}
    held = beliefs.held_sources()
    held_calls = {h.call_id for h in held}

    out: list[tuple[Digest, Claim]] = []
    for d in digests:
        if d.status != "digested":
            continue
        touched: set[str | None] = set()
        for check in d.belief_checks:
            e = current.get(check.target)
            if e is not None and _checked_current(d, e) and not (overrides and is_dismissed(check, d, overrides)):
                touched.update(check.claims)
        for source, span in (placed_sources(d, held) if d.call_id in held_calls else []):
            if span is not None:
                touched.update(c.id for c in d.claims
                               if any(span_overlap(span, own) >= SOURCE_OVERLAP for own in c.spans))
                continue
            c = _best_claim(d, source.quote)
            if c is not None:
                touched.add(c.id)
        out.extend((d, c) for c in d.claims if c.id not in touched)
    return out


# ---------- calls.csv and claims.csv ----------

def _cell(value: Any) -> str:
    if value is None:
        return ""
    return _clean(_oneline(value))


def _csv(columns: list[str], rows: list[dict]) -> str:
    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\n")
    writer.writerow(columns)
    for row in rows:
        writer.writerow([_cell(row.get(c)) for c in columns])
    return buf.getvalue()


def render_calls_csv(digests: list[Digest]) -> str:
    """`calls.csv`: one row per digest, stubs included, in the digests' order."""
    rows = []
    for d in digests:
        row = {c: getattr(d, c, None) for c in CALLS_COLUMNS if c != "claims"}
        row["date"] = _iso(d.date)
        row["claims"] = len(d.claims)
        rows.append(row)
    return _csv(CALLS_COLUMNS, rows)


def _targets(d: Digest, claim_id: str | None) -> str:
    hits = sorted({(b.target, b.verdict) for b in d.belief_checks if claim_id in b.claims})
    return "; ".join(f"{target} {verdict}" for target, verdict in hits)


QUOTE_JOIN = " | "     # between a finding's quotes in the claims.csv `quote` column


def render_claims_csv(digests: list[Digest]) -> str:
    """`claims.csv`: one row per finding of every digested call. `quote` holds the finding's quotes in
    order, joined by " | "; `speaker` and `attribution` are the first quote's. `seat` is the finding's
    scope seat, or the call's seat when the scope has none; `belief_targets` lists every check citing
    the finding, e.g. "B-003 contradicts; W-001 supports"."""
    rows = []
    for d in digests:
        if d.status != "digested":
            continue
        for c in d.claims:
            rows.append({
                "call_id": d.call_id, "claim_id": c.id, "date": _iso(d.date), "speaker": c.speaker,
                "attribution": c.quotes[0].attribution if c.quotes else None, "seat": c.scope.seat or d.seat,
                "line": c.scope.line, "party": c.scope.party, "segment": c.scope.segment,
                "text": c.text, "quote": QUOTE_JOIN.join(_oneline(q) for q in c.quote_texts),
                "belief_targets": _targets(d, c.id), "found_by": c.found_by,
            })
    return _csv(CLAIMS_COLUMNS, rows)


# ---------- findings.md ----------

def _newest_first(items: list[tuple[Digest, Claim]]) -> list[tuple[Digest, Claim]]:
    """Calls newest first (undated last), claims in transcript order within a call."""
    by_offset = sorted(items, key=lambda dc: dc[1].offset[0] if dc[1].offset else 0)
    return sorted(by_offset, key=lambda dc: _newest_first_key(dc[0]), reverse=True)


def _finding_lines(items: list[tuple[Digest, Claim]]) -> list[str]:
    lines = []
    for d, c in _newest_first(items):
        where = SEP.join(p for p in (d.call_id, d.seat) if p)
        lines.append(f"- {where}: {_oneline(c.text)}")
        lines.extend(f"  > {_oneline(q)}" for q in c.quote_texts)
    return lines


def group_findings(digests: list[Digest], findings: list[tuple[Digest, Claim]],
                   themes: list[dict]) -> tuple[dict[str, list[tuple[Digest, Claim]]], list[tuple[Digest, Claim]]]:
    """(theme name -> findings, ungrouped findings), in the order of `findings`. A theme claim resolves
    to the closest claim of its call (quote overlap >= MATCH_THRESHOLD); a finding joins the first theme
    that names it. findings.md and the MCP server's get_findings both group with this."""
    by_call = {d.call_id: d for d in digests if d.status == "digested"}
    is_finding = {(d.call_id, c.id) for d, c in findings}
    assigned: dict[tuple[str, str | None], str] = {}
    for theme in themes:
        for ref in theme.get("claims") or []:
            if not isinstance(ref, dict):
                continue
            d = by_call.get(ref.get("call_id"))
            c = _best_claim(d, ref.get("quote"), _span(ref.get("offset"))) if d else None
            if c is None:
                continue
            key = (d.call_id, c.id)
            if key in is_finding and key not in assigned:
                assigned[key] = str(theme["name"])
    grouped: dict[str, list[tuple[Digest, Claim]]] = {}
    ungrouped: list[tuple[Digest, Claim]] = []
    for d, c in findings:
        name = assigned.get((d.call_id, c.id))
        if name is None:
            ungrouped.append((d, c))
        else:
            grouped.setdefault(name, []).append((d, c))
    return grouped, ungrouped


def _calls_and_seats(items: list[tuple[Digest, Claim]]) -> tuple[int, int]:
    return len({d.call_id for d, _ in items}), len({d.seat for d, _ in items if d.seat})


def render_findings(digests: list[Digest], beliefs: BeliefsDoc, themes: list[dict],
                    last_rollup: str | None = None, overrides: list[dict] | None = None) -> str:
    """`findings.md`: claims no current check touches (dismissed checks do not count), grouped by
    theme (most calls first) with call and seat counts, then the ungrouped ones newest first."""
    findings = finding_claims(digests, beliefs, overrides)
    grouped, ungrouped = group_findings(digests, findings, themes)
    calls = len({d.call_id for d, _ in findings})
    lines = ["# Findings", "",
             f"Claims no current belief or Watching item touches: {_plural(len(findings), 'claim')} from "
             f"{_plural(calls, 'call')}. Generated by nua/indexes.py from the digests and config/themes.yml; "
             "edits here are overwritten.", ""]
    if not findings:
        lines += ["No findings yet.", ""]

    def order(name: str) -> tuple[int, int, str]:
        n_calls, n_seats = _calls_and_seats(grouped[name])
        return (-n_calls, -n_seats, name.lower())

    for name in sorted(grouped, key=order):
        n_calls, n_seats = _calls_and_seats(grouped[name])
        lines += [f"## {_oneline(name)} ({_plural(n_calls, 'call')}, {_plural(n_seats, 'seat')})", ""]
        lines += _finding_lines(grouped[name]) + [""]
    if ungrouped:
        heading = f"## Ungrouped since {last_rollup}" if last_rollup else "## Ungrouped (no roll-up yet)"
        lines += [heading, "", f"{_plural(len(ungrouped), 'claim')}, newest first.", ""]
        lines += _finding_lines(ungrouped) + [""]
    return _clean("\n".join(lines).rstrip() + "\n")


# ---------- beliefs-status.md ----------

def _by_seat(items: list[dict]) -> list[tuple[str | None, list[dict]]]:
    """Evidence grouped by seat, most calls first; items newest first within a seat."""
    groups: dict[str | None, list[dict]] = {}
    for i in items:
        groups.setdefault(i["seat"], []).append(i)

    def order(seat: str | None) -> tuple[int, int, str]:
        return (-len({i["call_id"] for i in groups[seat]}), seat is None, (seat or "").lower())

    out = []
    for seat in sorted(groups, key=order):
        rows = sorted(groups[seat], key=lambda i: (i["date"] or "", i["call_id"]), reverse=True)
        out.append((seat, rows))
    return out


def _evidence_block(title: str, items: list[dict]) -> list[str]:
    if not items:
        return [f"**{title}**: none", ""]
    calls = len({i["call_id"] for i in items})
    seats = len({i["seat"] for i in items if i["seat"]})
    lines = [f"**{title}** ({_plural(calls, 'call')}, {_plural(seats, 'seat')})", ""]
    for seat, rows in _by_seat(items):
        lines.append(f"- {seat or 'seat unknown'}")
        for i in rows:
            label = DECIDED_LABELS.get(i["decided_by"], i["decided_by"])
            quote = _oneline(i["quote"])
            lines.append(f'  - {i["call_id"]} [{label}]' + (f': "{quote}"' if quote else ""))
    return lines + [""]


def _entry_block(e: Entry, status: EntryStatus, decisions: list[Decision]) -> list[str]:
    lines = [f"### {e.id} {_oneline(e.statement)}", ""]
    scope = format_scope(e.scope)
    if scope:
        lines.append(f"- scope: {scope}")
    lines.append(f"- checked {status.checked}/{status.total} calls")
    linked = [f"{d.id} {_oneline(d.title)}".strip() for d in decisions if e.id in d.rests_on]
    if linked:
        lines.append(f"- decisions: {'; '.join(linked)}")
    if e.section == "beliefs":
        counted = _pressure_items(e, status.contradicts)
        n_calls = len({i["call_id"] for i in counted})
        n_seats = len({i["seat"] for i in counted if i["seat"]})
        if status.under_pressure:
            lines.append(f"- under pressure: contradicted by {_plural(n_calls, 'call')} in "
                         f"{_plural(n_seats, 'seat')} (agree or arbiter)")
        kept = _keep_date(e)
        if kept:
            lines.append(f"- kept {kept}: only contradictions from calls after it count toward pressure")
    lines.append("")
    if not (status.supports or status.contradicts or status.scope_mismatch):
        return lines + ["No evidence yet.", ""]
    for verdict, title in VERDICT_TITLES:
        lines += _evidence_block(title, getattr(status, verdict))
    return lines


def render_beliefs_status(beliefs: BeliefsDoc, decisions: list[Decision],
                          statuses: dict[str, EntryStatus]) -> str:
    """`beliefs-status.md`: for every Belief and Watching item, evidence for and against by seat with
    quotes, pressure, Keep window and coverage ("checked 70/70 calls")."""
    current = beliefs.current()
    total = max((s.total for s in statuses.values()), default=0)
    pressured = [e.id for e in current if statuses[e.id].under_pressure]
    lines = ["# Beliefs status", "",
             f"Evidence for and against every belief and Watching item, from {_plural(total, 'digested call')}. "
             "Generated by nua/indexes.py; edits here are overwritten.", "",
             f"Under pressure: {', '.join(pressured) if pressured else 'none'}.", ""]
    for section, title in (("beliefs", "Beliefs"), ("watching", "Watching")):
        entries = [e for e in current if e.section == section]
        lines += [f"## {title}", ""]
        if not entries:
            lines += ["None.", ""]
        for e in entries:
            lines += _entry_block(e, statuses[e.id], decisions)
    return _clean("\n".join(lines).rstrip() + "\n")


# ---------- open-questions.md ----------

def _open_prep_questions(digests: list[Digest]) -> list[tuple[str, list[tuple[Digest, str]]]]:
    """[(question text, [(digest, status)])] for prep questions dodged or never asked, dropped once a
    later call marks the same question text (normalized) answered. Newest open question first."""
    live = [d for d in digests if d.status == "digested"]
    answered: dict[str, tuple[str, str]] = {}
    for d in live:
        for q in d.question_coverage:
            if q.status == "answered":
                key = normalize(q.question)
                answered[key] = max(answered.get(key, ("", "")), _newest_first_key(d))
    groups: dict[str, list[tuple[Digest, str]]] = {}
    texts: dict[str, str] = {}
    for d in sorted(live, key=_newest_first_key):
        for q in d.question_coverage:
            if q.status not in COVERAGE_WORDS:
                continue
            key = normalize(q.question)
            if not key or (key in answered and answered[key] > _newest_first_key(d)):
                continue
            texts.setdefault(key, _oneline(q.question))
            groups.setdefault(key, []).append((d, q.status))
    order = sorted(groups, key=lambda k: (max(_newest_first_key(d) for d, _ in groups[k]), texts[k]), reverse=True)
    return [(texts[k], groups[k]) for k in order]


def _seat_counts(items: list[dict]) -> list[str]:
    calls: dict[str, set[str]] = {}
    for i in items:
        calls.setdefault(i["seat"] or "seat unknown", set()).add(i["call_id"])
    order = sorted(calls, key=lambda s: (-len(calls[s]), s.lower()))
    return [f"{s} ({_plural(len(calls[s]), 'call')})" for s in order]


def _manual_section(existing: str) -> str:
    """The hand-edited text between the manual markers, or a single newline when there is none."""
    m = _MANUAL.search(existing or "")
    return m.group(1) if m else "\n"


def render_open_questions(digests: list[Digest], beliefs: BeliefsDoc, statuses: dict[str, EntryStatus],
                          config: Config, existing: str = "") -> str:
    """`open-questions.md`: open prep questions, then each belief under pressure with the seats
    contradicting it and the seats not yet heard from, then the manual section copied from `existing`."""
    lines = ["# Open questions", "",
             "Prep questions still open, and who to hear from on beliefs under pressure. Generated by "
             "nua/indexes.py; only the manual section at the end is hand-edited.", "",
             "## Prep questions still open", ""]
    questions = _open_prep_questions(digests)
    if not questions:
        lines += ["None.", ""]
    for text, where in questions:
        lines.append(f"- {text}")
        for d, status in where:
            who = ", ".join(p for p in (d.contact, d.seat) if p)
            lines.append(f"  - {COVERAGE_WORDS[status]}: {d.call_id}" + (f" ({who})" if who else ""))
    if questions:
        lines.append("")

    lines += ["## Beliefs under pressure", ""]
    pressured = [e for e in beliefs.current() if statuses[e.id].under_pressure]
    if not pressured:
        lines += ["None.", ""]
    for e in pressured:
        s = statuses[e.id]
        heard = {i["seat"] for i in s.supports + s.contradicts + s.scope_mismatch if i["seat"]}
        unheard = [seat for seat in config.seats if seat not in heard and seat not in SKIP_UNHEARD_SEATS]
        lines += [f"### {e.id} {_oneline(e.statement)}", "",
                  f"- contradicted by: {', '.join(_seat_counts(_pressure_items(e, s.contradicts)))}",
                  f"- not yet heard from: {', '.join(unheard) if unheard else 'none'}", ""]

    generated = _clean("\n".join(lines).rstrip() + "\n")
    return f"{generated}\n## Manual\n\n{MANUAL_START}{_manual_section(existing)}{MANUAL_END}\n"


# ---------- regenerate ----------

def regenerate(ctx: Ctx) -> list[str]:
    """Rebuild every file in GENERATED from the repo and return their paths (in GENERATED order).

    Reads the digests (with `config/overrides.yml` applied), beliefs.md, decisions.md,
    config/themes.yml and config/verdict_overrides.yml. Deterministic: the same inputs give the
    same bytes. The manual section of open-questions.md is carried over.
    """
    repo, config = ctx.repo, ctx.config
    digests = _with_overrides(load_digests(repo), config)
    beliefs = parse_beliefs(repo.read_or(BELIEFS_FILE, EMPTY_BELIEFS))
    decisions = parse_decisions(repo.read_or(DECISIONS_FILE, ""))
    overrides = load_verdict_overrides(repo)
    statuses = {e.id: entry_status(e, digests, overrides, config) for e in beliefs.current()}
    files = {
        "calls.csv": render_calls_csv(digests),
        "claims.csv": render_claims_csv(digests),
        "findings.md": render_findings(digests, beliefs, load_themes(repo), _last_rollup(repo), overrides),
        "beliefs-status.md": render_beliefs_status(beliefs, decisions, statuses),
        "open-questions.md": render_open_questions(digests, beliefs, statuses, config,
                                                   repo.read_or("open-questions.md", "")),
    }
    for rel in GENERATED:
        repo.write(rel, files[rel])
    return list(GENERATED)
