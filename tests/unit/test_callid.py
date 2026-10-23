"""Tests for discovery/callid.py: slugs, call ids, collisions, undated and stub ids."""
from __future__ import annotations

import pytest

from discovery.callid import make_call_id, slug_name, stub_call_id


def test_slug_name_basic():
    assert slug_name("Darius Kemp") == "darius-kemp"
    assert slug_name("  Darius   Kemp  ") == "darius-kemp"


def test_slug_name_ascii_folds_and_strips_punctuation():
    assert slug_name("José Díaz") == "jose-diaz"
    assert slug_name("Lena Okafor-Smith") == "lena-okafor-smith"
    assert slug_name("Conor O'Brien") == "conor-obrien"
    assert slug_name("Conor O’Brien") == "conor-obrien"
    assert slug_name("J. P. Smith") == "j-p-smith"
    # letters NFKD cannot decompose to ascii are transliterated, never dropped
    assert slug_name("Jörg Strauß") == "jorg-strauss"
    assert slug_name("Łukasz Nowak") == "lukasz-nowak"
    assert slug_name("Đặng Văn Lâm") == "dang-van-lam"
    assert slug_name("Øyvind Ødegård") == "oyvind-odegard"
    assert slug_name("Søren Bjærke") == "soren-bjaerke"
    assert slug_name("ÆSIR Œuvre Þór Ðan Kılıç") == "aesir-oeuvre-thor-dan-kilic"


def test_slug_name_unknown():
    assert slug_name(None) == "unknown"
    assert slug_name("") == "unknown"
    assert slug_name("   ") == "unknown"
    assert slug_name("王小明") == "unknown"   # nothing survives ascii folding


def test_make_call_id_dated():
    assert make_call_id("2026-09-18", "Darius Kemp", set()) == "2026-09-18_darius-kemp"


def test_make_call_id_undated():
    assert make_call_id(None, "Viktor Lang", set()) == "undated_viktor-lang"
    assert make_call_id(None, None, set()) == "undated_unknown"


def test_make_call_id_collisions():
    existing = {"2026-09-18_darius-kemp"}
    assert make_call_id("2026-09-18", "Darius Kemp", existing) == "2026-09-18_darius-kemp-2"
    existing.add("2026-09-18_darius-kemp-2")
    assert make_call_id("2026-09-18", "Darius Kemp", existing) == "2026-09-18_darius-kemp-3"


def test_make_call_id_undated_collisions():
    assert make_call_id(None, "Viktor Lang", {"undated_viktor-lang"}) == "undated_viktor-lang-2"


def test_make_call_id_other_person_same_day_is_no_collision():
    assert make_call_id("2026-09-18", "Wade Pruitt", {"2026-09-18_darius-kemp"}) == "2026-09-18_wade-pruitt"


def test_make_call_id_rejects_bad_date():
    with pytest.raises(ValueError):
        make_call_id("Sep 18", "Darius Kemp", set())
    with pytest.raises(ValueError):
        make_call_id("2026-09-18T16:00:00-07:00", "Darius Kemp", set())


def test_stub_call_id_is_file_stem():
    assert stub_call_id("transcripts/granola_abc_v2.md") == "granola_abc_v2"
    assert stub_call_id("transcripts/circleback_123.md") == "circleback_123"
    assert stub_call_id("transcripts/Meeting with Darius.txt") == "Meeting with Darius"
    assert stub_call_id("granola_abc.md") == "granola_abc"
