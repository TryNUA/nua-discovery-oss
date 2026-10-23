"""Tests for nua/prep.py: template validation, parsing, loading and matching prep files."""
from __future__ import annotations

import pytest

from nua import config as config_mod
from nua.ports import RepoFS
from nua.prep import load_preps, match_prep, parse_prep, validate_prep
from nua.types import PrepFile

GOOD = """---
planned_date: 2026-10-02
contact: Jane Doe
firm: Example Mutual
attendee_email: jane@example.com
seat: underwriter
caller: alex
---

## Context
Prior calls, mentions, open follow-ups.

## Questions
1. How do you set reserves on a first-notice claim? (B-003)
2. Who sets the review criteria,
   you or the claims manager?
3) What breaks first when a claim litigates?
- Would you share a sample claim file?
"""


@pytest.fixture
def cfg(tmp_repo):
    return config_mod.load(tmp_repo)


# ---------- validate / parse ----------

def test_valid_template_has_no_errors():
    assert validate_prep(GOOD) == []


def test_parse_prep_fields():
    p = parse_prep("prep/2026-10-02_jane-doe.md", GOOD)
    assert p.path == "prep/2026-10-02_jane-doe.md"
    assert p.planned_date == "2026-10-02"
    assert p.contact == "Jane Doe"
    assert p.firm == "Example Mutual"
    assert p.attendee_email == "jane@example.com"
    assert p.seat == "underwriter"
    assert p.caller == "alex"
    assert p.questions == [
        "How do you set reserves on a first-notice claim? (B-003)",
        "Who sets the review criteria, you or the claims manager?",
        "What breaks first when a claim litigates?",
        "Would you share a sample claim file?",
    ]


def test_questions_stop_at_next_heading():
    text = GOOD + "\n## Notes\n1. Not a question\n"
    assert len(parse_prep("prep/x.md", text).questions) == 4


def test_missing_questions_section_fails():
    text = GOOD.split("## Questions")[0]
    errors = validate_prep(text)
    assert any("## Questions" in e for e in errors)
    with pytest.raises(ValueError):
        parse_prep("prep/x.md", text)


def test_empty_questions_section_fails():
    text = GOOD.split("## Questions")[0] + "## Questions\n\n"
    assert any("no questions" in e for e in validate_prep(text))


def test_missing_front_matter_fails():
    assert any("front-matter" in e for e in validate_prep("## Questions\n1. Why?\n"))


def test_bad_yaml_fails():
    text = "---\nplanned_date: [2026-10-02\n---\n\n## Questions\n1. Why?\n"
    assert validate_prep(text)


@pytest.mark.parametrize("value", ["!!int 12x", "[" * 3000 + "]" * 3000])
def test_front_matter_pyyaml_cannot_load_fails_as_invalid_yaml(value):
    # PyYAML raises ValueError / RecursionError here; frontmatter.load_yaml turns them into a YAMLError
    text = f"---\nplanned_date: {value}\n---\n\n## Questions\n1. Why?\n"
    errors = validate_prep(text)
    assert any("not valid YAML" in e for e in errors)


def test_missing_required_fields_fail():
    text = "---\nfirm: Example Mutual\n---\n\n## Questions\n1. Why?\n"
    errors = validate_prep(text)
    assert any("planned_date" in e for e in errors)
    assert any("contact" in e for e in errors)


def test_bad_planned_date_fails():
    text = GOOD.replace("planned_date: 2026-10-02", "planned_date: next Friday")
    assert any("planned_date" in e for e in validate_prep(text))
    text = GOOD.replace("planned_date: 2026-10-02", "planned_date: '2026-13-40'")
    assert any("planned_date" in e for e in validate_prep(text))


def test_quoted_planned_date_is_accepted():
    text = GOOD.replace("planned_date: 2026-10-02", "planned_date: '2026-10-02'")
    assert parse_prep("prep/x.md", text).planned_date == "2026-10-02"


def test_bad_attendee_email_fails():
    text = GOOD.replace("attendee_email: jane@example.com", "attendee_email: jane")
    assert any("attendee_email" in e for e in validate_prep(text))


def test_optional_fields_may_be_absent():
    text = "---\nplanned_date: 2026-10-02\ncontact: Jane Doe\n---\n\n## Questions\n1. Why?\n"
    p = parse_prep("prep/x.md", text)
    assert p.firm is None and p.attendee_email is None and p.seat is None and p.caller is None
    assert p.questions == ["Why?"]


# ---------- load ----------

def test_load_preps_skips_invalid_files(tmp_repo):
    repo = RepoFS(tmp_repo)
    repo.write("prep/2026-10-02_jane-doe.md", GOOD)
    repo.write("prep/2026-10-03_bad.md", "no front-matter here\n")
    repo.write("prep/README.txt", "not a prep file")
    preps = load_preps(repo)
    assert [p.path for p in preps] == ["prep/2026-10-02_jane-doe.md"]


def test_load_preps_with_no_folder(tmp_path):
    assert load_preps(RepoFS(tmp_path)) == []


# ---------- match ----------

def prep(path: str, date: str, contact: str, email: str | None = None) -> PrepFile:
    return PrepFile(path=path, planned_date=date, contact=contact, attendee_email=email, questions=["Why?"])


def test_match_by_email_beats_name(cfg):
    by_name = prep("prep/2026-10-02_jane-doe.md", "2026-10-02", "Jane Doe")
    by_email = prep("prep/2026-10-02_j-doe.md", "2026-10-02", "J. Doe", "jane@example.com")
    got = match_prep([by_name, by_email], date="2026-10-02", contact="Jane Doe",
                     attendee_emails=["alex@acme.dev", "Jane@Example.com"], config=cfg)
    assert got is by_email


def test_match_by_name_case_insensitive(cfg):
    p = prep("prep/2026-10-02_jane-doe.md", "2026-10-02", "Jane Doe")
    assert match_prep([p], date="2026-10-03", contact="jane doe", attendee_emails=[], config=cfg) is p


def test_match_by_name_through_entities(cfg):
    cfg.entities["Darius Kemp"] = ["Darius Basel"]
    p = prep("prep/2026-09-23_darius-kemp.md", "2026-09-23", "Darius Kemp")
    assert match_prep([p], date="2026-09-23", contact="Darius Basel", attendee_emails=[], config=cfg) is p


def test_window_respected(cfg):
    p = prep("prep/2026-10-02_jane-doe.md", "2026-10-02", "Jane Doe", "jane@example.com")
    assert match_prep([p], date="2026-10-09", contact="Jane Doe", attendee_emails=[], config=cfg) is p
    assert match_prep([p], date="2026-09-25", contact="Jane Doe", attendee_emails=[], config=cfg) is p
    assert match_prep([p], date="2026-10-10", contact="Jane Doe", attendee_emails=[], config=cfg) is None
    assert match_prep([p], date="2026-10-10", contact=None, attendee_emails=["jane@example.com"],
                      config=cfg) is None
    assert match_prep([p], date="2026-10-10", contact="Jane Doe", attendee_emails=[], config=cfg,
                      window_days=8) is p


def test_email_outside_window_falls_back_to_name_inside_window(cfg):
    old = prep("prep/2026-08-01_jane-doe.md", "2026-08-01", "Jane Doe", "jane@example.com")
    new = prep("prep/2026-10-02_jane-doe.md", "2026-10-02", "Jane Doe")
    got = match_prep([old, new], date="2026-10-02", contact="Jane Doe",
                     attendee_emails=["jane@example.com"], config=cfg)
    assert got is new


def test_closest_planned_date_wins(cfg):
    a = prep("prep/2026-09-28_jane-doe.md", "2026-09-28", "Jane Doe")
    b = prep("prep/2026-10-01_jane-doe.md", "2026-10-01", "Jane Doe")
    assert match_prep([a, b], date="2026-10-02", contact="Jane Doe", attendee_emails=[], config=cfg) is b


def test_no_date_means_no_match_by_name(cfg):
    p = prep("prep/2026-10-02_jane-doe.md", "2026-10-02", "Jane Doe")
    assert match_prep([p], date=None, contact="Jane Doe", attendee_emails=[], config=cfg) is None


def test_no_date_matches_a_single_email_prep(cfg):
    p = prep("prep/2026-10-02_jane-doe.md", "2026-10-02", "Jane Doe", "jane@example.com")
    q = prep("prep/2026-11-02_jane-doe.md", "2026-11-02", "Jane Doe", "jane@example.com")
    assert match_prep([p], date=None, contact=None, attendee_emails=["jane@example.com"], config=cfg) is p
    # Two candidates and no date to choose between them: no match rather than a guess.
    assert match_prep([p, q], date=None, contact=None, attendee_emails=["jane@example.com"], config=cfg) is None


def test_no_contact_no_emails_no_match(cfg):
    p = prep("prep/2026-10-02_jane-doe.md", "2026-10-02", "Jane Doe")
    assert match_prep([p], date="2026-10-02", contact=None, attendee_emails=[], config=cfg) is None
    assert match_prep([], date="2026-10-02", contact="Jane Doe", attendee_emails=[], config=cfg) is None


def test_crlf_line_endings_are_accepted():
    p = parse_prep("prep/x.md", GOOD.replace("\n", "\r\n"))
    assert p.contact == "Jane Doe" and len(p.questions) == 4


def test_planned_date_with_time_keeps_the_date():
    text = GOOD.replace("planned_date: 2026-10-02", "planned_date: 2026-10-02 10:00:00")
    assert parse_prep("prep/x.md", text).planned_date == "2026-10-02"


def test_questions_end_at_any_heading_and_need_a_marker():
    text = GOOD + "**Bold aside** that continues the last question\n### Later\n1. Not a question\n"
    qs = parse_prep("prep/x.md", text).questions
    assert len(qs) == 4
    assert qs[-1] == "Would you share a sample claim file? **Bold aside** that continues the last question"


def test_spec_example_is_valid():
    text = (
        "---\nplanned_date: 2026-10-02\ncontact: Jane Doe\nfirm: Example Mutual\n"
        "attendee_email: jane@example.com   # if known\nseat: underwriter\ncaller: alex\n---\n\n"
        "## Context\nPrior calls, mentions, open follow-ups (we owe / they offered)\n\n"
        "## Questions\n1. ... (B-003)\n2. ...\n"
    )
    assert validate_prep(text) == []
    assert parse_prep("prep/2026-10-02_jane-doe.md", text).questions == ["... (B-003)", "..."]


def test_indented_sub_bullets_and_wrapped_numbers_continue_the_question():
    text = GOOD.split("## Questions")[0] + (
        "## Questions\n"
        "1. Do you need footage before a liability call? (B-003)\n"
        "   - if yes, from which camera?\n"
        "2. How do you assign claims\n"
        "   across adjusters?\n"
        "3. How many claims close inside a\n"
        "3.5-day window?\n"
        "- Would you share a sample claim file?\n"
        "    1. only the last 12 months\n"
    )
    assert parse_prep("prep/x.md", text).questions == [
        "Do you need footage before a liability call? (B-003) - if yes, from which camera?",
        "How do you assign claims across adjusters?",
        "How many claims close inside a 3.5-day window?",
        "Would you share a sample claim file? 1. only the last 12 months",
    ]


def test_indented_lists_start_items_at_their_own_indent():
    text = GOOD.split("## Questions")[0] + (
        "## Questions\n"
        "  - First question?\n"
        "     - a detail\n"
        "  - Second question?\n"
    )
    assert parse_prep("prep/x.md", text).questions == ["First question? - a detail", "Second question?"]
