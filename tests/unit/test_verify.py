"""Quote index and finding verification (discovery/verify.py): every quote of a finding is checked on its own."""
from __future__ import annotations

import re

import pytest

from conftest import fixture_text
from discovery.types import Claim, ParsedTranscript, Quote, Turn
from discovery.verify import (Match, QuoteIndex, contains, normalize, quote_overlap, token_overlap, verify_claims,
                        verify_follow_ups)

_TURN_LINE = re.compile(r"^\[(\d+:\d+(?::\d+)?)\] \*\*(.+?)\*\*: ?(.*)$", re.M)


def circleback(name: str) -> ParsedTranscript:
    """Build a ParsedTranscript for a Circleback copy fixture without depending on discovery.parse."""
    raw = fixture_text(name)
    matches = list(_TURN_LINE.finditer(raw))
    turns = [Turn(index=i, time=m.group(1), speaker=m.group(2), text=m.group(3),
                  start=m.start(3), end=m.end(3))
             for i, m in enumerate(matches, start=1)]
    body_start = matches[0].start()
    return ParsedTranscript(path=f"transcripts/{name}.md", raw=raw, shape="circleback_copy",
                            front_matter={}, notes=raw[:body_start], body_start=body_start,
                            body_end=len(raw), turns=turns, speaker_labels="named", origin="backfill")


def inline_export(raw: str, marker: str = "Transcript:\n") -> ParsedTranscript:
    """Build a Granola-export-like ParsedTranscript with inline `Me:` / `Them:` labels."""
    body_start = raw.index(marker) + len(marker)
    turns = []
    labels = list(re.finditer(r"(Me|Them): ", raw[body_start:]))
    for i, m in enumerate(labels, start=1):
        start = body_start + m.end()
        end = body_start + labels[i].start() if i < len(labels) else len(raw)
        while end > start and raw[end - 1] in " \n":
            end -= 1
        turns.append(Turn(index=i, time=None, speaker=m.group(1), text=raw[start:end], start=start, end=end))
    return ParsedTranscript(path="transcripts/x.md", raw=raw, shape="granola_export", front_matter={},
                            notes=raw[:body_start], body_start=body_start, body_end=len(raw),
                            turns=turns, speaker_labels="me_them", origin="backfill")


@pytest.fixture(scope="module")
def darius() -> ParsedTranscript:
    return circleback("darius-kemp")


@pytest.fixture(scope="module")
def index(darius: ParsedTranscript) -> QuoteIndex:
    return QuoteIndex(darius)


def turn_of(parsed: ParsedTranscript, time: str, speaker: str | None = None) -> Turn:
    return next(t for t in parsed.turns if t.time == time and speaker in (None, t.speaker))


def claim(quote: str, **kw) -> Claim:
    return Claim(id=None, text=kw.pop("text", "a claim"), quote=quote, model=kw.pop("model", "fake:a"), **kw)


# ---------- normalize and token_overlap ----------

def test_normalize_case_quotes_punctuation_whitespace():
    assert normalize("Hello,  World!") == "hello world"
    assert normalize("I’m  only\nreviewing “800” claim files.") == "i'm only reviewing 800 claim files"
    assert normalize("  'Cause adjusters' don't   ") == "cause adjusters don't"
    assert normalize("...") == ""


def test_token_overlap():
    assert token_overlap("the cat sat", "The cat, sat!") == 1.0
    assert token_overlap("the cat", "the cat sat on the mat") == 1.0
    assert token_overlap("a b c d", "a b x y") == 0.5
    assert token_overlap("", "anything") == 0.0
    assert token_overlap("...", "anything") == 0.0


def test_quote_overlap_is_the_longest_shared_run_of_words():
    assert quote_overlap("the cat sat", "The cat, sat!") == 1.0
    assert quote_overlap("so the cat sat on the mat", "the cat sat down") == 0.75   # "the cat sat" of 4
    assert quote_overlap("a b c d", "d c b a") == 0.25        # same words, no shared run
    assert token_overlap("a b c d", "d c b a") == 1.0         # which is where the two rules differ
    assert quote_overlap("", "anything") == 0.0


def test_contains_after_normalization():
    assert contains("Well, I DON'T think so, honestly.", "i don't think so")
    assert not contains("I don't think so.", "i do think so")
    assert not contains("anything", "...")                     # an empty quote is never inside anything
    assert not contains("", "quote")


# ---------- QuoteIndex.find ----------

def test_exact_quote_matches_with_exact_offsets(darius, index):
    quote = ("But anything outside the company is off limits for us, because compliance is terrified of claimant "
             "data leaving the building.")
    start = darius.raw.index(quote)
    m = index.find(quote)
    assert isinstance(m, Match)
    assert (m.start, m.end) == (start, start + len(quote))
    assert m.raw_text == quote
    assert darius.raw[m.start:m.end] == quote
    assert m.turns == [turn_of(darius, "15:37", "Darius Kemp").index]


def test_quote_with_case_curly_quotes_and_missing_commas_matches(darius, index):
    original = "Hey, thanks a lot, Alex. I'm happy to help out again anytime."
    variant = "hey thanks a lot ALEX I’m happy to help out again anytime"
    start = darius.raw.index(original)
    m = index.find(variant)
    assert m is not None
    assert m.start == start
    assert m.end == start + len(original) - 1          # no trailing period in the variant
    assert m.raw_text == darius.raw[m.start:m.end]
    assert m.turns == [turn_of(darius, "37:05").index]


def test_notes_sentence_never_matches(darius, index):
    sentence = "Footage is the strongest evidence he gets but is absent or useless"
    assert sentence in darius.notes
    assert index.find(sentence) is None
    assert index.find("Duck Creek came up as a platform Darius knows by name only") is None


def test_quote_across_turn_boundary(darius, index):
    first, second = turn_of(darius, "32:45"), turn_of(darius, "32:47")
    m = index.find("Are you building anything yet")
    assert m is not None
    assert m.turns == [first.index, second.index]
    assert m.start == darius.raw.index("Are you building")
    assert m.end == second.start + len("anything yet")
    assert m.raw_text == "Are you building anything yet"
    assert "**" not in m.raw_text


def test_quote_copied_with_markup_still_matches(darius, index):
    plain = index.find("Are you building anything yet")
    for quote in ("Are you building\n\n[32:47] **Participant 2**: anything yet",
                  "Are you building [T146 32:47] Participant 2: anything yet"):
        m = index.find(quote)
        assert m is not None, quote
        assert (m.start, m.end, m.turns) == (plain.start, plain.end, plain.turns)


def test_word_boundaries_respected(index):
    assert index.find("eally liked my time with that group") is None       # "eally" is inside "really"


def test_empty_quote_is_not_found(index):
    assert index.find("") is None
    assert index.find(" ... ") is None


def test_find_all_returns_every_occurrence(darius, index):
    matches = index.find_all("slip and fall")
    assert len(matches) >= 2
    assert all(normalize(m.raw_text) == "slip and fall" for m in matches)
    assert matches == sorted(matches, key=lambda m: m.start)


@pytest.mark.parametrize("name", ["darius-kemp"])
def test_every_turn_matches_itself(name):
    parsed = circleback(name)
    index = QuoteIndex(parsed)
    for t in parsed.turns:
        if not normalize(t.text):
            continue
        m = index.find(t.text)
        assert m is not None, t.index
        assert normalize(m.raw_text) == normalize(t.text)
        assert parsed.body_start <= m.start < m.end <= parsed.body_end


def test_turn_text_and_context(darius, index):
    t = turn_of(darius, "37:05")
    said = "Nope, nothing from me. Hey, thanks a lot, Alex. I'm happy to help out again anytime."
    assert index.turn_text(t.index) == said
    assert index.turn_text(10_000) == ""
    ctx = index.context([t.index], radius=1).splitlines()
    assert ctx == [
        f"[T{t.index - 1}] Alex Rivera: {' '.join(darius.turns[t.index - 2].text.split())}",
        f"[T{t.index}] Darius Kemp: {said}",
        f"[T{t.index + 1}] Alex Rivera: {' '.join(darius.turns[t.index].text.split())}",
    ]


def test_context_clips_at_edges_and_marks_gaps(darius, index):
    lines = index.context([1, 10], radius=1).splitlines()
    assert lines[0].startswith("[T1] Alex Rivera: Hi.")
    assert lines[1].startswith("[T2] ")
    assert lines[2] == "..."
    assert [l.split("]")[0] for l in lines[3:]] == ["[T9", "[T10", "[T11"]


def test_inline_labels_are_markup():
    raw = ("Some notes about the call. They said no to dashcam footage.\n\nTranscript:\n"
           "Me: Would a store give you the dashcam footage? Them: No. Not until we had a subpoena for "
           "the footage. Me: Got it.")
    parsed = inline_export(raw)
    index = QuoteIndex(parsed)
    assert index.find("They said no to dashcam footage") is None
    m = index.find("dashcam footage? Them: No. Not until we had a subpoena")
    assert m is not None
    assert m.turns == [1, 2]
    assert m.raw_text == "dashcam footage? No. Not until we had a subpoena"
    m2 = index.find("Not until we had a subpoena for the footage.")
    assert raw[m2.start:m2.end] == "Not until we had a subpoena for the footage."


# ---------- verify_claims ----------

def test_verify_sets_offset_quote_and_turns(darius):
    quote = "And on the commercial side the carriers buy a lot less of it than they do for personal auto and homeowners"
    c = claim("And on the commercial side, the carriers buy a lot less of it, than they do for personal auto and homeowners.")
    out, v = verify_claims(darius, [c])
    assert len(out) == 1
    got = out[0]
    start = darius.raw.index(quote)
    assert got.offset == (start, start + len(quote) + 1)   # trailing period included
    assert got.quote == darius.raw[start:start + len(quote) + 1]
    assert got.quotes[0].turns == [turn_of(darius, "19:04").index]
    assert got.quotes[0].attribution == "span"
    assert (v.quotes, v.matched_first_pass, v.repaired, v.dropped) == (1, 1, 0, [])
    assert c.offset is None                                 # input not mutated


def test_verify_cross_turn_is_inferred(darius):
    out, v = verify_claims(darius, [claim("Are you building anything yet or are you just looking")])
    assert out[0].quotes[0].attribution == "inferred"
    assert out[0].quotes[0].turns == [turn_of(darius, "32:45").index, turn_of(darius, "32:47").index]
    assert out[0].quote == "Are you building anything yet or are you just looking"
    assert v.matched_first_pass == 1


def test_short_answer_without_question_turn_is_dropped(darius):
    out, v = verify_claims(darius, [claim("Nowhere close to that.", model="fake:b")])
    assert out == []
    assert v.quotes == 1 and v.matched_first_pass == 0 and v.repaired == 0
    assert v.dropped == [{"model": "fake:b", "quote": "Nowhere close to that.",
                          "reason": "short answer needs question_turn"}]


def test_short_answer_repaired_with_question_turn(darius):
    answer = turn_of(darius, "24:27")
    reasons = []

    def repair(c: Claim, q: Quote, reason: str) -> Quote | None:
        reasons.append(reason)
        return q.model_copy(update={"question_turn": answer.index - 1})

    out, v = verify_claims(darius, [claim("Nowhere close to that.")], repair=repair)
    assert reasons == ["short answer needs question_turn"]
    assert len(out) == 1
    assert out[0].quotes[0].turns == [answer.index]
    assert out[0].quotes[0].question_turn == answer.index - 1
    assert out[0].offset == (answer.start, answer.start + len("Nowhere close to that."))
    assert (v.matched_first_pass, v.repaired, v.dropped) == (0, 1, [])


def test_short_answer_with_valid_question_turn_matches_first_pass(darius):
    answer = turn_of(darius, "24:27")
    out, v = verify_claims(darius, [claim("Nowhere close to that.", question_turn=answer.index - 1)])
    assert out[0].offset == (answer.start, answer.start + len("Nowhere close to that."))
    assert v.matched_first_pass == 1


def test_question_turn_must_come_before_the_answer(darius):
    answer = turn_of(darius, "24:27")
    for bad in (answer.index, answer.index + 3, 10_000, 0):
        out, v = verify_claims(darius, [claim("Nowhere close to that.", question_turn=bad)])
        assert out == [], bad
        assert v.dropped[0]["reason"] == "short answer needs question_turn"


def test_short_answer_picks_occurrence_after_question():
    raw = ("notes\nTranscript:\n"
           "Me: Do you get store footage? Them: No. Me: Do you get dashcam footage? Them: No. Me: Ok.")
    parsed = inline_export(raw)
    out, _ = verify_claims(parsed, [claim("No.", question_turn=3)])
    assert out[0].quotes[0].turns == [4]
    assert out[0].offset[0] == raw.rindex("No.")


# ---------- short answers on a one-block transcript (Granola text export) ----------

SHORT = "short answer needs question_turn"
ONE_BLOCK = ("Notes about the call.\n\nTranscript:\nMe: Do you keep the store footage? No. Why not? "
             "Because it's too expensive. Would a carrier pay for it? I don't think so.\n")


def one_block(raw: str, marker: str = "Transcript:\n") -> ParsedTranscript:
    """A Granola text export with a single `Me:` label: the whole call is one single_stream turn."""
    body_start = raw.index(marker) + len(marker)
    start, end = body_start + len("Me: "), len(raw.rstrip())
    turn = Turn(index=1, time=None, speaker="Me", text=raw[start:end], start=start, end=end)
    return ParsedTranscript(path="transcripts/x.md", raw=raw, shape="granola_export", front_matter={},
                            notes=raw[:body_start], body_start=body_start, body_end=end, turns=[turn],
                            speaker_labels="single_stream", origin="backfill")


def test_one_block_short_answer_takes_its_own_turn_as_question_turn():
    parsed = one_block(ONE_BLOCK)
    out, v = verify_claims(parsed, [claim("I don't think so.", turns=[1], question_turn=1)])
    at = ONE_BLOCK.index("I don't think so.")
    assert [(c.quotes[0].turns, c.quotes[0].question_turn, c.offset) for c in out] == [([1], 1, (at, at + len("I don't think so.")))]
    assert (v.matched_first_pass, v.dropped) == (1, [])


def test_one_block_short_answer_is_repaired_to_its_own_turn():
    reasons = []

    def repair(c: Claim, q: Quote, reason: str) -> Quote | None:
        reasons.append(reason)
        return q.model_copy(update={"question_turn": 1})

    out, v = verify_claims(one_block(ONE_BLOCK), [claim("Because it's too expensive.")], repair=repair)
    assert reasons == [SHORT]
    assert [c.quotes[0].question_turn for c in out] == [1]
    assert (v.repaired, v.dropped) == (1, [])


def test_one_block_short_answer_needs_words_before_it_in_the_turn():
    # the quote opens the block, so nothing in its own turn can be the question it answers
    out, v = verify_claims(one_block(ONE_BLOCK), [claim("Do you keep the store footage?", question_turn=1)])
    assert out == [] and v.dropped[0]["reason"] == SHORT
    raw = "Notes.\n\nTranscript:\nMe: No. Do you keep the footage? No.\n"
    out, _ = verify_claims(one_block(raw), [claim("No.", question_turn=1)])
    assert out[0].offset[0] == raw.rindex("No.")        # the occurrence with a question before it


def test_same_turn_question_is_still_rejected_where_turns_separate_speakers():
    raw = "Notes.\n\nTranscript:\nMe: Do you keep the footage? Them: Well, honestly. No.\n"
    parsed = inline_export(raw)
    assert len(parsed.turns) == 2
    out, v = verify_claims(parsed, [claim("No.", question_turn=2)])
    assert out == [] and v.dropped[0]["reason"] == SHORT
    out, _ = verify_claims(parsed, [claim("No.", question_turn=1)])
    assert out[0].quotes[0].turns == [2]


def test_granola_export_fixture_keeps_a_short_answer():
    from discovery.parse import parse
    parsed = parse("transcripts/viktor-lang.md", fixture_text("viktor-lang"), commit_date="2026-10-04")
    assert (len(parsed.turns), parsed.speaker_labels) == (1, "single_stream")
    quote = "I only handle commercial."
    out, v = verify_claims(parsed, [claim(quote, turns=[1], question_turn=1)])
    assert [c.quote for c in out] == [quote]
    assert (v.matched_first_pass, v.dropped) == (1, [])


def test_text_before_is_the_turn_text_up_to_an_offset():
    parsed = one_block(ONE_BLOCK)
    index = QuoteIndex(parsed)
    at = ONE_BLOCK.index("I don't think so.")
    assert index.text_before(1, at) == ("Do you keep the store footage? No. Why not? Because it's too expensive. "
                                        "Would a carrier pay for it?")
    assert index.text_before(1, parsed.turns[0].start) == ""
    assert index.text_before(1, len(ONE_BLOCK)) == parsed.turns[0].text
    assert index.text_before(2, at) == ""


def test_miss_is_repaired_once(darius):
    calls = []

    def repair(c: Claim, q: Quote, reason: str) -> Quote | None:
        calls.append(reason)
        return q.model_copy(update={"quote": "The minute a lawyer is retained, the claim costs more, period."})

    out, v = verify_claims(darius, [claim("Attorney claims always cost more than unrepresented claims.")], repair=repair)
    assert calls == ["quote not found in transcript"]
    assert len(out) == 1 and out[0].quotes[0].turns == [turn_of(darius, "10:47").index]
    assert out[0].model == "fake:a"
    assert (v.quotes, v.matched_first_pass, v.repaired) == (1, 0, 1)


def test_second_miss_drops(darius):
    calls = []

    def repair(c: Claim, q: Quote, reason: str) -> Quote | None:
        calls.append(q.quote)
        return q.model_copy(update={"quote": "still not in the transcript at all"})

    good = claim("But anything outside the company is off limits for us, because compliance is terrified of "
                 "claimant data leaving the building.")
    bad = claim("Attorney claims always cost more than unrepresented claims.", model="fake:b")
    out, v = verify_claims(darius, [good, bad], repair=repair)
    assert calls == [bad.quote]
    assert [c.quote for c in out] == [good.quote]
    assert v.quotes == 2 and v.matched_first_pass == 1 and v.repaired == 0
    assert v.dropped == [{"model": "fake:b", "quote": bad.quote, "reason": "quote not found in transcript"}]


def test_repair_returning_none_drops(darius):
    out, v = verify_claims(darius, [claim("not a real quote from this call")], repair=lambda c, q, r: None)
    assert out == []
    assert len(v.dropped) == 1


def test_repair_llm_error_counts_as_no_repair(darius):
    from discovery.llm import LLMError

    def repair(c: Claim, q: Quote, reason: str) -> Quote | None:
        raise LLMError("down")

    out, v = verify_claims(darius, [claim("not a real quote from this call")], repair=repair)
    assert out == [] and len(v.dropped) == 1


def test_notes_quote_is_dropped(darius):
    out, v = verify_claims(darius, [claim("Footage is the strongest evidence he gets but is absent or useless")])
    assert out == []
    assert v.dropped[0]["reason"] == "quote not found in transcript"


def test_short_answer_words_threshold(darius):
    quote = "The minute a lawyer is retained, the claim costs more, period."   # 11 words
    out, _ = verify_claims(darius, [claim(quote)])
    assert len(out) == 1
    out, v = verify_claims(darius, [claim(quote)], short_answer_words=11)
    assert out == [] and v.dropped[0]["reason"] == "short answer needs question_turn"


def test_offsets_map_back_to_raw(darius):
    quotes = ["Costco", "Nowhere close to that.", "Are you building anything yet",
              "because guide wire has been the big name"]
    index = QuoteIndex(darius)
    for q in quotes:
        m = index.find(q)
        if m is None:
            continue
        assert darius.body_start <= m.start < m.end <= darius.body_end
        stripped = re.sub(r"\n+\[\d+:\d+\] \*\*[^*]+\*\*: ", " ", darius.raw[m.start:m.end])
        assert stripped == m.raw_text


# ---------- speakers from turn labels (named calls) ----------

def _long_turn(parsed: ParsedTranscript, speaker: str) -> Turn:
    return next(t for t in parsed.turns if t.speaker == speaker and len(t.text.split()) > 12)


def test_named_call_speaker_comes_from_the_turn_label(darius):
    alex = _long_turn(darius, "Alex Rivera")
    out, _ = verify_claims(darius, [claim(alex.text, speaker="Darius Kemp")])
    assert (out[0].speaker, out[0].quotes[0].attribution, out[0].quotes[0].turns) == ("Alex Rivera", "span", [alex.index])


def test_named_call_speaker_label_is_canonicalized(darius):
    turn = _long_turn(darius, "Darius Kemp")
    out, _ = verify_claims(darius, [claim(turn.text, speaker=None)],
                           canonical=lambda n: "Darius J. Kemp" if n == "Darius Kemp" else n)
    assert (out[0].speaker, out[0].quotes[0].attribution) == ("Darius J. Kemp", "span")


def test_cross_turn_quote_keeps_the_model_speaker(darius):
    out, _ = verify_claims(darius, [claim("Are you building anything yet or are you just looking", speaker="Darius Kemp")])
    assert (out[0].speaker, out[0].quotes[0].attribution) == ("Darius Kemp", "inferred")


def test_unnamed_labels_keep_the_model_speaker():
    raw = ("Notes.\n\nTranscript:\nMe: Would a store give you the dashcam footage? Them: Not until we had a "
           "subpoena for the footage.")
    parsed = inline_export(raw)
    out, _ = verify_claims(parsed, [claim("Not until we had a subpoena for the footage.", speaker="Darius Kemp",
                                          question_turn=1)])
    assert (out[0].speaker, out[0].quotes[0].attribution) == ("Darius Kemp", "span")


# ---------- follow-up quotes ----------

def test_follow_up_quotes_are_checked_like_claim_quotes(darius):
    from discovery.types import FollowUp
    real = _long_turn(darius, "Darius Kemp").text
    follow_ups = [
        FollowUp(direction="they_offered", what="intro", who="Darius Kemp",
                 quote="I'll intro you to my old manager on the Guidewire claims team tomorrow."),
        FollowUp(direction="we_owe", what="deck", who=None, quote=real.upper()),
        FollowUp(direction="we_owe", what="notes only", quote="Footage is the strongest evidence he gets but is absent or useless"),
        FollowUp(direction="we_owe", what="no quote", quote=""),
    ]
    out, dropped = verify_follow_ups(darius, follow_ups)
    assert [f.what for f in out] == ["intro", "deck", "notes only", "no quote"]
    assert [f.quote for f in out] == ["", real.strip(), "", ""]
    assert dropped == 2
    assert follow_ups[0].quote.startswith("I'll intro")      # input not mutated


# ---------- findings with several quotes ----------

GOOD_A = "The minute a lawyer is retained, the claim costs more, period."
GOOD_B = ("But anything outside the company is off limits for us, because compliance is terrified of claimant "
          "data leaving the building.")


def finding(*quotes: str, **kw) -> Claim:
    return Claim(id=None, text=kw.pop("text", "a finding"), quotes=[Quote(quote=q) for q in quotes],
                 model=kw.pop("model", "fake:a"), **kw)


def test_each_quote_of_a_finding_is_verified_on_its_own(darius):
    out, v = verify_claims(darius, [finding(GOOD_A, GOOD_B)])
    [got] = out
    assert [q.quote for q in got.quotes] == [GOOD_A, GOOD_B]
    assert all(q.offset for q in got.quotes) and got.spans[0] != got.spans[1]
    assert (v.quotes, v.matched_first_pass, v.dropped, v.findings_dropped) == (2, 2, [], 0)


def test_a_bad_quote_is_dropped_and_the_finding_kept(darius):
    calls = []

    def repair(c: Claim, q: Quote, reason: str) -> Quote | None:
        calls.append(q.quote)
        return None

    out, v = verify_claims(darius, [finding(GOOD_A, "words nobody said on this call", GOOD_B, model="fake:b")],
                           repair=repair)
    [got] = out
    assert [q.quote for q in got.quotes] == [GOOD_A, GOOD_B]
    assert calls == ["words nobody said on this call"]            # only the failing quote is repaired
    assert v.dropped == [{"model": "fake:b", "quote": "words nobody said on this call",
                          "reason": "quote not found in transcript"}]
    assert (v.quotes, v.matched_first_pass, v.findings_dropped) == (3, 2, 0)


def test_a_finding_is_dropped_only_when_no_quote_survives(darius):
    out, v = verify_claims(darius, [finding("words nobody said", "nor these either"), finding(GOOD_A)],
                           repair=lambda c, q, r: None)
    assert [c.quote for c in out] == [GOOD_A]
    assert (len(v.dropped), v.findings_dropped) == (2, 1)


def test_a_repaired_quote_keeps_its_place_in_the_finding(darius):
    def repair(c: Claim, q: Quote, reason: str) -> Quote | None:
        return q.model_copy(update={"quote": GOOD_B})

    out, v = verify_claims(darius, [finding(GOOD_A, "not quite what he said")], repair=repair)
    assert [q.quote for q in out[0].quotes] == [GOOD_A, GOOD_B]
    assert (v.matched_first_pass, v.repaired) == (1, 1)


def test_two_quotes_on_one_span_fold_into_one(darius):
    out, v = verify_claims(darius, [finding(GOOD_A, "a lawyer is retained, the claim costs more, period.")])
    assert [q.quote for q in out[0].quotes] == [GOOD_A]
    assert v.dropped == []


def test_each_quote_takes_its_own_turn_label_on_a_named_call(darius):
    alex = _long_turn(darius, "Alex Rivera")
    out, _ = verify_claims(darius, [finding(GOOD_A, alex.text)])
    assert [q.speaker for q in out[0].quotes] == ["Darius Kemp", "Alex Rivera"]


def test_the_one_quote_shape_still_reads():
    c = Claim.from_dict({"id": "c01", "text": "t", "quote": "q", "turns": [3], "offset": [10, 20],
                         "question_turn": 2, "speaker": "S", "attribution": "inferred"})
    assert c.quotes == [Quote(quote="q", turns=[3], offset=(10, 20), question_turn=2, speaker="S",
                              attribution="inferred")]
    assert (c.quote, c.offset, c.speaker) == ("q", (10, 20), "S")
    assert "quote" not in c.to_dict() and c.to_dict()["quotes"][0]["quote"] == "q"
