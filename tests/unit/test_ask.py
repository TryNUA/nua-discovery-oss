"""Tests for discovery/ask.py: map/reduce over every digested call, coverage and quote checks. Offline."""
from __future__ import annotations

import re
import threading

import pytest

from discovery import ask as ask_mod
from discovery.ask import AskResult, ask
from discovery.digest import write_digest
from discovery.llm import FakeLLM, LLMError
from discovery.types import Claim, Digest, Scope
from conftest import fixture_text

EM_DASH = chr(0x2014)
CALL_LINE = re.compile(r"^=== (\S+)$", re.M)

# A span from the transcript body of tests/fixtures/darius-kemp.md that no claim below carries.
TRANSCRIPT_ONLY = "So for the field work we use outside vendors."
# Words from the recorder overview at the top of the same fixture: context, never evidence.
NOTES_ONLY = "Footage is the strongest evidence he gets but is absent or useless"
# A verbatim span from the same transcript body, used as a claim quote.
COMMERCIAL_QUOTE = ("And on the commercial side the carriers buy a lot less of it than they do for personal "
                    "auto and homeowners.")


# ---------- helpers ----------

def _claim(cid: str, quote: str, text: str = "A claim", speaker: str = "Darius Kemp") -> Claim:
    return Claim(id=cid, text=text, quote=quote, turns=[1], speaker=speaker,
                 scope=Scope(line="general liability"))


def _digest(call_id: str, *, date: str | None = "2026-09-23", seat: str | None = "claims adjuster (staff)",
            status: str = "digested", claims: list[Claim] | None = None,
            transcript: str | None = None) -> Digest:
    return Digest(call_id=call_id, transcript=transcript or f"transcripts/{call_id}.md", status=status,
                  origin="live", date=date, seat=seat, contact="Some One", firm="Firm",
                  summary=["A summary bullet."], claims=claims or [], prompt_version="v1")


def _write(ctx, *digests: Digest) -> None:
    for d in digests:
        write_digest(ctx.repo, d)


def _calls_in(prompt: str) -> list[str]:
    return CALL_LINE.findall(prompt)


def _map_all(system, prompt, schema):
    """Mark every claim of every digest in the batch as relevant."""
    out = []
    for block in prompt.split("\n=== ")[1:]:
        call_id = block.split("\n", 1)[0].strip()
        for cid in re.findall(r"^\[(c\d+)\]", block, re.M):
            out.append({"call_id": call_id, "claim_id": cid, "why": "bears on it"})
    return {"relevant": out}


def _map_none(system, prompt, schema):
    return {"relevant": []}


def _reduce(answer="The answer.", citations=()):
    def responder(system, prompt, schema):
        return {"answer": answer, "citations": [{"call_id": c, "quote": q} for c, q in citations]}
    return responder


def _register(ctx, map_responder, reduce_responder):
    m = FakeLLM(ctx.config.pipeline.ask_map, map_responder)
    r = FakeLLM(ctx.config.pipeline.ask_reduce, reduce_responder)
    ctx.llms.register(m)
    ctx.llms.register(r)
    return m, r


# ---------- the prompt ----------

def test_prompt_has_map_and_reduce():
    from discovery.prompts import ask as prompt
    for text in (prompt.SYSTEM, prompt.MAP, prompt.REDUCE):
        assert text.strip() and EM_DASH not in text


def test_prompts_fill_without_missing_placeholders(ctx):
    _write(ctx, _digest("2026-09-23_darius-kemp", claims=[_claim("c01", "we pull the store footage")]))
    m, r = _register(ctx, _map_all, _reduce())
    ask(ctx, "What do {braces} do?")
    assert "What do {braces} do?" in m.calls[0]["prompt"]
    assert "What do {braces} do?" in r.calls[0]["prompt"]
    assert m.calls[0]["system"] and m.calls[0]["system"] == r.calls[0]["system"]


# ---------- coverage ----------

def test_coverage_counts_digested_only(ctx):
    _write(ctx, *[_digest(f"2026-09-1{i}_person-{i}", claims=[_claim("c01", f"quote number {i}")]) for i in range(5)])
    _write(ctx, _digest("2026-09-20_internal", status="internal"),
           _digest("2026-09-21_broken", status="parse_failed"),
           _digest("2026-09-22_failed", status="failed"))
    m, r = _register(ctx, _map_all, _reduce())
    res = ask(ctx, "Anything?", batch_size=2)
    assert isinstance(res, AskResult)
    assert (res.read, res.total) == (5, 5)
    assert res.coverage == "read 5/5 digests"
    assert len(m.calls) == 3                       # 2 + 2 + 1
    seen = sorted(c for call in m.calls for c in _calls_in(call["prompt"]))
    assert seen == [f"2026-09-1{i}_person-{i}" for i in range(5)]
    assert len(r.calls) == 1


def test_no_digests_makes_no_model_calls(ctx):
    _write(ctx, _digest("2026-09-20_internal", status="internal"))
    m, r = _register(ctx, _map_all, _reduce())
    res = ask(ctx, "Anything?")
    assert (res.read, res.total, res.citations) == (0, 0, [])
    assert res.coverage == "read 0/0 digests"
    assert res.answer
    assert m.calls == [] and r.calls == []


def test_no_relevant_claims_skips_reduce(ctx):
    _write(ctx, _digest("2026-09-23_darius-kemp", claims=[_claim("c01", "we pull the store footage")]))
    m, r = _register(ctx, _map_none, _reduce())
    res = ask(ctx, "Anything?")
    assert (res.read, res.total) == (1, 1)
    assert res.citations == [] and r.calls == []
    assert "1" in res.answer


# ---------- filters ----------

def test_since_and_seat_filters(ctx):
    _write(ctx,
           _digest("2026-09-01_early", date="2026-09-01", seat="underwriter", claims=[_claim("c01", "early quote")]),
           _digest("2026-09-15_mid", date="2026-09-15", seat="underwriter", claims=[_claim("c01", "mid quote")]),
           _digest("2026-09-20_late", date="2026-09-20", seat="claims adjuster (staff)", claims=[_claim("c01", "late quote")]),
           _digest("2026-09-25_undated", date=None, seat="underwriter", claims=[_claim("c01", "undated quote")]))
    m, _ = _register(ctx, _map_all, _reduce())

    res = ask(ctx, "Q?", since="2026-09-10")
    assert res.total == 2
    assert sorted(_calls_in(m.calls[-1]["prompt"])) == ["2026-09-15_mid", "2026-09-20_late"]

    res = ask(ctx, "Q?", seat="  Underwriter ")
    assert res.total == 3

    res = ask(ctx, "Q?", since="2026-09-10", seat="underwriter")
    assert res.total == 1
    assert _calls_in(m.calls[-1]["prompt"]) == ["2026-09-15_mid"]


def test_seat_filter_sees_overrides(ctx):
    _write(ctx, _digest("2026-09-23_darius-kemp", seat="other", claims=[_claim("c01", "we pull the store footage")]))
    ctx.config.overrides["2026-09-23_darius-kemp"] = {"seat": "underwriter"}
    _register(ctx, _map_all, _reduce())
    assert ask(ctx, "Q?", seat="underwriter").total == 1
    assert ask(ctx, "Q?", seat="other").total == 0


def test_bad_since_raises(ctx):
    with pytest.raises(ValueError):
        ask(ctx, "Q?", since="last week")


# ---------- map step ----------

def test_map_prompt_carries_claims_and_speaker_labels(ctx):
    d = _digest("2026-09-23_darius-kemp", claims=[_claim("c01", "because guide wire has been the big name in claims",
                                                            text="Carriers keep the claims platform they have")])
    d.speaker_labels = "me_them"
    _write(ctx, d)
    m, _ = _register(ctx, _map_all, _reduce())
    ask(ctx, "Q?")
    prompt = m.calls[0]["prompt"]
    assert "=== 2026-09-23_darius-kemp" in prompt
    assert "[c01]" in prompt and "because guide wire has been the big name in claims" in prompt
    assert "Carriers keep the claims platform they have" in prompt
    assert "me_them" in prompt
    assert "Alex Rivera" in prompt                     # Acme people are named for the model


def test_reduce_gets_only_kept_claims_with_their_quotes(ctx):
    _write(ctx, _digest("2026-09-23_darius-kemp", claims=[
        _claim("c01", "kept quote about footage"), _claim("c02", "dropped quote about reserves")]))

    def map_some(system, prompt, schema):
        return {"relevant": [
            {"call_id": "2026-09-23_darius-kemp", "claim_id": "[c01]", "why": "on footage"},
            {"call_id": "2026-09-23_darius-kemp", "claim_id": "c01", "why": "duplicate"},
            {"call_id": "2026-09-23_darius-kemp", "claim_id": "c99", "why": "no such claim"},
            {"call_id": "2099-01-01_nobody", "claim_id": "c01", "why": "not in this batch"},
        ]}

    _, r = _register(ctx, map_some, _reduce())
    ask(ctx, "Q?")
    prompt = r.calls[0]["prompt"]
    assert "kept quote about footage" in prompt
    assert "dropped quote about reserves" not in prompt
    assert prompt.count("kept quote about footage") == 1
    assert "2099-01-01_nobody" not in prompt
    assert "read: 1 of 1" in prompt


def test_failed_batch_is_retried_once_then_unread(ctx):
    _write(ctx, *[_digest(f"2026-09-1{i}_person-{i}", claims=[_claim("c01", f"quote number {i}")]) for i in range(4)])
    attempts: dict[str, int] = {}
    lock = threading.Lock()

    def flaky(system, prompt, schema):
        first = _calls_in(prompt)[0]
        with lock:
            attempts[first] = attempts.get(first, 0) + 1
            n = attempts[first]
        if first == "2026-09-10_person-0":
            return LLMError("always down")
        if first == "2026-09-12_person-2" and n == 1:
            return LLMError("down once")
        return _map_all(system, prompt, schema)

    _register(ctx, flaky, _reduce())
    res = ask(ctx, "Q?", batch_size=2)
    assert attempts == {"2026-09-10_person-0": 2, "2026-09-12_person-2": 2}
    assert (res.read, res.total) == (2, 4)
    assert res.coverage == "read 2/4 digests"


def test_malformed_map_reply_counts_as_a_failure(ctx):
    _write(ctx, _digest("2026-09-23_darius-kemp", claims=[_claim("c01", "we pull the store footage")]))
    m, _ = _register(ctx, lambda s, p, sc: {"nope": 1}, _reduce())
    res = ask(ctx, "Q?")
    assert len(m.calls) == 2
    assert (res.read, res.total) == (0, 1)
    assert res.citations == []


def test_progress_reports_every_batch(ctx):
    _write(ctx, *[_digest(f"2026-09-1{i}_person-{i}", claims=[_claim("c01", f"quote number {i}")]) for i in range(5)])
    _register(ctx, _map_all, _reduce())
    seen: list[tuple[int, int]] = []
    ask(ctx, "Q?", batch_size=2, concurrency=2, progress=lambda done, total: seen.append((done, total)))
    assert len(seen) == 3
    assert all(total == 5 for _, total in seen)
    assert [done for done, _ in seen] == sorted(done for done, _ in seen)
    assert seen[-1] == (5, 5)


# ---------- citations ----------

def test_citations_are_checked_against_claims_and_transcript(ctx):
    ctx.repo.write("transcripts/darius-kemp.md", fixture_text("darius-kemp"))
    claims = [
        _claim("c01", COMMERCIAL_QUOTE),
        _claim("c02", "Nowhere close to that."),
    ]
    _write(ctx, _digest("2026-09-23_darius-kemp", transcript="transcripts/darius-kemp.md", claims=claims))
    fabricated = "Carriers always require dashcam footage before they will pay any claim"
    short_claim_trap = "Nowhere close to that many adjusters see video on a small claim"
    cited = [
        ("2026-09-23_darius-kemp", claims[0].quote),                         # exact claim quote
        ("2026-09-23_darius-kemp", "ON THE COMMERCIAL SIDE THE CARRIERS BUY A LOT LESS"),     # piece of it, other case
        ("2026-09-23_darius-kemp", TRANSCRIPT_ONLY),                         # in the transcript, not a claim
        ("2026-09-23_darius-kemp", fabricated),                              # made up
        ("2026-09-23_darius-kemp", short_claim_trap),                        # shares every word of a short claim
        ("2026-09-23_darius-kemp", NOTES_ONLY),                              # recorder notes are not evidence
        ("2026-01-01_nobody", claims[0].quote),                            # no such call
        ("2026-09-23_darius-kemp", claims[0].quote),                         # duplicate citation
    ]
    _register(ctx, _map_all, _reduce(citations=cited))
    res = ask(ctx, "Q?")
    got = [(c["call_id"], c["verified"]) for c in res.citations]
    assert got == [
        ("2026-09-23_darius-kemp", True),
        ("2026-09-23_darius-kemp", True),
        ("2026-09-23_darius-kemp", True),
        ("2026-09-23_darius-kemp", False),
        ("2026-09-23_darius-kemp", False),
        ("2026-09-23_darius-kemp", False),
        ("2026-01-01_nobody", False),
    ]
    assert res.citations[0]["quote"] == claims[0].quote
    assert res.citations[3]["quote"] == fabricated
    assert set(res.citations[0]) == {"call_id", "quote", "verified"}


def test_quote_near_a_claim_quote_verifies_without_a_transcript(ctx):
    quote = "we just pull the store footage for these claims and review the clips over time"
    _write(ctx, _digest("2026-09-23_darius-kemp", claims=[_claim("c01", quote)]))
    near = "we just pull the store footage for these claims and review clips"   # one word dropped
    _register(ctx, _map_all, _reduce(citations=[("2026-09-23_darius-kemp", near)]))
    [c] = ask(ctx, "Q?").citations
    assert c["verified"] is True
    assert c["quote"] == quote                   # the stored verbatim quote, never the model's retyping


def test_reordered_quote_is_not_verified(ctx):
    ctx.repo.write("transcripts/darius-kemp.md", fixture_text("darius-kemp"))
    quote = COMMERCIAL_QUOTE
    _write(ctx, _digest("2026-09-23_darius-kemp", transcript="transcripts/darius-kemp.md", claims=[_claim("c01", quote)]))
    reordered = "on the personal side the carriers buy a lot less of it than they do for commercial auto and homeowners"   # same words, new meaning
    _register(ctx, _map_all, _reduce(citations=[("2026-09-23_darius-kemp", reordered)]))
    [c] = ask(ctx, "Q?").citations
    assert c["verified"] is False
    assert c["quote"] == reordered


def test_near_quote_may_not_drop_a_negation_or_invent_a_word(ctx):
    quote = "we do not need a video before the carrier will pay the claim"
    _write(ctx, _digest("2026-09-23_darius-kemp", claims=[_claim("c01", quote)]))
    cited = [("2026-09-23_darius-kemp", "we do need a video before the carrier will pay the claim"),   # drops "not"
             ("2026-09-23_darius-kemp", "we do not need a video before the adjuster will pay the claim"),   # new word
             ("2026-09-23_darius-kemp", "we need a video before the carrier will pay the claim"),     # 3 skipped
             ("2026-09-23_darius-kemp", "need a video before the carrier will pay the")]            # a piece of it
    _register(ctx, _map_all, _reduce(citations=cited))
    got = [(c["verified"], c["quote"]) for c in ask(ctx, "Q?").citations]
    assert got == [(False, cited[0][1]), (False, cited[1][1]), (False, cited[2][1]), (True, quote)]


def test_quote_order_check_directly():
    assert ask_mod.quote_in_order("review clips", "and review the clips over time")
    assert not ask_mod.quote_in_order("clips review", "and review the clips over time")
    assert not ask_mod.quote_in_order("", "anything")
    assert not ask_mod.quote_in_order("a b c d", "a x y z b c d")        # 3 skips is over the budget


def test_stub_digests_do_not_vouch_for_quotes(ctx):
    _write(ctx, _digest("2026-09-23_darius-kemp", claims=[_claim("c01", "we pull the store footage")]),
           _digest("2026-09-19_stub", status="failed", claims=[_claim("c01", "a stub quote")]))
    _register(ctx, _map_all, _reduce(citations=[("2026-09-19_stub", "a stub quote")]))
    [c] = ask(ctx, "Q?").citations
    assert c["verified"] is False


# ---------- reduce step ----------

def test_answer_has_no_em_dash(ctx):
    _write(ctx, _digest("2026-09-23_darius-kemp", claims=[_claim("c01", "we pull the store footage")]))
    _register(ctx, _map_all, _reduce(answer=f"Yes{EM_DASH}mostly."))
    res = ask(ctx, "Q?")
    assert EM_DASH not in res.answer
    assert "Yes" in res.answer and "mostly." in res.answer


def test_reduce_failure_falls_back_to_the_evidence(ctx):
    _write(ctx, _digest("2026-09-23_darius-kemp", claims=[_claim("c01", "we pull the store footage"),
                                                        _claim("c02", "they send the claim to a TPA")]))
    _, r = _register(ctx, _map_all, lambda s, p, sc: LLMError("reduce down"))
    res = ask(ctx, "Q?")
    assert len(r.calls) == 2                         # retried once
    assert (res.read, res.total) == (1, 1)
    assert [c["quote"] for c in res.citations] == ["we pull the store footage", "they send the claim to a TPA"]
    assert all(c["verified"] for c in res.citations)
    assert "failed" in res.answer.lower()
    assert EM_DASH not in res.answer


def test_every_batch_failing_reads_nothing(ctx):
    _write(ctx, _digest("2026-09-23_darius-kemp", claims=[_claim("c01", "we pull the store footage")]))
    _, r = _register(ctx, lambda s, p, sc: LLMError("down"), _reduce())
    res = ask(ctx, "Q?")
    assert (res.read, res.total) == (0, 1)
    assert res.citations == [] and r.calls == []
    assert res.answer


def test_unknown_map_model_counts_every_batch_unread(ctx, monkeypatch):
    _write(ctx, _digest("2026-09-23_darius-kemp", claims=[_claim("c01", "we pull the store footage")]))
    ctx.config.pipeline.ask_map = "fake:not-registered"

    def boom(spec, cost=None):
        raise LLMError(f"{spec} has no key")

    monkeypatch.setattr("discovery.providers.build", boom)
    ctx.llms.register(FakeLLM(ctx.config.pipeline.ask_reduce, _reduce()))
    res = ask(ctx, "Q?")
    assert (res.read, res.total) == (0, 1)


def test_digests_are_loaded_once_per_ask(ctx, monkeypatch):
    """The quote check reuses the digests the map read: a second load after the map could hit a tree
    the MCP server is pulling underneath it."""
    _write(ctx, _digest("2026-09-23_darius-kemp", claims=[_claim("c01", "we pull the store footage")]))
    loads = []
    real = ask_mod.load_digests
    monkeypatch.setattr(ask_mod, "load_digests", lambda repo: loads.append(1) or real(repo))
    _register(ctx, _map_all, _reduce(citations=[("2026-09-23_darius-kemp", "we pull the store footage")]))
    [c] = ask(ctx, "Q?").citations
    assert c["verified"] is True
    assert len(loads) == 1


# ---------- time limit ----------

def test_time_limit_counts_unfinished_batches_as_unread(ctx):
    _write(ctx, *[_digest(f"2026-09-1{i}_person-{i}", claims=[_claim("c01", f"quote number {i}")]) for i in range(4)])
    release = threading.Event()

    def slow_for_one(system, prompt, schema):
        if _calls_in(prompt)[0] == "2026-09-10_person-0":
            release.wait(5)                      # a hung provider call
            return LLMError("late")
        return _map_all(system, prompt, schema)

    m, r = _register(ctx, slow_for_one, _reduce(citations=[("2026-09-12_person-2", "quote number 2")]))
    import time as _time
    start = _time.monotonic()
    try:
        res = ask(ctx, "Q?", batch_size=2, deadline_seconds=1.0)
    finally:
        release.set()
    assert _time.monotonic() - start < 3
    assert (res.read, res.total) == (2, 4)
    assert res.answer == "The answer."
    assert res.coverage == "read 2/4 digests"
    assert len(r.calls) == 1
    assert [c["verified"] for c in res.citations] == [True]


def test_time_limit_skips_the_second_map_try(ctx, monkeypatch):
    _write(ctx, _digest("2026-09-23_darius-kemp", claims=[_claim("c01", "we pull the store footage")]))
    clock = [0.0]
    monkeypatch.setattr(ask_mod, "_now", lambda: clock[0])

    def fail_late(system, prompt, schema):
        clock[0] += 100                           # the first try used up the map's share of the time
        return LLMError("down")

    m, _ = _register(ctx, fail_late, _reduce())
    res = ask(ctx, "Q?", deadline_seconds=120)
    assert len(m.calls) == 1
    assert (res.read, res.total) == (0, 1)
    assert "time" in res.answer


def test_no_time_left_for_the_answer_falls_back_to_the_evidence(ctx, monkeypatch):
    _write(ctx, _digest("2026-09-23_darius-kemp", claims=[_claim("c01", "we pull the store footage")]))
    clock = [0.0]
    monkeypatch.setattr(ask_mod, "_now", lambda: clock[0])

    def map_slowly(system, prompt, schema):
        clock[0] += 200
        return _map_all(system, prompt, schema)

    _, r = _register(ctx, map_slowly, _reduce())
    res = ask(ctx, "Q?", deadline_seconds=200)
    assert r.calls == []
    assert "ran out of time" in res.answer
    assert [(c["quote"], c["verified"]) for c in res.citations] == [("we pull the store footage", True)]


def test_answer_step_past_the_time_limit_falls_back_to_the_evidence(ctx):
    _write(ctx, _digest("2026-09-23_darius-kemp", claims=[_claim("c01", "we pull the store footage")]))
    release = threading.Event()

    def hung_reduce(system, prompt, schema):
        release.wait(5)
        return {"answer": "late", "citations": []}

    _, r = _register(ctx, _map_all, hung_reduce)
    try:
        res = ask(ctx, "Q?", deadline_seconds=0.5)
    finally:
        release.set()
    assert "ran out of time" in res.answer
    assert [c["quote"] for c in res.citations] == ["we pull the store footage"]


@pytest.mark.parametrize("deadline", [None, 200])
def test_time_limit_asks_for_short_requests_without_provider_retries(ctx, deadline):
    _write(ctx, _digest("2026-09-23_darius-kemp", claims=[_claim("c01", "we pull the store footage")]))
    m, r = _register(ctx, _map_all, _reduce())
    asked = []
    real = ctx.llms.__call__

    class Spy:
        def __call__(self, spec, **opts):
            asked.append((spec, opts))
            return real(spec, **opts)

        def __getattr__(self, name):
            return getattr(real.__self__, name)

    ctx.llms = Spy()
    ask(ctx, "Q?", deadline_seconds=deadline)
    if deadline is None:
        assert asked == [(m.spec, {}), (r.spec, {})]
    else:
        assert asked == [(m.spec, {"timeout": ask_mod.MAP_CALL_TIMEOUT_SECONDS, "delays": ()}),
                         (r.spec, {"timeout": ask_mod.REDUCE_CALL_TIMEOUT_SECONDS, "delays": ()})]


def test_bad_batch_size_raises(ctx):
    with pytest.raises(ValueError):
        ask(ctx, "Q?", batch_size=0)


def test_module_exports_interface():
    assert callable(ask_mod.ask)
    r = AskResult(answer="a", citations=[], read=3, total=4)
    assert r.coverage == "read 3/4 digests"


def test_unparseable_transcript_falls_back_to_claim_quotes(ctx):
    ctx.repo.write("transcripts/broken.md", "no turns in here at all\n")
    _write(ctx, _digest("2026-09-23_darius-kemp", transcript="transcripts/broken.md",
                        claims=[_claim("c01", "we pull the store footage for these claims")]))
    cited = [("2026-09-23_darius-kemp", "we pull the store footage for these claims"),
             ("2026-09-23_darius-kemp", "no turns in here at all")]
    _register(ctx, _map_all, _reduce(citations=cited))
    assert [c["verified"] for c in ask(ctx, "Q?").citations] == [True, False]


def test_empty_filter_result_names_the_filters(ctx):
    _write(ctx, _digest("2026-09-23_darius-kemp", claims=[_claim("c01", "we pull the store footage")]))
    res = ask(ctx, "Q?", since="2026-10-01", seat="reinsurance")
    assert (res.read, res.total) == (0, 0)
    assert "since 2026-10-01" in res.answer and "seat reinsurance" in res.answer


# ---------- digest files that cannot be read ----------

BROKEN_DIGEST = ("---\ncall_id: 2026-09-01_jane-doe\ntranscript: transcripts/jane.md\nstatus: digested\n"
                 "origin: live\nseat: [unclosed\n---\n")


def test_unreadable_digest_files_count_as_unread(ctx):
    _write(ctx, _digest("2026-09-23_darius-kemp", claims=[_claim("c01", "we pull the store footage")]),
           _digest("2026-09-19_jane-roe", claims=[_claim("c01", "we review dashcam clips")]))
    ctx.repo.write("digests/2026-09-01_jane-doe.md", BROKEN_DIGEST)
    m, r = _register(ctx, _map_all, _reduce())
    res = ask(ctx, "Q?")
    assert (res.read, res.total) == (2, 3)
    assert res.coverage == "read 2/3 digests"          # never N/N while a digest file cannot be read
    assert "2026-09-01_jane-doe" not in m.calls[0]["prompt"]
    assert "read: 2 of 3" in r.calls[0]["prompt"]


def test_unreadable_digest_files_are_unread_in_the_fallback_answers(ctx):
    _write(ctx, _digest("2026-09-23_darius-kemp", claims=[_claim("c01", "we pull the store footage")]))
    ctx.repo.write("digests/2026-09-01_jane-doe.md", BROKEN_DIGEST)
    _register(ctx, _map_none, _reduce())
    res = ask(ctx, "Q?")
    assert res.coverage == "read 1/2 digests"
    assert "1 of 2 digests could not be read" in res.answer


@pytest.mark.parametrize("since", [None, "2026-10-01"])
def test_only_unreadable_digest_files_makes_no_model_calls(ctx, since):
    _write(ctx, _digest("2026-09-23_darius-kemp", claims=[_claim("c01", "we pull the store footage")]) if since else
           _digest("2026-09-20_internal", status="internal"))
    ctx.repo.write("digests/2026-09-01_jane-doe.md", BROKEN_DIGEST)
    m, r = _register(ctx, _map_all, _reduce())
    res = ask(ctx, "Q?", since=since)
    assert (res.read, res.total, res.citations) == (0, 1, [])
    assert res.coverage == "read 0/1 digests"
    assert "1 of 1 digests could not be read" in res.answer
    assert m.calls == [] and r.calls == []


@pytest.mark.parametrize("where", ["symlink", "absolute", "dotdot"])
def test_a_transcript_path_outside_the_repo_falls_back_to_claim_quotes(ctx, tmp_path_factory, where):
    outside = tmp_path_factory.mktemp("outside") / "darius-kemp.md"
    outside.write_text(fixture_text("darius-kemp"), encoding="utf-8")
    if where == "symlink":
        (ctx.repo.root / "transcripts" / "link.md").symlink_to(outside)
        transcript = "transcripts/link.md"
    elif where == "absolute":
        transcript = str(outside)
    else:
        transcript = "../" + outside.parent.name + "/darius-kemp.md"
    quote = "we pull the store footage for these claims"
    _write(ctx, _digest("2026-09-23_darius-kemp", transcript=transcript, claims=[_claim("c01", quote)]))
    cited = [("2026-09-23_darius-kemp", quote), ("2026-09-23_darius-kemp", TRANSCRIPT_ONLY)]
    _register(ctx, _map_all, _reduce(citations=cited))
    res = ask(ctx, "Q?")
    assert [c["verified"] for c in res.citations] == [True, False]   # the outside file is never read
    assert any("2026-09-23_darius-kemp" in line or transcript in line for line in ctx.log)
