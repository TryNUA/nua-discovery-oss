"""Tests for nua/verdicts.py: the pure `combine` and `run_verdicts` with FakeLLMs. Offline."""
from __future__ import annotations

import importlib

import pytest

from nua import verdicts as vd
from nua.llm import FakeLLM, LLMError
from nua.types import Attendee, Claim, Entry, HistoryLine, ParsedTranscript, Scope, Turn

A, B = "model:a", "model:b"


def never_called(disputes):
    raise AssertionError("the arbiter must not be called")


def bc(target, verdict, claims, reason=""):
    return {"target": target, "verdict": verdict, "claims": list(claims), "reason": reason}


def vote(*checks, coverage=(), speakers=()):
    return {"belief_checks": list(checks), "question_coverage": list(coverage), "speakers": list(speakers)}


# ---------- combine: belief checks ----------

def test_agree_is_accepted_without_arbiter():
    out = vd.combine({A: vote(bc("B-003", "contradicts", ["c04"], "carrier view")),
                      B: vote(bc("B-003", "contradicts", ["c04"], "same"))}, never_called)
    [check] = out.belief_checks
    assert (check.target, check.verdict, check.claims, check.decided_by) == ("B-003", "contradicts", ["c04"], "agree")
    assert check.votes == {A: "contradicts", B: "contradicts"}
    assert check.note == "carrier view"
    assert out.degraded == []


def test_agreeing_claims_group_into_one_check():
    out = vd.combine({A: vote(bc("B-001", "supports", ["c02", "c01"])),
                      B: vote(bc("B-001", "supports", ["c01"]), bc("B-001", "supports", ["c02"]))}, never_called)
    [check] = out.belief_checks
    assert check.claims == ["c01", "c02"] and check.decided_by == "agree"


def test_different_verdicts_go_to_the_arbiter():
    seen = {}

    def arbiter(disputes):
        seen.update(disputes)
        d = disputes["belief_checks"][0]
        return {"belief_checks": [{"id": d["id"], "verdict": "scope_mismatch", "reason": "different party"}],
                "question_coverage": [], "speakers": []}

    out = vd.combine({A: vote(bc("B-003", "contradicts", ["c04"], "r1")),
                      B: vote(bc("B-003", "supports", ["c04"], "r2"))}, arbiter)
    [d] = seen["belief_checks"]
    assert d["target"] == "B-003" and d["claim"] == "c04"
    assert d["votes"] == {A: {"verdict": "contradicts", "reason": "r1"}, B: {"verdict": "supports", "reason": "r2"}}
    [check] = out.belief_checks
    assert (check.verdict, check.decided_by, check.note) == ("scope_mismatch", "arbiter", "different party")
    assert check.votes == {A: "contradicts", B: "supports"}
    assert out.degraded == []


def test_claim_linked_by_only_one_model_goes_to_the_arbiter():
    calls = []

    def arbiter(disputes):
        calls.append(disputes)
        return {"belief_checks": [{"id": disputes["belief_checks"][0]["id"], "verdict": "supports", "reason": "ok"}],
                "question_coverage": [], "speakers": []}

    out = vd.combine({A: vote(bc("W-001", "supports", ["c01"])), B: vote()}, arbiter)
    assert len(calls) == 1
    assert calls[0]["belief_checks"][0]["votes"][B] == {"verdict": "none", "reason": ""}
    [check] = out.belief_checks
    assert check.decided_by == "arbiter" and check.votes == {A: "supports", B: "none"}


def test_arbiter_ruling_none_records_no_check():
    def arbiter(disputes):
        return {"belief_checks": [{"id": disputes["belief_checks"][0]["id"], "verdict": "none", "reason": "unrelated"}]}

    out = vd.combine({A: vote(bc("B-002", "contradicts", ["c03"])), B: vote()}, arbiter)
    assert out.belief_checks == [] and out.degraded == []


@pytest.mark.parametrize("arbiter", [None, lambda disputes: (_ for _ in ()).throw(LLMError("arbiter down"))])
def test_arbiter_down_keeps_both_answers_unarbitrated(arbiter):
    out = vd.combine({A: vote(bc("B-003", "supports", ["c04"], "because x")),
                      B: vote(bc("B-003", "contradicts", ["c04"], "because y"))}, arbiter)
    [check] = out.belief_checks
    assert check.decided_by == "unarbitrated"
    assert check.verdict == "contradicts"                  # the more serious reading is surfaced
    assert check.votes == {A: "supports", B: "contradicts"}
    assert "because x" in check.note and "because y" in check.note
    assert out.degraded == ["no_arbiter"]


def test_missing_ruling_is_unarbitrated():
    out = vd.combine({A: vote(bc("B-001", "supports", ["c01"])), B: vote()},
                     lambda disputes: {"belief_checks": [], "question_coverage": [], "speakers": []})
    [check] = out.belief_checks
    assert check.decided_by == "unarbitrated" and check.verdict == "supports"
    assert out.degraded == ["no_arbiter"]


def test_one_model_gives_single():
    out = vd.combine({A: vote(bc("B-003", "contradicts", ["c04"], "r"), bc("W-001", "supports", ["c01"]))}, never_called)
    assert [(c.target, c.decided_by, c.votes) for c in out.belief_checks] == [
        ("B-003", "single", {A: "contradicts"}), ("W-001", "single", {A: "supports"})]
    assert out.degraded == []


def test_no_votes_gives_empty_outcome():
    out = vd.combine({}, never_called)
    assert out == vd.VerdictOutcome([], [], {}, [])


# ---------- combine: question coverage and speakers ----------

def qc(question, status, claims=(), reason=""):
    return {"question": question, "status": status, "claims": list(claims), "reason": reason}


Q1, Q2, Q3 = "How often is there video?", "What is in the claim file?", "Who pulls the store footage?"


def test_question_coverage_combines_like_verdicts():
    rulings = {}

    def arbiter(disputes):
        rulings["seen"] = disputes["question_coverage"]
        return {"belief_checks": [], "speakers": [],
                "question_coverage": [{"id": d["id"], "status": "dodged", "claims": ["c09"], "reason": "T40"}
                                      for d in disputes["question_coverage"]]}

    out = vd.combine({
        A: vote(coverage=[qc(Q1, "answered", ["c01"]), qc(Q2, "never_asked"), qc(Q3, "answered", ["c05"])]),
        B: vote(coverage=[qc(Q1, "answered", ["c02"]), qc(Q2, "dodged", ["c09"])]),
    }, arbiter)
    got = {q.question: q for q in out.question_coverage}
    assert (got[Q1].status, got[Q1].claims, got[Q1].decided_by) == ("answered", ["c01", "c02"], "agree")
    assert (got[Q2].status, got[Q2].claims, got[Q2].decided_by) == ("dodged", ["c09"], "arbiter")
    assert got[Q3].decided_by == "arbiter"                 # only one model covered it
    assert {d["question"] for d in rulings["seen"]} == {Q2, Q3}
    assert [q.question for q in out.question_coverage] == [Q1, Q2, Q3]


def test_question_coverage_unarbitrated_and_single():
    out = vd.combine({A: vote(coverage=[qc(Q1, "answered")]), B: vote(coverage=[qc(Q1, "dodged", ["c03"])])}, None)
    [q] = out.question_coverage
    assert (q.status, q.claims, q.decided_by) == ("dodged", ["c03"], "unarbitrated")   # stays open
    assert out.degraded == ["no_arbiter"]
    out = vd.combine({A: vote(coverage=[qc(Q1, "never_asked")])}, never_called)
    assert [(q.status, q.decided_by) for q in out.question_coverage] == [("never_asked", "single")]


def test_speakers_combine_like_verdicts():
    def arbiter(disputes):
        return {"belief_checks": [], "question_coverage": [],
                "speakers": [{"id": d["id"], "speaker": "Viktor Lang"} for d in disputes["speakers"]
                             if d["claim"] == "c02"]}

    out = vd.combine({
        A: vote(speakers=[{"claim": "c01", "speaker": "Viktor Lang"}, {"claim": "c02", "speaker": "Me"},
                          {"claim": "c03", "speaker": "Alex Rivera"}]),
        B: vote(speakers=[{"claim": "c01", "speaker": "viktor lang"}, {"claim": "c02", "speaker": "Viktor Lang"},
                          {"claim": "c03", "speaker": "Viktor Lang"}]),
    }, arbiter)
    assert out.speakers == {"c01": "Viktor Lang", "c02": "Viktor Lang"}   # c03 had no ruling
    assert out.degraded == ["no_arbiter"]
    single = vd.combine({A: vote(speakers=[{"claim": "c01", "speaker": "Viktor Lang"}])}, never_called)
    assert single.speakers == {"c01": "Viktor Lang"}


def test_one_arbiter_call_covers_every_kind_of_dispute():
    calls = []

    def arbiter(disputes):
        calls.append(disputes)
        return {"belief_checks": [], "question_coverage": [], "speakers": []}

    vd.combine({A: vote(bc("B-001", "supports", ["c01"]), coverage=[qc(Q1, "answered")],
                        speakers=[{"claim": "c01", "speaker": "X"}]),
                B: vote(coverage=[qc(Q1, "dodged")], speakers=[{"claim": "c01", "speaker": "Y"}])}, arbiter)
    assert len(calls) == 1
    assert [len(calls[0][k]) for k in ("belief_checks", "question_coverage", "speakers")] == [1, 1, 1]


# ---------- run_verdicts ----------

def make_parsed(turns, *, labels="named") -> ParsedTranscript:
    raw, out = "", []
    for i, (tm, speaker, text) in enumerate(turns, 1):
        raw += f"[{tm}] **{speaker}**: "
        start = len(raw)
        raw += text
        out.append(Turn(index=i, time=tm, speaker=speaker, text=text, start=start, end=len(raw)))
        raw += "\n\n"
    return ParsedTranscript(path="transcripts/t.md", raw=raw, shape="circleback_copy", front_matter={}, notes="",
                            body_start=0, body_end=len(raw), turns=out, speaker_labels=labels, origin="backfill",
                            recorded_by="alex", attendees=[Attendee(name="Darius Kemp", email="darius@examplemutual.com")])


TURNS = [("0:%02d" % i, "Alex Rivera" if i % 2 else "Darius Kemp", f"line {i}") for i in range(1, 41)]
TURNS[3] = ("0:04", "Darius Kemp", "I don't think so.")
TURNS[29] = ("0:30", "Darius Kemp", "We never got the store footage before it was overwritten.")
PARSED = make_parsed(TURNS)

CLAIMS = [
    Claim(id="c01", text="Small stores overwrite their footage", quote="line 2", turns=[2], speaker="Darius Kemp",
          scope=Scope(party="insured")),
    Claim(id="c02", text="Store cameras cannot stand in", quote="I don't think so.", turns=[4], question_turn=3,
          speaker="Darius Kemp"),
    Claim(id="c03", text="The store footage was gone before the claim came in",
          quote="We never got the store footage before it was overwritten.", turns=[30], speaker="Darius Kemp"),
]


def entry(eid, statement, section, **scope):
    return Entry(id=eid, statement=statement, scope=Scope(**scope), section=section,
                 history=[HistoryLine(date="2026-10-01", action="add", who="Alex", via="bootstrap", ref="1.1:x")])


ENTRIES = [
    entry("B-001", "Video footage is the most valuable evidence on a liability claim", "beliefs",
          line="general liability"),
    entry("B-003", "Adjusters lose real time scrubbing long footage for the moment of the incident", "beliefs",
          party="independent adjusting firm"),
    entry("W-001", "Commercial lines get less claims software than personal lines", "watching"),
    entry("B-009", "A retired idea nobody holds any more", "retired"),
]


def setup(ctx, a, b, arbiter=None):
    ex = ctx.config.pipeline.extractors
    fakes = {"a": FakeLLM(ex[0], a), "b": FakeLLM(ex[1], b)}
    if arbiter is not None:
        fakes["arb"] = FakeLLM(ctx.config.pipeline.arbiter, arbiter)
    for f in fakes.values():
        ctx.llms.register(f)
    return fakes


def model_vote(*checks, coverage=(), speakers=()):
    return lambda s, p, sc: {"belief_checks": list(checks), "question_coverage": list(coverage),
                             "speakers": list(speakers)}


def test_run_verdicts_agree(ctx):
    same = model_vote(bc("B-003", "contradicts", ["c02"], "carrier, not TPA"))
    fakes = setup(ctx, same, same, arbiter=lambda s, p, sc: AssertionError("no arbiter"))
    out = vd.run_verdicts(ctx, PARSED, CLAIMS, ENTRIES, [])
    [check] = out.belief_checks
    assert (check.target, check.verdict, check.decided_by, check.source) == ("B-003", "contradicts", "agree", "ingest")
    assert set(check.votes) == set(ctx.config.pipeline.extractors)
    assert out.degraded == [] and fakes["arb"].calls == []
    call = fakes["a"].calls[0]
    assert call["schema"] is vd.VERDICT_SCHEMA
    prompt = call["prompt"]
    assert "B-003" in prompt and "W-001" in prompt and "B-009" not in prompt     # current entries only
    assert "party: independent adjusting firm" in prompt
    assert "c02" in prompt and "I don't think so." in prompt
    assert "[T3 0:03] Alex Rivera: line 3" in prompt                  # turns around the claims
    assert "[T15 0:15]" not in prompt                              # but not the whole call
    assert "{" not in prompt


def test_run_verdicts_disagreement_goes_to_arbiter(ctx):
    fakes = setup(ctx, model_vote(bc("B-003", "contradicts", ["c02"], "reason one")),
                  model_vote(bc("B-003", "scope_mismatch", ["c02"], "reason two")),
                  arbiter=lambda s, p, sc: {"belief_checks": [{"id": "b1", "verdict": "scope_mismatch",
                                                               "reason": "TPA vs carrier"}],
                                            "question_coverage": [], "speakers": []})
    out = vd.run_verdicts(ctx, PARSED, CLAIMS, ENTRIES, [])
    [check] = out.belief_checks
    assert (check.verdict, check.decided_by, check.note) == ("scope_mismatch", "arbiter", "TPA vs carrier")
    [call] = fakes["arb"].calls
    assert call["schema"] is vd.ARBITER_SCHEMA
    prompt = call["prompt"]
    assert "b1" in prompt and "Adjusters lose real time scrubbing" in prompt and "I don't think so." in prompt
    assert "reason one" in prompt and "reason two" in prompt
    assert "Reader 1" in prompt and "Reader 2" in prompt
    for spec in ctx.config.pipeline.extractors:                   # readers are anonymous to the arbiter
        assert spec not in prompt
    assert "[T3 0:03] Alex Rivera: line 3" in prompt


def test_run_verdicts_arbiter_down(ctx):
    setup(ctx, model_vote(bc("B-001", "supports", ["c01"])), model_vote(),
          arbiter=lambda s, p, sc: LLMError("arbiter down"))
    out = vd.run_verdicts(ctx, PARSED, CLAIMS, ENTRIES, [])
    assert [c.decided_by for c in out.belief_checks] == ["unarbitrated"]
    assert out.degraded == ["no_arbiter"]


def test_run_verdicts_one_model_down(ctx):
    setup(ctx, lambda s, p, sc: LLMError("down"), model_vote(bc("W-001", "supports", ["c03"])),
          arbiter=lambda s, p, sc: AssertionError("no arbiter"))
    out = vd.run_verdicts(ctx, PARSED, CLAIMS, ENTRIES, [])
    [check] = out.belief_checks
    assert check.decided_by == "single" and check.votes == {ctx.config.pipeline.extractors[1]: "supports"}
    assert out.degraded == ["one_model"]


def test_run_verdicts_both_down_raises(ctx):
    setup(ctx, lambda s, p, sc: LLMError("down"), lambda s, p, sc: LLMError("down"))
    with pytest.raises(LLMError):
        vd.run_verdicts(ctx, PARSED, CLAIMS, ENTRIES, [])


def test_run_verdicts_backcheck_source_and_no_transcript(ctx):
    fakes = setup(ctx, model_vote(bc("B-001", "supports", ["c01"])), model_vote(bc("B-001", "supports", ["c01"])))
    out = vd.run_verdicts(ctx, None, CLAIMS, ENTRIES[:1], [], source="backcheck")
    assert [(c.source, c.decided_by) for c in out.belief_checks] == [("backcheck", "agree")]
    assert "not available" in fakes["a"].calls[0]["prompt"]


def test_run_verdicts_drops_unknown_ids_and_verdicts(ctx):
    junk = model_vote(bc("B-999", "supports", ["c01"]), bc("B-001", "maybe", ["c01"]),
                      bc("B-001", "supports", ["c77"]), bc("b-1", "supports", ["c01", "c01"]),
                      bc("B-009", "contradicts", ["c01"]))
    setup(ctx, junk, junk)
    out = vd.run_verdicts(ctx, PARSED, CLAIMS, ENTRIES, [])
    assert [(c.target, c.verdict, c.claims) for c in out.belief_checks] == [("B-001", "supports", ["c01"])]


def test_run_verdicts_questions_by_number_with_full_transcript(ctx):
    questions = ["How often do your liability files have usable video at all?",
                 "What does the claim file look like on day one?"]
    fakes = setup(ctx, model_vote(coverage=[qc(2, "never_asked"), qc(1, "dodged", ["c03"])]),
                  model_vote(coverage=[{"question": "1", "status": "dodged", "claims": ["c03"], "reason": ""},
                                       qc(2, "never_asked")]))
    out = vd.run_verdicts(ctx, PARSED, CLAIMS, [], questions)
    assert [(q.question, q.status, q.claims, q.decided_by) for q in out.question_coverage] == [
        (questions[0], "dodged", ["c03"], "agree"), (questions[1], "never_asked", [], "agree")]
    prompt = fakes["a"].calls[0]["prompt"]
    assert "1. How often do your liability files" in prompt
    assert "[T15 0:15]" in prompt                                   # questions need the whole call


def test_run_verdicts_speakers_only_on_unnamed_calls(ctx):
    say = model_vote(speakers=[{"claim": "c01", "speaker": "Darius Kemp"}])
    fakes = setup(ctx, say, say)
    named = vd.run_verdicts(ctx, PARSED, CLAIMS, ENTRIES, [])
    assert named.speakers == {}
    me_them = make_parsed(TURNS, labels="me_them")
    out = vd.run_verdicts(ctx, me_them, CLAIMS, [], [])
    assert out.speakers == {"c01": "Darius Kemp"}
    assert "me_them" in fakes["a"].calls[-1]["prompt"]


def test_run_verdicts_skips_models_when_nothing_to_check(ctx):
    fakes = setup(ctx, model_vote(), model_vote())
    out = vd.run_verdicts(ctx, PARSED, CLAIMS, ENTRIES[3:], [])      # only a retired entry
    assert out == vd.VerdictOutcome([], [], {}, [])
    assert fakes["a"].calls == [] and fakes["b"].calls == []


def test_run_verdicts_needs_claim_ids(ctx):
    setup(ctx, model_vote(), model_vote())
    with pytest.raises(ValueError):
        vd.run_verdicts(ctx, PARSED, [Claim(id=None, text="t", quote="q")], ENTRIES, [])


# ---------- prompt files and schemas ----------

def assert_strict(schema: dict, path: str = "$") -> None:
    if schema.get("type") == "object":
        assert schema.get("additionalProperties") is False, path
        assert sorted(schema["required"]) == sorted(schema["properties"]), path
        for k, v in schema["properties"].items():
            assert_strict(v, f"{path}.{k}")
    if schema.get("type") == "array":
        assert_strict(schema["items"], f"{path}[]")
    for i, v in enumerate(schema.get("anyOf", [])):
        assert_strict(v, f"{path}|{i}")


def test_schemas_are_strict():
    assert_strict(vd.VERDICT_SCHEMA)
    assert_strict(vd.ARBITER_SCHEMA)


@pytest.mark.parametrize("name,request_name", [("verdict", "VERDICT"), ("arbiter", "ARBITER")])
def test_prompts(name, request_name):
    module = importlib.import_module(f"nua.prompts.{name}")
    for text in (module.SYSTEM, getattr(module, request_name)):
        assert text.strip() and chr(0x2014) not in text


def test_claim_text_points_a_same_turn_question_before_the_quote():
    assert "    answering the question at T3" in vd._claim_text(CLAIMS[1]).splitlines()
    # a Granola text export is one turn T1: the question is the text before the quote, not all of T1
    same = CLAIMS[1].model_copy(update={"quotes": [CLAIMS[1].quotes[0].model_copy(update={"turns": [1],
                                                                                         "question_turn": 1})]})
    assert "    answering the question just before the quote in T1" in vd._claim_text(same).splitlines()


def test_claim_text_shows_every_quote_of_a_finding():
    from nua.types import Quote
    two = CLAIMS[1].model_copy(update={"quotes": [*CLAIMS[1].quotes,
                                                  Quote(quote="Not on a slip and fall.", turns=[9], speaker="Darius")]})
    lines = vd._claim_text(two).splitlines()
    assert lines[0] == "c02 [T4, T9]: Store cameras cannot stand in"
    assert '  quote (T9, Darius): "Not on a slip and fall."' in lines
    assert lines.index("    answering the question at T3") == 2     # under the quote it belongs to


def test_run_verdicts_skips_models_that_failed_earlier(ctx):
    fakes = setup(ctx, model_vote(bc("W-001", "supports", ["c03"])),
                  lambda s, p, sc: AssertionError("must not be asked"),
                  arbiter=lambda s, p, sc: AssertionError("no arbiter"))
    up = [ctx.config.pipeline.extractors[0]]
    out = vd.run_verdicts(ctx, PARSED, CLAIMS, ENTRIES, [], models=up)
    [check] = out.belief_checks
    assert check.decided_by == "single" and check.votes == {up[0]: "supports"}
    assert out.degraded == ["one_model"]
    assert fakes["b"].calls == []


def test_run_verdicts_with_no_model_left_raises(ctx):
    setup(ctx, model_vote(), model_vote())
    with pytest.raises(LLMError):
        vd.run_verdicts(ctx, PARSED, CLAIMS, ENTRIES, [], models=[])


# ---------- round-2 fixes: a prep question no model covered ----------

def test_question_no_model_covered_goes_to_the_arbiter():
    seen = []

    def arbiter(disputes):
        seen.extend(disputes["question_coverage"])
        return {"belief_checks": [], "speakers": [], "question_coverage": [
            {"id": d["id"], "status": "answered", "claims": ["c04"], "reason": "T12"}
            for d in disputes["question_coverage"]]}

    out = vd.combine({A: vote(coverage=[qc(Q1, "answered", ["c01"])]),
                      B: vote(coverage=[qc(Q1, "answered", ["c01"])])}, arbiter, questions=[Q1, Q2])
    [d] = seen
    assert d["question"] == Q2
    assert d["votes"] == {A: {"status": "none", "claims": [], "reason": ""},
                          B: {"status": "none", "claims": [], "reason": ""}}
    assert [(q.question, q.status, q.claims, q.decided_by) for q in out.question_coverage] == [
        (Q1, "answered", ["c01"], "agree"), (Q2, "answered", ["c04"], "arbiter")]
    assert out.degraded == []


@pytest.mark.parametrize("arbiter", [
    None,
    lambda disputes: (_ for _ in ()).throw(LLMError("arbiter down")),
    lambda disputes: {"belief_checks": [], "question_coverage": [], "speakers": []},   # no ruling for it
])
def test_question_no_model_covered_stays_open_when_unarbitrated(arbiter):
    out = vd.combine({A: vote(coverage=[qc(Q1, "answered")]), B: vote(coverage=[qc(Q1, "answered")])},
                     arbiter, questions=[Q1, Q2])
    got = {q.question: q for q in out.question_coverage}
    assert (got[Q2].status, got[Q2].claims, got[Q2].decided_by) == ("never_asked", [], "unarbitrated")
    assert got[Q1].decided_by == "agree"
    assert out.degraded == ["no_arbiter"]


def test_question_the_single_model_skipped_is_never_asked():
    out = vd.combine({A: vote(coverage=[qc(Q1, "dodged")])}, never_called, questions=[Q1, Q2, Q3])
    assert [(q.question, q.status, q.decided_by) for q in out.question_coverage] == [
        (Q1, "dodged", "single"), (Q2, "never_asked", "single"), (Q3, "never_asked", "single")]
    assert out.degraded == []


def test_run_verdicts_keeps_every_prep_question(ctx):
    questions = ["How often do your liability files have usable video at all?",
                 "What does the claim file look like on day one?",
                 "Who pulls the store footage?"]
    only_first = model_vote(coverage=[qc(1, "dodged", ["c03"])])
    fakes = setup(ctx, only_first, only_first,
                  arbiter=lambda s, p, sc: {"belief_checks": [], "speakers": [], "question_coverage": [
                      {"id": "q1", "status": "answered", "claims": ["c01"], "reason": "T2"}]})
    out = vd.run_verdicts(ctx, PARSED, CLAIMS, [], questions)
    assert [(q.question, q.status, q.decided_by) for q in out.question_coverage] == [
        (questions[0], "dodged", "agree"), (questions[1], "answered", "arbiter"),
        (questions[2], "never_asked", "unarbitrated")]
    assert out.degraded == ["no_arbiter"]
    [call] = fakes["arb"].calls
    assert "What does the claim file look like on day one?" in call["prompt"]
    assert "Who pulls the store footage?" in call["prompt"]
    assert "[T15 0:15]" in call["prompt"]                           # the whole call, to find the answer


def test_run_verdicts_single_model_keeps_every_prep_question(ctx):
    questions = ["How often do your liability files have usable video at all?",
                 "What does the claim file look like on day one?"]
    setup(ctx, model_vote(coverage=[qc(1, "answered", ["c03"])]), lambda s, p, sc: LLMError("down"),
          arbiter=lambda s, p, sc: AssertionError("no arbiter in single mode"))
    out = vd.run_verdicts(ctx, PARSED, CLAIMS, [], questions)
    assert [(q.question, q.status, q.decided_by) for q in out.question_coverage] == [
        (questions[0], "answered", "single"), (questions[1], "never_asked", "single")]
    assert out.degraded == ["one_model"]
