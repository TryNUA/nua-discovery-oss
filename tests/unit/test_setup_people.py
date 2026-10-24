"""nua/setup/people.py: keys, email patterns and suggested domains (setup-ui/src/lib/people.ts mirrors it)."""
from __future__ import annotations

import pytest

from nua.setup import configio, people


@pytest.mark.parametrize("email, fmt", [
    ("alex.rivera@acme.dev", "{f}.{l}"), ("alex_rivera@acme.dev", "{f}_{l}"), ("alexrivera@acme.dev", "{f}{l}"),
    ("a.rivera@acme.dev", "{i}.{l}"), ("arivera@acme.dev", "{i}{l}"), ("rivera.alex@acme.dev", "{l}.{f}"),
    ("riveraalex@acme.dev", "{l}{f}"), ("Alex@ACME.dev", "{f}"),
])
def test_infer_each_format(email, fmt):
    assert people.infer_email_pattern("Alex Rivera", email) == (fmt, "acme.dev")


def test_infer_strips_accents_and_needs_a_match():
    assert people.infer_email_pattern("José Núñez", "jose.nunez@x.io") == ("{f}.{l}", "x.io")
    assert people.infer_email_pattern("Alex Rivera", "ceo@acme.dev") is None
    assert people.infer_email_pattern("Alex Rivera", "not-an-email") is None
    assert people.infer_email_pattern("", "alex@acme.dev") is None


def test_email_for_applies_the_pattern():
    pattern = people.infer_email_pattern("Alex Rivera", "alex.rivera@acme.dev")
    assert people.email_for("Sam Chen", pattern) == "sam.chen@acme.dev"
    assert people.email_for("Mary Ann Lee", pattern) == "mary.lee@acme.dev"
    assert people.email_for("Cher", pattern) is None                # no last name for {f}.{l}
    assert people.email_for("Cher", ("{f}", "acme.dev")) == "cher@acme.dev"
    assert people.email_for("Sam Chen", None) is None


def test_person_key_is_unique_and_valid():
    assert people.person_key("Alex Rivera", []) == "alex"
    assert people.person_key("Alex Chen", ["alex"]) == "alexc"
    assert people.person_key("Alex Cole", ["alex", "alexc"]) == "alex2"
    assert people.person_key("Alex", ["alex", "alex2"]) == "alex3"
    assert people.person_key("Élodie Brun", []) == "elodie"
    assert people.person_key("3M Bot", []) == "p3m"
    for key in ("alex", "alexc", "elodie", "p3m"):
        assert not configio.validate_setup({"people": {key: {"name": "x"}}})


def test_suggested_domains_skip_personal_mail():
    emails = ["a@Acme.dev", "b@gmail.com", None, "c@acme.dev", "d@sub.acme.io", "e@proton.me"]
    assert people.suggested_domains(emails) == ["acme.dev", "sub.acme.io"]
