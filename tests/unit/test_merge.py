"""Combining findings across extraction models (nua/merge.py): the consolidation call, its validation,
and the quote-overlap merge it falls back to."""
from __future__ import annotations

import copy
from dataclasses import replace

import pytest

from nua import merge
from nua.llm import FakeLLM, LLMError
from nua.merge import merge_claims, pick_quotes, span_overlap
from nua.types import Claim, Flags, Quote, Scope


def claim(start: int, end: int, *, model: str = "fake:a", text: str = "a claim", **scope) -> Claim:
    return Claim(id=None, text=text, quote=f"quote {start}-{end}", turns=[1], offset=(start, end),
                 model=model, scope=Scope(**scope))


# ---------- span_overlap ----------

def test_span_overlap():
    assert span_overlap((0, 10), (20, 30)) == 0.0
    assert span_overlap((0, 10), (10, 20)) == 0.0          # half-open spans only touch
    assert span_overlap((0, 10), (2, 5)) == 1.0             # shorter one fully inside
    assert span_overlap((0, 10), (5, 25)) == 0.5
    assert span_overlap((5, 25), (0, 10)) == 0.5
    assert span_overlap((3, 3), (0, 10)) == 0.0             # empty span


# ---------- merge_claims ----------

def test_overlapping_claims_from_two_models_merge():
    a = [claim(100, 150, model="fake:a"), claim(300, 340, model="fake:a")]
    b = [claim(120, 170, model="fake:b"), claim(500, 520, model="fake:b")]
    out = merge_claims([a, b])
    assert [c.offset for c in out] == [(100, 150), (300, 340), (500, 520)]
    assert [c.found_by for c in out] == [2, 1, 1]
    assert [c.id for c in out] == ["c01", "c02", "c03"]
    assert all(c.model is None for c in out)


def test_better_scoped_version_is_kept():
    a = [claim(100, 150, model="fake:a", text="thin", seat="claims adjuster (staff)")]
    b = [claim(110, 140, model="fake:b", text="rich", seat="claims adjuster (staff)",
               line="general liability", party="carrier")]
    out = merge_claims([a, b])
    assert len(out) == 1
    assert out[0].text == "rich"
    assert out[0].offset == (110, 140)
    assert out[0].found_by == 2


def test_scope_tie_keeps_earlier_list():
    a = [claim(100, 150, model="fake:a", text="first", segment="small commercial")]
    b = [claim(100, 150, model="fake:b", text="second", seat="claims adjuster (staff)")]
    out = merge_claims([a, b])
    assert [c.text for c in out] == ["first"]


def test_same_model_claims_never_merge_with_each_other():
    a = [claim(100, 150, text="one"), claim(140, 200, text="two")]
    out = merge_claims([a])
    assert [c.text for c in out] == ["one", "two"]
    assert [c.found_by for c in out] == [1, 1]


def test_one_claim_per_model_in_a_group():
    a = [claim(100, 200, model="fake:a", text="wide")]
    b = [claim(100, 130, model="fake:b", text="left"), claim(170, 260, model="fake:b", text="right")]
    out = merge_claims([a, b])
    assert len(out) == 2
    by_text = {c.text: c.found_by for c in out}
    assert sorted(by_text.values()) == [1, 2]
    assert by_text.get("right") == 1                        # "left" overlaps "wide" fully, so it pairs


def test_output_sorted_by_offset_and_renumbered():
    a = [claim(900, 950, model="fake:a"), claim(10, 20, model="fake:a")]
    b = [claim(400, 420, model="fake:b")]
    out = merge_claims([a, b])
    assert [c.offset[0] for c in out] == [10, 400, 900]
    assert [c.id for c in out] == ["c01", "c02", "c03"]


def test_three_models():
    a = [claim(100, 150, model="fake:a")]
    b = [claim(120, 160, model="fake:b")]
    c = [claim(140, 180, model="fake:c"), claim(600, 610, model="fake:c")]
    out = merge_claims([a, b, c])
    assert [x.found_by for x in out] == [3, 1]


def test_empty_inputs():
    assert merge_claims([]) == []
    assert merge_claims([[], []]) == []


def test_inputs_not_mutated():
    a = [claim(100, 150, model="fake:a")]
    b = [claim(120, 170, model="fake:b")]
    before = copy.deepcopy((a, b))
    merge_claims([a, b])
    assert (a, b) == before


def test_claims_without_offset_are_kept_last():
    a = [Claim(id=None, text="no span", quote="x", model="fake:a"), claim(50, 60, model="fake:a")]
    out = merge_claims([a])
    assert [c.text for c in out] == ["a claim", "no span"]
    assert [c.id for c in out] == ["c01", "c02"]


# ---------- findings with several quotes ----------

def q(start: int, end: int) -> Quote:
    return Quote(quote=f"quote {start}-{end}", turns=[1], offset=(start, end))


def finding(*spans: tuple[int, int], model: str = "fake:a", text: str = "a finding", **scope) -> Claim:
    return Claim(id=None, text=text, quotes=[q(*sp) for sp in spans], model=model, scope=Scope(**scope))


def test_pick_quotes_dedupes_caps_and_spreads():
    quotes = [q(100, 150), q(110, 140), q(160, 170), q(900, 950), q(500, 520), q(180, 190)]
    out = pick_quotes(quotes)
    assert [x.offset for x in out] == [(100, 150), (500, 520), (900, 950)]   # far apart, in transcript order
    assert [x.offset for x in pick_quotes([q(300, 310), q(100, 120)])] == [(100, 120), (300, 310)]
    assert pick_quotes([q(100, 150), q(105, 145)]) == [q(100, 150)]          # one span, one quote


def test_overlap_merge_joins_findings_through_any_quote():
    a = [finding((100, 150), (700, 740), model="fake:a", text="a")]
    b = [finding((400, 420), (710, 735), model="fake:b", text="b")]
    [out] = merge_claims([a, b])
    assert out.found_by == 2 and out.text == "a"
    assert [x.offset for x in out.quotes] == [(100, 150), (400, 420), (700, 740)]


# ---------- consolidation ----------

FINDINGS = [
    [finding((100, 150), model="fake:a", text="State regulators audit claim files one by one"),
     finding((300, 340), model="fake:a", text="Claim notes can end up in court"),
     finding((800, 820), model="fake:a", text="Vendors need exec approval")],
    [finding((200, 240), model="fake:b", text="Documentation must be meticulous"),
     finding((805, 830), model="fake:b", text="New vendors take four to five months")],
]


@pytest.fixture
def arbiter(ctx):
    def register(responder):
        fake = FakeLLM(ctx.config.pipeline.arbiter, responder)
        ctx.llms.register(fake)
        return fake
    return register


def group(ids, text="", **scope):
    return {"ids": ids, "text": text,
            "scope": {k: scope.get(k, "") for k in ("line", "party", "segment", "seat")},
            "flags": {"hedged": False, "firsthand": True, "prompted": False}}


def test_consolidation_groups_findings_across_models(ctx, arbiter):
    fake = arbiter(lambda s, p, schema: {"groups": [
        group(["a1", "a2", "b1"], "Regulators audit claim files, so documentation must be meticulous.",
              party="TPA", seat="claims adjuster (staff)"),
        group(["a3", "b2"], "A new vendor needs exec approval and takes months."),
    ]})
    out, degraded = merge.consolidate(ctx, None, FINDINGS)
    assert degraded == [] and len(fake.calls) == 1
    call = fake.calls[0]
    assert call["schema"] is merge.CONSOLIDATE_SCHEMA
    assert '<finding id="a1" reader="1">' in call["prompt"] and '<finding id="b2" reader="2">' in call["prompt"]
    assert 'quote (T1): "quote 805-830"' in call["prompt"]
    first, second = out
    assert first.text == "Regulators audit claim files, so documentation must be meticulous."
    assert [x.offset for x in first.quotes] == [(100, 150), (200, 240), (300, 340)]
    assert first.found_by == 2 and first.scope.party == "TPA" and first.flags.firsthand
    assert second.text == "A new vendor needs exec approval and takes months."
    assert [x.offset for x in second.quotes] == [(800, 820)]                # overlapping quotes fold into one
    assert second.found_by == 2
    assert [c.id for c in out] == ["c01", "c02"] and all(c.model is None for c in out)


def test_found_by_counts_distinct_models_not_members(ctx, arbiter):
    arbiter(lambda s, p, schema: {"groups": [group(["a1", "a2"], "One reader said it twice.")]})
    out, _ = merge.consolidate(ctx, None, FINDINGS)
    joined = next(c for c in out if c.text == "One reader said it twice.")
    assert joined.found_by == 1
    assert sorted(c.found_by for c in out) == [1, 1, 1, 1]


def test_every_finding_lands_in_exactly_one_group(ctx, arbiter):
    arbiter(lambda s, p, schema: {"groups": [
        group(["a1", "zz9", "b1"], "joined"),        # an unknown id is dropped
        group(["b1", "a2"], "b1 is already placed"),  # b1 stays in the first group; a2 keeps this one
        group(["nope"], "nothing real"),             # a group with no known id is skipped
        group([], "empty"),
    ]})                                               # a3 and b2 are left out: each becomes its own group
    out, degraded = merge.consolidate(ctx, None, FINDINGS)
    assert degraded == []
    assert [c.text for c in out] == ["joined", "b1 is already placed", "Vendors need exec approval",
                                     "New vendors take four to five months"]
    spans = [s for c in out for s in c.spans]
    assert sorted(spans) == sorted(s for per in FINDINGS for c in per for s in c.spans)   # nothing lost or doubled
    assert out[0].found_by == 2 and out[1].found_by == 1


def test_a_group_without_text_or_scope_keeps_the_members(ctx, arbiter):
    rich = [[finding((100, 150), model="fake:a", text="primary text", line="cyber")],
            [finding((400, 420), model="fake:b", text="second text")]]
    arbiter(lambda s, p, schema: {"groups": [group(["b1", "a1"], "", seat="A Seat Nobody Listed")]})
    [out], _ = merge.consolidate(ctx, None, rich)
    assert out.text == "primary text"                  # the primary extractor's member leads
    assert out.scope.seat == "a seat nobody listed" and out.scope.line is None   # the group's scope, new seat kept


def test_consolidation_failure_falls_back_to_overlap_merge(ctx, arbiter):
    arbiter(lambda s, p, schema: LLMError("arbiter down"))
    out, degraded = merge.consolidate(ctx, None, FINDINGS)
    assert degraded == ["no_consolidation"]
    assert out == merge_claims(FINDINGS)
    assert [c.found_by for c in out] == [1, 1, 1, 2]   # only the overlapping vendor quotes joined
    assert any("consolidate" in line for line in ctx.log)


def test_a_reply_of_the_wrong_shape_falls_back(ctx, arbiter):
    arbiter(lambda s, p, schema: {"groups": "not a list"})
    out, degraded = merge.consolidate(ctx, None, FINDINGS)
    assert out == merge_claims(FINDINGS) and degraded == ["no_consolidation"]


def test_no_arbiter_configured_falls_back(ctx):
    pipeline = replace(ctx.config.pipeline, arbiter="")
    ctx = replace(ctx, config=replace(ctx.config, pipeline=pipeline))
    out, degraded = merge.consolidate(ctx, None, FINDINGS)
    assert out == merge_claims(FINDINGS) and degraded == ["no_consolidation"]


def test_one_finding_needs_no_call(ctx, arbiter):
    fake = arbiter(lambda s, p, schema: {"groups": []})
    out, degraded = merge.consolidate(ctx, None, [[finding((10, 20))], []])
    assert fake.calls == [] and degraded == [] and [c.id for c in out] == ["c01"]
    assert merge.consolidate(ctx, None, [[], []]) == ([], [])


def test_consolidation_inputs_not_mutated(ctx, arbiter):
    arbiter(lambda s, p, schema: {"groups": [group(["a1", "b1"], "x")]})
    before = copy.deepcopy(FINDINGS)
    merge.consolidate(ctx, None, FINDINGS)
    assert FINDINGS == before


def test_consolidate_schema_is_strict():
    from test_extract import assert_strict
    assert_strict(merge.CONSOLIDATE_SCHEMA)
    assert Flags() == Flags(hedged=False, firsthand=False, prompted=False)
