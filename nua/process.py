"""process_call: one transcript in, one digest (or stub) out, plus the brief. Backfill batches and final failures.

See SPEC "process_call" steps 1 to 14, "Duplicates", "Internal calls", "Backfill", "Degraded modes and
failures" and the posting rules under "Per-call Slack brief".

Outcomes of `process_call`:
- `digested`: a digest was written (a first run, a re-run, or a stub replaced by a successful run)
- `superseded`: this richer copy re-digested a call that already had a digest, under the same call id
- `duplicate`: this copy lost to another copy of the same call; recorded on the winner's digest when the winner has run
  (and this copy's own stub, left by an earlier failed run, is deleted in the same commit)
- `stub:internal`, `stub:parse_failed`: a stub digest was written (named after the transcript stem, or
  `<stem>-2` when another transcript's digest already has that id; a stub never overwrites another call)
- `skipped:current` (a digest already points here at the current prompt_version), `skipped:stub` (stubs
  only re-run through Re-run), `skipped:missing` (no such transcript file, or a symlink, which is never
  read), `skipped:unreadable` (the digest of this transcript, or of another copy of the call, is a file
  in digests/ that cannot be read: it is fixed by hand and never overwritten, even by Re-run), `skipped:internal` and
  `skipped:parse_failed` (a re-run hit a stub condition, and a real digest is never replaced by a stub).
  When a run that is not forced ends in one of these three and the transcript has its own digest (a
  stale re-run), the digest is marked `reprocess_failed`, so the nightly sweep stops picking it until a Re-run

`ctx.dry_run` writes repo files as usual but posts nothing to Slack and never touches `ctx.remote`.
nua/indexes.py is imported inside functions only.
"""
from __future__ import annotations

import copy
import re
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any

import yaml

from nua import frontmatter
from nua.beliefs import EMPTY_BELIEFS, BeliefsDoc, parse_beliefs, parse_decisions
from nua.brief import BriefMessages, acted_on, render_brief, render_text
from nua.callid import make_call_id, stub_call_id
from nua.context import Ctx
from nua.digest import (apply_overrides, digest_for_transcript, digest_path, is_stub, is_unreadable, load_digests,
                        make_stub, override_problems, unreadable_of, write_digest)
from nua.extract import extract, repair_quote
from nua.merge import consolidate
from nua.parse import LIVE_SOURCES, ParseError, parse
from nua.prep import load_preps, match_prep, parse_prep
from nua.rules import by_arrival, is_internal, note_version, pick_winner, same_call
from nua.tasks import task_name
from nua.types import BriefRef, Claim, Digest, ExtractionResult, ParsedTranscript, Verification, Who
from nua.verdicts import run_verdicts
from nua.verify import QuoteIndex, verify_claims, verify_follow_ups

BRIEF_EVENT = "nua_brief"           # Slack metadata on a brief's top message; key = transcript path
BATCH_EVENT = "nua_batch"           # Slack metadata on a backfill summary; key = "batch:<id>"
BATCH_DIR = "config/batches"
SUMMARY_TS = "summary_ts"           # batch file key: ts of the posted summary (nua.sweep.SUMMARY_TS reads it)
VERDICT_OVERRIDES = "config/verdict_overrides.yml"
REJECTED = "config/rejected.yml"
MAX_REASON = 500                    # characters of an error kept in a stub's reason
MAX_SECTION = 2900                  # characters per Slack section block (Slack's limit is 3000)
MAX_BLOCKS = 50
EM_DASH = chr(0x2014)               # never written in text we produce; spelled this way so no file contains one


@dataclass
class ProcessResult:
    transcript: str
    outcome: str      # "digested" | "stub:<status>" | "skipped:<reason>" | "duplicate" | "superseded"
    call_id: str | None = None
    briefed: bool = False
    brief_text: str | None = None   # dry runs only: the brief as plain text (render_text), for any origin


# ---------- process_call ----------

def process_call(ctx: Ctx, transcript: str, *, force: bool = False, batch: str | None = None,
                 beliefs_path: str = "beliefs.md", decisions_path: str = "decisions.md",
                 prep_path: str | None = None) -> ProcessResult:
    """Parse, check, extract, verify, consolidate, judge, write, index, brief and commit one transcript.

    `force` (Re-run) processes even when a digest or stub already points to the transcript.
    `batch` names the backfill batch this transcript belongs to; the batch completion check runs
    afterwards, including when the call is skipped, and a failure there (Slack down) is only logged,
    since the nightly sweep repeats the check. `beliefs_path`, `decisions_path` and `prep_path`
    point the run at other files (dry-run golden tests); `prep_path` forces that prep file.
    Model failures (LLMError when every model fails, CostCapExceeded) propagate to the caller.
    """
    result = _process(ctx, _norm(transcript), force=force, beliefs_path=beliefs_path,
                      decisions_path=decisions_path, prep_path=prep_path, batch=batch)
    if batch:
        _check_batch(ctx, batch)
    return result


def _check_batch(ctx: Ctx, batch_id: str) -> None:
    """The batch completion check, where a failure (Slack down) is only logged. The call's own work is
    already committed, so a lost summary must not fail the task and re-run the models: the nightly
    sweep runs the same check for every batch file without `summary_ts`."""
    try:
        batch_completion_check(ctx, batch_id)
    except Exception as e:
        ctx.info(f"process_call: completion check of batch {batch_id} failed, left for the sweep: {e}")


@dataclass
class _Copies:
    """Outcome of the duplicate check for a copy no real digest names as its own transcript."""
    result: ProcessResult | None = None     # set when this copy stops here
    takeover: Digest | None = None          # the digest this copy supersedes
    duplicates: list[str] | None = None     # every other copy of the call
    drop: list[str] | None = None           # stub files of other copies, deleted on success


def _process(ctx: Ctx, transcript: str, *, force: bool, beliefs_path: str, decisions_path: str,
             prep_path: str | None, batch: str | None = None) -> ProcessResult:
    version = ctx.config.pipeline.prompt_version
    digests = load_digests(ctx.repo)
    existing = digest_for_transcript(digests, transcript)
    if existing is not None and is_unreadable(existing):
        return _unreadable(ctx, transcript, existing)
    if existing is not None and not force:
        if is_stub(existing):
            return ProcessResult(transcript, "skipped:stub", existing.call_id)
        if existing.prompt_version == version:
            return ProcessResult(transcript, "skipped:current", existing.call_id)
    if not ctx.repo.exists(transcript):
        result = ProcessResult(transcript, "skipped:missing", existing.call_id if existing else None)
        return _kept_stale(ctx, transcript, result, existing, force)
    own = existing if existing is not None and _norm(existing.transcript) == transcript else None

    # 1-2. parse; origin comes from the file itself
    try:
        raw = ctx.repo.read(transcript)
    except UnicodeDecodeError as e:
        reason = f"not UTF-8 text ({e.reason} at byte {e.start}); re-drop it saved as UTF-8"
        result = _parse_failed(ctx, transcript, _read_lenient(ctx, transcript), reason, own, batch, force=force)
        return _kept_stale(ctx, transcript, result, own, force)
    try:
        parsed = _parse(ctx, transcript, raw, own)
    except ParseError as e:
        result = _parse_failed(ctx, transcript, raw, str(e), own, batch, force=force)
        return _kept_stale(ctx, transcript, result, own, force)

    # 3. internal calls get a stub
    if is_internal(parsed.attendees, ctx.config):
        result = _write_stub(ctx, transcript, "internal", "internal call: every attendee has a team email",
                             parsed.origin, parsed, own)
        return _kept_stale(ctx, transcript, result, own, force)

    # 4. duplicates, only for a copy no real digest names as its own transcript. Its own stub (a run that
    # failed on its last attempt, or a parse failure since fixed) does not count: a richer copy whose
    # first run failed still takes over the call's id and brief on Re-run, and a losing copy drops its stub.
    beliefs = _load_beliefs(ctx, beliefs_path)
    copies = _Copies(duplicates=list(own.duplicates) if own else [], drop=[])
    if own is None or is_stub(own):
        copies = _duplicate_check(ctx, parsed, digests, beliefs, own_stub=own)
        if copies.result is not None:
            return copies.result
        if own is not None:
            copies.duplicates = sorted({*(copies.duplicates or []), *(_norm(p) for p in own.duplicates)})
    takeover = copies.takeover

    # 5. extract with every extraction model; the first listed is primary
    results, degraded = extract(ctx, parsed)
    who = _who(results)

    # 6. call id: kept on a re-run or supersede, new otherwise. Stubs this run replaces (this transcript's
    # own, and other copies') are deleted in the same commit, so their ids are free again.
    replaced = set(copies.drop or [])
    if own is not None and is_stub(own):
        replaced.add(digest_path(own.call_id))
    if own is not None and not is_stub(own):
        call_id = own.call_id
    elif takeover is not None:
        call_id = takeover.call_id
    else:
        # an unreadable digest file's stem stays taken: that file is never overwritten
        taken = {d.call_id for d in digests if not (is_stub(d) and digest_path(d.call_id) in replaced)} | \
            {u.call_id for u in unreadable_of(digests)}
        call_id = make_call_id(parsed.date, who.contact, taken)

    # 7-8. verify quotes against the body, then consolidate findings across models
    kept, verification = _verify(ctx, parsed, results)
    claims, merged_degraded = consolidate(ctx, parsed, kept)
    degraded = _union(degraded, merged_degraded)

    primary = results[0]
    follow_ups, missed = verify_follow_ups(parsed, primary.follow_ups)
    verification.follow_up_quotes_dropped = missed
    if missed:
        ctx.info(f"process_call: {missed} follow-up quote(s) in {transcript} not found in the transcript body")
    previous = own if own is not None and not is_stub(own) else takeover
    d = Digest(
        call_id=call_id, transcript=transcript, status="digested", origin=parsed.origin,
        duplicates=sorted(set(copies.duplicates or []) - {transcript}),
        source=parsed.source, source_id=parsed.source_id, date=parsed.date, date_source=parsed.date_source,
        recorded_by=parsed.recorded_by, attendees=copy.deepcopy(parsed.attendees),
        contact=who.contact, firm=who.firm, firm_source=who.firm_source if who.firm else None, seat=who.seat,
        institution_type=who.institution_type, role_in_thesis=who.role_in_thesis,
        speaker_labels=parsed.speaker_labels, duration_min=duration_minutes(parsed),
        prompt_version=ctx.config.pipeline.prompt_version,
        models={"extract": [r.model for r in results], "arbiter": ctx.config.pipeline.arbiter},
        brief=copy.deepcopy(previous.brief) if previous is not None else None,
        summary=list(primary.summary), claims=claims, follow_ups=follow_ups,
        ask_next=list(primary.ask_next), mentions=_mentions(results), verification=verification,
    )
    d = apply_overrides(d, ctx.config.overrides)
    for problem in override_problems(ctx.config.overrides, call_id):
        ctx.info(f"process_call: {problem}")

    # 9. prep questions
    questions: list[str] = []
    prep = _prep_for(ctx, d, prep_path)
    if prep is not None:
        d.prep, questions = prep.path, list(prep.questions)

    # 10. verdicts against every current belief and Watching item
    entries = beliefs.current()
    outcome = run_verdicts(ctx, parsed, claims, entries, questions, source="ingest",
                           models=[r.model for r in results])
    d.claims = _apply_speakers(claims, outcome.speakers)
    d.belief_checks = outcome.belief_checks
    d.question_coverage = outcome.question_coverage
    d.degraded = _union(degraded, outcome.degraded)
    d.checked = {e.id: e.version for e in entries if e.version}
    cited = {cid for check in d.belief_checks for cid in check.claims}
    d.new_findings = [c.id for c in d.claims if c.id not in cited]

    # 11-12. write the digest, drop replaced stubs, regenerate indexes
    rel = write_digest(ctx.repo, d)
    drop = sorted(p for p in replaced if p != rel)    # a successful run replaces the stubs it supersedes
    for p in drop:
        ctx.repo.delete(p)
    generated = _regenerate(ctx)

    # 13. brief: live calls with no brief recorded yet, or a supersede
    briefed = False
    msgs: BriefMessages | None = None
    if d.origin == "live" and (d.brief is None or takeover is not None) and _slack_on(ctx):
        msgs = build_brief(ctx, d, beliefs_path=beliefs_path, decisions_path=decisions_path, parsed=parsed)
        briefed = _brief(ctx, d, msgs, supersede=takeover is not None)
        if briefed or takeover is not None:     # a failed supersede must not keep the old thread's ref
            write_digest(ctx.repo, d)
    brief_text = None
    if ctx.dry_run:
        msgs = msgs or build_brief(ctx, d, beliefs_path=beliefs_path, decisions_path=decisions_path, parsed=parsed)
        brief_text = render_text(msgs)

    # 14. commit and push
    verb = "supersede" if takeover is not None else "digest"
    _commit(ctx, f"process_call: {verb} {d.call_id} from {transcript}", [rel, *drop, *generated])
    return ProcessResult(transcript, "superseded" if takeover is not None else "digested", d.call_id,
                         briefed=briefed, brief_text=brief_text)


def _unreadable(ctx: Ctx, transcript: str, owner: Digest) -> ProcessResult:
    """`transcript` belongs to a digest file that cannot be read (owner is its stand-in). Nothing is written:
    the file is fixed by hand, and the nightly sweep reports it in ops."""
    ctx.info(f"process_call: {transcript} belongs to {digest_path(owner.call_id)}, which cannot be read "
             f"({owner.reason}); left alone until the file is fixed by hand")
    return ProcessResult(transcript, "skipped:unreadable", owner.call_id)


def _brief(ctx: Ctx, d: Digest, msgs: BriefMessages, *, supersede: bool) -> bool:
    """Post the brief and record it in `d.brief`; True when posted. The digest lands either way.

    A first post that fails leaves `d.brief` None and the nightly sweep re-posts it. A supersede that
    fails partway (the old replies are already deleted) is tried once more through the recorded top
    message, which works however old the call is. If that fails too, `d.brief` is cleared rather than
    left pointing at deleted replies, so the sweep re-posts it and finds the top message through the
    duplicates' metadata (at any call age when a duplicate is a live copy; SPEC "Nightly sweep" step 3).
    """
    tries = 2 if supersede else 1
    for attempt in range(1, tries + 1):
        try:
            d.brief = _post_messages(ctx, d, msgs)
            return True
        except Exception as e:   # Slack down
            ctx.info(f"process_call: brief for {d.call_id} not posted (attempt {attempt} of {tries}): {e}")
    if supersede:
        d.brief = None
    return False


# ---------- stubs ----------

def _parse_failed(ctx: Ctx, transcript: str, raw: str, error: str, own: Digest | None,
                  batch: str | None = None, *, force: bool = False) -> ProcessResult:
    """Unrecognized transcript: a parse_failed stub (unless a real digest exists) and an error in ops,
    with a Re-run button (stubs are only ever re-run through it). A non-forced run of a digest already
    marked `reprocess_failed` at the current prompt_version posts nothing: ops heard about it once."""
    result = _write_stub(ctx, transcript, "parse_failed", error, _origin_of(raw), None, own)
    reported = (not force and own is not None
                and own.reprocess_failed == ctx.config.pipeline.prompt_version)
    if _slack_on(ctx) and not reported:
        from nua import runner   # imported lazily, like runner imports this module
        kept = " The existing digest was kept." if result.outcome.startswith("skipped") else ""
        shown = _one_line(transcript)            # a dropped file's name may carry an em dash; the button keeps it
        text = f"Could not parse {shown}: {_one_line(error)}.{kept} Fix the file or the parser, then Re-run."
        payload = {"transcript": transcript, **({"batch": batch} if batch else {})}
        blocks = [_section(text)]
        value = runner.fit_rerun_value("process_call", payload)   # None: too big for a button, so post none
        if value is not None:
            blocks.append({"type": "actions", "block_id": "rerun", "elements": [{
                "type": "button", "action_id": "rerun", "text": {"type": "plain_text", "text": "Re-run"},
                "value": value}]})
        try:
            ctx.slack.post(ctx.channel("ops"), blocks, text)
        except Exception as e:
            ctx.info(f"process_call: ops error for {transcript} not posted: {e}")
    return result


def _write_stub(ctx: Ctx, transcript: str, status: str, reason: str, origin: str,
                parsed: ParsedTranscript | None, own: Digest | None) -> ProcessResult:
    """Write a stub digest named after the transcript stem (see _stub_id). A real digest is never replaced
    by a stub, and a stub never overwrites another transcript's digest."""
    if own is not None and not is_stub(own):
        ctx.info(f"process_call: {transcript} is now {status}; kept digest {own.call_id}")
        return ProcessResult(transcript, f"skipped:{status}", own.call_id)
    call_id = own.call_id if own is not None else _stub_id(ctx, transcript, load_digests(ctx.repo))
    d = make_stub(call_id=call_id, transcript=transcript, status=status, origin=origin,
                  reason=_reason(reason), parsed=parsed)
    d.duplicates = list(own.duplicates) if own is not None else []
    rel = write_digest(ctx.repo, d)          # this transcript's own stub, if any, is rewritten in place
    generated = _regenerate(ctx)
    _commit(ctx, f"process_call: {status} stub for {transcript}", [rel, *generated])
    return ProcessResult(transcript, f"stub:{status}", d.call_id)


def mark_final_failure(ctx: Ctx, transcript: str, error: str) -> None:
    """Record that process_call failed on its last attempt (or hit its cost cap).

    No digest yet: write a `failed` stub. The transcript's own digest (or stub) exists: set only
    `reprocess_failed` to the current prompt_version, which stops nightly retries until a Re-run.
    A transcript recorded as another digest's duplicate, or owned by a digest file that cannot be read,
    is left alone. Then runs the completion check
    of every backfill batch that lists the transcript (a failure there is only logged, so it cannot
    mask this one). Posting the Re-run button is the runner's job.
    """
    transcript = _norm(transcript)
    digests = load_digests(ctx.repo)
    existing = digest_for_transcript(digests, transcript)
    if existing is not None and is_unreadable(existing):
        _unreadable(ctx, transcript, existing)
    elif existing is None:
        try:
            raw = ctx.repo.read_or(transcript, "")
        except UnicodeDecodeError:
            raw = _read_lenient(ctx, transcript)      # metadata only; the stub stores no quotes
        except OSError:                               # a symlink or a path outside the repo: never read
            raw = ""
        parsed = None
        try:
            parsed = _parse(ctx, transcript, raw, None)
        except ParseError:
            pass
        except Exception as e:   # backstop: parse() raises only ParseError, but the stub must still be written
            ctx.info(f"mark_final_failure: {transcript} could not be parsed ({type(e).__name__}: {e}); "
                     "writing the stub without its metadata")
        d = make_stub(call_id=_stub_id(ctx, transcript, digests), transcript=transcript, status="failed",
                      origin=parsed.origin if parsed else _origin_of(raw), reason=_reason(error), parsed=parsed)
        rel = write_digest(ctx.repo, d)
        generated = _regenerate(ctx)
        _commit(ctx, f"process_call: failed on {transcript}", [rel, *generated])
    elif _norm(existing.transcript) == transcript:
        _mark_reprocess_failed(ctx, existing, f"process_call: failed on {transcript}")
    for batch_id in _batches_with(ctx, transcript):
        _check_batch(ctx, batch_id)


_KEPT = ("skipped:missing", "skipped:internal", "skipped:parse_failed")


def _kept_stale(ctx: Ctx, transcript: str, result: ProcessResult, existing: Digest | None,
                force: bool) -> ProcessResult:
    """A non-forced run (the nightly stale re-run, SPEC "Nightly sweep" step 7) that kept this
    transcript's own digest because the file is gone, now internal or no longer parses: mark the digest
    `reprocess_failed` at the current prompt_version, so the sweep stops picking it until a Re-run."""
    if (not force and result.outcome in _KEPT and existing is not None and not is_stub(existing)
            and _norm(existing.transcript) == transcript):
        _mark_reprocess_failed(ctx, existing, f"process_call: {transcript} {result.outcome[8:]}, "
                                              f"kept {existing.call_id}")
    return result


def _mark_reprocess_failed(ctx: Ctx, d: Digest, message: str) -> None:
    """Set `reprocess_failed` on `d` to the current prompt_version and commit, unless it is set already.
    Nothing else in the digest changes; a later successful run writes a fresh digest without the mark."""
    version = ctx.config.pipeline.prompt_version
    if d.reprocess_failed == version:
        return
    d.reprocess_failed = version
    rel = write_digest(ctx.repo, d)
    generated = _regenerate(ctx)
    _commit(ctx, message, [rel, *generated])


def _stub_id(ctx: Ctx, transcript: str, digests: list[Digest]) -> str:
    """A stub's call id: the transcript's filename stem (SPEC "Call id").

    When a digest of another transcript already has that id, or its file already exists (an unreadable
    one included), the first free `<stem>-2`, `<stem>-3`... instead, so a drop named by the call-id
    convention (for example `transcripts/2026-09-24_darius-kemp.md`) never overwrites the live call's digest.
    """
    base = stub_call_id(transcript)
    others = {d.call_id for d in digests if _norm(d.transcript) != transcript} | \
        {u.call_id for u in unreadable_of(digests)}
    mine = {d.call_id for d in digests if _norm(d.transcript) == transcript}

    def free(call_id: str) -> bool:
        return call_id not in others and (call_id in mine or not ctx.repo.exists(digest_path(call_id)))

    if free(base):
        return base
    n = 2
    while not free(f"{base}-{n}"):
        n += 1
    return f"{base}-{n}"


def _parse(ctx: Ctx, transcript: str, raw: str, own: Digest | None) -> ParsedTranscript:
    """parse(), with a yearless footer date read against the right day (SPEC "Backfill": the latest year
    on or before the commit date).

    A digest or stub of this transcript that already read its date from the footer keeps that date, so
    a re-run a year later never moves it. A first run uses the day the backfill batch listing the file
    was created, when there is one, else today.
    """
    today = _today(ctx)
    parsed = parse(transcript, raw, commit_date=today, tz=ctx.config.pipeline.timezone)
    if parsed.date_source != "footer":
        return parsed
    anchor = _footer_anchor(ctx, transcript, own)
    return parsed if anchor in (None, today) else parse(transcript, raw, commit_date=anchor,
                                                       tz=ctx.config.pipeline.timezone)


def _footer_anchor(ctx: Ctx, transcript: str, own: Digest | None) -> str | None:
    if own is not None and own.date_source == "footer" and own.date:
        return own.date
    days = [day for _, data in _batch_files_with(ctx, transcript) if (day := _day_of(ctx, data.get("created")))]
    return min(days) if days else None


def _day_of(ctx: Ctx, value: Any) -> str | None:
    """Date of a batch file's `created` timestamp in the configured time zone, or None when it is missing
    or malformed."""
    if not value:
        return None
    try:
        when = value if isinstance(value, datetime) else datetime.fromisoformat(str(value))
    except ValueError:
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=ctx.tz)
    return when.astimezone(ctx.tz).date().isoformat()


def _read_lenient(ctx: Ctx, rel: str) -> str:
    """A file that is not valid UTF-8, decoded with replacement characters (never used for quotes)."""
    try:
        return ctx.repo.read_bytes(rel).decode("utf-8", errors="replace")
    except OSError:     # gone, or a symlink (ports.UnsafePathError)
        return ""


# ---------- duplicates ----------

def _duplicate_check(ctx: Ctx, parsed: ParsedTranscript, digests: list[Digest], beliefs: BeliefsDoc,
                     own_stub: Digest | None = None) -> _Copies:
    """SPEC "Duplicates" for a copy no real digest names as its own transcript. `own_stub` is this copy's
    own stub, if any: when the copy loses, the stub is deleted in the commit that records the copy on
    the winner, since a stub naming the transcript would otherwise keep winning digest_for_transcript."""
    others = _same_call_copies(ctx, parsed)
    if not others:
        return _Copies(duplicates=[], drop=[])
    owners = {o.path: digest_for_transcript(digests, o.path) for o in others}
    # A copy whose digest cannot be read: recording this copy on it, or replacing it, would overwrite or
    # delete that file, and digesting this copy apart would make two digests of one call. Wait for the fix.
    broken = next((d for d in owners.values() if d is not None and is_unreadable(d)), None)
    if broken is not None:
        return _Copies(result=_unreadable(ctx, parsed.path, broken))
    # Arrival order: `ingested_at` decides when every tied copy has one (rules.pick_winner). Otherwise
    # copies that already have their own digest arrived first, then the rest by ingested_at, then path.
    # A Granola note's versions share its first arrival: a Resumed v2 inherits v1's stamp and v1's place
    # in `ran`, so a note that won the tie before the Resume still wins it after (rules.by_arrival).
    ran =[o.path for o in others if owners[o.path] is not None and _norm(owners[o.path].transcript) == o.path]
    tz = ctx.config.pipeline.timezone
    arrival = by_arrival([parsed, *others], first=ran, tz=tz)
    winner = pick_winner([parsed, *others], arrival, tz=tz)
    own_drop = [digest_path(own_stub.call_id)] if own_stub is not None else []

    if winner.path != parsed.path:
        host = digest_for_transcript(digests, winner.path)
        if host is None or _norm(host.transcript) == parsed.path:
            ctx.info(f"process_call: {parsed.path} loses to {winner.path}, which has not run yet")
            return _Copies(result=ProcessResult(parsed.path, "duplicate"))
        _record_duplicate(ctx, host, parsed.path, drop=own_drop)
        return _Copies(result=ProcessResult(parsed.path, "duplicate", host.call_id))

    found = [d for d in owners.values() if d is not None]
    takeover = next((d for d in found if not is_stub(d)), None)
    if takeover is not None and _is_cited(ctx, takeover.call_id, beliefs):
        _record_duplicate(ctx, takeover, parsed.path, drop=own_drop)
        return _Copies(result=ProcessResult(parsed.path, "duplicate", takeover.call_id))

    dups = {o.path for o in others}
    if takeover is not None:
        dups |= {_norm(takeover.transcript), *(_norm(p) for p in takeover.duplicates)}
    drop = sorted({digest_path(d.call_id) for d in found if is_stub(d)})
    return _Copies(takeover=takeover, duplicates=sorted(dups - {parsed.path}), drop=drop)


def _could_have_copies(parsed: ParsedTranscript) -> bool:
    """False when rules.same_call cannot match this file against anything (a dropped file with no metadata)."""
    return bool(note_version(parsed.path)[0] or (parsed.source and parsed.source_id)
                or parsed.ical_uid or parsed.start)


def _same_call_copies(ctx: Ctx, parsed: ParsedTranscript) -> list[ParsedTranscript]:
    """Every other transcript in the repo that records the same call, digested or not."""
    if not _could_have_copies(parsed):
        return []
    window = ctx.config.pipeline.t("dup_window_minutes")
    out = []
    for rel in ctx.repo.list("transcripts/*"):
        if rel == parsed.path or rel.rsplit("/", 1)[-1].startswith("."):
            continue
        try:
            other = parse(rel, ctx.repo.read(rel), commit_date=_today(ctx), tz=ctx.config.pipeline.timezone)
        except (ParseError, UnicodeDecodeError):
            continue
        except Exception as e:   # backstop: parse() raises only ParseError; one odd sibling never blocks a call
            ctx.info(f"process_call: {rel} could not be parsed ({type(e).__name__}: {e}); "
                     f"not compared with {parsed.path}")
            continue
        if same_call(parsed, other, window_minutes=window, tz=ctx.config.pipeline.timezone, config=ctx.config):
            out.append(other)
    return out


def _record_duplicate(ctx: Ctx, host: Digest, transcript: str, *, drop: list[str] | None = None) -> None:
    """Add `transcript` to the winner's `duplicates` and commit, when it is not there yet. `drop` names
    stub files of the losing copy, deleted in the same commit."""
    drop = [p for p in drop or [] if p != digest_path(host.call_id) and ctx.repo.exists(p)]
    listed = transcript in {_norm(p) for p in host.duplicates} or _norm(host.transcript) == transcript
    if listed and not drop:
        return
    paths: list[str] = []
    if not listed:
        host = copy.deepcopy(host)
        host.duplicates = sorted({*host.duplicates, transcript})
        paths.append(write_digest(ctx.repo, host))
    for p in drop:
        ctx.repo.delete(p)
    generated = _regenerate(ctx)
    _commit(ctx, f"process_call: {transcript} is a copy of {host.call_id}", [*paths, *drop, *generated])


def _is_cited(ctx: Ctx, call_id: str, beliefs: BeliefsDoc) -> bool:
    """True when any history line, verdict dismissal or rejection cites the call. Cited calls are never superseded."""
    if any(cid == call_id for e in beliefs.entries for h in e.history for cid, _ in h.sources):
        return True
    for rel in (VERDICT_OVERRIDES, REJECTED):
        try:
            data = yaml.safe_load(ctx.repo.read_or(rel, ""))
        except yaml.YAMLError:
            ctx.info(f"process_call: {rel} is not valid YAML; treating {call_id} as cited")
            return True
        if call_id in _call_ids(data):
            return True
    return False


def _call_ids(data: Any) -> set[str]:
    """Every `call_id` value anywhere in a YAML structure (the processor-written config files nest them)."""
    found: set[str] = set()
    if isinstance(data, dict):
        for k, v in data.items():
            if k == "call_id" and v:
                found.add(str(v))
            else:
                found |= _call_ids(v)
    elif isinstance(data, list):
        for v in data:
            found |= _call_ids(v)
    return found


# ---------- extraction helpers ----------

def _who(results: list[ExtractionResult]) -> Who:
    """The primary's who fields, with gaps filled from the other models (firm and firm_source travel together)."""
    who = results[0].who.model_copy()
    for r in results[1:]:
        if who.firm is None and r.who.firm is not None:
            who.firm, who.firm_source = r.who.firm, r.who.firm_source
        for name in Who.model_fields:
            if name not in ("firm", "firm_source") and getattr(who, name) is None:
                setattr(who, name, getattr(r.who, name))
    return who


def _verify(ctx: Ctx, parsed: ParsedTranscript, results: list[ExtractionResult]) -> tuple[list[list[Claim]], Verification]:
    """Verify each model's claims (one repair each); counts summed across models."""
    total = Verification()
    kept: list[list[Claim]] = []
    words = ctx.config.pipeline.t("short_answer_words")
    for r in results:
        claims, v = verify_claims(parsed, r.claims, repair=lambda c, q, reason: repair_quote(ctx, parsed, c, q, reason),
                                  short_answer_words=words, canonical=ctx.config.canonical)
        kept.append(claims)
        total.quotes += v.quotes
        total.matched_first_pass += v.matched_first_pass
        total.repaired += v.repaired
        total.dropped.extend(v.dropped)
        total.findings_dropped += v.findings_dropped
    return kept, total


def _mentions(results: list[ExtractionResult]) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {"people": [], "firms": []}
    for r in results:
        for key, names in out.items():
            for name in (r.mentions or {}).get(key) or []:
                if name and name not in names:
                    names.append(name)
    return out


def _apply_speakers(claims: list[Claim], speakers: dict[str, str]) -> list[Claim]:
    """Speakers the verdict step named on unnamed calls, set on every quote of the finding; such a
    speaker is inferred, not labeled."""
    out = []
    for c in claims:
        if c.id in speakers:
            quotes = [q.model_copy(update={"speaker": speakers[c.id], "attribution": "inferred"}) for q in c.quotes]
            c = c.model_copy(update={"quotes": quotes})
        out.append(c)
    return out


def _real_date(value: str | None) -> str | None:
    """`value` when it is a real YYYY-MM-DD date, else None."""
    try:
        return date.fromisoformat(value).isoformat() if isinstance(value, str) else None
    except ValueError:
        return None


def _prep_for(ctx: Ctx, d: Digest, prep_path: str | None):
    """The forced prep file, or the one matched by attendee email, then contact name, within the window.
    A call date that is not a real date counts as unknown, so matching never raises on it."""
    if prep_path:
        return parse_prep(prep_path, _read_given(ctx, prep_path))
    return match_prep(load_preps(ctx.repo), date=_real_date(d.date), contact=d.contact,
                      attendee_emails=[a.email for a in d.attendees if a.email], config=ctx.config,
                      window_days=ctx.config.pipeline.t("prep_window_days"))


# ---------- briefs ----------

def build_brief(ctx: Ctx, d: Digest, *, beliefs_path: str = "beliefs.md", decisions_path: str = "decisions.md",
                parsed: ParsedTranscript | None = None) -> BriefMessages:
    """Render the brief for `d` against the given beliefs and decisions, with evidence counts over every
    digest in the repo (with `d` in place of any older version of it), leaving out verdicts dismissed in
    config/verdict_overrides.yml as beliefs-status.md does. A short answer shows the question it answers,
    read from `parsed` or else from `d.transcript` (see _questions). Nothing is posted."""
    from nua import indexes   # Task 11; imported lazily

    beliefs = _load_beliefs(ctx, beliefs_path)
    decisions = parse_decisions(_read_given(ctx, decisions_path, ""))
    digests = [x for x in load_digests(ctx.repo) if x.call_id != d.call_id] + [d]
    counts = indexes.evidence_counts(digests, beliefs, overrides=indexes.load_verdict_overrides(ctx.repo))
    spans, acted = acted_on(d, beliefs)
    return render_brief(d, beliefs, decisions, counts=counts, digest_url=_digest_url(ctx, d),
                        acted_sources=acted, acted_spans=spans, questions=_questions(ctx, d, parsed))


def _questions(ctx: Ctx, d: Digest, parsed: ParsedTranscript | None = None) -> dict[int | str, str]:
    """The questions the short answers `d`'s verdicts cite answer, on one line, so the brief shows each with
    its question: {question_turn: that turn's text} for one asked in an earlier turn, and {claim id: the
    text of its own turn before it} for one asked in the same turn (a one-block Granola export, whose one
    turn runs to the end of the call). Best effort: the transcript is parsed from the repo when `parsed` is
    not given, and when it is missing, not UTF-8, a symlink or no longer parses, the map is empty and the
    brief shows the claim text instead."""
    cited = {cid for check in d.belief_checks for cid in check.claims}
    shorts = [(c, q) for c in d.claims if c.id in cited for q in c.quotes if isinstance(q.question_turn, int)]
    if not shorts:
        return {}
    if parsed is None:
        try:
            parsed = parse(d.transcript, ctx.repo.read(d.transcript), commit_date=_today(ctx),
                           tz=ctx.config.pipeline.timezone)
        except Exception as e:     # missing, not UTF-8, a symlink (UnsafePathError), or no longer parses
            ctx.info(f"brief: questions of {d.call_id} not read from {d.transcript} ({type(e).__name__}); "
                     "short answers show their claim text")
            return {}
    index = QuoteIndex(parsed)
    out: dict[int | str, str] = {}
    for c, q in shorts:
        if q.turns and q.question_turn >= q.turns[0]:
            if c.id and q.offset and c.id not in out:
                out[c.id] = " ".join(index.text_before(q.question_turn, q.offset[0]).split())
        elif q.question_turn not in out:
            out[q.question_turn] = " ".join(index.turn_text(q.question_turn).split())
    return {k: text for k, text in out.items() if text}


def post_brief(ctx: Ctx, d: Digest) -> BriefRef:
    """Post or update the brief for `d` in the calls channel and return where it lives.

    Looks for an earlier top message first (metadata keyed by the transcript path). Found for this
    transcript (a retry after a failed push): the top message and thread replies are updated in place.
    Found only through `d.brief` or a duplicate's path (a supersede): the top message is updated in
    place and every bot reply is deleted and posted again. Not found: posts a new top message and thread.
    The caller records the returned ref in the digest. Dry runs and a missing Slack client post nothing
    and return an empty BriefRef.
    """
    if not _slack_on(ctx):
        return BriefRef()
    return _post_messages(ctx, d, build_brief(ctx, d))


def _post_messages(ctx: Ctx, d: Digest, msgs: BriefMessages) -> BriefRef:
    slack = ctx.slack
    calls = ctx.channel("calls")
    channel, replace_thread = calls, False
    ts = slack.find_by_metadata(calls, BRIEF_EVENT, d.transcript)
    if ts is None and d.brief is not None and d.brief.ts:
        channel, ts, replace_thread = d.brief.channel or calls, d.brief.ts, True
    if ts is None:
        for path in d.duplicates:
            ts = slack.find_by_metadata(calls, BRIEF_EVENT, path)
            if ts is not None:
                replace_thread = True
                break
    if ts is None:
        ts = slack.post(calls, msgs.top_blocks, msgs.top_text,
                        metadata={"event_type": BRIEF_EVENT, "event_payload": {"key": d.transcript}})
        thread = [slack.post(calls, blocks, text, thread_ts=ts) for blocks, text in msgs.thread]
        return BriefRef(channel=calls, ts=ts, thread=thread)

    slack.update(channel, ts, msgs.top_blocks, msgs.top_text)
    old = [r for r in slack.replies(channel, ts) if r.get("bot")]
    if replace_thread:
        for r in old:
            slack.delete(channel, r["ts"])
        thread = [slack.post(channel, blocks, text, thread_ts=ts) for blocks, text in msgs.thread]
        return BriefRef(channel=channel, ts=ts, thread=thread)
    thread = []
    for i, (blocks, text) in enumerate(msgs.thread):
        if i < len(old):
            slack.update(channel, old[i]["ts"], blocks, text)
            thread.append(old[i]["ts"])
        else:
            thread.append(slack.post(channel, blocks, text, thread_ts=ts))
    for r in old[len(msgs.thread):]:
        slack.delete(channel, r["ts"])
    return BriefRef(channel=channel, ts=ts, thread=thread)


def _digest_url(ctx: Ctx, d: Digest) -> str | None:
    """GitHub link to the digest when the remote knows its repo (gitops.GitRemote), else None."""
    slug = getattr(ctx.remote, "repo_slug", None)
    if not slug:
        return None
    branch = getattr(ctx.remote, "branch", None) or "main"
    return f"https://github.com/{slug}/blob/{branch}/{digest_path(d.call_id)}"


def duration_minutes(parsed: ParsedTranscript) -> int | None:
    """Call length in whole minutes from the latest turn timestamp (at least 1), or None when turns
    carry no times (Granola text exports)."""
    seconds = [_seconds(t.time) for t in parsed.turns if t.time]
    seconds = [s for s in seconds if s is not None]
    if not seconds:
        return None
    return max(1, int(max(seconds) / 60 + 0.5))


def _seconds(time: str) -> int | None:
    parts = time.split(":")
    if not all(p.isdigit() for p in parts) or len(parts) not in (2, 3):
        return None
    total = 0
    for p in parts:
        total = total * 60 + int(p)
    return total


# ---------- backfill batches ----------

def batch_path(batch_id: str) -> str:
    """`config/batches/<id>.yml`, with anything unsafe in the id replaced."""
    return f"{BATCH_DIR}/{re.sub(r'[^A-Za-z0-9_.-]+', '-', batch_id)}.yml"


def load_batch(ctx: Ctx, batch_id: str) -> list[str] | None:
    """Transcript paths listed in a batch file, or None when there is no such batch."""
    data = _batch_data(ctx, batch_path(batch_id))
    if data is None:
        return None
    return [_norm(str(p)) for p in (data.get("paths") or []) if p]


def _batch_data(ctx: Ctx, rel: str) -> dict | None:
    """A batch file as a mapping, or None when there is no such file. A bare list of paths (the sweep
    accepts one too) becomes {"paths": [...]}; anything else unreadable is an empty batch."""
    if not ctx.repo.exists(rel):
        return None
    data = yaml.safe_load(ctx.repo.read(rel))
    if isinstance(data, list):
        return {"paths": data}
    return data if isinstance(data, dict) else {}


def start_backfill(ctx: Ctx, paths: list[str], batch_id: str) -> None:
    """The `backfill` task: commit `config/batches/<id>.yml`, then enqueue `process_call` per path no digest points to.

    Payloads are {"transcript": path, "batch": batch_id}. A retry keeps the batch file's paths, creation
    time and `summary_ts`. With nothing left to enqueue, the completion check runs at once. With no queue
    (local runs) each path is processed inline.
    """
    paths = _union([_norm(p) for p in paths])
    rel = batch_path(batch_id)
    old = _batch_data(ctx, rel) or {}
    created = old.get("created") or ctx.now().isoformat(timespec="seconds")
    listed = _union([_norm(str(p)) for p in old.get("paths") or [] if p], paths)
    data = {"batch": batch_id, "created": str(created), "paths": listed}
    if old.get(SUMMARY_TS):
        data[SUMMARY_TS] = str(old[SUMMARY_TS])
    ctx.repo.write(rel, frontmatter.dump_yaml(data))
    _commit(ctx, f"backfill: batch {batch_id}, {len(listed)} transcripts", [rel])

    digests = load_digests(ctx.repo)
    todo = [p for p in paths if digest_for_transcript(digests, p) is None]
    if ctx.queue is None:
        for p in todo:
            process_call(ctx, p, batch=batch_id)
    else:
        for p in todo:
            ctx.queue.enqueue("process_call", {"transcript": p, "batch": batch_id},
                              name=task_name("process_call", p))
    if not todo:
        batch_completion_check(ctx, batch_id)


def batch_completion_check(ctx: Ctx, batch_id: str) -> bool:
    """True when every path in the batch has a digest or stub (or its file is gone).

    A batch file that records `summary_ts` is done: True at once, with no Slack scan. Otherwise, when
    complete and no Slack message carries `batch:<id>` metadata, posts the one summary: counts, failed
    stubs and the fix-up list (unknown dates, unknown firms). The summary's ts (posted or found) is then
    written to the batch file as `summary_ts` and committed, so later checks and sweeps never look for it
    again (the metadata lookup only reaches the channel's latest messages). Dry runs post and record nothing.
    """
    rel = batch_path(batch_id)
    data = _batch_data(ctx, rel)
    if data is None:
        return False
    if data.get(SUMMARY_TS):
        return True
    paths = [_norm(str(p)) for p in (data.get("paths") or []) if p]
    digests = load_digests(ctx.repo)
    found = {p: digest_for_transcript(digests, p) for p in paths}
    if any(d is None and ctx.repo.exists(p) for p, d in found.items()):
        return False
    if not _slack_on(ctx):
        return True
    channel, key = ctx.channel("calls"), f"batch:{batch_id}"
    ts = ctx.slack.find_by_metadata(channel, BATCH_EVENT, key)
    if ts is None:
        blocks, text = _batch_summary(batch_id, found)
        ts = ctx.slack.post(channel, blocks, text, metadata={"event_type": BATCH_EVENT, "event_payload": {"key": key}})
    if ts:
        _record_summary_ts(ctx, batch_id, rel, str(ts))
    return True


def _record_summary_ts(ctx: Ctx, batch_id: str, rel: str, ts: str) -> None:
    """Write `summary_ts` into the batch file (re-read, so paths added meanwhile are kept) and commit it."""
    data = _batch_data(ctx, rel)
    if data is None or data.get(SUMMARY_TS):
        return
    data[SUMMARY_TS] = ts
    ctx.repo.write(rel, frontmatter.dump_yaml(data))
    _commit(ctx, f"backfill: record summary of batch {batch_id}", [rel])


def _batches_with(ctx: Ctx, transcript: str) -> list[str]:
    """Ids of the batch files that list `transcript`."""
    return [batch_id for batch_id, _ in _batch_files_with(ctx, transcript)]


def _batch_files_with(ctx: Ctx, transcript: str) -> list[tuple[str, dict]]:
    """(batch id, batch file data) for every batch file that lists `transcript`."""
    out = []
    for rel in ctx.repo.list(f"{BATCH_DIR}/*.yml"):
        try:
            data = yaml.safe_load(ctx.repo.read(rel)) or {}
        except (yaml.YAMLError, UnicodeDecodeError):
            continue
        if not isinstance(data, dict):
            continue
        if transcript in {_norm(str(p)) for p in data.get("paths") or []}:
            out.append((str(data.get("batch") or rel.rsplit("/", 1)[-1][:-4]), data))
    return out


def _batch_summary(batch_id: str, found: dict[str, Digest | None]) -> tuple[list[dict], str]:
    """(blocks, text) of a finished batch: counts, then failures, then the fix-up list."""
    counts: dict[str, int] = {}
    failures: list[str] = []
    unreadable: list[str] = []
    fixups: list[str] = []
    for path, d in found.items():
        if d is None:
            counts["missing"] = counts.get("missing", 0) + 1
            continue
        if _norm(d.transcript) != path:
            counts["duplicate copies"] = counts.get("duplicate copies", 0) + 1
            continue
        label = d.status.replace("_", " ")
        counts[label] = counts.get(label, 0) + 1
        shown = path.replace(EM_DASH, "-")        # a dropped file's name may carry an em dash
        if is_unreadable(d):
            unreadable.append(f"- {shown}: {_one_line(d.reason or digest_path(d.call_id))}")
        elif d.status in ("failed", "parse_failed"):
            failures.append(f"- {shown} ({label}): {_one_line(d.reason or 'no reason recorded')}")
        elif d.status == "digested":
            missing = [w for w, gone in (("unknown date", d.date is None or d.date_source == "unknown"),
                                         ("unknown firm", not d.firm)) if gone]
            if missing:
                fixups.append(f"- {d.call_id} ({shown}): {', '.join(missing)}")
    order = ["digested", "duplicate copies", "internal", "parse failed", "failed", "unreadable", "missing"]
    parts = [f"{counts[k]} {k}" for k in order if counts.get(k)]
    total = f"{len(found)} transcript" + ("" if len(found) == 1 else "s")
    head = f"Backfill batch {batch_id[:12]} done: {total}. {', '.join(parts) or 'nothing processed'}."
    lines = [head]
    if failures:
        lines += ["", "Failed (Re-run from #discovery-ops once fixed):", *failures]
    if unreadable:
        lines += ["", "Digest file cannot be read (fix it by hand; it is never overwritten):", *unreadable]
    if fixups:
        lines += ["", "Fix-up list (set in config/overrides.yml):", *fixups]
    if not failures and not unreadable and not fixups:
        lines += ["", "Nothing to fix up."]
    text = "\n".join(lines)
    return _sections(lines), text


# ---------- small helpers ----------

def _slack_on(ctx: Ctx) -> bool:
    return not ctx.dry_run and ctx.slack is not None


def _commit(ctx: Ctx, message: str, paths: list[str]) -> None:
    """Commit and push unless this is a dry run; on a rejected push the remote regenerates the indexes."""
    if ctx.dry_run:
        return
    ctx.remote.commit_and_push(message, _union(paths), regenerate=lambda: _regenerate(ctx))


def _regenerate(ctx: Ctx) -> list[str]:
    from nua import indexes   # Task 11; imported lazily
    return list(indexes.regenerate(ctx))


def _load_beliefs(ctx: Ctx, path: str) -> BeliefsDoc:
    return parse_beliefs(_read_given(ctx, path, EMPTY_BELIEFS))


def _read_given(ctx: Ctx, path: str, default: str | None = None) -> str:
    """A file the caller of process_call named: the CLI's --beliefs, --decisions or --prep, or a golden
    test's file (`default` when it is missing; None means it must exist). A path inside the repo is read
    through ctx.repo, so never through a symlink. An absolute path outside the repo is the operator's own
    choice on the command line and is read as given. Paths from repo data (transcripts, digest fields,
    batch files, task payloads) never come here: the runner passes only the defaults."""
    p = Path(path)
    if p.is_absolute():
        root = ctx.repo.root.resolve()
        real = p.resolve()
        if real == root or root in real.parents:
            path = real.relative_to(root).as_posix()
        else:
            if default is not None and not p.is_file():
                return default
            return p.read_text(encoding="utf-8")
    return ctx.repo.read(path) if default is None else ctx.repo.read_or(path, default)


def _origin_of(raw: str) -> str:
    """live when the file's front-matter names a recorder source, else backfill (for files that do not parse)."""
    try:
        fm, _, _ = frontmatter.split(raw)
    except Exception:   # yaml.YAMLError (frontmatter.load_yaml wraps the rest), and anything else as a backstop
        return "backfill"
    return "live" if str(fm.get("source") or "").strip().lower() in LIVE_SOURCES else "backfill"


def _today(ctx: Ctx) -> str:
    return ctx.now().astimezone(ctx.tz).date().isoformat()


def _norm(path: str) -> str:
    return path[2:] if path.startswith("./") else path


def _union(*lists: list[str]) -> list[str]:
    out: list[str] = []
    for items in lists:
        for item in items:
            if item not in out:
                out.append(item)
    return out


def _one_line(text: str) -> str:
    return " ".join(str(text).split()).replace(EM_DASH, "-")


def _reason(error: str) -> str:
    text = _one_line(error)
    return text if len(text) <= MAX_REASON else text[:MAX_REASON - 3].rstrip() + "..."


def _section(text: str) -> dict:
    return {"type": "section", "text": {"type": "mrkdwn", "text": _esc(text)[:MAX_SECTION]}}


def _esc(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _sections(lines: list[str]) -> list[dict]:
    """Section blocks holding `lines`, each under the section size limit, at most MAX_BLOCKS of them."""
    chunks: list[list[str]] = [[]]
    size = 0
    for line in lines:
        if not line:
            if chunks[-1]:
                chunks.append([])
                size = 0
            continue
        line = line[:MAX_SECTION]
        if chunks[-1] and size + len(line) + 1 > MAX_SECTION:
            chunks.append([])
            size = 0
        chunks[-1].append(line)
        size += len(line) + 1
    chunks = [c for c in chunks if c]
    if len(chunks) > MAX_BLOCKS:
        rest = sum(len(c) for c in chunks[MAX_BLOCKS - 1:])
        chunks = chunks[:MAX_BLOCKS - 1] + [[f"... and {rest} more lines in the batch file's digests."]]
    return [_section("\n".join(c)) for c in chunks]
