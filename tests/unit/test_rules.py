"""Tests for nua/rules.py: internal-call rule, duplicates, winner choice, cofounder invites."""
from __future__ import annotations

import pytest

from nua import config as config_mod
from datetime import datetime, timezone

from nua.rules import (as_datetime, by_arrival, cofounder_invited, ingest_stamp, ingested_at, is_internal,
                       note_version, pick_winner, same_call)
from nua.types import Attendee, ParsedTranscript


def make(path: str, *, labels: str = "named", start: str | None = None, date: str | None = None,
         ical_uid: str | None = None, emails: list[str] | None = None, source: str | None = None,
         source_id: str | None = None, recorded_by: str | None = None,
         ingested: object = None) -> ParsedTranscript:
    """A minimal ParsedTranscript for rule tests; the body is irrelevant here."""
    return ParsedTranscript(
        path=path, raw="", shape="standard", front_matter={"ingested_at": ingested} if ingested else {},
        notes="", body_start=0, body_end=0,
        turns=[], speaker_labels=labels, origin="live" if source else "backfill", source=source,
        source_id=source_id, ical_uid=ical_uid, start=start, date=date, recorded_by=recorded_by,
        attendees=[Attendee(name=None, email=e) for e in (emails or [])])


@pytest.fixture
def cfg(tmp_repo):
    c = config_mod.load(tmp_repo)
    c.pipeline.internal_email_domains = ["acme.dev"]
    emails = {"alex": "alex@acme.dev", "sam": "sam@acme.dev", "jordan": "jordan@acme.dev"}
    for p in c.people:
        p.email = emails[p.key]
    return c


# ---------- is_internal ----------

def test_two_internal_attendees_is_internal(cfg):
    assert is_internal([Attendee(name="Alex", email="alex@acme.dev"), Attendee(name="Sam", email="sam@acme.dev")], cfg)


def test_only_owner_listed_is_not_internal(cfg):
    assert not is_internal([Attendee(name="Alex", email="alex@acme.dev")], cfg)
    assert not is_internal([], cfg)


def test_external_attendee_is_not_internal(cfg):
    assert not is_internal([Attendee(name="Alex", email="alex@acme.dev"), Attendee(name="Darius Kemp", email="darius@example.com")], cfg)


def test_attendee_without_email_is_not_internal(cfg):
    assert not is_internal([Attendee(name="Alex", email="alex@acme.dev"), Attendee(name="Darius Kemp", email=None)], cfg)


def test_owner_listed_twice_is_not_internal(cfg):
    assert not is_internal([Attendee(name="Alex", email="alex@acme.dev"), Attendee(name="Alex Rivera", email="ALEX@acme.dev")], cfg)


def test_nua_domain_match_is_case_insensitive(cfg):
    assert is_internal([Attendee(name=None, email="a@ACME.DEV"), Attendee(name=None, email="b@acme.dev")], cfg)


def test_cofounder_personal_email_counts_as_nua(cfg):
    cfg.people[0].email = "alex.rivera@gmail.com"
    assert is_internal([Attendee(name="Alex", email="alex.rivera@gmail.com"), Attendee(name="Sam", email="sam@acme.dev")], cfg)


# ---------- note_version ----------

def test_note_version():
    assert note_version("transcripts/granola_abc.md") == ("abc", 1)
    assert note_version("transcripts/granola_abc_v2.md") == ("abc", 2)
    assert note_version("transcripts/granola_not_1d3tm_v3.md") == ("not_1d3tm", 3)
    assert note_version("granola_not_1d3tm.md") == ("not_1d3tm", 1)
    assert note_version("transcripts/circleback_123.md") == (None, 1)
    assert note_version("transcripts/2026-09-23_darius-kemp.md") == (None, 1)


# ---------- same_call ----------

def test_same_call_by_ical_uid_and_pacific_date():
    a = make("transcripts/circleback_1.md", ical_uid="abc@google.com", start="2026-09-18T16:00:00-07:00")
    b = make("transcripts/circleback_2.md", ical_uid="abc@google.com", start="2026-09-18T16:45:00-07:00")
    assert same_call(a, b)


def test_ical_uid_on_different_day_is_a_different_call():
    # A recurring invite keeps its ical_uid across instances.
    a = make("transcripts/circleback_1.md", ical_uid="abc@google.com", start="2026-09-18T16:00:00-07:00")
    b = make("transcripts/circleback_2.md", ical_uid="abc@google.com", start="2026-09-25T16:00:00-07:00")
    assert not same_call(a, b)


def test_ical_date_is_taken_in_pacific_time():
    # 23:30 Pacific on the 18th is the 19th in UTC; both are the 18th in Pacific.
    a = make("transcripts/circleback_1.md", ical_uid="u", start="2026-09-18T23:30:00-07:00")
    b = make("transcripts/circleback_2.md", ical_uid="u", start="2026-09-19T06:40:00+00:00")
    assert same_call(a, b)
    c = make("transcripts/circleback_3.md", ical_uid="u", start="2026-09-19T07:10:00+00:00")
    assert not same_call(a, c)


def test_ical_date_is_taken_in_the_configured_zone():
    # 14:30 and 15:30 UTC on the 18th: the same day in Pacific, but 23:30 on the 18th and 00:30 on the
    # 19th in Tokyo.
    a = make("transcripts/circleback_1.md", ical_uid="u", start="2026-09-18T14:30:00+00:00")
    b = make("transcripts/circleback_2.md", ical_uid="u", start="2026-09-18T15:30:00+00:00")
    assert same_call(a, b)
    assert not same_call(a, b, tz="Asia/Tokyo")


def test_ical_uid_falls_back_to_date_field():
    a = make("transcripts/a.md", ical_uid="u", date="2026-09-18")
    b = make("transcripts/b.md", ical_uid="u", date="2026-09-18")
    assert same_call(a, b)


def test_start_out_of_range_in_pacific_falls_back_to_date_field():
    # A hand-written start one day before year 1 in Pacific must not raise (OverflowError) for
    # every copy the duplicate check compares against.
    a = make("transcripts/a.md", ical_uid="u", start="0001-01-01T00:00:00+14:00", date="2026-09-18")
    b = make("transcripts/b.md", ical_uid="u", date="2026-09-18")
    assert same_call(a, b)
    c = make("transcripts/c.md", ical_uid="u", start="0001-01-01T00:00:00+14:00")
    assert not same_call(c, b)


def test_same_call_by_start_window_and_shared_email():
    a = make("transcripts/circleback_1.md", start="2026-09-18T16:00:00-07:00",
             emails=["alex@acme.dev", "darius@example.com"])
    b = make("transcripts/granola_x.md", start="2026-09-18T16:08:00-07:00",
             emails=["sam@acme.dev", "DARIUS@example.com"])
    assert same_call(a, b)


def test_start_window_needs_a_shared_email():
    a = make("transcripts/circleback_1.md", start="2026-09-18T16:00:00-07:00", emails=["darius@example.com"])
    b = make("transcripts/granola_x.md", start="2026-09-18T16:02:00-07:00", emails=["jane@example.com"])
    assert not same_call(a, b)


def test_start_outside_window_is_a_different_call():
    a = make("transcripts/circleback_1.md", start="2026-09-18T16:00:00-07:00", emails=["darius@example.com"])
    b = make("transcripts/granola_x.md", start="2026-09-18T16:11:00-07:00", emails=["darius@example.com"])
    assert not same_call(a, b)
    assert same_call(a, b, window_minutes=15)


def test_start_window_compares_across_offsets():
    a = make("transcripts/circleback_1.md", start="2026-09-18T16:00:00-07:00", emails=["darius@example.com"])
    b = make("transcripts/granola_x.md", start="2026-09-18T23:05:00+00:00", emails=["darius@example.com"])
    assert same_call(a, b)


def test_two_ad_hoc_notes_by_the_same_cofounder_are_different_calls(cfg):
    # Granola lists only the owner on an ad-hoc phone or in-person note. Two such notes by Alex
    # 8 minutes apart share only his email, which says nothing about the call.
    cfg.people[0].email = "alex.rivera@gmail.com"
    jane = make("transcripts/granola_not_aaaa.md", start="2026-09-18T17:00:00+00:00",
                emails=["alex.rivera@gmail.com"], source="granola", source_id="not_aaaa", recorded_by="alex")
    bob = make("transcripts/granola_not_bbbb.md", start="2026-09-18T17:08:00+00:00",
               emails=["Alex.Rivera@Gmail.com"], source="granola", source_id="not_bbbb", recorded_by="alex")
    assert not same_call(jane, bob, config=cfg)


def test_back_to_back_calls_sharing_only_internal_emails_are_different_calls(cfg):
    a = make("transcripts/circleback_1.md", start="2026-09-18T16:00:00-07:00",
             emails=["alex@acme.dev", "sam@acme.dev", "jane@example.com"])
    b = make("transcripts/circleback_2.md", start="2026-09-18T16:09:00-07:00",
             emails=["alex@acme.dev", "other@ACME.DEV", "bob@example.com"])
    assert not same_call(a, b, config=cfg)


def test_granola_and_circleback_copies_sharing_an_external_email_are_the_same_call(cfg):
    cfg.people[0].email = "alex.rivera@gmail.com"
    granola = make("transcripts/granola_not_aaaa.md", start="2026-09-18T16:02:00-07:00",
                   emails=["darius@example.com", "alex.rivera@gmail.com"], source="granola",
                   source_id="not_aaaa", recorded_by="alex")
    circleback = make("transcripts/circleback_77.md", start="2026-09-18T16:00:00-07:00",
                      emails=["alex.rivera@gmail.com", "sam@acme.dev", "Darius@Example.com"],
                      source="circleback", source_id="77", recorded_by="sam")
    assert same_call(granola, circleback, config=cfg)
    assert same_call(circleback, granola, config=cfg)


def test_ical_uid_still_matches_copies_that_share_only_internal_emails(cfg):
    a = make("transcripts/circleback_1.md", ical_uid="u", start="2026-09-18T16:00:00-07:00",
             emails=["alex@acme.dev"])
    b = make("transcripts/granola_x.md", ical_uid="u", start="2026-09-18T16:03:00-07:00",
             emails=["alex@acme.dev"])
    assert same_call(a, b, config=cfg)


def test_missing_start_or_emails_never_matches_by_window():
    a = make("transcripts/a.md", emails=["darius@example.com"])
    b = make("transcripts/b.md", emails=["darius@example.com"])
    assert not same_call(a, b)


def test_same_call_by_granola_versions():
    a = make("transcripts/granola_abc.md", labels="me_them")
    b = make("transcripts/granola_abc_v2.md", labels="me_them")
    assert same_call(a, b)
    assert not same_call(a, make("transcripts/granola_abd.md"))


def test_same_call_by_source_id():
    a = make("transcripts/granola_abc.md", source="granola", source_id="abc")
    b = make("transcripts/granola_abc_v2.md", source="granola", source_id="abc")
    assert same_call(a, b)


# ---------- pick_winner ----------

def test_later_granola_version_wins():
    v1 = make("transcripts/granola_abc.md", labels="me_them")
    v2 = make("transcripts/granola_abc_v2.md", labels="me_them")
    v3 = make("transcripts/granola_abc_v3.md", labels="me_them")
    order = [v1.path, v2.path, v3.path]
    assert pick_winner([v1, v3, v2], order) is v3


def test_later_version_beats_earlier_even_if_earlier_is_named():
    v1 = make("transcripts/granola_abc.md", labels="named")
    v2 = make("transcripts/granola_abc_v2.md", labels="me_them")
    assert pick_winner([v1, v2], [v1.path, v2.path]) is v2


def test_named_beats_other_labels():
    granola = make("transcripts/granola_x.md", labels="me_them")
    circleback = make("transcripts/circleback_1.md", labels="named")
    assert pick_winner([granola, circleback], [granola.path, circleback.path]) is circleback


def test_tie_goes_to_first_arrival():
    a = make("transcripts/circleback_1.md", labels="named")
    b = make("transcripts/circleback_2.md", labels="named")
    assert pick_winner([a, b], [b.path, a.path]) is b
    assert pick_winner([a, b], [a.path, b.path]) is a


def test_unlisted_arrivals_rank_after_listed_ones():
    a = make("transcripts/granola_a.md", labels="diarized")
    b = make("transcripts/granola_b.md", labels="diarized")
    assert pick_winner([a, b], [b.path]) is b
    assert pick_winner([b, a], []) is a   # neither listed: path order


def test_version_collapse_then_named_rule():
    v1 = make("transcripts/granola_abc.md", labels="me_them")
    v2 = make("transcripts/granola_abc_v2.md", labels="me_them")
    cb = make("transcripts/circleback_9.md", labels="named")
    assert pick_winner([v1, v2, cb], [v1.path, cb.path, v2.path]) is cb


def test_tie_goes_to_the_earliest_ingested_at_when_every_tied_copy_has_one():
    a = make("transcripts/circleback_1.md", ingested="2026-10-01T09:05:00-07:00")
    b = make("transcripts/circleback_2.md", ingested="2026-10-01T16:02:00Z")   # 09:02 Pacific
    assert pick_winner([a, b], [a.path, b.path]) is b
    assert pick_winner([b, a], []) is b


def test_ingested_at_never_beats_the_named_rule_or_a_later_version():
    named = make("transcripts/circleback_1.md", ingested="2026-10-01T10:00:00-07:00")
    them = make("transcripts/granola_x.md", labels="me_them", ingested="2026-10-01T09:00:00-07:00")
    assert pick_winner([them, named], [them.path]) is named
    v1 = make("transcripts/granola_abc.md", labels="me_them", ingested="2026-10-01T09:00:00-07:00")
    v2 = make("transcripts/granola_abc_v2.md", labels="me_them", ingested="2026-10-01T11:00:00-07:00")
    assert pick_winner([v1, v2], [v1.path, v2.path]) is v2


def test_a_resumed_note_keeps_the_arrival_of_its_first_version():
    # SPEC Duplicates: a later version of the same note beats an earlier one. v2 was stamped after the
    # Circleback copy, but the note arrived first (v1), so v2 takes v1's slot and beats the tie.
    v1 = make("transcripts/granola_abc.md", ingested="2026-10-01T09:40:00-07:00")
    cb = make("transcripts/circleback_9.md", ingested="2026-10-01T09:45:00-07:00")
    v2 = make("transcripts/granola_abc_v2.md", ingested="2026-10-01T11:00:00-07:00")
    assert pick_winner([v1, cb, v2], [v1.path, cb.path, v2.path]) is v2
    assert pick_winner([v2, cb, v1], []) is v2
    # Without stamps the note's earliest position in the arrival order counts, not v2's own.
    u1, ucb, u2 = (make(c.path) for c in (v1, cb, v2))
    assert pick_winner([u1, ucb, u2], [u1.path, ucb.path, u2.path]) is u2
    # The Circleback copy still wins when it arrived before the note did.
    early_cb = make("transcripts/circleback_9.md", ingested="2026-10-01T09:30:00-07:00")
    assert pick_winner([v1, early_cb, v2], []) is early_cb


def test_a_named_copy_still_beats_a_resumed_me_them_note():
    v1 = make("transcripts/granola_abc.md", labels="me_them", ingested="2026-10-01T09:40:00-07:00")
    cb = make("transcripts/circleback_9.md", labels="named", ingested="2026-10-01T09:45:00-07:00")
    v2 = make("transcripts/granola_abc_v2.md", labels="me_them", ingested="2026-10-01T11:00:00-07:00")
    assert pick_winner([v1, cb, v2], [v1.path, cb.path, v2.path]) is cb


def test_a_tied_copy_without_ingested_at_falls_back_to_arrival_order():
    stamped = make("transcripts/circleback_1.md", ingested="2026-10-01T09:00:00-07:00")
    legacy = make("transcripts/circleback_2.md")
    assert pick_winner([stamped, legacy], [legacy.path, stamped.path]) is legacy
    assert pick_winner([stamped, legacy], [stamped.path, legacy.path]) is stamped


def test_ingested_at_reads_strings_and_yaml_datetimes():
    assert ingested_at(make("x.md", ingested="2026-10-01T09:00:00-07:00")) == \
        datetime(2026, 10, 1, 16, 0, tzinfo=timezone.utc)
    assert ingested_at(make("x.md", ingested=datetime(2026, 10, 1, 16, 0, tzinfo=timezone.utc))).hour == 16
    assert ingested_at(make("x.md", ingested="2026-10-01T09:00:00")).utcoffset().total_seconds() == -7 * 3600
    assert ingested_at(make("x.md")) is None
    assert ingested_at(make("x.md", ingested="not a time")) is None


def test_as_datetime_reads_strings_datetimes_and_dates():
    from datetime import date
    from zoneinfo import ZoneInfo

    assert as_datetime("2026-11-02T09:00:00-08:00").isoformat() == "2026-11-02T09:00:00-08:00"
    assert as_datetime("2026-10-01T16:00:00Z", "UTC").isoformat() == "2026-10-01T16:00:00+00:00"
    assert as_datetime(date(2026, 11, 2)).isoformat() == "2026-11-02T00:00:00-08:00"   # midnight Pacific
    assert as_datetime("2026-11-02", ZoneInfo("UTC")).isoformat() == "2026-11-02T00:00:00+00:00"
    aware = datetime(2026, 10, 1, 16, 0, tzinfo=timezone.utc)
    assert as_datetime(aware) is aware
    assert as_datetime(None) is None and as_datetime("") is None
    with pytest.raises(ValueError, match="go-live"):
        as_datetime("go-live")
    assert as_datetime("go-live", strict=False) is None
    assert as_datetime(1234, strict=False) is None


def test_ingest_stamp_is_pacific_iso_with_offset():
    assert ingest_stamp(datetime(2026, 10, 1, 16, 0, 5, 123456, tzinfo=timezone.utc)) == "2026-10-01T09:00:05-07:00"


def test_ingest_stamp_and_naive_values_use_the_given_zone():
    late = datetime(2026, 10, 1, 23, 30, 5, tzinfo=timezone.utc)
    assert ingest_stamp(late, "Europe/London") == "2026-10-02T00:30:05+01:00"
    assert ingest_stamp(late, "Asia/Tokyo") == "2026-10-02T08:30:05+09:00"
    assert ingest_stamp(late, "America/New_York") == "2026-10-01T19:30:05-04:00"
    assert as_datetime("2026-11-02", "Europe/London").isoformat() == "2026-11-02T00:00:00+00:00"
    naive = make("x.md", ingested="2026-10-01T09:00:00")
    assert ingested_at(naive, "Asia/Tokyo").utcoffset().total_seconds() == 9 * 3600


def test_naive_arrival_stamps_are_read_in_the_given_zone():
    # 09:00 with no offset is 00:00Z in Tokyo and 16:00Z in Pacific, so it is earlier or later than 08:00Z.
    naive = make("transcripts/circleback_a.md", ingested="2026-10-01T09:00:00")
    utc = make("transcripts/circleback_b.md", ingested="2026-10-01T08:00:00Z")
    assert by_arrival([naive, utc]) == [utc.path, naive.path]
    assert by_arrival([naive, utc], tz="Asia/Tokyo") == [naive.path, utc.path]
    assert pick_winner([naive, utc], []) is utc
    assert pick_winner([naive, utc], [], tz="Asia/Tokyo") is naive


def test_by_arrival_orders_known_first_then_legacy_then_stamps():
    late = make("transcripts/circleback_a.md", ingested="2026-10-01T10:00:00-07:00")
    early = make("transcripts/circleback_b.md", ingested="2026-10-01T09:00:00-07:00")
    legacy = make("transcripts/circleback_c.md")
    ran = make("transcripts/circleback_d.md", ingested="2026-10-01T11:00:00-07:00")
    assert by_arrival([late, early, legacy, ran], first=[ran.path]) == [ran.path, legacy.path, early.path, late.path]


def test_by_arrival_places_every_version_of_a_note_at_its_first_arrival():
    v1 = make("transcripts/granola_abc.md", ingested="2026-10-01T09:40:00-07:00")
    cb = make("transcripts/circleback_9.md", ingested="2026-10-01T09:45:00-07:00")
    v2 = make("transcripts/granola_abc_v2.md", ingested="2026-10-01T11:00:00-07:00")
    v10 = make("transcripts/granola_abc_v10.md", ingested="2026-10-01T12:00:00-07:00")
    assert by_arrival([cb, v10, v2, v1]) == [v1.path, v2.path, v10.path, cb.path]
    # A note leads when any of its versions already has its own digest.
    ran_cb = make("transcripts/circleback_9.md", ingested="2026-10-01T09:30:00-07:00")
    assert by_arrival([ran_cb, v2, v1], first=[v1.path]) == [v1.path, v2.path, ran_cb.path]


def test_pick_winner_needs_copies():
    with pytest.raises(ValueError):
        pick_winner([], [])


# ---------- cofounder_invited ----------

def test_other_cofounder_invited(cfg):
    p = make("transcripts/circleback_1.md", recorded_by="alex",
             emails=["alex@acme.dev", "sam@acme.dev", "darius@example.com"])
    assert cofounder_invited(p, cfg)


def test_only_recorder_invited(cfg):
    p = make("transcripts/circleback_1.md", recorded_by="alex", emails=["Alex@acme.dev", "darius@example.com"])
    assert not cofounder_invited(p, cfg)


def test_recorded_by_can_be_a_name(cfg):
    p = make("transcripts/circleback_1.md", recorded_by="Alex Rivera", emails=["alex@acme.dev", "darius@example.com"])
    assert not cofounder_invited(p, cfg)


def test_non_cofounder_internal_email_is_not_a_cofounder(cfg):
    p = make("transcripts/circleback_1.md", recorded_by="alex", emails=["alex@acme.dev", "intern@acme.dev"])
    assert not cofounder_invited(p, cfg)


def test_unknown_recorder_needs_two_cofounders(cfg):
    one = make("transcripts/granola_x.md", emails=["sam@acme.dev", "darius@example.com"])
    two = make("transcripts/granola_y.md", emails=["sam@acme.dev", "jordan@acme.dev"])
    assert not cofounder_invited(one, cfg)
    assert cofounder_invited(two, cfg)


def test_cofounder_without_email_in_config_is_ignored(cfg):
    for p in cfg.people:
        if p.key == "sam":
            p.email = None
    p = make("transcripts/circleback_1.md", recorded_by="alex", emails=["alex@acme.dev", "sam@acme.dev"])
    assert not cofounder_invited(p, cfg)
