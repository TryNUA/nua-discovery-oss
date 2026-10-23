"""Tests for discovery/extract.py with FakeLLM extractors. Offline."""
from __future__ import annotations

import time

import pytest

from discovery import extract as ex
from discovery.llm import FakeLLM, LLMError
from discovery.prompts import extract as extract_prompt
from discovery.types import Attendee, Claim, ParsedTranscript, Quote, Turn

EM_DASH = chr(0x2014)


def make_parsed(turns: list[tuple[str | None, str | None, str]], *, labels: str = "named",
                notes: str = "The overview says Darius loves dashcam footage.") -> ParsedTranscript:
    """A small circleback-style transcript with exact offsets, built by hand."""
    raw = "#### Overview\n\n" + notes + "\n\n"
    body_start = len(raw)
    out: list[Turn] = []
    for i, (tm, speaker, text) in enumerate(turns, 1):
        raw += (f"[{tm}] " if tm else "") + (f"**{speaker}**: " if speaker else "")
        start = len(raw)
        raw += text
        out.append(Turn(index=i, time=tm, speaker=speaker, text=text, start=start, end=len(raw)))
        raw += "\n\n"
    return ParsedTranscript(path="transcripts/test.md", raw=raw, shape="circleback_copy", front_matter={},
                            notes=notes, body_start=body_start, body_end=len(raw), turns=out,
                            speaker_labels=labels, origin="backfill", title="Darius / Alex", date="2026-09-23",
                            recorded_by="alex",
                            attendees=[Attendee(name="Darius Kemp", email="darius@examplemutual.com")])


DARIUS = make_parsed([
    ("0:22", "Alex Rivera", "Hi Darius, thanks for making time."),
    ("0:31", "Darius Kemp", "Happy to. So most of the insureds are already like our small business accounts."),
    ("1:05", "Alex Rivera", "Could the store's own camera footage stand in for a field investigator?"),
    ("1:12", "Darius Kemp", "I don't think so."),
    ("1:40", "Darius Kemp", "Guide wire doesn't cover the commercial side as well so like maybe I get half the tools"),
])


def reply(**overrides) -> dict:
    data = {
        "who": {"contact": "Darius Kemp", "firm": "carrigan hail", "firm_source": "transcript",
                "seat": "Claims Adjuster (Staff)", "institution_type": "commercial carrier",
                "role_in_thesis": "carrier"},
        "summary": ["one", "two", "three", "four", "five", "six"],
        "findings": [
            {"text": f"Commercial lines get fewer claim tools {EM_DASH} not better ones",
             "quotes": [{"quote": "maybe I get half the tools", "turns": [5], "question_turn": None,
                         "speaker": "Darius Kemp"},
                        {"quote": "  ", "turns": [5], "question_turn": None, "speaker": None},
                        {"quote": "Guide wire doesn't cover the commercial side", "turns": [5],
                         "question_turn": None, "speaker": "Darius Kemp"}],
             "scope": {"line": "general liability", "party": "carrier", "segment": None,
                       "seat": "claims adjuster (staff)"},
             "flags": {"hedged": False, "firsthand": True, "prompted": False}},
            {"text": "Store camera footage cannot stand in for a field investigator",
             "quotes": [{"quote": "I don't think so.", "turns": ["T4"], "question_turn": "3",
                         "speaker": "Darius Kemp"}],
             "scope": {"line": None, "party": None, "segment": "small commercial", "seat": "a seat nobody listed"},
             "flags": {"hedged": False, "firsthand": False, "prompted": True}},
            {"text": "A finding with no quote", "quotes": [{"quote": "   ", "turns": [2], "question_turn": None,
                                                            "speaker": "Darius Kemp"}], "scope": {}, "flags": {}},
            {"text": "A finding with too many quotes", "quotes": [
                {"quote": f"quote {n}", "turns": [], "question_turn": None, "speaker": None} for n in range(5)],
             "scope": {}, "flags": {}},
        ],
        "follow_ups": [{"direction": "we_owe", "what": "send the deck", "who": "Darius Kemp",
                        "quote": "send me the deck"},
                       {"direction": "sideways", "what": "bad direction", "who": None, "quote": ""}],
        "mentions": {"people": ["Darius Kemp"], "firms": ["guide wire", "Guidewire", "Duck Creek"]},
        "ask_next": ["q1", "q2", "q3", "q4"],
    }
    data.update(overrides)
    return data


def register(ctx, responders: dict):
    fakes = {}
    for spec, fn in responders.items():
        fakes[spec] = FakeLLM(spec, fn)
        ctx.llms.register(fakes[spec])
    return fakes


def assert_strict(schema: dict, path: str = "$") -> None:
    """OpenAI strict mode: every object closes its properties and requires all of them."""
    if schema.get("type") == "object":
        assert schema.get("additionalProperties") is False, path
        assert sorted(schema["required"]) == sorted(schema["properties"]), path
        for k, v in schema["properties"].items():
            assert_strict(v, f"{path}.{k}")
    if schema.get("type") == "array":
        assert_strict(schema["items"], f"{path}[]")
    for i, v in enumerate(schema.get("anyOf", [])):
        assert_strict(v, f"{path}|{i}")


# ---------- numbering ----------

def test_numbered_transcript_lines():
    parsed = make_parsed([("0:22", "Alex Rivera", "Hi."), (None, "Darius Kemp", "Hello\nthere."),
                          ("1:00", None, "No label.")])
    assert ex.numbered_transcript(parsed).splitlines() == [
        "[T1 0:22] Alex Rivera: Hi.",
        "[T2] Darius Kemp: Hello there.",
        "[T3 1:00] No label.",
    ]


def test_turns_excerpt_marks_gaps():
    parsed = make_parsed([("0:0%d" % i, "A", f"turn {i}") for i in range(1, 10)])
    lines = ex.turns_excerpt(parsed, [5], radius=1).splitlines()
    assert lines == ["...", "[T4 0:04] A: turn 4", "[T5 0:05] A: turn 5", "[T6 0:06] A: turn 6", "..."]


# ---------- extract ----------

def test_both_extractors_succeed_in_extractor_order(ctx):
    specs = ctx.config.pipeline.extractors
    assert len(specs) == 2

    def slow(system, prompt, schema):
        time.sleep(0.05)       # the primary finishes last; order must still follow the config
        return reply()

    fakes = register(ctx, {specs[0]: slow, specs[1]: lambda s, p, sc: reply(summary=["other"])})
    results, degraded = ex.extract(ctx, DARIUS)
    assert degraded == []
    assert [r.model for r in results] == specs
    assert results[1].summary == ["other"]
    call = fakes[specs[0]].calls[0]
    assert call["schema"] is ex.EXTRACT_SCHEMA
    assert "[T4 1:12] Darius Kemp: I don't think so." in call["prompt"]
    assert "The overview says Darius loves dashcam footage." in call["prompt"]
    assert "- claims adjuster (staff)" in call["prompt"]
    assert "Carrigan Hail" in call["prompt"]         # entity aliases are listed
    assert "Alex Rivera" in call["prompt"]             # recorder and Acme people named
    assert call["system"].strip()
    assert fakes[specs[1]].calls[0]["prompt"] == call["prompt"]


def test_one_extractor_down_gives_one_model(ctx):
    specs = ctx.config.pipeline.extractors
    register(ctx, {specs[0]: lambda s, p, sc: LLMError("primary down"), specs[1]: lambda s, p, sc: reply()})
    results, degraded = ex.extract(ctx, DARIUS)
    assert degraded == ["one_model"]
    assert [r.model for r in results] == [specs[1]]
    assert any("primary down" in line for line in ctx.log)


def test_both_extractors_down_raises(ctx):
    specs = ctx.config.pipeline.extractors
    register(ctx, {s: (lambda s_, p, sc: LLMError("down")) for s in specs})
    with pytest.raises(LLMError):
        ex.extract(ctx, DARIUS)


def test_missing_key_counts_as_a_failed_extractor(ctx, monkeypatch):
    specs = ctx.config.pipeline.extractors
    register(ctx, {specs[0]: lambda s, p, sc: reply()})

    def no_key(spec, cost=None):
        raise LLMError(f"no key for {spec}")

    monkeypatch.setattr("discovery.providers.build", no_key)
    results, degraded = ex.extract(ctx, DARIUS)
    assert degraded == ["one_model"] and results[0].model == specs[0]


def test_malformed_reply_counts_as_a_failure(ctx):
    specs = ctx.config.pipeline.extractors
    register(ctx, {specs[0]: lambda s, p, sc: reply(findings="not a list"), specs[1]: lambda s, p, sc: reply()})
    results, degraded = ex.extract(ctx, DARIUS)
    assert degraded == ["one_model"] and [r.model for r in results] == [specs[1]]


def test_reply_is_normalized(ctx):
    specs = ctx.config.pipeline.extractors
    register(ctx, {specs[0]: lambda s, p, sc: reply(), specs[1]: lambda s, p, sc: reply()})
    r = ex.extract(ctx, DARIUS)[0][0]
    assert r.who.firm == "Corrigan Hale"                       # entity map applied
    assert r.who.seat == "claims adjuster (staff)"            # case folded to the seat list
    assert r.who.role_in_thesis == "carrier"
    assert len(r.summary) == 5 and len(r.ask_next) == 3
    assert [c.quote for c in r.claims] == ["maybe I get half the tools", "I don't think so.", "quote 0"]
    first, second, third = r.claims
    assert [q.quote for q in first.quotes] == ["maybe I get half the tools",          # empty quote left out
                                               "Guide wire doesn't cover the commercial side"]
    assert len(third.quotes) == 3                                                     # at most 3 quotes
    assert EM_DASH not in first.text and "claim tools" in first.text
    assert first.model == specs[0] and first.id is None and first.found_by == 1
    assert first.scope.line == "general liability" and first.scope.segment is None
    assert first.flags.firsthand is True
    assert second.quotes[0].turns == [4] and second.quotes[0].question_turn == 3
    assert second.scope.seat == "other"                       # unknown seat goes to "other"
    assert second.flags.prompted is True
    assert [f.direction for f in r.follow_ups] == ["we_owe"]
    assert r.mentions["firms"] == ["Guidewire", "Duck Creek"]  # aliases folded and deduped


def test_prompt_has_no_unfilled_placeholders(ctx):
    specs = ctx.config.pipeline.extractors
    fakes = register(ctx, {s: (lambda s_, p, sc: reply()) for s in specs})
    ex.extract(ctx, DARIUS)
    prompt = fakes[specs[0]].calls[0]["prompt"]
    for name in ("{transcript}", "{notes}", "{seats}", "{entities}", "{speaker_labels}", "{short_answer_words}"):
        assert name not in prompt
    assert "8 words or fewer" in prompt


# ---------- repair ----------

LONG = make_parsed([("0:%02d" % i, "Darius Kemp" if i % 2 else "Alex Rivera", f"sentence number {i}")
                    for i in range(1, 31)])


def test_repair_asks_the_model_that_made_the_claim(ctx):
    specs = ctx.config.pipeline.extractors
    fixed = {"quote": "sentence number 15", "turns": [15], "question_turn": None, "speaker": "Darius Kemp"}
    fakes = register(ctx, {specs[0]: lambda s, p, sc: {"quote": fixed}, specs[1]: lambda s, p, sc: {"quote": None}})
    claim = Claim(id=None, text="Paraphrased", quotes=[Quote(quote="sentence number 3", turns=[3]),
                                                       Quote(quote="sentence 15", turns=[15], speaker="Darius Kemp")],
                  model=specs[0])
    out = ex.repair_quote(ctx, LONG, claim, claim.quotes[1], "quote not found in transcript")
    assert out == Quote(quote="sentence number 15", turns=[15], speaker="Darius Kemp")
    assert fakes[specs[1]].calls == []
    call = fakes[specs[0]].calls[0]
    assert call["schema"] is ex.REPAIR_SCHEMA
    assert "quote not found in transcript" in call["prompt"]
    assert "sentence 15" in call["prompt"]
    assert "[T15 0:15] Darius Kemp: sentence number 15" in call["prompt"]
    assert "[T1 0:01]" not in call["prompt"]                  # only nearby turns are sent


def test_repair_without_valid_turns_sends_whole_transcript(ctx):
    specs = ctx.config.pipeline.extractors
    fakes = register(ctx, {specs[0]: lambda s, p, sc: {"quote": None}})
    claim = Claim(id=None, text="t", quote="q", turns=[999], model=specs[0])
    assert ex.repair_quote(ctx, LONG, claim, claim.quotes[0], "short answer needs question_turn") is None
    prompt = fakes[specs[0]].calls[0]["prompt"]
    assert "[T1 0:01]" in prompt and "[T30 0:30]" in prompt


def test_repair_failure_returns_none(ctx):
    specs = ctx.config.pipeline.extractors
    register(ctx, {specs[1]: lambda s, p, sc: LLMError("down")})
    claim = Claim(id=None, text="t", quote="q", turns=[3], model=specs[1])
    assert ex.repair_quote(ctx, LONG, claim, claim.quotes[0], "no match") is None


# ---------- prompt and schema ----------

def test_schemas_are_strict():
    assert_strict(ex.EXTRACT_SCHEMA)
    assert_strict(ex.REPAIR_SCHEMA)


def test_extract_prompt_rules():
    for text in (extract_prompt.SYSTEM, extract_prompt.EXTRACT, extract_prompt.REPAIR):
        assert text.strip() and EM_DASH not in text
    body = extract_prompt.EXTRACT.lower()
    for rule in ("context only", "exactly", "question_turn", "scope", "ask_next", "up to 3"):
        assert rule in body, rule


def test_short_answer_rule_allows_the_quote_s_own_turn_on_a_one_block_call():
    # a Granola text export parses to one turn, T1, so no earlier turn exists (discovery/verify.py QuoteIndex.answers)
    for name in ("EXTRACT", "REPAIR"):
        body = getattr(extract_prompt, name)
        assert "quote's own turn" in body and "single_stream" in body and "single turn T1" in body, name


# ---------- a hung model ----------

def test_run_parallel_stops_waiting_for_a_hung_model():
    import threading
    release = threading.Event()

    def fn(spec):
        if spec == "fake:hung":
            release.wait(10)
            return "late"
        return "ok"

    try:
        started = time.monotonic()
        out = ex.run_parallel(["fake:hung", "fake:up"], fn, timeout=0.2)
        assert time.monotonic() - started < 2
        assert [(s, r) for s, r, _ in out] == [("fake:hung", None), ("fake:up", "ok")]
        assert isinstance(out[0][2], LLMError) and "no answer" in str(out[0][2])
        assert out[1][2] is None
    finally:
        release.set()


def test_hung_extractor_degrades_to_the_other_model(ctx, monkeypatch):
    import threading
    release = threading.Event()
    specs = ctx.config.pipeline.extractors

    def hung(system, prompt, schema):
        release.wait(10)
        return reply()

    monkeypatch.setattr(ex, "MODEL_STEP_SECONDS", 0.2)
    register(ctx, {specs[0]: hung, specs[1]: lambda s, p, sc: reply()})
    try:
        started = time.monotonic()
        results, degraded = ex.extract(ctx, DARIUS)
        assert time.monotonic() - started < 2
        assert degraded == ["one_model"] and [r.model for r in results] == [specs[1]]
    finally:
        release.set()


def test_model_step_timeout_outlasts_one_provider_call():
    from discovery import providers
    from discovery.runner import BUDGET_SECONDS
    # extraction and verdicts (each at most one step) fit the task budget with room to spare
    assert providers.MAX_CALL_SECONDS < ex.MODEL_STEP_SECONDS < BUDGET_SECONDS / 2
