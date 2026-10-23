"""Tests for discovery/brief.py: Slack blocks and plain text for the per-call brief."""
from __future__ import annotations

import json
from dataclasses import dataclass, field

import pytest

from discovery.brief import (MAX_BLOCKS, BriefMessages, finding_from_block, render_brief, render_text,
                       shown_quote)
from discovery.types import (BeliefCheck, Claim, Decision, Digest, Entry, Flags, FollowUp, QuestionCoverage, Quote,
                       Scope, Verification)

try:  # Task 3 builds the real class; fall back to a stand-in with the same lookup methods.
    from discovery.beliefs import BeliefsDoc
except ImportError:  # pragma: no cover - only while discovery/beliefs.py is missing
    @dataclass
    class BeliefsDoc:  # type: ignore[no-redef]
        entries: list[Entry] = field(default_factory=list)

        def get(self, entry_id: str) -> Entry | None:
            return next((e for e in self.entries if e.id == entry_id), None)

        def current(self) -> list[Entry]:
            return [e for e in self.entries if e.is_current]


CALL = "2026-09-23_darius-kemp"
EM_DASH = "\u2014"


def _entry(entry_id: str, statement: str, section: str = "beliefs") -> Entry:
    return Entry(id=entry_id, statement=statement, scope=Scope(), section=section)


BELIEFS = BeliefsDoc(entries=[
    _entry("B-001", "Video footage is the most valuable evidence on a liability claim"),
    _entry("B-002", "Small business footage is often overwritten before the claim is reported"),
    _entry("B-003", "Adjusters lose real time scrubbing long footage for the moment of the incident"),
    _entry("B-004", "Adjusters can use outside AI tools on claim files"),
    _entry("B-005", "Carriers publish how they pick claims vendors"),
    _entry("W-001", "Commercial lines get less claims software than personal lines", "watching"),
])
DECISIONS = [Decision(id="D-002", title="Footage review tool for adjusters", rests_on=["B-001", "B-002", "B-003"])]


def _claim(n: int, *, text: str | None = None, quote: str | None = None, firsthand: bool = False,
           prompted: bool = True, found_by: int = 1, question_turn: int | None = None) -> Claim:
    return Claim(id=f"c{n:02d}", text=text or f"Claim number {n}", quote=quote or f"quote number {n} from the call",
                 turns=[n], question_turn=question_turn, offset=(n * 100, n * 100 + 40), speaker="Darius Kemp",
                 flags=Flags(firsthand=firsthand, prompted=prompted), found_by=found_by)


def _digest(**kw) -> Digest:
    base = dict(call_id=CALL, transcript="transcripts/circleback_123.md", status="digested", origin="live",
                source="circleback", date="2026-09-23", recorded_by="alex", contact="Darius Kemp", firm="Example Mutual",
                seat="claims adjuster (staff)", prep="prep/2026-09-23_darius-kemp.md",
                verification=Verification(quotes=9, matched_first_pass=8, repaired=1))
    base.update(kw)
    return Digest(**base)


def _full_digest() -> Digest:
    claims = [
        _claim(1, text="Carriers spend less on commercial claims than personal lines",
               quote="And on the commercial side the carriers buy a lot less of it than they do for personal auto and homeowners.",
               firsthand=True, prompted=False, found_by=2),
        _claim(2, text="Finding the incident in long footage is not slow",
               quote="I don't think so.", question_turn=1, found_by=2),
        _claim(3, text="Video is the best evidence on a claim",
               quote="Honestly video is the single best thing you can have on a liability file."),
        _claim(4, text="Footage has to be pulled before anything else", quote="you need the footage first"),
        _claim(5, text="Carriers never write vendor criteria down", quote="nobody writes it down"),
        _claim(6, text="Footage review is an unsolved problem",
               quote="now I have 45 videos across these different claims", firsthand=True),
        _claim(7, text="Outside AI tools came in late", quote="the AI tools came in later"),
    ]
    checks = [
        # B-005 is listed first but is not linked to a decision, so B-003 must still lead.
        BeliefCheck(target="B-005", verdict="contradicts", claims=["c05"], decided_by="single"),
        BeliefCheck(target="B-003", verdict="contradicts", claims=["c02"], decided_by="agree",
                    note="Staff adjuster on short clips; B-003 came from independent adjusters."),
        BeliefCheck(target="W-001", verdict="supports", claims=["c01"], decided_by="agree"),
        BeliefCheck(target="B-001", verdict="supports", claims=["c03"], decided_by="agree"),
        BeliefCheck(target="B-004", verdict="supports", claims=["c07"], decided_by="agree"),
        BeliefCheck(target="B-002", verdict="scope_mismatch", claims=["c04"], decided_by="arbiter",
                    note="Fleet dashcam, not store CCTV."),
    ]
    return _digest(
        claims=claims, belief_checks=checks, new_findings=["c06"],
        summary=["Footage requests pile up by accident", "Preferences surface as price"],
        follow_ups=[FollowUp(direction="we_owe", what="send deck", who="Darius Kemp"),
                    FollowUp(direction="they_offered", what="research Guidewire", who="Darius Kemp",
                             quote="look at guide wire")],
        ask_next=["How do other claims leads split footage review across adjusters?"],
        question_coverage=[
            QuestionCoverage(question="How long a video have you had to review?", status="dodged",
                             claims=["c04"]),
            QuestionCoverage(question="What does the claim file look like?", status="never_asked"),
            QuestionCoverage(question="How does a vendor learn a carrier's criteria?", status="answered",
                             claims=["c01"]),
        ],
    )


def _counts(**by_entry) -> dict[str, dict]:
    """counts keyed by entry id, each (calls, seat_list)."""
    return {k: {"calls": calls, "seats": len(seats), "seat_list": list(seats)}
            for k, (calls, seats) in by_entry.items()}


# Seats that already hold evidence, so nothing is new unless a test says so.
SEEN = _counts(**{"B-001": (3, ["claims manager / supervisor", "claims adjuster (staff)"]),
                  "B-004": (4, ["claims adjuster (staff)", "independent adjuster / TPA"]),
                  "W-001": (2, ["claims adjuster (staff)"])})


def _all_messages(msgs: BriefMessages) -> list[tuple[list[dict], str]]:
    return [(msgs.top_blocks, msgs.top_text)] + list(msgs.thread)


def _all_values(blocks: list[dict]) -> list[str]:
    values = []
    for b in blocks:
        acc = b.get("accessory") or {}
        values += [o["value"] for o in acc.get("options", [])]
        if "value" in acc:
            values.append(acc["value"])
        values += [e["value"] for e in b.get("elements", []) if "value" in e]
    return values


def _block(blocks: list[dict], block_id: str) -> dict:
    return next(b for b in blocks if b.get("block_id") == block_id)


def _reply(msgs: BriefMessages, title_id: str) -> tuple[list[dict], str]:
    """The first thread reply whose blocks include `title_id` (e.g. "findings:title", "summary")."""
    return next(r for r in msgs.thread if any(b.get("block_id") == title_id for b in r[0]))


def _findings(msgs: BriefMessages) -> tuple[list[dict], str]:
    return _reply(msgs, "findings:title")


def _summary(msgs: BriefMessages) -> str:
    return _reply(msgs, "summary")[1]


# ---------- top message ----------

def test_header_omits_missing_fields():
    msgs = render_brief(_full_digest(), BELIEFS, DECISIONS, counts=SEEN)
    assert msgs.top_text.splitlines()[0] == "Darius Kemp · Example Mutual · claims adjuster (staff) · Alex · Circleback"

    bare = _digest(firm=None, seat=None, recorded_by=None, source=None, origin="backfill")
    msgs = render_brief(bare, BELIEFS, [], counts={})
    assert msgs.top_text.splitlines()[0] == "Darius Kemp"
    assert "None" not in render_text(msgs)


def test_header_shows_the_recorded_call_length():
    d = _full_digest()
    d.duration_min = 31
    msgs = render_brief(d, BELIEFS, DECISIONS, counts=SEEN)
    assert msgs.top_text.splitlines()[0] == "Darius Kemp · Example Mutual · claims adjuster (staff) · 31m · Alex · Circleback"


def test_decision_line_and_ordering():
    msgs = render_brief(_full_digest(), BELIEFS, DECISIONS, counts=SEEN)
    text = msgs.top_text
    assert "Touches D-002 (Footage review tool for adjusters)" in text
    order = [text.index(s) for s in (
        "Contradicts B-003", "Contradicts B-005", "Scope mismatch B-002", "Supports B-001", "Watching W-001")]
    assert order == sorted(order)
    # The decision line sits above WHAT CHANGED.
    assert text.index("Touches D-002") < text.index("WHAT CHANGED")


def test_no_decision_line_when_only_supports_hit_a_linked_belief():
    d = _full_digest()
    d.belief_checks = [c for c in d.belief_checks if c.verdict == "supports"]
    msgs = render_brief(d, BELIEFS, DECISIONS, counts=SEEN)
    assert "Touches" not in msgs.top_text


def test_wrong_read_button_and_labels():
    d = _full_digest()
    d.belief_checks.append(BeliefCheck(
        target="B-004", verdict="contradicts", claims=["c07"], decided_by="unarbitrated",
        votes={"anthropic:claude-sonnet-5": "contradicts", "openai:gpt-6-sol": "none"}))
    msgs = render_brief(d, BELIEFS, DECISIONS, counts=SEEN)
    actions = _block(msgs.top_blocks, "verdict:B-003:1")
    assert actions["type"] == "actions"
    button = actions["elements"][0]
    assert button["type"] == "button" and button["text"]["text"] == "Wrong read"
    assert button["value"] == f"{CALL}|B-003|200-240|contradicts|"
    assert _block(msgs.top_blocks, "verdict:B-005:1")["elements"][0]["value"] == f"{CALL}|B-005|500-540|contradicts|"

    text = msgs.top_text
    assert 'Contradicts B-003 "Adjusters lose real time' in text
    assert "[2/2 agree]" in text
    assert "[1 model]" in text
    assert "[arbiter]" in text
    assert "[unarbitrated: claude-sonnet-5 says contradicts, gpt-6-sol says no link]" in text
    assert '"I don\'t think so."' in text
    assert "Staff adjuster on short clips" in text
    # Short answers carry the claim text so the quote reads on its own.
    assert "Finding the incident in long footage is not slow" in text


@pytest.mark.parametrize("version, tail", [
    ("1790607600.000100:prop-02", "|contradicts|1790607600.000100:prop-02"),
    ("v" * 106, "||" + "v" * 106),       # too long with the verdict: the verdict goes first
    ("v" * 120, ""),                     # too long even then: the version goes too
])
def test_wrong_read_value_carries_the_version_the_read_was_checked_against(version, tail):
    d = _full_digest()
    d.checked = {"B-003": version}
    msgs = render_brief(d, BELIEFS, DECISIONS, counts=SEEN)
    value = _block(msgs.top_blocks, "verdict:B-003:1")["elements"][0]["value"]
    assert value == f"{CALL}|B-003|200-240{tail}" and len(value) < 150


def test_supports_collapse_when_no_seat_is_new():
    msgs = render_brief(_full_digest(), BELIEFS, DECISIONS, counts=SEEN)
    lines = msgs.top_text.splitlines()
    assert "Supports B-001, B-004" in lines
    assert not any(line.startswith('Supports B-001 "') for line in lines)


def test_supports_expand_for_a_new_seat():
    counts = dict(SEEN)
    # B-004: two earlier calls, both independent adjusters, plus this staff adjuster call.
    counts["B-004"] = {"calls": 3, "seats": 2, "seat_list": ["independent adjuster / TPA", "claims adjuster (staff)"],
                       "seat_calls": {"independent adjuster / TPA": 2, "claims adjuster (staff)": 1}}
    msgs = render_brief(_full_digest(), BELIEFS, DECISIONS, counts=counts)
    lines = msgs.top_text.splitlines()
    assert "Supports B-001" in lines
    expanded = next(line for line in lines if line.startswith('Supports B-004 "'))
    assert "now 3 calls, 2 seats" in expanded
    assert "new seat: claims adjuster (staff)" in expanded


def test_new_seat_inferred_without_seat_calls():
    counts = dict(SEEN)
    # One call per seat means this call's seat appears nowhere else.
    counts["B-001"] = {"calls": 2, "seats": 2, "seat_list": ["claims manager / supervisor", "claims adjuster (staff)"]}
    msgs = render_brief(_full_digest(), BELIEFS, DECISIONS, counts=counts)
    assert any(line.startswith('Supports B-001 "') for line in msgs.top_text.splitlines())


def test_count_line_and_digest_link():
    msgs = render_brief(_full_digest(), BELIEFS, DECISIONS, counts=SEEN,
                        digest_url="https://github.com/discovery/discovery/blob/main/digests/x.md")
    assert "1 new finding · 1 dodged question" in msgs.top_text
    assert "follow-up" not in msgs.top_text
    counts_block = msgs.top_blocks[-1]
    assert counts_block["type"] == "context"
    assert "<https://github.com/discovery/discovery/blob/main/digests/x.md|digest on GitHub>" in \
        counts_block["elements"][0]["text"]


def test_no_belief_touched_shows_three_strongest_findings():
    claims = [
        _claim(1, text="weak one", found_by=1),
        _claim(2, text="found by two", found_by=2),
        _claim(3, text="unprompted", prompted=False),
        _claim(4, text="firsthand", firsthand=True),
        _claim(5, text="best", firsthand=True, prompted=False, found_by=2),
    ]
    d = _digest(claims=claims, new_findings=[c.id for c in claims])
    msgs = render_brief(d, BELIEFS, DECISIONS, counts={})
    text = msgs.top_text
    assert "No belief touched" in text
    assert "Touches" not in text
    shown = [line[2:] for line in text.splitlines() if line.startswith("- ")]
    assert shown == ["best", "firsthand", "unprompted"]
    assert '"quote number 5 from the call"' in text
    assert "5 new findings" in text


def test_new_findings_fall_back_to_untouched_claims():
    d = _full_digest()
    d.new_findings = []
    msgs = render_brief(d, BELIEFS, DECISIONS, counts=SEEN)
    # c06 is the only claim no belief check cites.
    assert "1 new finding ·" in msgs.top_text


# ---------- thread ----------

def test_findings_reply_has_menus():
    msgs = render_brief(_full_digest(), BELIEFS, DECISIONS, counts=SEEN)
    blocks, text = _findings(msgs)
    block = _block(blocks, "finding:600-640")
    menu = block["accessory"]
    assert menu["type"] == "overflow"
    assert [o["text"]["text"] for o in menu["options"]] == ["Make belief", "Watch"]
    assert [o["value"] for o in menu["options"]] == [f"mb|{CALL}|600-640", f"w|{CALL}|600-640"]
    assert "NEW FINDINGS" in text
    assert finding_from_block(block) == ("Footage review is an unsolved problem",
                                         "now I have 45 videos across these different claims")


def test_sixty_findings_split_under_block_limit():
    claims = [_claim(n) for n in range(1, 61)]
    d = _digest(claims=claims, new_findings=[c.id for c in claims])
    msgs = render_brief(d, BELIEFS, DECISIONS, counts={})
    finding_msgs = [blocks for blocks, _ in msgs.thread
                    if any(str(b.get("block_id", "")).startswith("finding:") for b in blocks)]
    assert len(finding_msgs) >= 2
    for blocks, _ in _all_messages(msgs):
        assert len(blocks) <= MAX_BLOCKS
    ids = [b["block_id"] for blocks in finding_msgs for b in blocks if b["block_id"].startswith("finding:")]
    assert len(ids) == 60
    assert f"finding:{60 * 100}-{60 * 100 + 40}" in ids


def test_locked_findings_render_without_menu():
    d = _full_digest()
    d.new_findings = ["c01", "c06"]
    acted = [(CALL, "And on the commercial side the carriers buy a lot less"),
             ("2026-01-01_other", "now I have 45 videos")]
    msgs = render_brief(d, BELIEFS, DECISIONS, counts=SEEN, acted_sources=acted)
    blocks, text = _findings(msgs)
    locked = _block(blocks, "finding:100-140")
    assert "accessory" not in locked
    lock_line = _block(blocks, "finding-locked:100-140")
    assert lock_line["type"] == "context"
    # Same quote from a different call does not lock.
    assert _block(blocks, "finding:600-640")["accessory"]["type"] == "overflow"
    assert "Already in beliefs.md" in text


def _exactly_digest() -> Digest:
    claims = [_claim(n, quote="Exactly.") for n in range(1, 5)]
    for c in claims:
        c.quotes[0].offset = (c.offset[0], c.offset[0] + 8)
    return _digest(claims=claims, new_findings=[c.id for c in claims])


def _locked_ids(msgs: BriefMessages) -> list[str]:
    return [b["block_id"].split(":", 1)[1] for blocks, _ in msgs.thread for b in blocks
            if str(b.get("block_id", "")).startswith("finding-locked:")]


def test_one_held_exactly_locks_one_finding_not_every_exactly():
    """Four "Exactly." answers. W-001 watched the second from its finding menu (the ref names the
    span), then B-001 held one more by a request: two findings lock, as findings.md places them."""
    from discovery.brief import acted_on
    from discovery.types import HistoryLine

    def hist(action, ref, date, quote):
        return HistoryLine(date=date, action=action, who="Alex", via="slack", ref=ref, sources=[(CALL, quote)])

    d = _exactly_digest()
    w1 = Entry(id="W-001", statement="Watched", scope=Scope(), section="watching",
               history=[hist("watch", "111.1:finding:200-208", "2026-09-21", "Exactly.")])
    doc = BeliefsDoc(entries=[w1])
    spans, quotes = acted_on(d, doc)
    assert (spans, quotes) == ([(200, 208)], [])
    msgs = render_brief(d, doc, DECISIONS, counts={}, acted_spans=spans, acted_sources=quotes)
    assert _locked_ids(msgs) == ["200-208"]

    b1 = Entry(id="B-001", statement="Held", scope=Scope(), section="beliefs",
               history=[hist("add", "requests/x.yml@abc", "2026-09-22", "exactly")])
    doc = BeliefsDoc(entries=[b1, w1])
    spans, quotes = acted_on(d, doc)
    msgs = render_brief(d, doc, DECISIONS, counts={}, acted_spans=spans, acted_sources=quotes)
    assert _locked_ids(msgs) == ["100-108", "200-208"]


def test_a_source_from_another_call_or_no_claim_locks_nothing():
    from discovery.brief import acted_on
    from discovery.types import HistoryLine

    d = _exactly_digest()
    other = HistoryLine(date="2026-09-21", action="add", who="Alex", via="slack", ref="r1",
                        sources=[("2026-01-01_other", "Exactly."), (CALL, "something nobody said")])
    doc = BeliefsDoc(entries=[Entry(id="B-001", statement="Held", scope=Scope(), section="beliefs", history=[other])])
    spans, quotes = acted_on(d, doc)
    assert (spans, quotes) == ([], [(CALL, "something nobody said")])
    msgs = render_brief(d, doc, DECISIONS, counts={}, acted_spans=spans, acted_sources=quotes)
    assert _locked_ids(msgs) == []


def test_a_source_spanning_several_claims_locks_each_of_them():
    from discovery.brief import acted_on
    from discovery.types import HistoryLine

    d = _full_digest()
    d.new_findings = ["c03", "c04", "c05"]
    held = HistoryLine(date="2026-09-21", action="add", who="Alex", via="slack", ref="r1",
                       sources=[(CALL, "you need the footage first nobody writes it down")])
    doc = BeliefsDoc(entries=[Entry(id="B-001", statement="Held", scope=Scope(), section="beliefs", history=[held])])
    spans, quotes = acted_on(d, doc)
    assert spans == [(400, 540)] and quotes == []
    msgs = render_brief(d, doc, DECISIONS, counts={}, acted_spans=spans, acted_sources=quotes)
    assert _locked_ids(msgs) == ["400-440", "500-540"]


def test_a_held_span_locks_a_finding_it_overlaps_by_half_even_when_not_inside_it():
    """The rule apply, findings.md and the roll-up use: a held span overlapping the claim's span by
    SOURCE_OVERLAP holds it (a Make belief on it would be refused as a duplicate), contained or not."""
    d = _full_digest()
    d.new_findings = ["c01", "c06"]
    msgs = render_brief(d, BELIEFS, DECISIONS, counts=SEEN, acted_spans=[(110, 150), (630, 670)])
    assert _locked_ids(msgs) == ["100-140"]      # 30 of 40 characters overlap; 10 of 40 do not hold c06


def test_thread_is_summary_then_findings_then_questions():
    msgs = render_brief(_full_digest(), BELIEFS, DECISIONS, counts=SEEN)
    texts = [t for _, t in msgs.thread]
    assert len(texts) == 3
    summary, findings, questions = texts
    assert summary.startswith("SUMMARY")
    assert findings.startswith("NEW FINDINGS")
    assert questions.startswith("QUESTIONS FROM PREP")
    assert questions.index("Answered") < questions.index("Dodged") < questions.index("Never asked")
    assert "How long a video have you had to review?" in questions
    assert "Footage requests pile up by accident" in summary
    assert "Quotes matched first pass 8/9 (1 repaired)" in summary
    assert "Findings found by 2 models: 2, by 1 model: 5" in summary
    assert "1 verdict by arbiter" in summary
    assert "no prep matched" not in summary


def test_follow_ups_and_ask_next_are_not_in_the_brief():
    d = _full_digest()
    msgs = render_brief(d, BELIEFS, DECISIONS, counts=SEEN)
    out = render_text(msgs)
    for gone in ("FOLLOW-UPS", "We owe", "They offered", "send deck", "look at guide wire",
                 "ASK NEXT", "How do other claims leads split footage review"):
        assert gone not in out
    ids = [b["block_id"] for blocks, _ in _all_messages(msgs) for b in blocks]
    assert not any(i.startswith(("follow-ups", "ask-next")) for i in ids)
    # they stay in the digest
    assert len(d.follow_ups) == 2 and d.ask_next


def test_footer_no_prep_and_degraded():
    d = _full_digest()
    d.prep = None
    d.question_coverage = []
    d.degraded = ["one_model", "no_arbiter"]
    msgs = render_brief(d, BELIEFS, DECISIONS, counts=SEEN)
    footer = _summary(msgs)
    assert "no prep matched" in footer
    assert "Degraded: one extraction model down, arbiter down" in footer
    assert not any("QUESTIONS" in t for _, t in msgs.thread)

    d.origin = "backfill"
    footer = _summary(render_brief(d, BELIEFS, DECISIONS, counts=SEEN))
    assert "no prep matched" not in footer


# ---------- invariants ----------

def test_no_em_dash_anywhere():
    d = _full_digest()
    d.summary = [f"Footage requests {EM_DASH} added by accident"]
    d.claims[5].text = f"Allocation {EM_DASH} unsolved"
    d.claims[5].quotes[0].quote = f"I moved to{EM_DASH} I'm in Denver now"
    d.belief_checks[1].note = f"Different seat {EM_DASH} likely split"
    d.ask_next = [f"Ask about {EM_DASH} retention"]
    msgs = render_brief(d, BELIEFS, DECISIONS, counts=SEEN, digest_url="https://example.com/d")
    for blocks, text in _all_messages(msgs):
        assert EM_DASH not in json.dumps(blocks, ensure_ascii=False)
        assert EM_DASH not in text
    assert EM_DASH not in render_text(msgs)


def test_values_under_150_chars_and_block_ids_unique():
    d = _full_digest()
    d.claims += [Claim(id=f"x{n}", text=f"extra {n}", quote=f"extra quote {n}", offset=(10_000 + n * 50, 10_000 + n * 50 + 20))
                 for n in range(60)]
    d.new_findings = ["c06"] + [f"x{n}" for n in range(60)]
    msgs = render_brief(d, BELIEFS, DECISIONS, counts=SEEN)
    values = [v for blocks, _ in _all_messages(msgs) for v in _all_values(blocks)]
    assert values and all(len(v) < 150 for v in values)
    for blocks, _ in _all_messages(msgs):
        ids = [b["block_id"] for b in blocks]
        assert len(ids) == len(set(ids))
        assert len(blocks) <= MAX_BLOCKS


def test_long_call_id_drops_menu_instead_of_breaking_the_post():
    d = _full_digest()
    d.call_id = "2026-09-18_" + "x" * 150
    msgs = render_brief(d, BELIEFS, DECISIONS, counts=SEEN)
    for blocks, _ in _all_messages(msgs):
        assert all(len(v) < 150 for v in _all_values(blocks))


def test_render_text_joins_top_and_thread():
    msgs = render_brief(_full_digest(), BELIEFS, DECISIONS, counts=SEEN)
    out = render_text(msgs)
    assert out.startswith(msgs.top_text)
    for _, text in msgs.thread:
        assert text in out
    assert "[Wrong read]" in out and "[Make belief | Watch]" in out


def test_mrkdwn_is_escaped():
    d = _full_digest()
    d.claims[5].quotes[0].quote = "videos <= 30 days & saved"
    msgs = render_brief(d, BELIEFS, DECISIONS, counts=SEEN)
    blocks, text = _findings(msgs)
    block = _block(blocks, "finding:600-640")
    assert "&lt;= 30 days &amp; saved" in block["text"]["text"]
    assert finding_from_block(block)[1] == "videos <= 30 days & saved"
    assert "videos <= 30 days & saved" in text


@pytest.mark.parametrize("decided_by,label", [("agree", "[2/2 agree]"), ("arbiter", "[arbiter]"),
                                              ("single", "[1 model]")])
def test_decided_by_labels(decided_by, label):
    d = _digest(claims=[_claim(1)], belief_checks=[
        BeliefCheck(target="B-001", verdict="contradicts", claims=["c01"], decided_by=decided_by)])
    msgs = render_brief(d, BELIEFS, DECISIONS, counts={})
    assert label in msgs.top_text


# ---------- round-2 fixes: the quote a clicked block showed ----------

def test_shown_quote_of_a_finding_and_a_wrong_read():
    d = _full_digest()
    msgs = render_brief(d, BELIEFS, DECISIONS, counts=SEEN)
    thread = _findings(msgs)[0]
    assert shown_quote(thread, "finding:600-640") == d.claim("c06").quote
    # Wrong read: the first quote shown with the verdict is the claim the button's span names
    assert shown_quote(msgs.top_blocks, "verdict:B-003:1") == "I don't think so."
    assert shown_quote(msgs.top_blocks, "verdict:B-005:1") == "nobody writes it down"
    # nothing to read: an older payload without blocks, or a block not in them
    assert shown_quote(msgs.top_blocks, "finding:600-640") is None
    assert shown_quote(None, "finding:600-640") is None
    assert shown_quote(thread, None) is None


def test_shown_quote_is_unescaped():
    d = _digest(claims=[_claim(1, quote="videos <= 30 days & saved")], new_findings=["c01"])
    blocks = _findings(render_brief(d, BELIEFS, DECISIONS, counts={}))[0]
    assert shown_quote(blocks, "finding:100-140") == "videos <= 30 days & saved"


# ---------- a short answer shows the question it answers ----------

QUESTION = ("This is the part of the video where it happened, and then kind of jump to it from there rather than "
            "watching the whole recording first? Or is that uh or would that not work?")


def test_short_answer_shows_the_question_it_answers():
    # SPEC shape: `"I don't think so."  (answering: could the tool jump to the incident?)`
    d = _full_digest()
    msgs = render_brief(d, BELIEFS, DECISIONS, counts=SEEN, questions={1: QUESTION})
    lines = msgs.top_text.splitlines()
    assert f'  "I don\'t think so."  (answering: {QUESTION})' in lines
    assert "Claim: " not in msgs.top_text
    section = _block(msgs.top_blocks, "verdict-text:B-003:1")["text"]["text"]
    assert '> "I don\'t think so."' in section.splitlines()
    assert f"(answering: {QUESTION})" in section
    # the Wrong read button still reads its quote back from the block
    assert shown_quote(msgs.top_blocks, "verdict:B-003:1") == "I don't think so."
    # the prep question already sits above the quote in the questions reply
    questions = [text for _, text in msgs.thread if text.startswith("QUESTIONS FROM PREP")]
    assert all("answering" not in text for text in questions)


def test_a_long_question_turn_keeps_its_end_where_the_question_is():
    long_turn = ("So we have been talking to a lot of people about store footage and how it gets pulled, "
                 "and what we keep hearing is that adjusters sit through the whole recording every time. " * 3
                 + "Would a tool that jumps to the incident stand in for watching the whole video?")
    msgs = render_brief(_full_digest(), BELIEFS, DECISIONS, counts=SEEN, questions={1: long_turn})
    [line] = [x for x in msgs.top_text.splitlines() if x.startswith('  "I don\'t think so."')]
    shown = line.split("(answering: ", 1)[1][:-1]
    assert shown.startswith("... ") and shown.endswith("stand in for watching the whole video?")
    assert len(shown) <= 200


def test_a_short_answer_asked_in_its_own_turn_shows_the_text_just_before_it():
    # a Granola text export is one turn, so question_turn is the answer's own turn and that turn's text
    # runs to the end of the call; process._questions keys the text just before the answer by claim id
    d = _full_digest()
    d.claims[1].quotes[0].turns = [1]
    call_end = "Anyway, thanks so much for the time. Talk soon."
    msgs = render_brief(d, BELIEFS, DECISIONS, counts=SEEN, questions={1: call_end})
    assert "answering" not in msgs.top_text and call_end not in msgs.top_text
    assert "  Claim: Finding the incident in long footage is not slow" in msgs.top_text
    msgs = render_brief(d, BELIEFS, DECISIONS, counts=SEEN, questions={1: call_end, "c02": QUESTION})
    assert f'  "I don\'t think so."  (answering: {QUESTION})' in msgs.top_text.splitlines()
    assert call_end not in msgs.top_text


def test_short_answer_without_its_question_falls_back_to_the_claim_text():
    for questions in (None, {}, {7: "Another turn?"}, {1: "   "}):
        msgs = render_brief(_full_digest(), BELIEFS, DECISIONS, counts=SEEN, questions=questions)
        assert "  Claim: Finding the incident in long footage is not slow" in msgs.top_text
        assert "answering" not in msgs.top_text


# ---------- findings with several quotes ----------

def _multi(*quotes: str, n: int = 6) -> Claim:
    """Finding `n` with the given quotes at 100-character steps from n * 100."""
    return Claim(id=f"c{n:02d}", text="Regulators audit claim files, so notes must be meticulous",
                 quotes=[Quote(quote=text, turns=[n + i], offset=(n * 100 + i * 1000, n * 100 + i * 1000 + 40),
                               speaker="Nadia") for i, text in enumerate(quotes)],
                 flags=Flags(firsthand=True, prompted=False), found_by=2)


LONG_SECOND = ("Market conduct exams. The regulator pulled a sample of our claim files, read every diary note line "
               "by line, and we were answering questions about them for weeks.")


def test_a_finding_shows_its_first_quote_and_a_short_second_one():
    claim = _multi("You have to keep really clean notes on every file.", "Um, anything about litigation goes in the notes")
    d = _digest(claims=[claim], new_findings=["c06"])
    blocks, text = _findings(render_brief(d, BELIEFS, DECISIONS, counts={}))
    block = _block(blocks, "finding:600-640")
    lines = block["text"]["text"].splitlines()
    assert lines[1:3] == ['> "You have to keep really clean notes on every file."',
                          '> "Um, anything about litigation goes in the notes"']
    assert '  "Um, anything about litigation goes in the notes"' in text.splitlines()
    assert "more quote" not in text
    # the menu, the Make belief prefill and a click's shown quote all use the first quote
    assert [o["value"] for o in block["accessory"]["options"]] == [f"mb|{CALL}|600-640", f"w|{CALL}|600-640"]
    assert finding_from_block(block)[1] == "You have to keep really clean notes on every file."
    assert shown_quote(blocks, "finding:600-640") == "You have to keep really clean notes on every file."


def test_a_long_second_quote_is_counted_not_shown():
    claim = _multi("You have to keep really clean notes on every file.", LONG_SECOND, "a third quote here")
    d = _digest(claims=[claim], new_findings=["c06"])
    blocks, text = _findings(render_brief(d, BELIEFS, DECISIONS, counts={}))
    assert LONG_SECOND not in text and "a third quote here" not in text
    assert "  +2 more quotes" in text.splitlines()
    assert "_+2 more quotes_" in _block(blocks, "finding:600-640")["text"]["text"]


def test_a_short_second_and_a_third_quote():
    claim = _multi("first quote here", "second, short", "third, also short")
    blocks, text = _findings(render_brief(_digest(claims=[claim], new_findings=["c06"]), BELIEFS, DECISIONS, counts={}))
    assert '  "second, short"' in text.splitlines() and "third, also short" not in text
    assert "  +1 more quote" in text.splitlines()


def test_a_verdict_on_a_multi_quote_finding_shows_its_first_quote():
    claim = _multi("You have to keep really clean notes on every file.", LONG_SECOND)
    d = _digest(claims=[claim], new_findings=[],
                belief_checks=[BeliefCheck(target="B-004", verdict="contradicts", claims=["c06"], decided_by="agree")])
    msgs = render_brief(d, BELIEFS, DECISIONS, counts={})
    assert '  "You have to keep really clean notes on every file."' in msgs.top_text.splitlines()
    assert LONG_SECOND not in msgs.top_text
    assert shown_quote(msgs.top_blocks, "verdict:B-004:1") == "You have to keep really clean notes on every file."
    assert all(len(v) < 150 for blocks, _ in _all_messages(msgs) for v in _all_values(blocks))


def test_a_source_on_any_quote_locks_the_finding():
    claim = _multi("first quote here", "a held second quote")
    d = _digest(claims=[claim], new_findings=["c06"])
    by_span = render_brief(d, BELIEFS, DECISIONS, counts={}, acted_spans=[(1600, 1640)])
    assert _locked_ids(by_span) == ["600-640"]
    by_quote = render_brief(d, BELIEFS, DECISIONS, counts={}, acted_sources=[(CALL, "a held second quote")])
    assert _locked_ids(by_quote) == ["600-640"]


def test_footer_names_a_merge_without_consolidation():
    d = _full_digest()
    d.degraded = ["no_consolidation"]
    footer = _summary(render_brief(d, BELIEFS, DECISIONS, counts=SEEN))
    assert "Degraded: findings merged by quote overlap only" in footer
