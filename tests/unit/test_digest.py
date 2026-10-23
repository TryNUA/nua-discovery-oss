"""Tests for nua/digest.py: digest and stub files."""
from __future__ import annotations

import datetime as dt

import pytest
import yaml

from nua import frontmatter
from nua.digest import (
    STUB_STATUSES,
    DigestError,
    apply_overrides,
    digest_for_transcript,
    digest_path,
    digest_title,
    is_stub,
    is_unreadable,
    load_digests,
    make_stub,
    override_problems,
    read_digest,
    render_body,
    unreadable_paths,
    without_em_dashes,
    write_digest,
)
from nua.ports import RepoFS
from nua.types import (
    Attendee,
    BeliefCheck,
    BriefRef,
    Claim,
    Digest,
    Flags,
    FollowUp,
    ParsedTranscript,
    QuestionCoverage,
    Scope,
    Turn,
    Verification,
)

EM_DASH = "\u2014"


def full_digest(call_id: str = "2026-09-18_darius-kemp") -> Digest:
    """A digest with every field set, including tuples, nested types and awkward YAML strings."""
    return Digest(
        call_id=call_id,
        transcript="transcripts/circleback_12345.md",
        status="digested",
        origin="live",
        duplicates=["transcripts/granola_abc.md"],
        source="circleback",
        source_id="12345",
        date="2026-09-18",
        date_source="api",
        recorded_by="alex",
        attendees=[Attendee(name="Darius Kemp", email="darius@example.com"), Attendee(name="Alex Rivera", email=None)],
        contact="Darius Kemp",
        firm="Example Mutual",
        firm_source="transcript",
        seat="claims adjuster (staff)",
        institution_type="commercial carrier",
        role_in_thesis="carrier",
        speaker_labels="named",
        prompt_version="v1",
        models={"extract": ["anthropic:claude-sonnet-5", "openai:gpt-5"], "arbiter": "google:gemini-3-pro"},
        degraded=["no_arbiter"],
        brief=BriefRef(channel="C123", ts="1727470000.000100", thread=["1727470001.000200"]),
        prep="prep/2026-09-18_darius-kemp.md",
        summary=["Store footage is overwritten within days", "Guidewire already does this at scale"],
        claims=[
            Claim(
                id="c01",
                text="Carriers pay less for commercial claims tooling than personal lines",
                quote="on the commercial side the carriers buy a lot less of it than they do for personal auto and homeowners",
                turns=[57],
                question_turn=None,
                offset=(18234, 18311),
                speaker="Darius Kemp",
                attribution="span",
                scope=Scope(line="general liability", party="carrier",
                            segment="small commercial", seat="claims adjuster (staff)"),
                flags=Flags(hedged=False, firsthand=True, prompted=False),
                found_by=2,
            ),
            Claim(
                id="c02",
                text="Store systems loop over old footage",
                quote="yes: it's a three year deal\n  that keeps refreshing # like-for-like",
                turns=[60, 61],
                question_turn=59,
                offset=(19000, 19080),
                speaker="Speaker B",
                attribution="inferred",
                scope=Scope(line="commercial auto"),
                flags=Flags(hedged=True, firsthand=False, prompted=True),
                found_by=1,
            ),
        ],
        belief_checks=[
            BeliefCheck(target="B-003", verdict="contradicts", claims=["c01"], decided_by="agree",
                        votes={"anthropic:claude-sonnet-5": "contradicts", "openai:gpt-5": "contradicts"},
                        source="ingest", note="Adjuster view, B-003 came from a carrier view"),
            BeliefCheck(target="W-001", verdict="supports", claims=["c02"], decided_by="single",
                        votes={"anthropic:claude-sonnet-5": "supports", "openai:gpt-5": "none"},
                        source="backcheck"),
        ],
        checked={"B-003": "1727560000.000100:prop-07", "W-001": "requests/x.md@abc123"},
        new_findings=["c02"],
        question_coverage=[
            QuestionCoverage(question="Would a carrier buy from an unproven team?", status="dodged",
                             claims=["c01"], decided_by="agree"),
            QuestionCoverage(question="null", status="never_asked"),
        ],
        follow_ups=[
            FollowUp(direction="we_owe", what="send deck", who="Darius Kemp", quote="send me the deck"),
            FollowUp(direction="they_offered", what="intro to a claims VP", who=None,
                     quote="I can intro you to a VP"),
        ],
        ask_next=["What retention window did the store quote?", "yes"],
        mentions={"people": ["Darius Kemp"], "firms": ["Guidewire", "CompScience"]},
        verification=Verification(quotes=3, matched_first_pass=2, repaired=1,
                                  dropped=[{"model": "openai:gpt-5", "quote": "not there", "reason": "no match"}]),
        reason=None,
        reprocess_failed=None,
    )


def parsed_transcript() -> ParsedTranscript:
    raw = "---\nsource: granola\n---\n\n## Transcript\n\n[00:01] **Me**: hello\n"
    return ParsedTranscript(
        path="transcripts/granola_note-1.md",
        raw=raw,
        shape="standard",
        front_matter={"source": "granola"},
        notes="",
        body_start=0,
        body_end=len(raw),
        turns=[Turn(index=1, time="00:01", speaker="Me", text="hello", start=10, end=15)],
        speaker_labels="me_them",
        origin="live",
        source="granola",
        source_id="note-1",
        date="2026-09-20",
        date_source="api",
        recorded_by="sam",
        attendees=[Attendee(name="Sam", email="sam@acme.dev"), Attendee(name="Jordan", email="jordan@acme.dev")],
        title="Weekly sync",
    )


# ---------- paths ----------

def test_digest_path():
    assert digest_path("2026-09-18_darius-kemp") == "digests/2026-09-18_darius-kemp.md"


# ---------- round trips ----------

def test_full_digest_round_trip(tmp_path):
    repo = RepoFS(tmp_path)
    d = full_digest()
    rel = write_digest(repo, d)
    assert rel == "digests/2026-09-18_darius-kemp.md"
    back = read_digest(repo, rel)
    assert back == d
    assert back.claims[0].offset == (18234, 18311)
    assert isinstance(back.claims[0].offset, tuple)
    assert isinstance(back.claims[0].scope, Scope)
    assert isinstance(back.claims[1].flags, Flags)
    assert isinstance(back.brief, BriefRef)
    assert isinstance(back.belief_checks[0], BeliefCheck)
    assert isinstance(back.question_coverage[0], QuestionCoverage)
    assert isinstance(back.follow_ups[0], FollowUp)
    assert isinstance(back.attendees[0], Attendee)
    assert isinstance(back.verification, Verification)


def test_front_matter_is_to_dict(tmp_path):
    repo = RepoFS(tmp_path)
    d = full_digest()
    rel = write_digest(repo, d)
    fm, _, _ = frontmatter.split(repo.read(rel))
    assert fm == yaml.safe_load(frontmatter.dump_yaml(d.to_dict()))
    assert fm["date"] == "2026-09-18"          # stays a string, not a YAML date
    assert fm["brief"]["ts"] == "1727470000.000100"
    assert fm["claims"][0]["quotes"][0]["offset"] == [18234, 18311]
    assert "quote" not in fm["claims"][0]


def test_rewrite_is_stable(tmp_path):
    repo = RepoFS(tmp_path)
    rel = write_digest(repo, full_digest())
    first = repo.read(rel)
    write_digest(repo, read_digest(repo, rel))
    assert repo.read(rel) == first


def test_stub_round_trip(tmp_path):
    repo = RepoFS(tmp_path)
    stub = make_stub(call_id="granola_note-1", transcript="transcripts/granola_note-1.md",
                     status="internal", origin="live", reason="all attendees have Acme emails",
                     parsed=parsed_transcript())
    rel = write_digest(repo, stub)
    assert rel == "digests/granola_note-1.md"
    back = read_digest(repo, rel)
    assert back == stub
    assert back.status == "internal"
    assert back.reason == "all attendees have Acme emails"
    assert back.claims == []


# ---------- stubs ----------

def test_make_stub_copies_parsed_metadata():
    p = parsed_transcript()
    stub = make_stub(call_id="granola_note-1", transcript=p.path, status="internal", origin="backfill",
                     reason="internal call", parsed=p)
    assert stub.source == "granola"
    assert stub.source_id == "note-1"
    assert stub.date == "2026-09-20"
    assert stub.date_source == "api"
    assert stub.recorded_by == "sam"
    assert stub.speaker_labels == "me_them"
    assert [a.email for a in stub.attendees] == ["sam@acme.dev", "jordan@acme.dev"]
    assert stub.attendees is not p.attendees
    assert stub.origin == "backfill"          # the explicit origin wins
    assert stub.duplicates == []
    assert is_stub(stub)


def test_make_stub_without_parsed():
    stub = make_stub(call_id="weird-file", transcript="transcripts/weird-file.md", status="parse_failed",
                     origin="backfill", reason="unrecognized shape")
    assert stub.date is None
    assert stub.date_source == "unknown"
    assert stub.source is None
    assert stub.attendees == []
    assert stub.reason == "unrecognized shape"


def test_make_stub_rejects_bad_status_and_origin():
    with pytest.raises(ValueError):
        make_stub(call_id="x", transcript="transcripts/x.md", status="digested", origin="live", reason="r")
    with pytest.raises(ValueError):
        make_stub(call_id="x", transcript="transcripts/x.md", status="failed", origin="later", reason="r")
    assert set(STUB_STATUSES) == {"internal", "parse_failed", "failed"}


def test_stub_body_has_title_status_and_reason():
    stub = make_stub(call_id="granola_note-1", transcript="transcripts/granola_note-1.md", status="failed",
                     origin="live", reason="cost cap hit", parsed=parsed_transcript())
    body = render_body(stub)
    assert body.startswith("# ")
    assert "Status: failed" in body
    assert "Reason: cost cap hit" in body
    assert "## Findings" not in body
    assert "None" not in body


# ---------- readable body ----------

def test_body_sections_and_quotes():
    d = full_digest()
    body = render_body(d)
    assert body.startswith("# Darius Kemp · Example Mutual · claims adjuster (staff) · 2026-09-18\n")
    for heading in ("## Summary", "## Findings", "## Belief checks", "## Follow-ups", "## Ask next"):
        assert heading in body
    # quotes appear verbatim as blockquotes, one "> " per line
    assert "> on the commercial side the carriers buy a lot less of it than they do for personal auto and homeowners" in body
    assert "> yes: it's a three year deal\n>   that keeps refreshing # like-for-like" in body
    assert "Carriers pay less for commercial claims tooling than personal lines" in body
    assert "Darius Kemp" in body and "found by 2 models" in body and "found by 1 model" in body
    assert "B-003" in body and "contradicts" in body
    assert "send deck" in body and "> send me the deck" in body
    assert "What retention window did the store quote?" in body
    assert "3 quotes" in body and "2 matched first pass" in body and "1 repaired" in body and "1 dropped" in body


def test_body_counts_follow_up_quotes_left_out(tmp_path):
    d = full_digest()
    assert "follow-up quote" not in render_body(d)
    d.verification.follow_up_quotes_dropped = 2
    assert "2 follow-up quote(s) not found in the transcript, left out." in render_body(d)
    repo = RepoFS(tmp_path)
    back = read_digest(repo, write_digest(repo, d))
    assert back.verification.follow_up_quotes_dropped == 2


def test_body_omits_missing_fields_and_never_prints_none():
    d = full_digest()
    d.firm = None
    d.seat = None
    d.claims[0].quotes[0].speaker = None
    d.follow_ups[0].who = None
    body = render_body(d)
    assert body.startswith("# Darius Kemp · 2026-09-18\n")
    assert "None" not in body


def test_title_falls_back_to_call_id():
    d = make_stub(call_id="weird-file", transcript="transcripts/weird-file.md", status="parse_failed",
                  origin="backfill", reason="unrecognized shape")
    assert digest_title(d) == "weird-file"


def test_stub_title_leads_with_call_id_not_a_bare_date():
    stub = make_stub(call_id="granola_note-1", transcript="transcripts/granola_note-1.md", status="internal",
                     origin="live", reason="internal", parsed=parsed_transcript())
    assert digest_title(stub) == "granola_note-1 \u00b7 2026-09-20"


def test_body_has_no_em_dash_outside_quotes():
    d = full_digest()
    d.claims[0].text = f"Advance rates {EM_DASH} not stated terms {EM_DASH} carry the preference"
    d.belief_checks[0].note = f"Adjuster view{EM_DASH}not carrier"
    body = render_body(d)
    assert EM_DASH not in body


def test_written_file_has_no_em_dash_in_model_text(tmp_path):
    repo = RepoFS(tmp_path)
    d = full_digest()
    d.summary = [f"Guidewire already does this {EM_DASH} at scale"]
    d.claims[0].text = f"Advance rates{EM_DASH}not stated terms"
    d.belief_checks[0].note = f"{EM_DASH} adjuster view"
    d.follow_ups[0].what = f"send deck {EM_DASH} v2"
    d.ask_next = [f"Which bank {EM_DASH} JPM or Citi?"]
    rel = write_digest(repo, d)
    text = repo.read(rel)
    assert EM_DASH not in text
    back = read_digest(repo, rel)
    assert back.summary == ["Guidewire already does this, at scale"]
    assert back.claims[0].text == "Advance rates, not stated terms"
    assert back.belief_checks[0].note == "adjuster view"
    assert back.follow_ups[0].what == "send deck, v2"
    assert back.ask_next == ["Which bank, JPM or Citi?"]
    # the caller's digest is untouched
    assert d.summary == [f"Guidewire already does this {EM_DASH} at scale"]


def test_text_without_em_dash_is_written_untouched(tmp_path):
    repo = RepoFS(tmp_path)
    d = full_digest()
    d.summary = ["ends with a comma,", "  leading spaces kept"]
    d.claims[0].text = "Advance rates, not stated, "
    back = read_digest(repo, write_digest(repo, d))
    assert back == d


def test_em_dash_cleanup_never_touches_quotes(tmp_path):
    repo = RepoFS(tmp_path)
    d = full_digest()
    d.claims[0].quotes[0].quote = f"we went to the store {EM_DASH} they taped over it"
    d.follow_ups[0].quote = f"I'll send it {EM_DASH} promise"
    assert without_em_dashes(d).claims[0].quote == d.claims[0].quote
    back = read_digest(repo, write_digest(repo, d))
    assert back.claims[0].quote == f"we went to the store {EM_DASH} they taped over it"
    assert back.follow_ups[0].quote == f"I'll send it {EM_DASH} promise"


def test_quote_with_em_dash_stays_verbatim():
    d = full_digest()
    d.claims[0].quotes[0].quote = f"we went to the store {EM_DASH} they taped over it"
    body = render_body(d)
    assert f"> we went to the store {EM_DASH} they taped over it" in body


# ---------- loading and lookup ----------

def test_load_digests_sorted_by_call_id(tmp_path):
    repo = RepoFS(tmp_path)
    write_digest(repo, full_digest("2026-09-20_zed-a"))
    write_digest(repo, full_digest("2026-09-18_darius-kemp"))
    write_digest(repo, make_stub(call_id="granola_note-1", transcript="transcripts/granola_note-1.md",
                                 status="internal", origin="live", reason="internal"))
    repo.write("digests/notes.txt", "not a digest")
    got = load_digests(repo)
    assert [d.call_id for d in got] == ["2026-09-18_darius-kemp", "2026-09-20_zed-a", "granola_note-1"]


def test_load_digests_empty(tmp_path):
    assert load_digests(RepoFS(tmp_path)) == []


def test_read_digest_without_front_matter_raises(tmp_path):
    repo = RepoFS(tmp_path)
    repo.write("digests/bad.md", "# just markdown\n")
    with pytest.raises(ValueError, match="digests/bad.md"):
        read_digest(repo, "digests/bad.md")


# A hand correction with a YAML slip: the firm's closing quote is missing.
BROKEN = ('---\ncall_id: 2026-09-01_jane-doe\ntranscript: transcripts/circleback_777.md\nstatus: digested\n'
          'origin: live\nfirm: "Example\n---\n\n# Jane Doe\n')


@pytest.mark.parametrize("text", [
    BROKEN,
    "---\ncall_id: x\ntranscript: [a, b]\nstatus: digested\norigin: live\n---\n",   # a field of the wrong shape
    "---\ncall_id: x\ntranscript: t.md\nstatus: digested\norigin: live\nclaims: [1, 2]\n---\n",
])
def test_read_digest_names_the_file_whatever_went_wrong(tmp_path, text):
    repo = RepoFS(tmp_path)
    repo.write("digests/2026-09-01_jane-doe.md", text)
    with pytest.raises(DigestError, match="digests/2026-09-01_jane-doe.md"):
        read_digest(repo, "digests/2026-09-01_jane-doe.md")
    assert issubclass(DigestError, ValueError)


def test_load_digests_skips_an_unreadable_file_and_lists_it(tmp_path):
    repo = RepoFS(tmp_path)
    write_digest(repo, full_digest("2026-09-18_darius-kemp"))
    repo.write("digests/2026-09-01_jane-doe.md", BROKEN)
    repo.write("digests/README.md", "# What lives here\n")
    got = load_digests(repo)
    assert [d.call_id for d in got] == ["2026-09-18_darius-kemp"]
    assert [(u.path, u.call_id, u.transcript) for u in got.unreadable] == [
        ("digests/2026-09-01_jane-doe.md", "2026-09-01_jane-doe", "transcripts/circleback_777.md"),
        ("digests/README.md", "README", None),
    ]
    assert "digests/2026-09-01_jane-doe.md" in got.unreadable[0].error
    assert unreadable_paths(got) == ["digests/2026-09-01_jane-doe.md", "digests/README.md"]
    assert unreadable_paths([]) == []


@pytest.mark.parametrize("line", ["transcript: 'transcripts/circleback_777.md'",
                                  'transcript: "transcripts/circleback_777.md"',
                                  "transcript:   ./transcripts/circleback_777.md  "])
def test_an_unreadable_digest_still_owns_its_transcript(tmp_path, line):
    repo = RepoFS(tmp_path)
    repo.write("digests/2026-09-01_jane-doe.md", BROKEN.replace("transcript: transcripts/circleback_777.md", line))
    digests = load_digests(repo)
    owner = digest_for_transcript(digests, "transcripts/circleback_777.md")
    assert owner is not None and is_unreadable(owner) and is_stub(owner)
    assert owner.call_id == "2026-09-01_jane-doe"
    assert digest_for_transcript(digests, "transcripts/other.md") is None
    # a plain list (a reader that filtered the result) knows nothing of unreadable files
    assert digest_for_transcript(list(digests), "transcripts/circleback_777.md") is None


def test_a_readable_digest_of_the_transcript_wins_over_an_unreadable_one(tmp_path):
    repo = RepoFS(tmp_path)
    write_digest(repo, full_digest("2026-09-18_darius-kemp"))
    repo.write("digests/2026-09-01_jane-doe.md", BROKEN.replace("circleback_777", "circleback_12345"))
    got = digest_for_transcript(load_digests(repo), "transcripts/circleback_12345.md")
    assert got.call_id == "2026-09-18_darius-kemp"


def test_the_stand_in_for_an_unreadable_digest_is_never_written(tmp_path):
    repo = RepoFS(tmp_path)
    repo.write("digests/2026-09-01_jane-doe.md", BROKEN)
    owner = digest_for_transcript(load_digests(repo), "transcripts/circleback_777.md")
    with pytest.raises(DigestError, match="never"):
        write_digest(repo, owner)
    assert repo.read("digests/2026-09-01_jane-doe.md") == BROKEN


@pytest.mark.parametrize("value", ["2026-09-18", "2026-09-18T10:00:00-07:00", "2026-09-18 10:00:00"])
def test_hand_edited_unquoted_date_reads_back_as_text(tmp_path, value):
    # YAML loads an unquoted date (or datetime) as an object; every reader compares dates as text
    repo = RepoFS(tmp_path)
    rel = write_digest(repo, full_digest("2026-09-18_darius-kemp"))
    text = repo.read(rel)
    assert "date: '2026-09-18'" in text
    repo.write(rel, text.replace("date: '2026-09-18'", f"date: {value}", 1))
    d = read_digest(repo, rel)
    assert d.date == "2026-09-18" and isinstance(d.date, str)
    assert [x.date for x in load_digests(repo)] == ["2026-09-18"]


@pytest.mark.parametrize("value,expected", [(dt.date(2026, 9, 18), "2026-09-18"),
                                            (dt.datetime(2026, 9, 18, 23, 30), "2026-09-18"),
                                            ("2026-09-18", "2026-09-18"), (None, None), ("", "")])
def test_digest_from_dict_turns_date_objects_into_text(value, expected):
    d = Digest.from_dict({"call_id": "x", "transcript": "transcripts/x.md", "status": "digested",
                          "origin": "live", "date": value})
    assert d.date == expected


def test_digest_for_transcript_matches_transcript_and_duplicates():
    a = full_digest("2026-09-18_darius-kemp")
    b = full_digest("2026-09-20_zed-a")
    b.transcript = "transcripts/circleback_999.md"
    b.duplicates = ["transcripts/granola_zzz.md"]
    digests = [a, b]
    assert digest_for_transcript(digests, "transcripts/circleback_12345.md") is a
    assert digest_for_transcript(digests, "transcripts/granola_abc.md") is a
    assert digest_for_transcript(digests, "transcripts/granola_zzz.md") is b
    assert digest_for_transcript(digests, "./transcripts/granola_zzz.md") is b
    assert digest_for_transcript(digests, "transcripts/nobody.md") is None
    assert digest_for_transcript([], "transcripts/nobody.md") is None


def test_digest_for_transcript_prefers_own_transcript_over_duplicates():
    owner = full_digest("2026-09-18_darius-kemp")
    owner.transcript = "transcripts/granola_abc.md"
    owner.duplicates = []
    lister = full_digest("2026-09-19_other")
    lister.duplicates = ["transcripts/granola_abc.md"]
    assert digest_for_transcript([lister, owner], "transcripts/granola_abc.md") is owner


def test_digest_for_transcript_prefers_real_digest_over_stub():
    stub = make_stub(call_id="circleback_12345", transcript="transcripts/circleback_12345.md",
                     status="failed", origin="live", reason="timeout")
    real = full_digest()
    assert digest_for_transcript([stub, real], "transcripts/circleback_12345.md") is real


# ---------- overrides ----------

def test_apply_overrides_sets_fields_and_manual_sources():
    d = full_digest()
    overrides = yaml.safe_load(
        "2026-09-18_darius-kemp: {firm: Example Mutual Insurance, seat: underwriter, date: 2026-09-17, "
        "contact: Darius J. Kemp, role_in_thesis: expert, institution_type: MGA, recorded_by: jordan}\n"
        "2026-09-20_other: {firm: Nope}\n"
    )
    out = apply_overrides(d, overrides)
    assert out.firm == "Example Mutual Insurance" and out.firm_source == "manual"
    assert out.date == "2026-09-17" and out.date_source == "manual"
    assert out.seat == "underwriter"
    assert out.contact == "Darius J. Kemp"
    assert out.role_in_thesis == "expert"
    assert out.institution_type == "MGA"
    assert out.recorded_by == "alex"          # not an overridable field
    assert out.call_id == "2026-09-18_darius-kemp"   # the call id never changes
    # the input is left untouched
    assert d.firm == "Example Mutual" and d.firm_source == "transcript" and d.date_source == "api"


def test_apply_overrides_only_touches_given_fields():
    d = full_digest()
    out = apply_overrides(d, {"2026-09-18_darius-kemp": {"seat": "reserving actuary"}})
    assert out.seat == "reserving actuary"
    assert out.firm == "Example Mutual" and out.firm_source == "transcript"
    assert out.date == "2026-09-18" and out.date_source == "api"


def test_apply_overrides_no_entry_returns_equal_copy():
    d = full_digest()
    out = apply_overrides(d, {})
    assert out == d
    out2 = apply_overrides(d, {"other": {"firm": "X"}})
    assert out2 == d


def test_apply_overrides_ignores_empty_values_and_accepts_datetime():
    d = full_digest()
    out = apply_overrides(d, {d.call_id: {"firm": None, "date": dt.datetime(2026, 9, 16, 10, 30)}})
    assert out.firm == "Example Mutual" and out.firm_source == "transcript"
    assert out.date == "2026-09-16" and out.date_source == "manual"


def test_apply_overrides_skips_a_date_that_is_not_a_real_date():
    # frontmatter.load_yaml keeps an impossible date as text so config still loads
    d = full_digest()
    overrides = frontmatter.load_yaml("2026-09-18_darius-kemp: {date: 2026-09-31, seat: reserving actuary}\n")
    assert overrides["2026-09-18_darius-kemp"]["date"] == "2026-09-31"
    out = apply_overrides(d, overrides)
    assert (out.date, out.date_source) == ("2026-09-18", "api")
    assert out.seat == "reserving actuary"                       # the rest of the correction still applies
    for bad in ("2026-02-30", "Sept 17", "2026-09-17 or so", ["2026-09-17"]):
        out = apply_overrides(d, {d.call_id: {"date": bad}})
        assert (out.date, out.date_source) == ("2026-09-18", "api"), bad
    out = apply_overrides(d, {d.call_id: {"date": " 2026-09-17 "}})
    assert (out.date, out.date_source) == ("2026-09-17", "manual")
    assert override_problems(overrides) == [
        "config/overrides.yml: 2026-09-18_darius-kemp date '2026-09-31' is not a real YYYY-MM-DD date; ignored"]
    assert override_problems(overrides, "2026-09-20_other") == []
    assert override_problems({d.call_id: {"date": dt.date(2026, 9, 17)}, "x": None, "y": "junk"}) == []


def test_overridden_digest_round_trips(tmp_path):
    repo = RepoFS(tmp_path)
    d = apply_overrides(full_digest(), {"2026-09-18_darius-kemp": {"date": dt.date(2026, 9, 17)}})
    back = read_digest(repo, write_digest(repo, d))
    assert back.date == "2026-09-17" and back.date_source == "manual"
    assert back == d


def test_a_finding_with_several_quotes_round_trips_and_shows_each_quote(tmp_path):
    from nua.types import Quote
    d = full_digest()
    d.claims[0].quotes.append(Quote(quote="Market conduct exams. They read every note.", turns=[60, 61],
                                    offset=(19000, 19040), speaker="Darius Kemp", attribution="inferred"))
    repo = RepoFS(tmp_path)
    back = read_digest(repo, write_digest(repo, d))
    assert back.claims[0].quotes == d.claims[0].quotes
    body = render_body(d)
    assert "> Market conduct exams. They read every note." in body
    assert "Darius Kemp (inferred) · turns 60, 61" in body
    assert body.count("found by 2 models") == 1             # once per finding, after its quotes


def test_an_older_one_quote_digest_still_reads(tmp_path):
    repo = RepoFS(tmp_path)
    fm = full_digest().to_dict()
    q = fm["claims"][0].pop("quotes")[0]
    fm["claims"][0].update(q)                                # the shape before findings carried quotes
    repo.write("digests/old.md", frontmatter.join(fm, "body"))
    d = read_digest(repo, "digests/old.md")
    assert d.claims[0].quote == q["quote"] and d.claims[0].offset == tuple(q["offset"])
