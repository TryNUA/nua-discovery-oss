"""Tests for nua/beliefs.py: beliefs.md and decisions.md read/write and the history-line grammar."""
from __future__ import annotations

import pytest

from nua.beliefs import (
    EMPTY_BELIEFS,
    BeliefsDoc,
    format_history,
    format_scope,
    parse_beliefs,
    parse_decisions,
    parse_history,
    parse_scope,
    render_beliefs,
    render_decisions,
)
from nua.types import Decision, Entry, HistoryLine, Scope

# A beliefs.md example in the shape docs/SPEC.md shows, with quotes from the darius-kemp fixture.
SPEC_EXAMPLE = """## Beliefs

### B-001 Video footage is the most valuable evidence on a liability claim
- scope: line: general liability, commercial auto; party: carrier
- history:
  - 2026-10-05 added by Alex via bootstrap, source 2026-09-23_darius-kemp "Honestly video is the single best thing you can have on a liability file.", ref 1727560000.000100:prop-07

## Watching

### W-004 Commercial claims teams get less software spend, not less need
- scope: party: carrier
- history:
  - 2026-10-06 watched by Sam via slack, source 2026-09-23_darius-kemp "And on the commercial side the carriers buy a lot less of it than they do for personal auto and homeowners.", ref 1727470001.000200:c01

## Retired
"""

# The same example as the processor writes it: actions in their canonical form.
SPEC_CANONICAL = SPEC_EXAMPLE.replace("added by", "add by").replace("watched by", "watch by")


def _line(**kw) -> HistoryLine:
    base = dict(date="2026-10-05", action="add", who="Alex", via="slack", ref="1727560000.000100:c01")
    base.update(kw)
    return HistoryLine(**base)


def _entry(entry_id: str, section: str, history: list[HistoryLine], statement: str = "A statement") -> Entry:
    return Entry(id=entry_id, statement=statement, scope=Scope(), section=section, history=history)


# ---------- beliefs.md ----------

def test_empty_file_parses_and_renders():
    doc = parse_beliefs(EMPTY_BELIEFS)
    assert doc.entries == []
    assert render_beliefs(doc) == EMPTY_BELIEFS
    assert render_beliefs(BeliefsDoc(entries=[])) == EMPTY_BELIEFS
    assert parse_beliefs("").entries == []


def test_spec_example_parses():
    doc = parse_beliefs(SPEC_EXAMPLE)
    assert [e.id for e in doc.entries] == ["B-001", "W-004"]

    b1 = doc.get("B-001")
    assert b1.section == "beliefs"
    assert b1.statement == "Video footage is the most valuable evidence on a liability claim"
    assert b1.scope == Scope(line="general liability, commercial auto", party="carrier")
    assert len(b1.history) == 1
    h = b1.history[0]
    assert h.date == "2026-10-05"
    assert h.action == "add"            # "added" in the SPEC reads as the canonical action
    assert h.who == "Alex"
    assert h.via == "bootstrap"
    assert h.sources == [("2026-09-23_darius-kemp",
                          "Honestly video is the single best thing you can have on a liability file.")]
    assert h.reason is None and h.text is None
    assert h.ref == "1727560000.000100:prop-07"
    assert b1.version == "1727560000.000100:prop-07"

    w4 = doc.get("W-004")
    assert w4.section == "watching"
    assert w4.history[0].action == "watch"
    assert w4.history[0].sources[0][1].startswith("And on the commercial side the carriers")
    assert w4.version == "1727470001.000200:c01"


def test_spec_example_round_trip():
    doc = parse_beliefs(SPEC_EXAMPLE)
    assert parse_beliefs(render_beliefs(doc)) == doc
    # The canonical form round-trips byte for byte.
    assert render_beliefs(parse_beliefs(SPEC_CANONICAL)) == SPEC_CANONICAL
    assert render_beliefs(doc) == SPEC_CANONICAL


def test_round_trip_of_built_doc():
    doc = BeliefsDoc(entries=[
        Entry(id="B-001", statement="Video footage is the best evidence on a liability claim",
              scope=Scope(line="general liability", party="carriers, independent adjusting firms",
                          segment="small commercial", seat="claims adjuster (staff)"),
              section="beliefs",
              history=[
                  _line(via="bootstrap", sources=[("2026-09-23_darius-kemp", "quote one"),
                                                  ("2026-09-30_lena-okafor", "quote two")]),
                  _line(date="2026-10-20", action="keep", who="Sam", ref="1727600000.000100:pressure:B-001",
                        reason="still true for small commercial, middle market differs"),
              ]),
        Entry(id="B-012", statement="Commercial lines get less claims software",
              scope=Scope(party="carriers"), section="beliefs",
              history=[_line(action="promote", ref="requests/2026-10-21_sam_promote.yml@abc123")]),
        Entry(id="W-002", statement="Nothing yet", scope=Scope(), section="watching", history=[]),
        Entry(id="W-001", statement="Commercial lines get less claims software",
              scope=Scope(party="carriers"), section="retired",
              history=[
                  _line(action="watch", who="Jordan", via="agent", ref="requests/a.yml@1"),
                  _line(date="2026-10-21", action="promoted", text="to B-012", who="system", via="system",
                        ref="system:promote-W-001"),
              ]),
    ])
    text = render_beliefs(doc)
    assert parse_beliefs(text) == doc
    assert render_beliefs(parse_beliefs(text)) == text
    assert "- scope:\n" in text                      # empty scope renders as a bare key
    assert "promoted to B-012 by system via system" in text


def test_render_groups_by_section():
    doc = BeliefsDoc(entries=[
        _entry("W-001", "watching", [_line(action="watch")]),
        _entry("B-002", "retired", [_line()]),
        _entry("B-001", "beliefs", [_line()]),
    ])
    text = render_beliefs(doc)
    assert text.index("## Beliefs") < text.index("### B-001") < text.index("## Watching")
    assert text.index("## Watching") < text.index("### W-001") < text.index("## Retired")
    assert text.index("## Retired") < text.index("### B-002")
    assert [e.id for e in parse_beliefs(text).entries] == ["B-001", "W-001", "B-002"]


def test_next_id_counts_retired_entries():
    doc = BeliefsDoc(entries=[
        _entry("B-001", "beliefs", []),
        _entry("B-007", "retired", []),
        _entry("W-003", "retired", []),
        _entry("W-001", "watching", []),
    ])
    assert doc.next_id("B") == "B-008"
    assert doc.next_id("W") == "W-004"
    assert BeliefsDoc(entries=[]).next_id("B") == "B-001"
    assert BeliefsDoc(entries=[]).next_id("W") == "W-001"
    with pytest.raises(ValueError):
        doc.next_id("D")


def test_get_and_current():
    doc = BeliefsDoc(entries=[
        _entry("W-001", "watching", []),
        _entry("B-002", "retired", []),
        _entry("B-001", "beliefs", []),
    ])
    assert doc.get("B-002").section == "retired"
    assert doc.get("B-099") is None
    assert [e.id for e in doc.current()] == ["B-001", "W-001"]


def test_entry_version_ignores_keep():
    e = _entry("B-001", "beliefs", [
        _line(action="add", ref="r-add"),
        _line(action="keep", ref="r-keep"),
    ])
    assert e.version == "r-add"
    e.history.append(_line(action="reword", ref="r-reword"))
    e.history.append(_line(action="keep", ref="r-keep-2"))
    assert e.version == "r-reword"

    parsed = parse_beliefs(render_beliefs(BeliefsDoc(entries=[e])))
    assert parsed.get("B-001").version == "r-reword"


def test_current_sources_excludes_retired():
    doc = BeliefsDoc(entries=[
        _entry("B-001", "beliefs", [_line(sources=[("call-a", "quote a"), ("call-b", "quote b")])]),
        _entry("W-001", "watching", [_line(action="watch", sources=[("call-c", "quote c")])]),
        _entry("B-002", "retired", [_line(sources=[("call-d", "quote d")])]),
    ])
    assert doc.current_sources() == [
        ("B-001", "call-a", "quote a"),
        ("B-001", "call-b", "quote b"),
        ("W-001", "call-c", "quote c"),
    ]


def test_held_sources_keep_the_ref_and_date_of_the_change_that_first_held_them():
    watched = _line(date="2026-09-01", action="watch", ref="1.1:finding:200-208", sources=[("call-a", "Exactly.")])
    other = _line(date="2026-09-02", action="watch", ref="requests/w.yml@1", sources=[("call-b", "quote b")])
    doc = BeliefsDoc(entries=[
        _entry("B-002", "beliefs", [_line(date="2026-09-10", action="promote", text="from W-001", ref="2.2:prop:x",
                                          sources=[("call-a", "Exactly."), ("call-c", "new quote")])]),
        _entry("B-003", "beliefs", [_line(date="2026-09-03", ref="3.3:c01", sources=[("call-d", "quote d")]),
                                    _line(date="2026-09-11", action="merge", text="with W-002", ref="4.4:prop:y",
                                          sources=[("call-b", "quote b")])]),
        _entry("W-001", "retired", [watched, _line(date="2026-09-10", action="promoted", text="to B-002")]),
        _entry("W-002", "retired", [other, _line(date="2026-09-11", action="merged", text="into B-003")]),
    ])
    assert [tuple(h) for h in doc.held_sources()] == [
        ("B-002", "call-a", "Exactly.", "1.1:finding:200-208", "2026-09-01"),    # carried from W-001
        ("B-002", "call-c", "new quote", "2.2:prop:x", "2026-09-10"),
        ("B-003", "call-d", "quote d", "3.3:c01", "2026-09-03"),
        ("B-003", "call-b", "quote b", "requests/w.yml@1", "2026-09-02"),        # carried from W-002
    ]


def test_parse_rejects_malformed_files():
    with pytest.raises(ValueError):
        parse_beliefs("## Beliefs\n\n### B-001 Statement\n- colour: blue\n")
    with pytest.raises(ValueError):
        parse_beliefs("## Opinions\n")
    with pytest.raises(ValueError):
        parse_beliefs("## Beliefs\n\n### B-001 One\n\n### B-001 Two\n")
    with pytest.raises(ValueError):
        parse_beliefs("## Beliefs\n\n### B-001 One\n- history:\n  - not a history line\n")


def test_preamble_is_ignored_but_entries_before_a_section_raise():
    text = "# Beliefs\n\n<!-- written by the processor -->\n\n" + SPEC_EXAMPLE
    assert parse_beliefs(text) == parse_beliefs(SPEC_EXAMPLE)
    with pytest.raises(ValueError):
        parse_beliefs("##Beliefs\n\n### B-001 Lost entry\n- scope:\n- history:\n")


def test_statement_newlines_are_flattened():
    doc = BeliefsDoc(entries=[_entry("B-001", "beliefs", [_line()], statement="Line one\nline two")])
    text = render_beliefs(doc)
    assert "### B-001 Line one line two\n" in text
    assert parse_beliefs(text).get("B-001").statement == "Line one line two"


# ---------- history lines ----------

def test_history_line_spec_example():
    line = ('2026-10-05 added by Alex via bootstrap, source 2026-09-23_darius-kemp '
            '"Honestly video is the single best thing you can have on a liability file.", ref 1727560000.000100:prop-07')
    h = parse_history(line)
    assert h.action == "add"
    assert format_history(h) == line.replace("added by", "add by")


def test_quote_with_quote_and_backslash():
    quote = 'He said "no way" and typed C:\\temp\\ then "ok\\"'
    h = _line(sources=[("2026-09-23_darius-kemp", quote)])
    line = format_history(h)
    assert '\\"no way\\"' in line
    assert "C:\\\\temp\\\\" in line
    assert parse_history(line) == h


def test_multiple_sources():
    h = _line(sources=[("2026-09-23_darius-kemp", 'first; with "semicolon"'),
                       ("2026-09-30_lena-okafor", "second, with ref 12:34 inside")])
    line = format_history(h)
    assert '; 2026-09-30_lena-okafor "second' in line
    assert parse_history(line) == h


def test_reason_with_commas():
    h = _line(action="retire", reason="wrong, too broad, and, frankly, ref-looking text, ref inside",
              ref="requests/2026-10-06_sam_retire.yml@9f8e7d")
    line = format_history(h)
    assert line.endswith(", ref requests/2026-10-06_sam_retire.yml@9f8e7d")
    assert parse_history(line) == h


def test_sources_and_reason_together():
    h = _line(action="reword", sources=[("c1", "a quote, with commas")], reason="tighter wording: see thread")
    line = format_history(h)
    assert parse_history(line) == h


def test_text_after_action():
    h = _line(action="promoted", text="to B-012", who="system", via="system", ref="system:promote")
    line = format_history(h)
    assert line == "2026-10-05 promoted to B-012 by system via system, ref system:promote"
    assert parse_history(line) == h
    merged = _line(action="merged", text="into B-003", ref="1.2:m")
    assert parse_history(format_history(merged)) == merged


def test_history_line_without_sources_or_reason():
    h = _line(action="keep", ref="1727600000.000100:pressure:B-001")
    line = format_history(h)
    assert line == "2026-10-05 keep by Alex via slack, ref 1727600000.000100:pressure:B-001"
    assert parse_history(line) == h


def test_history_line_accepts_list_prefix():
    h = _line()
    assert parse_history("- " + format_history(h)) == h


def test_bad_history_lines_raise():
    for bad in [
        "",
        "2026-10-05 add by Alex via slack",                          # no ref
        "2026-10-05 add by Alex via email, ref x",                   # unknown via
        "yesterday add by Alex via slack, ref x",                    # no date
        '2026-10-05 add by Alex via slack, source c1 "unterminated, ref x',
        "2026-10-05 add via slack, ref x",                            # no who
    ]:
        with pytest.raises(ValueError):
            parse_history(bad)


def test_format_rejects_bad_values():
    with pytest.raises(ValueError):
        format_history(_line(via="email"))
    with pytest.raises(ValueError):
        format_history(_line(action="two words"))
    with pytest.raises(ValueError):
        format_history(_line(ref=""))
    with pytest.raises(ValueError):
        format_history(_line(ref="a, ref b"))
    with pytest.raises(ValueError):
        format_history(_line(who="Alex by proxy"))


# ---------- scope ----------

def test_scope_format_and_parse():
    s = Scope(party="carriers, independent adjusting firms", segment="small commercial")
    assert format_scope(s) == "party: carriers, independent adjusting firms; segment: small commercial"
    assert parse_scope(format_scope(s)) == s
    full = Scope(line="commercial auto", party="carrier", segment="small commercial", seat="claims adjuster (staff)")
    assert format_scope(full) == ("line: commercial auto; party: carrier; "
                                  "segment: small commercial; seat: claims adjuster (staff)")
    assert parse_scope(format_scope(full)) == full
    assert format_scope(Scope()) == ""
    assert parse_scope("") == Scope()
    assert parse_scope("  segment:  small commercial ; ") == Scope(segment="small commercial")


def test_scope_value_with_colon_and_semicolon():
    assert parse_scope("party: carriers: large ones") == Scope(party="carriers: large ones")
    s = Scope(segment="small commercial; E&S")
    assert parse_scope(format_scope(s)) == Scope(segment="small commercial, E&S")


def test_scope_unknown_key_raises():
    with pytest.raises(ValueError):
        parse_scope("colour: blue")
    with pytest.raises(ValueError):
        parse_scope("just words")


# ---------- decisions.md ----------

def test_decisions_round_trip():
    decisions = [
        Decision(id="D-002", title="Footage review tool", rests_on=["B-001", "B-003"]),
        Decision(id="D-003", title="Start with small commercial", rests_on=[]),
    ]
    text = render_decisions(decisions)
    assert "### D-002 Footage review tool\n- rests on: B-001, B-003\n" in text
    assert parse_decisions(text) == decisions
    assert render_decisions(parse_decisions(text)) == text


def test_decisions_hand_edited_file():
    text = """# Decisions

Hand-edited. Each decision lists the beliefs it rests on.

### D-002 Footage review tool
Some prose about why we decided this, mentioning B-009 in passing.
- rests on: B-001, B-3
- owner: Alex

### D-004 Sell to small commercial carriers first: rests on B-002

### D-005 Placeholder
- rests on:
  - B-004
  - W-001

## Notes
- rests on: B-099
"""
    decisions = parse_decisions(text)
    assert decisions == [
        Decision(id="D-002", title="Footage review tool", rests_on=["B-001", "B-003"]),
        Decision(id="D-004", title="Sell to small commercial carriers first", rests_on=["B-002"]),
        Decision(id="D-005", title="Placeholder", rests_on=["B-004", "W-001"]),
    ]


def test_decisions_spec_one_line_form():
    text = ("- D-002 Footage review tool: rests on B-001, B-003\n"
            "D-006 Pricing: rests on B-4\n"
            "- D-007 was dropped, it rests on nothing\n"
            "### D-008 Pricing rests on volume\n")
    assert parse_decisions(text) == [
        Decision(id="D-002", title="Footage review tool", rests_on=["B-001", "B-003"]),
        Decision(id="D-006", title="Pricing", rests_on=["B-004"]),
        Decision(id="D-008", title="Pricing rests on volume", rests_on=[]),
    ]


def test_empty_decisions():
    assert parse_decisions("") == []
    assert parse_decisions(render_decisions([])) == []


def test_no_em_dash_in_output():
    doc = parse_beliefs(SPEC_EXAMPLE)
    text = render_beliefs(doc) + render_decisions([Decision(id="D-002", title="Footage review tool")])
    assert "\u2014" not in text
