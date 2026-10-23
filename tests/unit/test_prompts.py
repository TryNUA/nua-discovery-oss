"""Tests for nua/prompts/: every prompt part is text, and filling it with the placeholders the code
passes never raises KeyError. Offline."""
from __future__ import annotations

import importlib

import pytest

from conftest import fixture_text
from nua import extract as ex
from nua.parse import parse

PARTS = {
    "extract": ("SYSTEM", "EXTRACT", "REPAIR"),
    "consolidate": ("SYSTEM", "CONSOLIDATE"),
    "verdict": ("SYSTEM", "VERDICT"),
    "arbiter": ("SYSTEM", "ARBITER"),
    "ask": ("SYSTEM", "MAP", "REDUCE"),
    "rollup": ("SYSTEM", "WEEKLY", "BOOTSTRAP", "OUTPUT"),
}

# Keys each part is filled with, on top of the shared ones built by the code (see _values).
REPAIR = ("reason", "text", "quote", "turns", "question_turn", "speaker", "transcript", "short_answer_words")
CONSOLIDATE = ("findings", "count", "readers")
VERDICT = ("entries", "claims", "questions", "transcript")
ARBITER = ("disputes", "transcript")
ASK = ("question", "internal_people", "scope")
ASK_MAP = (*ASK, "count", "digests")
ASK_REDUCE = (*ASK, "read", "total", "count", "evidence")
ROLLUP = ("week", "today", "cap", "calls_total", "calls_new", "findings_total", "entries", "decisions",
          "seat_counts", "seats", "findings", "new_claims", "rejected", "open_proposals", "add_calls", "add_seats",
          "split_scope_mismatch", "retire_calls", "retire_seats", "drop_watching_weeks", "under_pressure_calls")


def _values(ctx) -> dict[tuple[str, str], dict]:
    parsed = parse("transcripts/darius-kemp.md", fixture_text("darius-kemp"))
    call = ex.call_values(ctx, parsed)
    extract = ex.extract_values(ctx, parsed)

    def keyed(base: dict, keys: tuple[str, ...]) -> dict:
        return {**base, **{k: f"<{k}>" for k in keys}}

    from nua.prompts import domain_values

    domain = domain_values(ctx.config)
    rollup = keyed(domain, ROLLUP)
    return {
        ("extract", "SYSTEM"): extract, ("extract", "EXTRACT"): extract,
        ("extract", "REPAIR"): keyed(call, REPAIR),
        ("consolidate", "SYSTEM"): keyed(call, CONSOLIDATE), ("consolidate", "CONSOLIDATE"): keyed(call, CONSOLIDATE),
        ("verdict", "SYSTEM"): keyed(call, VERDICT), ("verdict", "VERDICT"): keyed(call, VERDICT),
        ("arbiter", "SYSTEM"): keyed(call, ARBITER), ("arbiter", "ARBITER"): keyed(call, ARBITER),
        ("ask", "SYSTEM"): domain,
        ("ask", "MAP"): keyed(domain, ASK_MAP), ("ask", "REDUCE"): keyed(domain, ASK_REDUCE),
        **{("rollup", part): rollup for part in PARTS["rollup"]},
    }


@pytest.mark.parametrize("name,part", [(n, p) for n, parts in PARTS.items() for p in parts])
def test_prompt_part_is_text_and_fills(ctx, name, part):
    text = getattr(importlib.import_module(f"nua.prompts.{name}"), part)
    assert isinstance(text, str) and text.strip()
    assert text.endswith("\n") and not text.startswith((" ", "\n"))
    assert chr(0x2014) not in text
    filled = ex.fill(text, _values(ctx)[(name, part)])     # a KeyError means a placeholder the code never passes
    assert "{" not in filled.replace("{statement, scope}", "")


def test_literal_braces_survive_filling(ctx):
    from nua.prompts import rollup
    assert "{statement, scope}" in ex.fill(rollup.OUTPUT, _values(ctx)[("rollup", "OUTPUT")])


def test_extract_and_consolidate_follow_the_prompting_guide():
    """Long input first and the request last, sections in XML tags, a short role, and 3 to 5 examples."""
    import re
    from nua.prompts import consolidate, extract
    for system in (extract.SYSTEM, consolidate.SYSTEM):
        assert system.startswith("You are ") and len(system) < 1000
    for body, first in ((extract.EXTRACT, "<recorder_notes>"), (consolidate.CONSOLIDATE, "<findings>")):
        assert body.startswith(first)
        order = [body.index(tag) for tag in ("<context>", "<instructions>", "<examples>")]
        assert order == sorted(order)
        assert 3 <= len(re.findall(r"^<example>$", body, re.M)) <= 5
        assert body.rstrip().splitlines()[-1].startswith("Now ")
    assert extract.EXTRACT.index("</transcript>") < extract.EXTRACT.index("<instructions>")


def test_extract_examples_cover_the_owner_s_cases():
    from nua.prompts import extract
    body = extract.EXTRACT
    assert "IPO" in body and "career" in body.lower()                       # the two exclusions
    assert "Large shippers audit the brokerage's load files line by line" in body    # the consolidated audit finding
    assert "question_turn 235" in body                                       # a short answer
    assert "no target count" in body.lower()


def test_prompts_name_no_market_and_take_it_from_domain_yml(ctx):
    """The market lives in config/domain.yml, never in a prompt: the filled prompt follows the file."""
    import re

    ctx.config.domain.company = "Zephyr Labs"
    ctx.config.domain.market = "dental practice software"
    values = _values(ctx)
    for name, parts in PARTS.items():
        module = importlib.import_module(f"nua.prompts.{name}")
        for part in parts:
            raw = getattr(module, part)
            assert not re.search(r"(?i)\bnua\b|insurance|underwrit|actuar", raw), (name, part)
    from nua.prompts import domain_values
    filled = ex.fill(importlib.import_module("nua.prompts.extract").SYSTEM, {**values[("extract", "SYSTEM")],
                                                                              **domain_values(ctx.config)})
    assert "Zephyr Labs" in filled
    ask_system = ex.fill(importlib.import_module("nua.prompts.ask").SYSTEM, domain_values(ctx.config))
    assert "dental practice software" in ask_system


def test_domain_yml_falls_back_to_neutral_defaults(tmp_path):
    from nua.config import _load_domain

    d = _load_domain({"company": "  Zephyr  ", "scope": {"line": "the workflow"}, "roles": {"Buyer": "buys it"}})
    assert d.company == "Zephyr" and d.scope["line"] == "the workflow" and d.scope["party"]
    assert d.roles == {"buyer": "buys it"} and d.market == "our market"
    assert _load_domain("not a mapping").company == "our company"
