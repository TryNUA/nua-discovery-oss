"""Backcheck: check past calls against new entry versions (SPEC "Backcheck").

When an entry is added, watched, promoted, reworded, split or merged, its current version is missing
from past digests' `checked`. `backcheck`:
1. finds every current entry whose version is missing from some `status: digested` digest, so one
   run absorbs every approval made since the last one;
2. pre-filters each digest's stored claims by relevance to the pending entries, then runs the verdict
   step (both models, arbiter on disagreement) per digest, several digests at once. Transcripts are
   read only for surrounding turns;
3. on every digest missing a version, drops that entry's earlier `belief_checks`, writes the new
   verdicts with `source: backcheck` and records the version in `checked`, whether or not any claim
   passed the pre-filter;
4. regenerates the indexes and commits.

Digests that need model calls run in batches of BATCH_SIZE, with one commit per batch, so a task
that runs out of budget keeps every batch it finished. A digest whose verdict call fails is left
untouched (still missing), the rest are committed, and the task then raises so it is retried; the
retry only redoes what is still missing. When the per-task cost cap is hit, no further model call
starts, every digest that finished is committed, and the CostCapExceeded is raised again, so each
Re-run moves forward.

Debounce (SPEC "Bootstrap": an approval session runs one backcheck): `discovery/apply.py` records the
latest version-changing change in config/backcheck.yml ({"last_change": {"ref", "at"}}) and enqueues
`backcheck` with {"after": ref}. A backcheck whose `after` is not the latest change, while that
change is younger than `backcheck_delay_seconds`, makes no model call: it re-enqueues itself under
the latest change's task name (which collapses into the one apply already queued) and exits.
"""
from __future__ import annotations

import math
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timedelta

import yaml

from discovery.beliefs import EMPTY_BELIEFS, BeliefsDoc, parse_beliefs
from discovery.context import Ctx
from discovery.digest import load_digests, write_digest
from discovery.frontmatter import dump_yaml
from discovery.llm import CostCapExceeded, LLMError
from discovery.types import BeliefCheck, Claim, Digest, Entry, ParsedTranscript
from discovery.verify import normalize

CONCURRENCY = 8
BATCH_SIZE = 2 * CONCURRENCY    # digests with model calls per commit, so a timeout keeps finished batches
STATE_FILE = "config/backcheck.yml"
STATE_HEADER = "# Processor-written by discovery/apply.py (the backcheck debounce). Do not edit by hand.\n"

# Words that carry no topic in this corpus: English function words and ASR filler.
_STOPWORDS = frozenset("""
    about above across after again against also although among and another any are around aren't because been
    before being below between both but can can't cannot could couldn't did didn't does doesn't doing done
    don't down during each either else even ever every few for from further get gets getting got gonna had
    hadn't has hasn't have haven't having her here hers herself him himself his how however i'd i'll i'm i've
    into isn't it's its itself just kind know let's like likely lot lots made make makes making many may maybe
    mean might more most much must need needs nor not now off often once one only onto other others ought our
    ours out over own really right said same say says see she should shouldn't since some something sort
    still such than that that's the their theirs them themselves then there there's these they they're thing
    things think this those though through thus too under until upon very want wants was wasn't way well
    were weren't what what's when where whether which while who whom whose why will with within without
    won't would wouldn't yeah yes yet you you're your yours okay guess actually basically pretty going
    """.split())
_SUFFIXES = ("ing", "ed", "er")
MIN_PREFIX = 4           # a shorter term must be this long to match a longer one it starts


@dataclass
class _Job:
    digest: Digest
    entries: list[Entry]                   # pending entries for this digest
    claims: list[Claim] = field(default_factory=list)   # claims that passed the pre-filter


def missing_versions(digests: list[Digest], beliefs: BeliefsDoc) -> dict[str, list[str]]:
    """entry_id -> call_ids of `status: digested` digests whose `checked` lacks the entry's current version.

    Only current entries (Beliefs and Watching) with a version count. Entries with nothing missing are left out.
    """
    out: dict[str, list[str]] = {}
    for e in beliefs.current():
        version = e.version
        if not version:
            continue
        calls = [d.call_id for d in digests if d.status == "digested" and d.checked.get(e.id) != version]
        if calls:
            out[e.id] = calls
    return out


# ---------- pre-filter ----------

def _stem(word: str) -> str:
    """A light, predictable stem: plural dropped ("policies" -> "policy", "losses" -> "loss"), then
    -ing, -ed and -er (repeatedly, keeping at least 3 letters), then a final e. So "rate", "rates",
    "rated" and "rating" all give "rat", and "insure", "insurer" and "insuring" give "insur"."""
    if len(word) > 4 and word.endswith("ies"):
        word = word[:-3] + "y"
    elif len(word) > 3 and word.endswith("s") and not word.endswith(("ss", "us", "is")):
        word = word[:-1]
    stripped = True
    while stripped:
        stripped = False
        for suffix in _SUFFIXES:
            if word.endswith(suffix) and len(word) - len(suffix) >= 3:
                word = word[:-len(suffix)]
                stripped = True
                break
    if len(word) > 3 and word.endswith("e"):
        word = word[:-1]
    return word


def _terms(text: str | None) -> set[str]:
    """Topic terms of `text`: normalized words minus stopwords, apostrophes dropped, stemmed by `_stem`."""
    out: set[str] = set()
    for word in normalize(text or "").split():
        if word in _STOPWORDS:
            continue
        word = word.replace("'", "")
        if len(word) < 3:
            continue
        out.add(_stem(word))
    return out


def terms_match(a: str, b: str) -> bool:
    """Two stemmed terms match when they are equal, or the shorter has at least MIN_PREFIX letters and
    starts the longer ("fund" meets "fundamental", "financ" meets "financial")."""
    if a == b:
        return True
    short, long_ = (a, b) if len(a) <= len(b) else (b, a)
    return len(short) >= MIN_PREFIX and long_.startswith(short)


def _shares_term(terms: set[str], wanted: set[str]) -> bool:
    if terms & wanted:
        return True
    return any(terms_match(a, b) for a in terms for b in wanted)


def _entry_terms(e: Entry) -> set[str]:
    terms = _terms(e.statement)
    for value in e.scope.to_dict().values():
        terms |= _terms(value)
    return terms


def _claim_terms(c: Claim) -> set[str]:
    terms = _terms(c.text)
    for q in c.quote_texts:
        terms |= _terms(q)
    for value in c.scope.to_dict().values():
        terms |= _terms(value)
    return terms


def relevant_claims(entry: Entry, claims: list[Claim], *, keep: set[str] | None = None) -> list[Claim]:
    """Claims worth showing the verdict models for `entry`, in their original order.

    A cheap, recall-first pre-filter: a claim passes when its text, quote or scope shares a topic term
    with the entry's statement or scope (light stems matched by `terms_match`, so "claims" meets "claim",
    "policies" meets "policy", "adjusting" meets "adjust" and "rated" meets "rating"). Claims whose ids
    are in `keep` (for example, ones an earlier version was checked against) always pass. An entry with
    no topic terms keeps every claim.
    """
    wanted = _entry_terms(entry)
    keep = keep or set()
    if not wanted:
        return list(claims)
    return [c for c in claims if c.id in keep or _shares_term(_claim_terms(c), wanted)]


def _job_claims(d: Digest, entries: list[Entry]) -> list[Claim]:
    """Union of the claims relevant to any pending entry, in digest order."""
    ids: set[str] = set()
    for e in entries:
        previous = {cid for check in d.belief_checks if check.target == e.id for cid in check.claims}
        ids |= {c.id for c in relevant_claims(e, d.claims, keep=previous)}
    return [c for c in d.claims if c.id in ids]


# ---------- running ----------

def _parsed(ctx: Ctx, d: Digest) -> ParsedTranscript | None:
    """The digest's transcript for surrounding turns, or None when it is missing or does not parse."""
    if not ctx.repo.exists(d.transcript):
        return None
    from discovery.parse import ParseError, parse
    try:
        return parse(d.transcript, ctx.repo.read(d.transcript))
    except ParseError:
        return None


def _run_one(ctx: Ctx, job: _Job) -> list[BeliefCheck]:
    """Verdicts for one digest's pending entries. Raises LLMError when every verdict model fails."""
    from discovery.verdicts import run_verdicts
    if not job.claims:
        return []
    parsed = _parsed(ctx, job.digest)
    if parsed is not None and parsed.speaker_labels != "named":
        # Who said each claim was settled at ingest (the claims carry it); do not ask or arbitrate it again.
        parsed = parsed.model_copy(update={"speaker_labels": "named"})
    out = run_verdicts(ctx, parsed, job.claims, job.entries, [], source="backcheck")
    if out.degraded:
        ctx.info(f"backcheck {job.digest.call_id}: degraded {', '.join(out.degraded)}")
    pending = {e.id for e in job.entries}
    return [c for c in out.belief_checks if c.target in pending]


def _apply(d: Digest, entries: list[Entry], checks: list[BeliefCheck]) -> None:
    """Drop earlier checks for `entries`, add the new ones, record each entry's version in `checked`."""
    ids = {e.id for e in entries}
    kept = [c for c in d.belief_checks if c.target not in ids]
    for c in checks:
        c.source = "backcheck"
    d.belief_checks = sorted(kept + checks, key=lambda c: (c.target, c.claims[0] if c.claims else ""))
    for e in entries:
        d.checked[e.id] = e.version


def _regenerate(ctx: Ctx) -> list[str]:
    """Rebuild the generated index files (Task 11); returns their paths."""
    from discovery import indexes
    return list(indexes.regenerate(ctx))


# ---------- debounce ----------

def change_state(ref: str, at: datetime) -> str:
    """Text of config/backcheck.yml recording the latest change that gave an entry a new version.
    discovery/apply.py writes it in the same commit as the change."""
    return STATE_HEADER + dump_yaml({"last_change": {"ref": ref, "at": at.isoformat()}})


def last_change(ctx: Ctx) -> tuple[str | None, datetime | None]:
    """(ref, time) of the latest version-changing change from config/backcheck.yml; (None, None) when
    the file is missing or unreadable."""
    try:
        data = yaml.safe_load(ctx.repo.read_or(STATE_FILE, "")) or {}
    except yaml.YAMLError:
        return None, None
    last = data.get("last_change") if isinstance(data, dict) else None
    if not isinstance(last, dict) or not last.get("ref"):
        return None, None
    at = last.get("at")
    if isinstance(at, str):
        try:
            at = datetime.fromisoformat(at)
        except ValueError:
            at = None
    if not isinstance(at, datetime) or at.tzinfo is None:
        at = None
    return str(last["ref"]), at


def backcheck_task_name(ref: str) -> str:
    """Cloud Tasks name of the backcheck scheduled after the change `ref`."""
    from discovery.tasks import task_name
    return task_name("backcheck", f"after:{ref}")


def _defer(ctx: Ctx, after: str) -> str | None:
    """The newer change this backcheck (scheduled after `after`) should wait for, or None to run now.

    Waits only while that change is younger than `backcheck_delay_seconds`; then it re-enqueues itself
    under that change's task name, which collapses into the backcheck apply already queued for it.
    """
    ref, at = last_change(ctx)
    if ref is None or ref == after or at is None or ctx.queue is None or ctx.dry_run:
        return None
    delay = int(ctx.config.pipeline.t("backcheck_delay_seconds"))
    remaining = (at + timedelta(seconds=delay) - ctx.now()).total_seconds()
    if remaining <= 0:
        return None
    ctx.queue.enqueue("backcheck", {"after": ref}, name=backcheck_task_name(ref),
                      delay_seconds=max(1, math.ceil(remaining)))
    ctx.info(f"backcheck after {after}: {ref} changed since, waiting {math.ceil(remaining)}s for it")
    return ref


# ---------- running ----------

class _Stopped(Exception):
    """A job that never started because the cost cap was already hit."""


def _guarded(ctx: Ctx, job: _Job, stop: threading.Event) -> list[BeliefCheck]:
    """`_run_one` unless `stop` is set. Hitting the cost cap (or anything but an LLMError) sets `stop`,
    so queued jobs make no further paid call."""
    if stop.is_set():
        raise _Stopped()
    try:
        return _run_one(ctx, job)
    except LLMError:
        raise
    except BaseException:
        stop.set()
        raise


def _commit(ctx: Ctx, done: list[_Job], results: dict[str, list[BeliefCheck]], pending: list[Entry]) -> None:
    """Apply, write and commit the finished jobs (with regenerated indexes)."""
    paths: list[str] = []
    for j in done:
        _apply(j.digest, j.entries, results[j.digest.call_id])
        paths.append(write_digest(ctx.repo, j.digest))
    if not paths:
        return
    paths += [p for p in _regenerate(ctx) if p not in paths]
    if not ctx.dry_run:
        ids = ", ".join(e.id for e in pending)
        ctx.remote.commit_and_push(f"Backcheck {ids} across {len(done)} calls", paths,
                                   regenerate=lambda: _regenerate(ctx))


def backcheck(ctx: Ctx, payload: dict | None = None) -> dict:
    """Run the backcheck for every pending entry version. Returns {"entries": [ids], "digests_updated": n}
    (plus "deferred_to": ref when it waited for a newer change instead, see the module docstring).

    `payload` is the task payload; {"after": ref} (from discovery/apply.py) turns on the debounce. Nothing
    pending: no model call, no commit. Digests needing model calls run in batches of BATCH_SIZE, one
    commit per batch. Raises LLMError after committing the finished digests when some digest's verdicts
    failed, so the task is retried for the rest. Raises CostCapExceeded after committing every digest
    that finished, once the cost cap stops it.
    """
    after = (payload or {}).get("after")
    if after:
        newer = _defer(ctx, str(after))
        if newer:
            return {"entries": [], "digests_updated": 0, "deferred_to": newer}

    digests = load_digests(ctx.repo)
    doc = parse_beliefs(ctx.repo.read_or("beliefs.md", EMPTY_BELIEFS))
    missing = missing_versions(digests, doc)
    if not missing:
        ctx.info("backcheck: nothing pending")
        return {"entries": [], "digests_updated": 0}

    pending = [e for e in doc.current() if e.id in missing]
    jobs: list[_Job] = []
    for d in digests:
        entries = [e for e in pending if d.call_id in missing[e.id]]
        if entries:
            jobs.append(_Job(digest=d, entries=entries, claims=_job_claims(d, entries)))
    free = [j for j in jobs if not j.claims]          # nothing passed the pre-filter: no model call
    paid = [j for j in jobs if j.claims]
    ctx.info(f"backcheck: {', '.join(e.id for e in pending)} across {len(jobs)} digests, "
             f"{len(paid)} with relevant claims")

    results: dict[str, list[BeliefCheck]] = {j.digest.call_id: [] for j in free}
    failures: list[str] = []
    capped: CostCapExceeded | None = None
    stop = threading.Event()
    batches = [paid[i:i + BATCH_SIZE] for i in range(0, len(paid), BATCH_SIZE)] or [[]]
    for n, batch in enumerate(batches):
        with ThreadPoolExecutor(max_workers=CONCURRENCY) as pool:
            futures = {j.digest.call_id: pool.submit(_guarded, ctx, j, stop) for j in batch}
        finished: list[_Job] = list(free) if n == 0 else []
        for j in batch:
            call_id = j.digest.call_id
            try:
                results[call_id] = futures[call_id].result()
                finished.append(j)
            except _Stopped:
                continue
            except CostCapExceeded as e:
                capped = capped or e
                ctx.info(f"backcheck {call_id} stopped: {e}")
            except LLMError as e:
                failures.append(f"{call_id}: {e}")
                ctx.info(f"backcheck {call_id} failed: {e}")
        _commit(ctx, finished, results, pending)
        if capped is not None:
            break

    if capped is not None:
        left = len(jobs) - len(results)
        ctx.info(f"backcheck: cost cap hit, {len(results)} of {len(jobs)} digests committed, {left} left")
        raise capped
    if failures:
        raise LLMError(f"backcheck failed for {len(failures)} of {len(jobs)} digests: " + "; ".join(failures))
    return {"entries": [e.id for e in pending], "digests_updated": len(results)}
