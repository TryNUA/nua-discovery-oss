"""Tests for discovery/parse.py: the three input shapes, offsets, dates and the standard writer."""
from __future__ import annotations

from pathlib import Path

import pytest

from discovery.parse import ParseError, pacific_date, parse, write_standard

FIXTURES = Path(__file__).resolve().parents[2] / "tests" / "fixtures"
NAMES = ["darius-kemp", "lena-okafor", "viktor-lang", "wade-pruitt"]


def load(name: str) -> str:
    return (FIXTURES / f"{name}.md").read_text(encoding="utf-8")


def parse_fixture(name: str):
    return parse(f"transcripts/{name}.md", load(name), commit_date="2026-10-04")


# ---------- fixtures ----------

@pytest.mark.parametrize("name,count", [("darius-kemp", 176)])
def test_circleback_copies(name, count):
    p = parse_fixture(name)
    assert p.shape == "circleback_copy"
    assert p.speaker_labels == "named"
    assert len(p.turns) == count
    assert p.origin == "backfill"
    assert p.source is None
    assert p.front_matter == {}
    assert all(t.time is not None and t.speaker for t in p.turns)
    assert p.notes.startswith("#### Overview")
    assert p.date is None and p.date_source == "unknown"


def test_darius_kemp_first_and_last_turns():
    p = parse_fixture("darius-kemp")
    first, last = p.turns[0], p.turns[-1]
    assert (first.index, first.time, first.speaker, first.text) == (1, "1:13", "Alex Rivera", "Hi.")
    assert last.index == 176
    assert last.time == "37:36"
    assert last.speaker == "Alex Rivera"
    assert last.text == "Bye."


def test_circleback_participant_label_is_kept_as_a_speaker():
    # Circleback labels part of Darius's audio "Participant 2"; the parser keeps that label as is.
    p = parse_fixture("darius-kemp")
    assert {t.speaker for t in p.turns} == {"Alex Rivera", "Darius Kemp", "Participant 2"}


def test_viktor_lang_granola_export_single_stream():
    p = parse_fixture("viktor-lang")
    assert p.shape == "granola_export"
    assert p.speaker_labels == "single_stream"
    assert len(p.turns) == 1
    assert {t.speaker for t in p.turns} == {"Me"}
    assert all(t.time is None for t in p.turns)
    assert p.turns[0].text.startswith("Hello, this is Viktor. Hey Viktor, it's Alex.")
    assert p.date == "2026-09-30"
    assert p.date_source == "footer"
    assert p.title == "Property claims video review and AI report automation with Viktor"
    assert [(a.name, a.email) for a in p.attendees] == [("Alex Rivera", None)]
    assert p.origin == "backfill"
    # the footer is part of the notes, never the body
    assert "Meeting participants:" in p.notes
    assert "Transcript:" not in p.body.splitlines()[0]


@pytest.mark.parametrize("name,count,title,attendees,date", [
    ("lena-okafor", 202, "Lena Okafor and Alex Rivera", [("Alex Rivera", None), ("Okaforlena27", None)],
     "2026-09-30"),
    ("wade-pruitt", 178, "Alex / Wade", [("Alex Rivera", None), ("Wpruitt", None)], "2026-10-01"),
])
def test_granola_export_me_them(name, count, title, attendees, date):
    p = parse_fixture(name)
    assert p.shape == "granola_export"
    assert p.speaker_labels == "me_them"
    assert len(p.turns) == count
    assert {t.speaker for t in p.turns} == {"Me", "Them"}
    assert all(t.time is None for t in p.turns)
    assert p.date == date
    assert p.date_source == "footer"
    assert p.title == title
    assert [(a.name, a.email) for a in p.attendees] == attendees
    assert p.origin == "backfill"
    assert "Meeting participants:" in p.notes
    assert "Transcript:" not in p.body.splitlines()[0]


@pytest.mark.parametrize("name", NAMES)
def test_offsets_exact_for_every_turn(name):
    raw = load(name)
    p = parse_fixture(name)
    prev_end = p.body_start
    for i, t in enumerate(p.turns, start=1):
        assert t.index == i
        assert raw[t.start:t.end] == t.text
        assert p.body_start <= t.start <= t.end <= p.body_end
        assert t.start >= prev_end
        prev_end = t.end


@pytest.mark.parametrize("name", NAMES)
def test_notes_never_overlap_body(name):
    raw = load(name)
    p = parse_fixture(name)
    assert p.notes
    notes_at = raw.find(p.notes)
    assert notes_at >= 0
    assert notes_at + len(p.notes) <= p.body_start
    assert p.body == raw[p.body_start:p.body_end]
    assert "#### Overview" not in p.body
    assert "Meeting Title:" not in p.body


# ---------- dates ----------

def test_filename_date():
    p = parse("transcripts/2026-09-23_darius-kemp.md", load("darius-kemp"))
    assert p.date == "2026-09-23"
    assert p.date_source == "filename"


def test_invalid_filename_date_is_ignored():
    p = parse("transcripts/2026-13-45_darius-kemp.md", load("darius-kemp"))
    assert p.date is None
    assert p.date_source == "unknown"


def test_footer_year_rolls_back_past_commit_date():
    raw = load("viktor-lang")
    assert parse("x.md", raw, commit_date="2026-10-04").date == "2026-09-30"
    assert parse("x.md", raw, commit_date="2026-09-30").date == "2026-09-30"
    assert parse("x.md", raw, commit_date="2026-09-29").date == "2025-09-30"
    raw = load("wade-pruitt")
    assert parse("x.md", raw, commit_date="2026-10-01").date == "2026-10-01"
    assert parse("x.md", raw, commit_date="2026-09-30").date == "2025-10-01"


def test_footer_date_with_explicit_year():
    raw = granola_export(date_line="Date: Dec 20, 2024")
    p = parse("x.md", raw, commit_date="2026-09-27")
    assert (p.date, p.date_source) == ("2024-12-20", "footer")


def test_filename_date_beats_footer():
    p = parse("transcripts/2026-09-10_viktor-lang.md", load("viktor-lang"), commit_date="2026-10-04")
    assert (p.date, p.date_source) == ("2026-09-10", "filename")


def test_date_tbd_in_notes_is_ignored():
    notes = "# Call notes\n\n- Date: TBD\n" + "".join(f"- point {i}\n" for i in range(20))
    raw = granola_export(notes=notes, date_line="Date: Sep 18")
    p = parse("x.md", raw, commit_date="2026-09-27")
    assert (p.date, p.date_source) == ("2026-09-18", "footer")


def test_date_in_notes_never_used_as_footer():
    notes = "Date: Sep 3\n" + "".join(f"- point {i}\n" for i in range(20))
    raw = granola_export(notes=notes, date_line=None)
    p = parse("x.md", raw, commit_date="2026-09-27")
    assert (p.date, p.date_source) == (None, "unknown")


def test_unparseable_footer_date_is_unknown():
    raw = granola_export(date_line="Date: TBD")
    p = parse("x.md", raw, commit_date="2026-09-27")
    assert (p.date, p.date_source) == (None, "unknown")


def test_pacific_date():
    assert pacific_date("2026-09-18T23:30:00-07:00") == "2026-09-18"
    assert pacific_date("2026-09-19T05:30:00Z") == "2026-09-18"
    assert pacific_date("2026-09-19T05:30:00+00:00") == "2026-09-18"
    assert pacific_date("2026-09-18T16:00:00") == "2026-09-18"
    assert pacific_date("2026-09-18") == "2026-09-18"


# ---------- Granola exports ----------

def granola_export(*, notes: str = "# Notes\n\n- something\n", date_line: str | None = "Date: Sep 18",
                   participants: str = "Alex Rivera, Jane Doe",
                   body: str = "Me: Hello there. Them: Hi, how are you? Me: Good.\n") -> str:
    footer = ["Chat with meeting transcript: https://notes.granola.ai/t/abc", "",
              "Meeting Title: Test call"]
    if date_line:
        footer.append(date_line)
    footer.append(f"Meeting participants: {participants}")
    return notes + "\n---\n\n" + "\n".join(footer) + "\n\nTranscript:\n" + body


def test_granola_me_them_inline_labels():
    raw = granola_export()
    p = parse("x.md", raw, commit_date="2026-09-27")
    assert p.shape == "granola_export"
    assert p.speaker_labels == "me_them"
    assert [(t.speaker, t.text) for t in p.turns] == [
        ("Me", "Hello there."), ("Them", "Hi, how are you?"), ("Me", "Good.")]
    assert all(raw[t.start:t.end] == t.text for t in p.turns)
    assert [a.name for a in p.attendees] == ["Alex Rivera", "Jane Doe"]
    assert p.title == "Test call"


def test_granola_diarized_line_start_labels():
    body = "Speaker A: First line.\nSpeaker B: Second line\ncontinues here.\nSpeaker A: Third.\n"
    raw = granola_export(body=body)
    p = parse("x.md", raw, commit_date="2026-09-27")
    assert p.speaker_labels == "diarized"
    assert [t.speaker for t in p.turns] == ["Speaker A", "Speaker B", "Speaker A"]
    assert p.turns[1].text == "Second line\ncontinues here."
    assert all(raw[t.start:t.end] == t.text for t in p.turns)


def test_granola_single_label_is_single_stream():
    raw = granola_export(body="Them: only them talking. Them: still them.\n")
    p = parse("x.md", raw, commit_date="2026-09-27")
    assert p.speaker_labels == "single_stream"


def test_granola_without_labels_raises():
    raw = granola_export(body="just a block of text with no labels at all\n")
    with pytest.raises(ParseError):
        parse("x.md", raw)


def test_granola_lowercase_me_colon_is_not_a_label():
    raw = granola_export(body="Me: He said to me: fine. Them: ok.\n")
    p = parse("x.md", raw, commit_date="2026-09-27")
    assert [t.text for t in p.turns] == ["He said to me: fine.", "ok."]


def test_granola_export_without_footer():
    raw = "Some notes\n\nTranscript:\nMe: hello. Them: hi.\n"
    p = parse("x.md", raw)
    assert p.shape == "granola_export"
    assert p.title is None and p.attendees == []
    assert p.notes == "Some notes"


# ---------- shape detection ----------

def test_unknown_text_raises():
    with pytest.raises(ParseError):
        parse("x.md", "Just some meeting notes.\n\nNothing that looks like a transcript.\n")


def test_empty_file_raises():
    with pytest.raises(ParseError):
        parse("x.md", "")


def test_transcript_marker_inside_turn_body_is_circleback_copy():
    raw = ("#### Overview\n\n* a point\n\n"
           "[0:01] **Alex Rivera**: Hi.\n\n"
           "Transcript:\n\n"
           "[0:05] **Darius Kemp**: Hey. Transcript: is a word.\n")
    p = parse("x.md", raw)
    assert p.shape == "circleback_copy"
    assert len(p.turns) == 2
    assert p.turns[0].text == "Hi.\n\nTranscript:"
    assert p.turns[1].text == "Hey. Transcript: is a word."
    assert p.notes == "#### Overview\n\n* a point"


def test_hour_timestamps():
    raw = "[59:58] **A B**: one\n\n[1:00:02] **C D**: two\n"
    p = parse("x.md", raw)
    assert [t.time for t in p.turns] == ["59:58", "1:00:02"]


def test_standard_without_transcript_heading_raises():
    raw = "---\nsource: granola\nsource_id: abc\n---\n\nNo transcript here.\n"
    with pytest.raises(ParseError):
        parse("transcripts/granola_abc.md", raw)


def test_standard_with_no_turns_raises():
    raw = write_standard({"source": "granola", "source_id": "abc"}, "notes", [])
    with pytest.raises(ParseError):
        parse("transcripts/granola_abc.md", raw)


def test_crlf_line_endings():
    raw = "#### Overview\r\n\r\n* point\r\n\r\n[0:01] **A B**: Hi.\r\n\r\n[0:02] **C D**: Hey.\r\n"
    p = parse("x.md", raw)
    assert [t.text for t in p.turns] == ["Hi.", "Hey."]
    assert all(raw[t.start:t.end] == t.text for t in p.turns)


# ---------- standard format ----------

FM = {
    "source": "circleback",
    "source_id": 12345,
    "ical_uid": "abc@google.com",
    "start": "2026-09-18T16:00:00-07:00",
    "start_source": "calendar",
    "recorded_by": "alex",
    "attendees": [{"name": "Darius Kemp", "email": "darius@example.com"},
                  {"name": "Alex Rivera", "email": "alex@acme.dev"}],
    "speaker_labels": "named",
}
TURNS = [("0:22", "Alex Rivera", "Hi."), ("0:31", "Darius Kemp", "Hey Alex, \"quoted\" and *starred*."),
         ("1:02:03", "Alex Rivera", "Later on.")]


def test_write_standard_layout():
    text = write_standard(FM, "#### Overview\n\n* a point", TURNS)
    assert text.startswith("---\nsource: circleback\n")
    assert ("---\n\n## Recorder notes (context only, not evidence)\n\n#### Overview\n\n* a point\n\n"
            "## Transcript\n\n[0:22] **Alex Rivera**: Hi.\n\n[0:31] **Darius Kemp**: ") in text
    assert text.endswith("[1:02:03] **Alex Rivera**: Later on.\n")
    assert chr(0x2014) not in text


def test_write_standard_round_trip():
    text = write_standard(FM, "#### Overview\n\n* a point", TURNS)
    p = parse("transcripts/circleback_12345.md", text)
    assert p.shape == "standard"
    assert p.origin == "live"
    assert p.source == "circleback"
    assert p.source_id == "12345"
    assert p.ical_uid == "abc@google.com"
    assert p.start == "2026-09-18T16:00:00-07:00"
    assert p.start_source == "calendar"
    assert (p.date, p.date_source) == ("2026-09-18", "api")
    assert p.recorded_by == "alex"
    assert [(a.name, a.email) for a in p.attendees] == [("Darius Kemp", "darius@example.com"),
                                                        ("Alex Rivera", "alex@acme.dev")]
    assert p.speaker_labels == "named"
    assert p.notes == "#### Overview\n\n* a point"
    assert [(t.time, t.speaker, t.text) for t in p.turns] == TURNS
    assert all(text[t.start:t.end] == t.text for t in p.turns)
    assert "a point" not in p.body


def test_write_standard_granola_me_them():
    fm = {"source": "granola", "source_id": "not_abc", "start": "2026-09-19T05:30:00Z",
          "recorded_by": "sam", "speaker_labels": "me_them", "attendees": []}
    turns = [("0:01", "Me", "Hello."), ("0:04", "Them", "Hi\nthere.")]
    text = write_standard(fm, "", turns)
    p = parse("transcripts/granola_not_abc.md", text)
    assert p.origin == "live"
    assert p.speaker_labels == "me_them"
    assert p.notes == ""
    assert [(t.speaker, t.text) for t in p.turns] == [("Me", "Hello."), ("Them", "Hi there.")]
    assert p.date == "2026-09-18"


def test_write_standard_missing_time_or_speaker():
    fm = {"source": "granola", "source_id": "x1"}
    turns = [(None, "Me", "no time"), ("0:05", None, "no speaker"), (None, None, "neither")]
    text = write_standard(fm, "n", turns)
    p = parse("transcripts/granola_x1.md", text)
    assert [(t.time, t.speaker, t.text) for t in p.turns] == [
        (None, "Me", "no time"), ("0:05", None, "no speaker"), (None, "Unknown", "neither")]
    assert all(text[t.start:t.end] == t.text for t in p.turns)


def test_write_standard_rejects_bad_time():
    with pytest.raises(ValueError):
        write_standard({"source": "granola"}, "", [("65", "Me", "x")])


def test_standard_speaker_labels_inferred_when_missing():
    text = write_standard({"source": "granola", "source_id": "d1"}, "",
                          [("0:01", "Speaker A", "a"), ("0:02", "Speaker B", "b")])
    assert parse("transcripts/granola_d1.md", text).speaker_labels == "diarized"


def test_standard_unquoted_yaml_timestamp():
    raw = ("---\nsource: circleback\nsource_id: 9\nstart: 2026-09-18T23:30:00-07:00\n---\n\n"
           "## Recorder notes (context only, not evidence)\n\nnotes\n\n## Transcript\n\n"
           "[0:01] **A B**: hi\n")
    p = parse("transcripts/circleback_9.md", raw)
    assert p.start == "2026-09-18T23:30:00-07:00"
    assert (p.date, p.date_source) == ("2026-09-18", "api")


def test_standard_notes_containing_transcript_heading():
    notes = "## Transcript\n\nThis heading is part of the AI notes."
    text = write_standard({"source": "granola", "source_id": "h1"}, notes, [("0:01", "Me", "real turn")])
    p = parse("transcripts/granola_h1.md", text)
    assert p.notes == notes
    assert [t.text for t in p.turns] == ["real turn"]


def test_front_matter_without_known_source_is_backfill():
    text = write_standard({"source": "zoom"}, "", [("0:01", "A B", "hi")])
    p = parse("transcripts/zoom.md", text)
    assert p.shape == "standard"
    assert p.origin == "backfill"


# ---------- odd front-matter in dropped files ----------
# One dropped file must never raise anything but ParseError: process_call parses every transcript
# for its duplicate check, so a stray ValueError would fail every other call too.

_DROPPED_BODY = "#### Overview\n\nDarius talked.\n\n[0:22] **Alex Rivera**: Hi.\n\n[0:25] **Darius Kemp**: Hello.\n"


@pytest.mark.parametrize("date_value", ["2025-09-31", "2025-02-29", "0000-00-00", "2026-13-01",
                                        "2026-09-31 10:00:00"])
def test_impossible_front_matter_date_is_not_an_error(date_value):
    raw = f"---\ntitle: Call with Darius\ndate: {date_value}\n---\n\n{_DROPPED_BODY}"
    p = parse("transcripts/darius.md", raw)
    assert p.shape == "circleback_copy"
    assert [t.text for t in p.turns] == ["Hi.", "Hello."]
    assert p.origin == "backfill"


def test_impossible_date_in_a_granola_export_still_parses():
    raw = "---\ndate: 2026-09-31\n---\n\nNotes\n\nTranscript:\nMe: hi\nThem: hello\n"
    p = parse("transcripts/x.md", raw)
    assert p.shape == "granola_export"
    assert [t.text for t in p.turns] == ["hi", "hello"]


@pytest.mark.parametrize("fm", [pytest.param("date: !!int 12x", id="bad-int-tag"),
                                pytest.param("tags: " + "[" * 3000 + "]" * 3000, id="deep-nesting")])
def test_front_matter_yaml_cannot_load_counts_as_none(fm):
    # PyYAML raises ValueError / RecursionError here, not a YAMLError.
    p = parse("transcripts/darius.md", f"---\n{fm}\n---\n\n{_DROPPED_BODY}")
    assert p.front_matter == {}
    assert [t.text for t in p.turns] == ["Hi.", "Hello."]


@pytest.mark.parametrize("value", ["3", "yes", "2026-09-18", "2.5", "true"])
def test_scalar_attendees_are_ignored(value):
    raw = f"---\ntitle: Weekly\nattendees: {value}\n---\n\n{_DROPPED_BODY}"
    p = parse("transcripts/2026-09-20 weekly.md", raw)
    assert p.attendees == []
    assert p.title == "Weekly"
    assert len(p.turns) == 2


def test_scalar_attendees_fall_back_to_the_footer():
    raw = ("---\nattendees: 3\n---\n\nNotes\n\n---\n\nMeeting Title: Darius\nDate: Sep 18\n"
           "Meeting participants: Darius Kemp, Alex Rivera\n\nTranscript:\nMe: hi\nThem: hello\n")
    p = parse("transcripts/x.md", raw, commit_date="2026-09-27")
    assert [a.name for a in p.attendees] == ["Darius Kemp", "Alex Rivera"]


@pytest.mark.parametrize("start", ["0001-01-01T00:00:00+14:00", "9999-12-31T23:00:00-12:00"])
def test_start_out_of_datetime_range_is_ignored(start):
    raw = (f"---\nsource: circleback\nsource_id: 9\nstart: '{start}'\n---\n\n"
           "## Transcript\n\n[0:01] **A B**: hi\n")
    p = parse("transcripts/2026-09-18_x.md", raw)
    assert (p.date, p.date_source) == ("2026-09-18", "filename")


def test_metadata_failure_is_a_parse_error_naming_the_file(monkeypatch):
    import discovery.parse as parse_mod

    def boom(value):
        raise TypeError("'int' object is not iterable")

    monkeypatch.setattr(parse_mod, "_attendees", boom)
    with pytest.raises(ParseError, match="transcripts/darius.md"):
        parse("transcripts/darius.md", f"---\nattendees: 3\n---\n\n{_DROPPED_BODY}")
