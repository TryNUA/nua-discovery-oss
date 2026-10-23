"""Tests for nua/frontmatter.py: front-matter split and join, and the YAML loader config/ shares. Offline."""
from __future__ import annotations

from datetime import date, datetime

import pytest
import yaml

from nua import frontmatter


def test_split_and_join_round_trip():
    text = frontmatter.join({"source": "circleback", "attendees": ["Darius Kemp"]}, "Body line.\n")
    fm, body, offset = frontmatter.split(text)
    assert fm == {"source": "circleback", "attendees": ["Darius Kemp"]}
    assert body.lstrip("\n") == "Body line.\n" and text[offset:] == body


def test_split_without_front_matter():
    assert frontmatter.split("Just notes.\n") == ({}, "Just notes.\n", 0)


@pytest.mark.parametrize("value", ["2026-11-31", "2026-02-30T09:00:00-08:00", "2026-11-02T25:00:00Z"])
def test_impossible_timestamp_loads_as_its_text(value):
    # yaml.safe_load raises ValueError (not a YAMLError) on these, which no caller expects
    assert frontmatter.load_yaml(f"when: {value}\n") == {"when": value}
    fm, body, _ = frontmatter.split(f"---\ndate: {value}\n---\nBody.\n")
    assert fm == {"date": value} and body == "Body.\n"


def test_real_timestamps_still_load_as_dates_and_datetimes():
    data = frontmatter.load_yaml("d: 2026-11-02\nt: 2026-11-02T09:00:00-08:00\n")
    assert type(data["d"]) is date and data["d"] == date(2026, 11, 2)
    assert isinstance(data["t"], datetime) and data["t"].utcoffset() is not None


def test_loader_is_safe_and_leaves_yaml_safe_loader_alone():
    with pytest.raises(yaml.YAMLError):
        frontmatter.load_yaml("x: !!python/object/apply:os.system ['true']\n")
    with pytest.raises(ValueError):
        yaml.safe_load("d: 2026-11-31\n")


_UNLOADABLE = (pytest.param("d: !!int 12x\n", ValueError, id="bad-int-tag"),
               pytest.param("d: !!float abc\n", ValueError, id="bad-float-tag"),
               pytest.param("d: " + "[" * 3000 + "]" * 3000 + "\n", RecursionError, id="deep-nesting"))


@pytest.mark.parametrize("text,cause", _UNLOADABLE)
def test_yaml_pyyaml_fails_on_without_a_yamlerror_raises_one(text, cause):
    # PyYAML raises a plain ValueError or RecursionError here. Callers catch yaml.YAMLError, so the
    # loader re-raises one, chained from the original.
    with pytest.raises(yaml.YAMLError) as info:
        frontmatter.load_yaml(text)
    assert isinstance(info.value.__cause__, cause)
    assert cause.__name__ in str(info.value)


@pytest.mark.parametrize("text,cause", _UNLOADABLE)
def test_split_raises_a_yamlerror_for_front_matter_pyyaml_cannot_load(text, cause):
    with pytest.raises(yaml.YAMLError):
        frontmatter.split(f"---\n{text}---\n\nBody.\n")


def test_invalid_yaml_still_raises_its_own_yamlerror():
    with pytest.raises(yaml.YAMLError) as info:
        frontmatter.load_yaml("a: [unclosed\n")
    assert info.value.__cause__ is None
