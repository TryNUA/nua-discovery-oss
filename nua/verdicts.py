"""Verdicts: how a call's consolidated findings (`Claim`, 1 to 3 quotes each) bear on current beliefs and
Watching items. Verdicts cite finding ids; the prompts show every quote of a finding.

Both extraction models answer independently (nua/prompts/verdict.py). `combine` compares them per
(entry, claim) pair, per prep question and per claim speaker:
- both agree: accepted, `decided_by: agree`
- they disagree, including one model linking a claim and the other not: one batched call to the
  arbiter (nua/prompts/arbiter.py), `decided_by: arbiter`
- arbiter unavailable: both answers kept, `decided_by: unarbitrated`, degraded `no_arbiter`
- only one model answered: `decided_by: single`, degraded `one_model`

Every matched prep question gets a coverage entry (SPEC step 10). A question no model returned is a
coverage dispute with every vote `none`, so the arbiter rules on it; left unruled it stays open as
`never_asked` (`unarbitrated`, or `single` when only one model answered).
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Annotated, Any, Callable, Iterable

from pydantic import WithJsonSchema

from nua.context import Ctx
from nua.extract import (call_values, clean_text, fill, numbered_transcript, run_parallel,
                         scope_text, turns_excerpt)
from nua.llm import LLMError
from nua.prompts import arbiter as arbiter_prompt
from nua.prompts import verdict as verdict_prompt
from nua.schemas import Objects, Reply, Text, Texts, choice, parse_reply, strict_schema
from nua.types import COVERAGE, VERDICTS, BeliefCheck, Claim, Entry, ParsedTranscript, QuestionCoverage

VERDICT_MAX_TOKENS = 8_000
ARBITER_MAX_TOKENS = 8_000
CONTEXT_RADIUS = 2
SEVERITY = ("contradicts", "scope_mismatch", "supports")   # unarbitrated: surface the most serious reading
KEEP_OPEN = ("dodged", "never_asked", "answered")           # unarbitrated coverage: keep the question open


# ---------- reply models (nua/schemas.py) ----------

class CheckVote(Reply):
    target: Text = ""
    verdict: choice(VERDICTS) = None
    claims: Texts = []
    reason: Text = ""


class CoverageVote(Reply):
    question: Annotated[Any, WithJsonSchema({"type": "integer"})] = None   # a number, "Q1" or the question text
    status: choice(COVERAGE) = None
    claims: Texts = []
    reason: Text = ""


class SpeakerVote(Reply):
    claim: Text = ""
    speaker: Text = ""


class VerdictReply(Reply):
    belief_checks: Objects[CheckVote] = []
    question_coverage: Objects[CoverageVote] = []
    speakers: Objects[SpeakerVote] = []


class CheckRuling(Reply):
    id: Text = ""
    verdict: choice((*VERDICTS, "none")) = None
    reason: Text = ""


class CoverageRuling(Reply):
    id: Text = ""
    status: choice(COVERAGE) = None
    claims: Texts = []
    reason: Text = ""


class SpeakerRuling(Reply):
    id: Text = ""
    speaker: Text = ""


class ArbiterReply(Reply):
    belief_checks: Objects[CheckRuling] = []
    question_coverage: Objects[CoverageRuling] = []
    speakers: Objects[SpeakerRuling] = []


VERDICT_SCHEMA = strict_schema(VerdictReply)
ARBITER_SCHEMA = strict_schema(ArbiterReply)


@dataclass
class VerdictOutcome:
    belief_checks: list[BeliefCheck] = field(default_factory=list)
    question_coverage: list[QuestionCoverage] = field(default_factory=list)
    speakers: dict[str, str] = field(default_factory=dict)   # claim id -> who said it (unnamed calls only)
    degraded: list[str] = field(default_factory=list)        # one_model | no_arbiter


# ---------- combine ----------

def _union(lists: Iterable[Iterable[str]]) -> list[str]:
    out: list[str] = []
    for ids in lists:
        for i in ids or []:
            if i not in out:
                out.append(i)
    return out


def _ask_arbiter(arbiter: Callable[[dict], dict] | None, disputes: dict) -> dict[str, dict] | None:
    """Rulings by dispute id, or None when the arbiter is missing or fails."""
    if arbiter is None:
        return None
    try:
        out = arbiter(disputes) or {}
    except LLMError:
        return None
    rulings: dict[str, dict] = {}
    for kind in ("belief_checks", "question_coverage", "speakers"):
        for r in out.get(kind) or []:
            if isinstance(r, dict) and r.get("id"):
                rulings[str(r["id"])] = r
    return rulings


def combine(votes: dict[str, dict], arbiter: Callable[[dict], dict] | None,
            questions: Iterable[str] = ()) -> VerdictOutcome:
    """Merge verdict votes from the extraction models. Pure apart from the arbiter call.

    `votes`: model spec -> {"belief_checks": [{"target", "verdict", "claims", "reason"}],
    "question_coverage": [{"question": <text>, "status", "claims", "reason"}],
    "speakers": [{"claim", "speaker"}]}, with ids already checked. The first model is primary.
    `questions` are the matched prep questions: each one gets a coverage entry, in this order, even
    when no model returned it (a dispute with every vote "none"; with one model, `never_asked`).

    `arbiter` is called at most once, with every disagreement:
    {"belief_checks": [{"id", "target", "claim", "votes": {model: {"verdict" | "none", "reason"}}}],
     "question_coverage": [{"id", "question", "votes": {model: {"status" | "none", "claims", "reason"}}}],
     "speakers": [{"id", "claim", "votes": {model: name | "none"}}]}
    and returns rulings by id: {"belief_checks": [{"id", "verdict" (or "none"), "reason"}],
    "question_coverage": [{"id", "status", "claims"}], "speakers": [{"id", "speaker"}]}.
    None or an LLMError means the arbiter is down: disputes become `unarbitrated`.

    Unarbitrated belief checks keep both votes and take the most serious verdict either model gave;
    unarbitrated coverage keeps the question open (`never_asked` when no model gave a status); an
    unarbitrated speaker is left out.
    """
    models = list(votes)
    if not models:
        return VerdictOutcome()
    single = len(models) == 1
    disputes: dict[str, list[dict]] = {"belief_checks": [], "question_coverage": [], "speakers": []}

    # belief checks, per (target, claim) pair
    pairs: dict[tuple[str, str], dict[str, tuple[str, str]]] = {}
    for m in models:
        for check in votes[m].get("belief_checks") or []:
            for cid in check.get("claims") or []:
                pairs.setdefault((check["target"], cid), {}).setdefault(m, (check["verdict"], check.get("reason") or ""))
    decided: list[tuple[str, str, str, str, dict[str, str], str]] = []   # target, claim, verdict, by, votes, note
    open_pairs: list[tuple[str, str, str, dict, dict[str, str]]] = []
    for (target, cid), got in pairs.items():
        vote_map = {m: got[m][0] if m in got else "none" for m in models}
        first_reason = next((got[m][1] for m in models if m in got and got[m][1]), "")
        if single:
            decided.append((target, cid, got[models[0]][0], "single", vote_map, first_reason))
        elif len(got) == len(models) and len({v for v, _ in got.values()}) == 1:
            decided.append((target, cid, got[models[0]][0], "agree", vote_map, first_reason))
        else:
            did = f"b{len(open_pairs) + 1}"
            open_pairs.append((did, target, cid, got, vote_map))
            disputes["belief_checks"].append({
                "id": did, "target": target, "claim": cid,
                "votes": {m: {"verdict": vote_map[m], "reason": got[m][1] if m in got else ""} for m in models},
            })

    # question coverage, per question: every prep question, then any other the votes name
    asked: dict[str, dict[str, dict]] = {q: {} for q in questions}
    for m in models:
        for q in votes[m].get("question_coverage") or []:
            asked.setdefault(q["question"], {}).setdefault(m, q)
    coverage: dict[str, QuestionCoverage] = {}
    open_questions: list[tuple[str, str, dict]] = []
    for text, got in asked.items():
        statuses = {g["status"] for g in got.values()}
        claims = _union(got[m].get("claims") for m in models if m in got)
        if single and not got:        # the one model left skipped it: nobody says it was asked
            coverage[text] = QuestionCoverage(question=text, status="never_asked", claims=[], decided_by="single")
        elif single or (len(got) == len(models) and len(statuses) == 1):
            coverage[text] = QuestionCoverage(question=text, status=got[models[0]]["status"], claims=claims,
                                              decided_by="single" if single else "agree")
        else:
            did = f"q{len(open_questions) + 1}"
            open_questions.append((did, text, got))
            disputes["question_coverage"].append({
                "id": did, "question": text,
                "votes": {m: {"status": got[m]["status"] if m in got else "none",
                              "claims": list(got[m].get("claims") or []) if m in got else [],
                              "reason": got[m].get("reason", "") if m in got else ""} for m in models},
            })

    # speakers, per claim
    said: dict[str, dict[str, str]] = {}
    for m in models:
        for s in votes[m].get("speakers") or []:
            said.setdefault(s["claim"], {}).setdefault(m, s["speaker"].strip())
    speakers: dict[str, str] = {}
    open_speakers: list[tuple[str, str]] = []
    for cid, got in said.items():
        if single or (len(got) == len(models) and len({n.lower() for n in got.values()}) == 1):
            speakers[cid] = next(got[m] for m in models if m in got)
        else:
            did = f"s{len(open_speakers) + 1}"
            open_speakers.append((did, cid))
            disputes["speakers"].append({"id": did, "claim": cid, "votes": {m: got.get(m, "none") for m in models}})

    rulings = _ask_arbiter(arbiter, disputes) if any(disputes.values()) else {}
    unarbitrated = False

    for did, target, cid, got, vote_map in open_pairs:
        r = (rulings or {}).get(did)
        if r is not None and r.get("verdict") in (*VERDICTS, "none"):
            if r["verdict"] != "none":
                decided.append((target, cid, r["verdict"], "arbiter", vote_map, clean_text(r.get("reason"))))
            continue
        unarbitrated = True
        verdict = next(v for v in SEVERITY if v in vote_map.values())
        note = "; ".join(f"{m}: {got[m][1]}" for m in models if m in got and got[m][1])
        decided.append((target, cid, verdict, "unarbitrated", vote_map, note))

    for did, text, got in open_questions:
        r = (rulings or {}).get(did)
        if r is not None and r.get("status") in COVERAGE:
            coverage[text] = QuestionCoverage(question=text, status=r["status"], claims=list(r.get("claims") or []),
                                              decided_by="arbiter")
            continue
        unarbitrated = True
        statuses = {g["status"] for g in got.values()}
        coverage[text] = QuestionCoverage(question=text,
                                          status=next((s for s in KEEP_OPEN if s in statuses), "never_asked"),
                                          claims=_union(g.get("claims") for g in got.values()),
                                          decided_by="unarbitrated")

    for did, cid in open_speakers:
        r = (rulings or {}).get(did)
        name = clean_text(r.get("speaker")) if r is not None else ""
        if name:
            speakers[cid] = name
        else:
            unarbitrated = True

    return VerdictOutcome(
        belief_checks=_group(decided),
        question_coverage=[coverage[q] for q in asked],
        speakers=speakers,
        degraded=["no_arbiter"] if unarbitrated else [],
    )


def _group(decided: list[tuple[str, str, str, str, dict[str, str], str]]) -> list[BeliefCheck]:
    """One BeliefCheck per target, verdict, decided_by and vote pattern, claims and notes merged."""
    groups: dict[tuple, BeliefCheck] = {}
    for target, cid, verdict, by, vote_map, note in decided:
        key = (target, verdict, by, tuple(vote_map.items()))
        check = groups.get(key)
        if check is None:
            groups[key] = BeliefCheck(target=target, verdict=verdict, claims=[cid], decided_by=by,
                                      votes=dict(vote_map), note=note)
            continue
        if cid not in check.claims:
            check.claims.append(cid)
        if note and note not in check.note:
            check.note = f"{check.note} / {note}" if check.note else note
    for check in groups.values():
        check.claims.sort()
    return sorted(groups.values(), key=lambda c: (c.target, c.claims[0], SEVERITY.index(c.verdict)))


# ---------- cleaning model replies ----------

def _entry_id(value: Any) -> str:
    """"b-3" or "B003" -> "B-003"; anything else is returned stripped."""
    s = str(value or "").strip()
    m = re.fullmatch(r"([BbWw])-?0*(\d+)", s)
    return f"{m.group(1).upper()}-{int(m.group(2)):03d}" if m else s


def _ids(values: Any, known: set[str]) -> list[str]:
    return _union([[v for v in (str(x).strip() for x in (values or [])) if v in known]])


def _question(value: Any, questions: list[str]) -> str | None:
    """Map a question number (1-based, "1" or "Q1") or its exact text back to the question."""
    m = re.fullmatch(r"\s*[Qq]?(\d+)\s*", str(value)) if value is not None and not isinstance(value, bool) else None
    if m and 1 <= int(m.group(1)) <= len(questions):
        return questions[int(m.group(1)) - 1]
    s = str(value or "").strip().lower()
    return next((q for q in questions if q.strip().lower() == s), None)


def _clean_vote(raw: Any, claim_ids: set[str], entry_ids: set[str], questions: list[str],
                need_speakers: bool) -> dict:
    """Keep only known entries, claims and questions from one model's verdict reply."""
    reply = parse_reply(VerdictReply, raw, "verdict reply")
    checks = []
    for c in reply.belief_checks:
        target, cids = _entry_id(c.target), _ids(c.claims, claim_ids)
        if target in entry_ids and c.verdict is not None and cids:
            checks.append({"target": target, "verdict": c.verdict, "claims": cids, "reason": clean_text(c.reason)})
    coverage = []
    for q in reply.question_coverage:
        text = _question(q.question, questions)
        if text and q.status is not None:
            coverage.append({"question": text, "status": q.status, "claims": _ids(q.claims, claim_ids),
                             "reason": clean_text(q.reason)})
    speakers = []
    for s in reply.speakers if need_speakers else []:
        if s.claim.strip() in claim_ids and clean_text(s.speaker):
            speakers.append({"claim": s.claim.strip(), "speaker": clean_text(s.speaker)})
    return {"belief_checks": checks, "question_coverage": coverage, "speakers": speakers}


def _clean_rulings(raw: Any, claim_ids: set[str]) -> dict:
    reply = parse_reply(ArbiterReply, raw, "arbiter reply")
    return {
        "belief_checks": [{"id": r.id, "verdict": r.verdict, "reason": clean_text(r.reason)}
                          for r in reply.belief_checks],
        "question_coverage": [{"id": r.id, "status": r.status, "claims": _ids(r.claims, claim_ids),
                               "reason": clean_text(r.reason)} for r in reply.question_coverage],
        "speakers": [{"id": r.id, "speaker": clean_text(r.speaker)} for r in reply.speakers],
    }


# ---------- prompt text ----------

def _entry_text(e: Entry) -> str:
    kind = "belief" if e.section == "beliefs" else "watching"
    return f"{e.id} ({kind}): {e.statement}\n  scope: {scope_text(e.scope)}"


def _claim_head(c: Claim) -> str:
    turns = ", ".join(f"T{t}" for t in c.all_turns) or "turn unknown"
    return f"{c.id} [{turns}]: {c.text}"


def _quote_lines(c: Claim) -> list[str]:
    """One `quote (T12, speaker): "..."` line per quote, each short answer followed by where its question is."""
    lines = []
    for q in c.quotes:
        where = ", ".join(f"T{t}" for t in q.turns) or "turn unknown"
        lines.append(f'  quote ({where}, {q.speaker or "unlabeled"}): "{" ".join(q.quote.split())}"')
        qt = q.question_turn
        if isinstance(qt, int) and q.turns and qt >= q.turns[0]:   # asked in the answer's own turn (one-block export)
            lines.append(f"    answering the question just before the quote in T{qt}")
        elif qt is not None:
            lines.append(f"    answering the question at T{qt}")
    return lines


def _claim_text(c: Claim) -> str:
    lines = [_claim_head(c), *_quote_lines(c)]
    lines.append(f"  scope: {scope_text(c.scope)}")
    flags = [name for name in ("hedged", "firsthand", "prompted") if getattr(c.flags, name)]
    if flags:
        lines.append(f"  flags: {', '.join(flags)}")
    return "\n".join(lines)


def _claim_turns(claims: Iterable[Claim]) -> list[int]:
    out: list[int] = []
    for c in claims:
        for q in c.quotes:
            out.extend(q.turns)
            if q.question_turn is not None:
                out.append(q.question_turn)
    return out


def _context(parsed: ParsedTranscript | None, claims: Iterable[Claim], *, whole: bool) -> str:
    if parsed is None:
        return "(not available)"
    if whole:
        return numbered_transcript(parsed)
    return turns_excerpt(parsed, _claim_turns(claims), radius=CONTEXT_RADIUS) or "(no turns cited)"


def _disputes_text(disputes: dict, claims: dict[str, Claim], entries: dict[str, Entry]) -> str:
    """Readable disagreements for the arbiter. Models appear as Reader 1, Reader 2 in a fixed order."""
    readers: dict[str, str] = {}

    def reader(model: str) -> str:
        return readers.setdefault(model, f"Reader {len(readers) + 1}")

    blocks = []
    for d in disputes["belief_checks"]:
        e, c = entries[d["target"]], claims[d["claim"]]
        lines = [f"#### {d['id']}: does claim {c.id} bear on {e.id}?", _entry_text(e), _claim_text(c)]
        for m, v in d["votes"].items():
            said = "no link" if v["verdict"] == "none" else v["verdict"]
            lines.append(f"{reader(m)}: {said}." + (f" Reason: {v['reason']}" if v["reason"] else ""))
        blocks.append("\n".join(lines))
    for d in disputes["question_coverage"]:
        lines = [f"#### {d['id']}: prep question", f"Question: {d['question']}"]
        if all(v["status"] == "none" for v in d["votes"].values()):
            lines.append("Neither reader gave this question a status. Decide from the transcript.")
        for m, v in d["votes"].items():
            said = "no answer" if v["status"] == "none" else v["status"]
            ids = f", claims {', '.join(v['claims'])}" if v["claims"] else ""
            lines.append(f"{reader(m)}: {said}{ids}." + (f" Reason: {v['reason']}" if v["reason"] else ""))
        blocks.append("\n".join(lines))
    for d in disputes["speakers"]:
        lines = [f"#### {d['id']}: who said claim {d['claim']}?", _claim_text(claims[d["claim"]])]
        for m, name in d["votes"].items():
            lines.append(f"{reader(m)}: {'no answer' if name == 'none' else name}")
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks)


# ---------- run ----------

def _arbiter(ctx: Ctx, spec: str, parsed: ParsedTranscript | None, claims: dict[str, Claim],
             entries: dict[str, Entry]) -> Callable[[dict], dict]:
    def call(disputes: dict) -> dict:
        involved = [claims[d["claim"]] for kind in ("belief_checks", "speakers") for d in disputes[kind]]
        values = {
            **call_values(ctx, parsed),
            "disputes": _disputes_text(disputes, claims, entries),
            "transcript": _context(parsed, involved, whole=bool(disputes["question_coverage"])),
        }
        raw = ctx.llms(spec).complete_json(system=fill(arbiter_prompt.SYSTEM, values),
                                           prompt=fill(arbiter_prompt.ARBITER, values),
                                           schema=ARBITER_SCHEMA, max_tokens=ARBITER_MAX_TOKENS)
        return _clean_rulings(raw, set(claims))
    return call


def run_verdicts(ctx: Ctx, parsed: ParsedTranscript | None, claims: list[Claim], entries: list[Entry],
                 questions: list[str], *, source: str = "ingest", models: list[str] | None = None) -> VerdictOutcome:
    """Run the verdict step: both extraction models in parallel, the arbiter on disagreement.

    `claims` are merged claims with ids. `entries` are checked only when current (Beliefs and
    Watching). `questions` are the matched prep questions; each one gets a coverage entry. `parsed`
    gives surrounding turns (the whole transcript when there are questions) and is None when the
    transcript is not at hand (backcheck).
    `models` limits the step to those extractors (process_call passes the ones whose extraction
    succeeded, so a model that is down is not waited on twice); a skipped extractor counts as down.
    Speakers are asked for only on calls whose labels are not `named`. No model is called when there
    is nothing to check. Raises LLMError when every verdict model fails.
    """
    if any(not c.id for c in claims):
        raise ValueError("run_verdicts needs merged claims with ids")
    entries = [e for e in entries if e.is_current]
    questions = [q.strip() for q in questions if q and q.strip()]
    need_speakers = parsed is not None and parsed.speaker_labels != "named" and bool(claims)
    if not ((entries and claims) or questions or need_speakers):
        return VerdictOutcome()

    values = {
        **call_values(ctx, parsed),
        "entries": "\n".join(_entry_text(e) for e in entries) or "(none)",
        "claims": "\n".join(_claim_text(c) for c in claims) or "(none)",
        "questions": "\n".join(f"{i}. {q}" for i, q in enumerate(questions, 1)) or "(none)",
        "transcript": _context(parsed, claims, whole=bool(questions)),
    }
    system, prompt = fill(verdict_prompt.SYSTEM, values), fill(verdict_prompt.VERDICT, values)
    claim_ids, entry_ids = {c.id for c in claims}, {e.id for e in entries}

    def run(spec: str) -> dict:
        raw = ctx.llms(spec).complete_json(system=system, prompt=prompt, schema=VERDICT_SCHEMA,
                                           max_tokens=VERDICT_MAX_TOKENS)
        return _clean_vote(raw, claim_ids, entry_ids, questions, need_speakers)

    extractors = list(ctx.config.pipeline.extractors)
    specs = [s for s in extractors if models is None or s in models]
    skipped = [s for s in extractors if s not in specs]
    votes, errors = {}, [f"{s}: skipped, it failed earlier in this task" for s in skipped]
    for spec, vote, err in run_parallel(specs, run) if specs else []:
        if err is None:
            votes[spec] = vote
        else:
            errors.append(f"{spec}: {err}")
            ctx.info(f"verdicts: {spec} failed: {err}")
    if not votes:
        raise LLMError("every verdict model failed: " + ("; ".join(errors) or "no model to ask"))

    spec = ctx.config.pipeline.arbiter
    arbiter = _arbiter(ctx, spec, parsed, {c.id: c for c in claims}, {e.id: e for e in entries}) if spec else None
    out = combine(votes, arbiter, questions)
    for check in out.belief_checks:
        check.source = source
    order = {q: i for i, q in enumerate(questions)}
    out.question_coverage.sort(key=lambda q: order.get(q.question, len(order)))
    if errors:
        out.degraded = ["one_model", *out.degraded]
    return out
