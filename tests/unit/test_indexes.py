"""Tests for discovery/indexes.py: generated index files and belief status."""
from __future__ import annotations

import csv
import io

import pytest
import yaml

from discovery.beliefs import BeliefsDoc, render_beliefs, render_decisions
from discovery.digest import make_stub, write_digest
from discovery.indexes import (
    GENERATED,
    EntryStatus,
    entry_status,
    evidence_counts,
    finding_claims,
    is_dismissed,
    load_themes,
    load_verdict_overrides,
    regenerate,
    under_pressure,
)
from discovery.types import (
    BeliefCheck,
    Claim,
    Decision,
    Digest,
    Entry,
    Flags,
    HistoryLine,
    QuestionCoverage,
    Quote,
    Scope,
)

EM_DASH = "\u2014"

CALLS_COLUMNS = ["call_id", "transcript", "status", "origin", "date", "date_source", "contact", "firm", "seat",
                 "institution_type", "role_in_thesis", "recorded_by", "source", "speaker_labels", "claims",
                 "prompt_version"]
CLAIMS_COLUMNS = ["call_id", "claim_id", "date", "speaker", "attribution", "seat", "line", "party",
                  "segment", "text", "quote", "belief_targets", "found_by"]

V1 = "1790000000.000100:prop-01"      # version ref of B-001
V2 = "1790000000.000200:prop-02"      # version ref of B-002
VW = "1790000000.000300:prop-03"      # version ref of W-001
VR = "1790000000.000400:prop-04"      # version ref of retired B-009


# ---------- builders ----------

def hist(action: str, ref: str, date: str = "2026-09-01", sources=None) -> HistoryLine:
    return HistoryLine(date=date, action=action, who="Alex", via="slack", ref=ref, sources=list(sources or []))


def entry(entry_id: str, section: str = "beliefs", history=None, statement: str | None = None) -> Entry:
    ref = {"B-001": V1, "B-002": V2, "W-001": VW, "B-009": VR}.get(entry_id, f"ref-{entry_id}")
    action = "watch" if entry_id.startswith("W-") else "add"
    return Entry(id=entry_id, statement=statement or f"Statement for {entry_id}", scope=Scope(),
                 section=section, history=history if history is not None else [hist(action, ref)])


def beliefs_doc(*entries: Entry) -> BeliefsDoc:
    return BeliefsDoc(entries=list(entries))


def claim(cid: str, quote: str, *, text: str | None = None, offset: tuple[int, int] | None = None,
          seat: str | None = None, speaker: str = "Guest") -> Claim:
    return Claim(id=cid, text=text or f"claim {cid}", quote=quote, turns=[1], offset=offset or (0, len(quote)),
                 speaker=speaker, scope=Scope(seat=seat), flags=Flags(firsthand=True), found_by=2)


def check(target: str, verdict: str, claims: list[str], decided_by: str = "agree") -> BeliefCheck:
    return BeliefCheck(target=target, verdict=verdict, claims=claims, decided_by=decided_by)


def digest(call_id: str, *, seat: str | None = "claims adjuster (staff)", date: str | None = None,
           claims: list[Claim] | None = None, checks: list[BeliefCheck] | None = None,
           checked: dict[str, str] | None = None, status: str = "digested",
           coverage: list[QuestionCoverage] | None = None, contact: str | None = None) -> Digest:
    date = date if date is not None else call_id[:10]
    return Digest(call_id=call_id, transcript=f"transcripts/{call_id}.md", status=status, origin="live",
                  date=date, date_source="api", contact=contact or "Guest Person", firm="Firm", seat=seat,
                  prompt_version="v1", claims=list(claims or []), belief_checks=list(checks or []),
                  checked=dict(checked if checked is not None else {"B-001": V1, "B-002": V2, "W-001": VW}),
                  question_coverage=list(coverage or []))


def contra(call_id: str, seat: str, *, target: str = "B-001", decided_by: str = "agree",
           quote: str | None = None, checked: dict | None = None) -> Digest:
    q = quote or f"we never pulled store footage at {call_id}"
    return digest(call_id, seat=seat, claims=[claim("c01", q)],
                  checks=[check(target, "contradicts", ["c01"], decided_by)], checked=checked)


def read_csv(text: str) -> list[dict]:
    return list(csv.DictReader(io.StringIO(text)))


# ---------- loaders ----------

def test_load_verdict_overrides_missing_and_forms(ctx):
    repo = ctx.repo
    repo.delete("config/verdict_overrides.yml")
    assert load_verdict_overrides(repo) == []
    row = {"call_id": "2026-09-18_darius-kemp", "target": "B-001", "quote": "abc", "who": "Alex",
           "reason": "wrong read", "ref": "1.2:verdict:B-001:1"}
    repo.write("config/verdict_overrides.yml", yaml.safe_dump({"overrides": [row]}))
    assert load_verdict_overrides(repo) == [row]
    repo.write("config/verdict_overrides.yml", yaml.safe_dump([row]))
    assert load_verdict_overrides(repo) == [row]
    repo.write("config/verdict_overrides.yml", "# Processor-written. Do not edit by hand.\n")
    assert load_verdict_overrides(repo) == []


def test_load_themes(ctx):
    repo = ctx.repo
    assert load_themes(repo) == []
    themes = {"updated": "2026-09-28", "themes": [
        {"name": "Footage review pain", "claims": [{"call_id": "2026-09-18_darius-kemp", "quote": "45 videos"}]}]}
    repo.write("config/themes.yml", yaml.safe_dump(themes))
    assert load_themes(repo) == themes["themes"]


# ---------- dismissal ----------

def test_is_dismissed_matches_call_target_and_quote_overlap():
    d = contra("2026-09-18_darius-kemp", "claims adjuster (staff)", quote="guide wire has been the big name in claims systems")
    chk = d.belief_checks[0]
    override = {"call_id": d.call_id, "target": "B-001", "quote": "guide wire has been the big name in claims systems"}
    assert is_dismissed(chk, d, [override])
    # a re-digest that shifted the quote a little still matches
    reworded = dict(override, quote="so guide wire, has been the big name in claims system")
    assert is_dismissed(chk, d, [reworded])
    assert not is_dismissed(chk, d, [dict(override, target="B-002")])
    assert not is_dismissed(chk, d, [dict(override, call_id="2026-09-19_other")])
    assert not is_dismissed(chk, d, [dict(override, quote="completely unrelated words here about pricing")])
    assert not is_dismissed(chk, d, [])


def test_a_dismissal_with_a_verdict_and_span_covers_only_that_read():
    """A Wrong read on a short answer must not hide a different claim that shares its words, nor
    another verdict on the same claim."""
    d = digest("2026-09-18_darius-kemp", claims=[
        claim("c01", "I don't think so.", offset=(100, 117)),
        claim("c09", "Yeah, I think so, for small commercial.", offset=(900, 934)),
    ], checks=[check("B-001", "contradicts", ["c01"]), check("B-001", "supports", ["c09"])])
    contradicts, supports = d.belief_checks
    override = {"call_id": d.call_id, "target": "B-001", "quote": "I don't think so.", "offset": [100, 117],
                "verdict": "contradicts", "version": V1}
    assert is_dismissed(contradicts, d, [override])
    assert not is_dismissed(supports, d, [override])                         # other claim, same words
    assert not is_dismissed(supports, d, [dict(override, offset=[900, 934])])   # same claim, other verdict
    # a re-digest that moved the span a little still matches
    assert is_dismissed(contradicts, d, [dict(override, offset=[98, 117])])
    # the span decides even when the words overlap: a contradiction on the other claim stays
    other = check("B-001", "contradicts", ["c09"])
    assert not is_dismissed(other, d, [override])


def test_a_legacy_dismissal_without_a_span_matches_a_run_of_words_not_a_bag():
    d = contra("2026-09-18_darius-kemp", "claims adjuster (staff)", quote="the footage matters more than the report")
    chk = d.belief_checks[0]
    shuffled = {"call_id": d.call_id, "target": "B-001", "quote": "report the than more matters footage the"}
    assert not is_dismissed(chk, d, [shuffled])
    assert is_dismissed(chk, d, [dict(shuffled, quote="the footage matters more")])


def test_a_dismissal_does_not_carry_over_past_a_reword():
    doc = beliefs_doc(entry("B-001", history=[hist("add", V1), hist("reword", "ref-reworded")]))
    d = contra("2026-09-18_darius-kemp", "claims adjuster (staff)", checked={"B-001": "ref-reworded"})
    d.belief_checks[0].verdict = "supports"
    d.belief_checks[0].source = "backcheck"
    override = {"call_id": d.call_id, "target": "B-001", "quote": d.claims[0].quote,
                "offset": list(d.claims[0].offset), "verdict": "contradicts", "version": V1}
    status = entry_status(doc.get("B-001"), [d], [override], ctx_config())
    assert len(status.supports) == 1                                           # the new read counts
    d.belief_checks[0].verdict = "contradicts"
    assert not is_dismissed(d.belief_checks[0], d, [override])                 # made against the new wording


def test_a_claim_whose_only_check_was_dismissed_is_a_finding():
    doc = beliefs_doc(entry("B-001"))
    d = contra("2026-09-18_darius-kemp", "claims adjuster (staff)")
    override = {"call_id": d.call_id, "target": "B-001", "quote": d.claims[0].quote}
    assert [c.id for _, c in finding_claims([d], doc)] == []
    assert [c.id for _, c in finding_claims([d], doc, [override])] == ["c01"]


# ---------- entry status and under pressure ----------

def test_under_pressure_needs_calls_and_seats(ctx):
    b1 = entry("B-001")
    one_call = [contra("2026-09-10_a-a", "claims adjuster (staff)")]
    assert not under_pressure(b1, one_call, [], ctx.config)
    same_seat = one_call + [contra("2026-09-11_b-b", "claims adjuster (staff)")]
    assert not under_pressure(b1, same_seat, [], ctx.config)
    two_seats = one_call + [contra("2026-09-11_b-b", "underwriter")]
    assert under_pressure(b1, two_seats, [], ctx.config)


def test_under_pressure_counts_only_agree_or_arbiter(ctx):
    b1 = entry("B-001")
    weak = [contra("2026-09-10_a-a", "claims adjuster (staff)", decided_by="unarbitrated"),
            contra("2026-09-11_b-b", "underwriter", decided_by="single")]
    assert not under_pressure(b1, weak, [], ctx.config)
    strong = [contra("2026-09-10_a-a", "claims adjuster (staff)", decided_by="arbiter"),
              contra("2026-09-11_b-b", "underwriter", decided_by="agree")]
    assert under_pressure(b1, strong, [], ctx.config)


def test_watching_items_never_go_under_pressure(ctx):
    w1 = entry("W-001", section="watching")
    ds = [contra("2026-09-10_a-a", "claims adjuster (staff)", target="W-001"),
          contra("2026-09-11_b-b", "underwriter", target="W-001")]
    status = entry_status(w1, ds, [], ctx.config)
    assert len(status.contradicts) == 2
    assert not status.under_pressure


def test_thresholds_come_from_config(ctx):
    ctx.config.pipeline.thresholds["under_pressure_calls"] = 3
    b1 = entry("B-001")
    ds = [contra("2026-09-10_a-a", "claims adjuster (staff)"), contra("2026-09-11_b-b", "underwriter")]
    assert not under_pressure(b1, ds, [], ctx.config)
    ds.append(contra("2026-09-12_c-c", "underwriter"))
    assert under_pressure(b1, ds, [], ctx.config)


def test_keep_window_counts_only_later_calls(ctx):
    kept = entry("B-001", history=[hist("add", V1, "2026-09-01"), hist("keep", "k1", "2026-09-20")])
    before = [contra("2026-09-10_a-a", "claims adjuster (staff)"), contra("2026-09-15_b-b", "underwriter")]
    assert not under_pressure(kept, before, [], ctx.config)
    after = before + [contra("2026-09-21_c-c", "claims adjuster (staff)"), contra("2026-09-22_d-d", "underwriter")]
    assert under_pressure(kept, after, [], ctx.config)
    # a contradiction dated the day of the Keep does not count
    same_day = before + [contra("2026-09-20_e-e", "claims adjuster (staff)"), contra("2026-09-22_d-d", "underwriter")]
    assert not under_pressure(kept, same_day, [], ctx.config)
    # the keep does not change the version, so the evidence lists still show every call
    status = entry_status(kept, after, [], ctx.config)
    assert len(status.contradicts) == 4


def test_reword_after_keep_resets_the_window(ctx):
    history = [hist("add", "old", "2026-09-01"), hist("keep", "k1", "2026-09-20"), hist("reword", V1, "2026-09-25")]
    b1 = entry("B-001", history=history)
    ds = [contra("2026-09-10_a-a", "claims adjuster (staff)"), contra("2026-09-15_b-b", "underwriter")]
    assert under_pressure(b1, ds, [], ctx.config)


def test_dismissed_contradiction_does_not_count(ctx):
    b1 = entry("B-001")
    ds = [contra("2026-09-10_a-a", "claims adjuster (staff)", quote="we settle everything without footage"),
          contra("2026-09-11_b-b", "underwriter")]
    assert under_pressure(b1, ds, [], ctx.config)
    override = {"call_id": "2026-09-10_a-a", "target": "B-001", "quote": "we settle everything without footage",
                "who": "Alex", "reason": "wrong read", "ref": "1.1:verdict:B-001:1"}
    status = entry_status(b1, ds, [override], ctx.config)
    assert [i["call_id"] for i in status.contradicts] == ["2026-09-11_b-b"]
    assert not status.under_pressure


def test_version_mismatch_does_not_count(ctx):
    b1 = entry("B-001")
    stale = {"B-001": "an-older-version"}
    ds = [contra("2026-09-10_a-a", "claims adjuster (staff)", checked=stale), contra("2026-09-11_b-b", "underwriter")]
    status = entry_status(b1, ds, [], ctx.config)
    assert [i["call_id"] for i in status.contradicts] == ["2026-09-11_b-b"]
    assert not status.under_pressure
    assert (status.checked, status.total) == (1, 2)


def test_entry_status_lists_and_stubs(ctx):
    b1 = entry("B-001")
    sup = digest("2026-09-12_c-c", seat="claims manager / supervisor", claims=[claim("c01", "we did it all ourselves")],
                 checks=[check("B-001", "supports", ["c01"])])
    mis = digest("2026-09-13_d-d", seat=None, claims=[claim("c01", "only for auto claims")],
                 checks=[check("B-001", "scope_mismatch", ["c01"], "arbiter")])
    stub = make_stub(call_id="internal_x", transcript="transcripts/internal_x.md", status="internal",
                     origin="live", reason="internal call")
    stub.belief_checks = [check("B-001", "contradicts", [])]
    status = entry_status(b1, [contra("2026-09-10_a-a", "claims adjuster (staff)"), sup, mis, stub], [], ctx.config)
    assert isinstance(status, EntryStatus)
    assert status.entry_id == "B-001"
    assert status.supports == [{"call_id": "2026-09-12_c-c", "seat": "claims manager / supervisor",
                                "quote": "we did it all ourselves", "date": "2026-09-12", "decided_by": "agree"}]
    assert status.scope_mismatch[0]["seat"] is None and status.scope_mismatch[0]["decided_by"] == "arbiter"
    assert len(status.contradicts) == 1
    assert (status.checked, status.total) == (3, 3)


# ---------- evidence counts ----------

def test_evidence_counts_supports_by_seat(ctx):
    doc = beliefs_doc(entry("B-001"), entry("W-001", section="watching"), entry("B-009", section="retired"))
    ds = [
        digest("2026-09-10_a-a", seat="claims adjuster (staff)", claims=[claim("c01", "one")],
               checks=[check("B-001", "supports", ["c01"])]),
        digest("2026-09-11_b-b", seat="claims adjuster (staff)", claims=[claim("c01", "two")],
               checks=[check("B-001", "supports", ["c01"]), check("W-001", "contradicts", ["c01"])]),
        digest("2026-09-12_c-c", seat="underwriter", claims=[claim("c01", "three")],
               checks=[check("B-001", "supports", ["c01"])], checked={"B-001": "stale"}),
    ]
    counts = evidence_counts(ds, doc)
    assert set(counts) == {"B-001", "W-001"}
    assert counts["B-001"]["calls"] == 2
    assert counts["B-001"]["seats"] == 1
    assert counts["B-001"]["seat_list"] == ["claims adjuster (staff)"]
    assert counts["B-001"]["seat_calls"] == {"claims adjuster (staff)": 2}
    assert counts["W-001"]["calls"] == 0 and counts["W-001"]["seat_list"] == []


def test_evidence_counts_leave_out_a_dismissed_supports_check(ctx):
    doc = beliefs_doc(entry("B-001"))
    kept = digest("2026-09-10_a-a", seat="claims adjuster (staff)", claims=[claim("c01", "we settle without footage")],
                  checks=[check("B-001", "supports", ["c01"])])
    wrong = digest("2026-09-11_b-b", seat="underwriter", claims=[claim("c01", "we settle from the police report")],
                   checks=[check("B-001", "supports", ["c01"])])
    override = {"call_id": wrong.call_id, "target": "B-001", "quote": wrong.claims[0].quote,
                "offset": list(wrong.claims[0].offset), "verdict": "supports", "version": V1}
    assert evidence_counts([kept, wrong], doc)["B-001"]["calls"] == 2           # no overrides given
    counts = evidence_counts([kept, wrong], doc, overrides=[override])
    assert counts["B-001"] == {"calls": 1, "seats": 1, "seat_list": ["claims adjuster (staff)"],
                               "seat_calls": {"claims adjuster (staff)": 1}}
    status = entry_status(doc.get("B-001"), [kept, wrong], [override], ctx.config)
    assert len({i["call_id"] for i in status.supports}) == counts["B-001"]["calls"]   # the brief and status agree


# ---------- findings ----------

def test_findings_exclude_claims_touched_by_a_current_check():
    doc = beliefs_doc(entry("B-001"), entry("W-001", section="watching"), entry("B-009", section="retired"))
    d = digest("2026-09-18_darius-kemp", claims=[
        claim("c01", "touched by a belief", offset=(0, 10)),
        claim("c02", "touched by a watching item", offset=(20, 30)),
        claim("c03", "touched only by a retired entry", offset=(40, 50)),
        claim("c04", "touched by a stale check", offset=(60, 70)),
        claim("c05", "touched by nothing", offset=(80, 90)),
    ], checks=[
        check("B-001", "supports", ["c01"]),
        check("W-001", "contradicts", ["c02"]),
        check("B-009", "supports", ["c03"]),
        check("B-002", "contradicts", ["c04"]),
    ], checked={"B-001": V1, "W-001": VW, "B-009": VR, "B-002": "stale"})
    doc.entries.append(entry("B-002"))
    stub = make_stub(call_id="x", transcript="transcripts/x.md", status="failed", origin="live", reason="boom")
    found = finding_claims([d, stub], doc)
    assert [c.id for _, c in found] == ["c03", "c04", "c05"]
    assert all(dg is d for dg, _ in found)


def test_findings_exclude_sources_of_current_entries():
    source = ("2026-09-18_darius-kemp", "now I have 45 videos across these different claims")
    doc = beliefs_doc(entry("B-001", history=[hist("add", V1, sources=[source])]))
    d = digest("2026-09-18_darius-kemp", claims=[
        claim("c01", "now I have 45 videos across these different claims", offset=(0, 10)),
        claim("c02", "we review every clip by hand on a spreadsheet", offset=(20, 30)),
    ])
    assert [c.id for _, c in finding_claims([d], doc)] == ["c02"]


def test_a_held_source_spanning_two_claims_holds_both():
    """An agent's request quoting a passage that covers two claims: apply refuses a new source at
    either claim as a duplicate (span overlap), so neither is a finding."""
    call = "2026-09-18_darius-kemp"
    q1 = "now I have 45 videos across these different claims"
    q2 = "I need to optimally watch each individual video every day for these claims"
    passage = f"{q1}. I have these two hour long recordings, and {q2}"
    d = digest(call, claims=[
        claim("c01", q1, offset=(100, 100 + len(q1))),
        claim("c02", q2, offset=(210, 210 + len(q2))),
        claim("c03", "we review every clip by hand on a spreadsheet", offset=(400, 445)),
    ])
    doc = beliefs_doc(entry("B-001", history=[hist("add", "requests/x.yml@abc", sources=[(call, passage)])]))
    assert [c.id for _, c in finding_claims([d], doc)] == ["c03"]


def test_findings_place_identical_short_answers_as_apply_does():
    """Four "Exactly." answers. W-001 watched the second from its finding menu (the ref names the
    span), then B-001 held the first by a request: the first two are held, the others stay findings."""
    call = "2026-09-20_lena-okafor"
    d = digest(call, claims=[claim(f"c{n:02d}", "Exactly.", offset=(n * 100, n * 100 + 8)) for n in range(1, 5)])
    w1 = entry("W-001", section="watching",
               history=[hist("watch", "111.1:finding:200-208", date="2026-09-21", sources=[(call, "Exactly.")])])
    b1 = entry("B-001", history=[hist("add", "requests/x.yml@abc", date="2026-09-22", sources=[(call, "exactly")])])
    doc = beliefs_doc(b1, w1)
    assert [c.id for _, c in finding_claims([d], doc)] == ["c03", "c04"]
    # without the finding ref, the earlier change holds the earlier answer, whatever the file order
    w1.history[0].ref = "requests/w.yml@def"
    assert [c.id for _, c in finding_claims([d], doc)] == ["c03", "c04"]
    doc = beliefs_doc(entry("W-001", section="watching", history=[
        hist("watch", "111.1:finding:300-308", sources=[(call, "Exactly.")])]))
    assert [c.id for _, c in finding_claims([d], doc)] == ["c01", "c02", "c04"]


def ctx_config():
    from discovery import config as config_mod
    from conftest import ROOT
    return config_mod.load(ROOT)


# ---------- regenerate ----------

def seed(ctx, digests: list[Digest], doc: BeliefsDoc, *, decisions: list[Decision] | None = None,
         themes: dict | None = None, overrides: list[dict] | None = None) -> None:
    for d in digests:
        write_digest(ctx.repo, d)
    ctx.repo.write("beliefs.md", render_beliefs(doc))
    ctx.repo.write("decisions.md", render_decisions(decisions or []))
    if themes is not None:
        ctx.repo.write("config/themes.yml", yaml.safe_dump(themes, sort_keys=False))
    if overrides is not None:
        ctx.repo.write("config/verdict_overrides.yml", yaml.safe_dump({"overrides": overrides}, sort_keys=False))


def corpus() -> tuple[list[Digest], BeliefsDoc]:
    doc = beliefs_doc(
        entry("B-001", statement="Video footage is the most valuable evidence on a liability claim"),
        entry("B-002", statement="Small business footage is often overwritten before the claim is reported"),
        entry("W-001", section="watching", statement="Commercial lines get less claims software than personal lines"),
    )
    darius = digest("2026-09-18_darius-kemp", seat="claims adjuster (staff)", contact="Darius Kemp", claims=[
        claim("c01", "now I have 45 videos across these different claims", offset=(100, 160),
              text="Video review across claims is manual"),
        claim("c02", "we never had any footage, it was all from witnesses", offset=(200, 250), seat="claims adjuster (staff)"),
        claim("c03", f"so I get 30 seconds of the video {EM_DASH} maybe less", offset=(300, 340)),
    ], checks=[check("B-001", "contradicts", ["c02"]), check("W-001", "supports", ["c03"])],
        coverage=[QuestionCoverage(question="How long a store video have you seen?", status="dodged"),
                  QuestionCoverage(question="What does the claim file look like?", status="never_asked")])
    wade = digest("2026-09-20_wade-pruitt", seat="underwriter", contact="Wade Pruitt", claims=[
        claim("c01", "honestly we needed a field investigator from day one", offset=(50, 90)),
        claim("c02", "the footage request is where every claim slows down", offset=(120, 170)),
        claim("c03", "we review every clip by hand on a spreadsheet", offset=(200, 240)),
    ], checks=[check("B-001", "contradicts", ["c01"], "arbiter")],
        coverage=[QuestionCoverage(question="how long a store video have you seen", status="answered")])
    stub = make_stub(call_id="granola_abc", transcript="transcripts/granola_abc.md", status="parse_failed",
                     origin="live", reason="unrecognized shape")
    return [darius, wade, stub], doc


def test_regenerate_writes_every_generated_file(ctx):
    digests, doc = corpus()
    seed(ctx, digests, doc)
    paths = regenerate(ctx)
    assert paths == GENERATED
    for rel in GENERATED:
        assert ctx.repo.exists(rel)
        assert EM_DASH not in ctx.repo.read(rel)


def test_calls_csv_columns_and_rows(ctx):
    digests, doc = corpus()
    seed(ctx, digests, doc)
    ctx.config.overrides["2026-09-20_wade-pruitt"] = {"seat": "pricing actuary", "firm": "Example Mutual"}
    regenerate(ctx)
    text = ctx.repo.read("calls.csv")
    assert text.splitlines()[0] == ",".join(CALLS_COLUMNS)
    rows = read_csv(text)
    assert [r["call_id"] for r in rows] == ["2026-09-18_darius-kemp", "2026-09-20_wade-pruitt", "granola_abc"]
    darius, wade, stub = rows
    assert darius["claims"] == "3" and darius["status"] == "digested" and darius["seat"] == "claims adjuster (staff)"
    assert wade["seat"] == "pricing actuary" and wade["firm"] == "Example Mutual"
    assert stub["status"] == "parse_failed" and stub["claims"] == "0" and stub["contact"] == ""


def test_claims_csv_columns_and_targets(ctx):
    digests, doc = corpus()
    seed(ctx, digests, doc)
    regenerate(ctx)
    text = ctx.repo.read("claims.csv")
    assert text.splitlines()[0] == ",".join(CLAIMS_COLUMNS)
    rows = read_csv(text)
    assert len(rows) == 6
    first = rows[0]
    assert (first["call_id"], first["claim_id"], first["date"]) == ("2026-09-18_darius-kemp", "c01", "2026-09-18")
    assert first["belief_targets"] == "" and first["found_by"] == "2"
    by_quote = {r["quote"]: r for r in rows}
    assert by_quote["we never had any footage, it was all from witnesses"]["belief_targets"] == "B-001 contradicts"
    assert rows[1]["seat"] == "claims adjuster (staff)"
    # a claim with no scope seat falls back to the call's seat
    assert rows[3]["seat"] == "underwriter"


def test_findings_md_groups_by_theme_then_ungrouped_newest_first(ctx):
    digests, doc = corpus()
    themes = {"updated": "2026-09-19", "themes": [{"name": "Footage review pain", "claims": [
        {"call_id": "2026-09-18_darius-kemp", "quote": "now I have 45 videos across these different claims"},
        {"call_id": "2026-09-20_wade-pruitt", "quote": "the footage request is where every claim slows down"},
    ]}]}
    seed(ctx, digests, doc, themes=themes)
    regenerate(ctx)
    text = ctx.repo.read("findings.md")
    assert "## Footage review pain (2 calls, 2 seats)" in text
    assert "## Ungrouped since 2026-09-19" in text
    theme_part, ungrouped = text.split("## Ungrouped since 2026-09-19")
    assert "45 videos" in theme_part and "every claim slows down" in theme_part
    # touched claims are not findings; the W-001 support is not either
    assert "any footage" not in text and "field investigator" not in text and "30 seconds" not in text
    assert "45 videos" not in ungrouped
    assert "by hand on a spreadsheet" in ungrouped


def test_findings_md_without_roll_up(ctx):
    digests, doc = corpus()
    seed(ctx, digests, doc)
    regenerate(ctx)
    text = ctx.repo.read("findings.md")
    assert "## Ungrouped (no roll-up yet)" in text
    ungrouped = text.split("## Ungrouped (no roll-up yet)")[1]
    # newest first: Wade (09-20) before Darius (09-18)
    assert ungrouped.index("2026-09-20_wade-pruitt") < ungrouped.index("2026-09-18_darius-kemp")


def test_a_dismissed_read_puts_its_claim_back_in_findings_md(ctx):
    digests, doc = corpus()
    darius = digests[0]
    override = {"call_id": darius.call_id, "target": "B-001", "quote": darius.claims[1].quote,
                "offset": list(darius.claims[1].offset), "verdict": "contradicts", "version": V1,
                "who": "Alex", "reason": "about footage", "ref": "1.2:verdict:B-001:1"}
    seed(ctx, digests, doc, overrides=[override])
    regenerate(ctx)
    assert "we never had any footage" in ctx.repo.read("findings.md")
    b1 = ctx.repo.read("beliefs-status.md").split("### B-001")[1].split("### B-002")[0]
    assert "any footage" not in b1


def test_beliefs_status_md(ctx):
    digests, doc = corpus()
    decisions = [Decision(id="D-002", title="Footage review tool", rests_on=["B-001", "B-002"])]
    seed(ctx, digests, doc, decisions=decisions)
    regenerate(ctx)
    text = ctx.repo.read("beliefs-status.md")
    assert "### B-001 Video footage is the most valuable evidence on a liability claim" in text
    b1 = text.split("### B-001")[1].split("### B-002")[0]
    assert "- under pressure: contradicted by 2 calls in 2 seats" in b1
    assert "under pressure" not in text.split("### B-002")[1].split("## Watching")[0]
    assert "Under pressure: B-001." in text
    assert "checked 2/2 calls" in text
    assert "D-002 Footage review tool" in text
    assert "we never had any footage, it was all from witnesses" in text
    assert "### W-001" in text
    assert "## Retired" not in text


def test_open_questions_md(ctx):
    digests, doc = corpus()
    seed(ctx, digests, doc)
    ctx.config.seats = ["claims adjuster (staff)", "underwriter", "claims manager / supervisor", "other"]
    regenerate(ctx)
    text = ctx.repo.read("open-questions.md")
    # dodged at 09-18, answered later at 09-20 (same text after normalization): dropped
    assert "How long a store video" not in text
    assert "What does the claim file look like?" in text
    assert "never asked" in text
    under = text.split("## Beliefs under pressure")[1]
    assert "### B-001" in under
    assert "claims adjuster (staff)" in under and "underwriter" in under
    assert "claims manager / supervisor" in under
    assert "<!-- manual:start -->" in text and "<!-- manual:end -->" in text


def test_open_question_answered_earlier_stays_open(ctx):
    doc = beliefs_doc(entry("B-001"))
    earlier = digest("2026-09-10_a-a", coverage=[QuestionCoverage(question="Who reviews your footage?", status="answered")])
    later = digest("2026-09-18_b-b", coverage=[QuestionCoverage(question="Who reviews your footage?", status="dodged")])
    seed(ctx, [earlier, later], doc)
    regenerate(ctx)
    assert "Who reviews your footage?" in ctx.repo.read("open-questions.md")


def test_manual_section_preserved(ctx):
    digests, doc = corpus()
    seed(ctx, digests, doc)
    manual = "\n- Ask Wade about the fleet camera vendor (Alex)\n- Who at Example Mutual owns footage review?\n"
    ctx.repo.write("open-questions.md", "# Open questions\n\nold generated text\n\n"
                                        f"<!-- manual:start -->{manual}<!-- manual:end -->\n")
    regenerate(ctx)
    text = ctx.repo.read("open-questions.md")
    assert f"<!-- manual:start -->{manual}<!-- manual:end -->" in text
    assert "old generated text" not in text


def test_regenerate_is_deterministic(ctx):
    digests, doc = corpus()
    themes = {"themes": [{"name": "Footage review pain", "claims": [
        {"call_id": "2026-09-18_darius-kemp", "quote": "now I have 45 videos across these different claims"}]}]}
    overrides = [{"call_id": "2026-09-18_darius-kemp", "target": "B-001",
                  "quote": "we never had any footage, it was all from witnesses", "who": "Alex",
                  "reason": "wrong read", "ref": "1.1:verdict:B-001:1"}]
    seed(ctx, digests, doc, themes=themes, overrides=overrides)
    regenerate(ctx)
    first = {rel: ctx.repo.read(rel) for rel in GENERATED}
    regenerate(ctx)
    second = {rel: ctx.repo.read(rel) for rel in GENERATED}
    assert first == second
    # the dismissal took B-001 out of pressure
    assert "## Beliefs under pressure\n\nNone." in first["open-questions.md"]


def test_regenerate_with_empty_repo(ctx):
    paths = regenerate(ctx)
    assert paths == GENERATED
    assert ctx.repo.read("calls.csv").splitlines() == [",".join(CALLS_COLUMNS)]
    assert ctx.repo.read("claims.csv").splitlines() == [",".join(CLAIMS_COLUMNS)]


@pytest.mark.parametrize("rel", GENERATED)
def test_generated_list(rel):
    assert rel in ("calls.csv", "claims.csv", "findings.md", "beliefs-status.md", "open-questions.md")


# ---------- findings with several quotes ----------

def two_quotes(cid: str, first: str, second: str, at: int = 0) -> Claim:
    return Claim(id=cid, text=f"finding {cid}", scope=Scope(), flags=Flags(firsthand=True), found_by=2,
                 quotes=[Quote(quote=first, turns=[1], offset=(at, at + 10), speaker="Guest"),
                         Quote(quote=second, turns=[9], offset=(at + 500, at + 510), speaker="Guest",
                               attribution="inferred")])


def test_claims_csv_joins_a_findings_quotes(ctx):
    d = digest("2026-09-18_nadia-ferris", claims=[two_quotes("c01", "late notices are the biggest worry",
                                                       "a missed deadline can bring the regulator in to audit us")])
    seed(ctx, [d], beliefs_doc(entry("B-001")))
    regenerate(ctx)
    [row] = read_csv(ctx.repo.read("claims.csv"))
    assert row["quote"] == "late notices are the biggest worry | a missed deadline can bring the regulator in to audit us"
    assert (row["speaker"], row["attribution"]) == ("Guest", "span")
    text = ctx.repo.read("findings.md")
    assert "  > late notices are the biggest worry" in text
    assert "  > a missed deadline can bring the regulator in to audit us" in text


def test_a_source_on_a_findings_second_quote_holds_it():
    source = ("2026-09-18_nadia-ferris", "a missed deadline can bring the regulator in to audit us")
    doc = beliefs_doc(entry("B-001", history=[hist("add", V1, sources=[source])]))
    d = digest("2026-09-18_nadia-ferris", claims=[
        two_quotes("c01", "late notices are the biggest worry", "a missed deadline can bring the regulator in to audit us"),
        claim("c02", "we review every clip by hand on a spreadsheet", offset=(2000, 2030)),
    ])
    assert [c.id for _, c in finding_claims([d], doc)] == ["c02"]


def test_a_dismissal_on_any_quote_covers_the_finding():
    d = digest("2026-09-18_nadia-ferris", claims=[two_quotes("c01", "late notices are the biggest worry",
                                                       "a missed deadline can bring the regulator in to audit us")],
               checks=[check("B-001", "contradicts", ["c01"])])
    chk = d.belief_checks[0]
    assert is_dismissed(chk, d, [{"call_id": d.call_id, "target": "B-001", "offset": [500, 510]}])
    assert is_dismissed(chk, d, [{"call_id": d.call_id, "target": "B-001",
                                  "quote": "a missed deadline can bring the regulator in to audit us"}])
    assert not is_dismissed(chk, d, [{"call_id": d.call_id, "target": "B-001", "offset": [200, 210]}])
