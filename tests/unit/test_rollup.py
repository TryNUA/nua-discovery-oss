"""Tests for discovery/rollup.py: thresholds, rejection filter, posting, retries and the bootstrap cap.

discovery.indexes is written in parallel, so every test swaps in the small stand-in below through
`rollup._indexes`. Models are FakeLLMs; Slack is FakeSlack; nothing touches the network.
"""
from __future__ import annotations

import types
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest
import yaml

from discovery import rollup
from discovery.beliefs import BeliefsDoc, parse_beliefs, render_beliefs
from discovery.digest import write_digest
from discovery.llm import FakeLLM, LLMError
from discovery.rollup import Proposal, rejection_suppressed, run_rollup
from discovery.types import BeliefCheck, Claim, Digest, Entry, Flags, HistoryLine, Scope

EM_DASH = chr(0x2014)      # spelled this way so no file contains one
PT = ZoneInfo("America/Los_Angeles")


# ---------- a stand-in for discovery.indexes ----------

def _fake_indexes() -> types.SimpleNamespace:
    """The few discovery.indexes functions the roll-up uses, with simple rules."""

    def is_dismissed(check, digest, overrides):
        return any(o.get("call_id") == digest.call_id and o.get("target") == check.target for o in overrides)

    def finding_claims(digests, beliefs, overrides=None):
        current = {e.id for e in beliefs.current()}
        out = []
        for d in digests:
            if d.status != "digested":
                continue
            touched = {cid for b in d.belief_checks if b.target in current
                       and not is_dismissed(b, d, overrides or []) for cid in b.claims}
            out += [(d, c) for c in d.claims if c.id not in touched]
        return out

    def load_verdict_overrides(repo):
        data = yaml.safe_load(repo.read_or("config/verdict_overrides.yml", "")) or {}
        return list(data.get("overrides") or [])

    def under_pressure(entry, digests, overrides, config):
        calls, seats = set(), set()
        for d in digests:
            for b in d.belief_checks:
                if (b.target == entry.id and b.verdict == "contradicts" and b.decided_by in ("agree", "arbiter")
                        and d.checked.get(entry.id) == entry.version and not is_dismissed(b, d, overrides)):
                    calls.add(d.call_id)
                    seats.add(d.seat)
        return entry.section == "beliefs" and len(calls) >= 2 and len(seats) >= 2

    regenerated = []

    def regenerate(ctx):
        regenerated.append(1)
        return ["findings.md"]

    return types.SimpleNamespace(finding_claims=finding_claims, is_dismissed=is_dismissed,
                                 load_verdict_overrides=load_verdict_overrides, under_pressure=under_pressure,
                                 regenerate=regenerate, regenerated=regenerated)


@pytest.fixture
def ix(monkeypatch):
    fake = _fake_indexes()
    monkeypatch.setattr(rollup, "_indexes", lambda: fake)
    return fake


# ---------- test data ----------

def _claim(n: int, quote: str, *, firsthand=False, prompted=True, found_by=1) -> Claim:
    return Claim(id=f"c{n:02d}", text=f"Claim {n}: {quote}", quote=quote, turns=[n], offset=(n * 100, n * 100 + 40),
                 speaker="Guest", flags=Flags(firsthand=firsthand, prompted=prompted), found_by=found_by)


def _digest(call_id: str, seat: str | None, date: str, claims: list[Claim],
            checks: list[tuple[str, str, list[str], str]] = (), checked: dict | None = None) -> Digest:
    return Digest(call_id=call_id, transcript=f"transcripts/{call_id}.md", status="digested", origin="live",
                  date=date, date_source="api", contact=call_id.split("_", 1)[1].replace("-", " ").title(),
                  seat=seat, claims=claims,
                  belief_checks=[BeliefCheck(target=t, verdict=v, claims=list(c), decided_by=by)
                                 for t, v, c, by in checks],
                  checked=dict(checked or {}))


def _entry(entry_id: str, statement: str, section: str, *, date="2026-08-01", ref=None,
           action=None, extra: list[HistoryLine] = ()) -> Entry:
    action = action or ("watch" if section == "watching" else "add")
    first = HistoryLine(date=date, action=action, who="Alex", via="slack", ref=ref or f"r-{entry_id}")
    return Entry(id=entry_id, statement=statement, scope=Scope(), section=section, history=[first, *extra])


def _write(ctx, digests: list[Digest], entries: list[Entry] = ()) -> None:
    for d in digests:
        write_digest(ctx.repo, d)
    ctx.repo.write("beliefs.md", render_beliefs(BeliefsDoc(entries=list(entries))))


def _model(ctx, reply) -> FakeLLM:
    """Register the roll-up model. `reply` is a dict, or a function of the prompt returning one."""
    fake = FakeLLM(ctx.config.pipeline.rollup, lambda s, p, schema: reply(p) if callable(reply) else reply)
    ctx.llms.register(fake)
    return fake


def _reply(proposals=(), themes=()) -> dict:
    return {"themes": list(themes), "proposals": list(proposals)}


def _prop(type_: str, *, statement=None, sources=(), target=None, targets=(), statements=(), theme=None,
          rationale="Because.") -> dict:
    return {"type": type_, "statement": statement,
            "scope": {"line": None, "party": None, "segment": None, "seat": None},
            "statements": list(statements), "sources": list(sources), "target": target, "targets": list(targets),
            "theme": theme, "rationale": rationale}


def _calls_across(seats: list[str | None], *, start_day=10, quote="We scrubbed store footage by hand every day") -> list[Digest]:
    """One call per seat, each with one finding c01 carrying the same point."""
    out = []
    for i, seat in enumerate(seats, 1):
        out.append(_digest(f"2026-09-{start_day + i:02d}_guest-{i}", seat, f"2026-09-{start_day + i:02d}",
                           [_claim(1, f"{quote} at firm {i}")]))
    return out


def _thread(ctx, key: str) -> tuple[dict, list[dict]]:
    ch = ctx.channel("calls")
    ts = ctx.slack.find_by_metadata(ch, "discovery_rollup", key)
    assert ts, f"no summary for {key}"
    top = next(m for m in ctx.slack.messages if m["ts"] == ts)
    return top, ctx.slack.replies(ch, ts)


def _block_ids(message: dict) -> list[str]:
    return [b.get("block_id", "") for b in message["blocks"]]


def _has_buttons(message: dict) -> bool:
    return any(b.get("type") == "actions" or "accessory" in b for b in message["blocks"])


def _all_text(ctx) -> str:
    return repr([m["blocks"] for m in ctx.slack.messages]) + "".join(m["text"] for m in ctx.slack.messages)


# ---------- rejection filter ----------

def _p(type_="add", target=None, sources=()) -> Proposal:
    return Proposal(id="x-01", type=type_, statement="S", scope=Scope(), sources=list(sources), target=target)


REJECTED = [{"type": "add", "target": None, "who": "Alex", "reason": "too vague", "ref": "1.2:prop:x",
             "claims": [{"call_id": "a", "quote": "we scrub store footage by hand"},
                        {"call_id": "b", "quote": "our carrier wants a police report first"}]}]


def test_rejection_suppressed_with_no_new_claims():
    p = _p(sources=[("a", "we scrub store footage by hand"), ("b", "our carrier wants a police report first")])
    assert rejection_suppressed(p, REJECTED)


def test_rejection_suppressed_with_one_new_claim():
    p = _p(sources=[("a", "we scrub store footage by hand"), ("c", "the store video is slow to arrive")])
    assert rejection_suppressed(p, REJECTED)


def test_rejection_not_suppressed_with_two_new_claims():
    p = _p(sources=[("a", "we scrub store footage by hand"), ("c", "the store video is slow to arrive"),
                    ("d", "the carrier asked for twelve months of loss runs")])
    assert not rejection_suppressed(p, REJECTED)


def test_rejection_matches_quotes_loosely_and_by_call():
    # Same words with different case and punctuation still count as the rejected claim.
    p = _p(sources=[("a", "We scrub store footage, by hand."), ("c", "the store video is slow to arrive")])
    assert rejection_suppressed(p, REJECTED)
    # The same quote from another call is a new claim.
    q = _p(sources=[("x", "we scrub store footage by hand"), ("y", "our carrier wants a police report first")])
    assert not rejection_suppressed(q, REJECTED)


def test_rejection_needs_same_type_and_target():
    retired = [{"type": "retire", "target": "B-001", "claims": [{"call_id": "a", "quote": "q one here"}]}]
    assert rejection_suppressed(_p("retire", "B-001", [("a", "q one here")]), retired)
    assert not rejection_suppressed(_p("retire", "B-002", [("a", "q one here")]), retired)
    assert not rejection_suppressed(_p("split", "B-001", [("a", "q one here")]), retired)
    assert not rejection_suppressed(_p("retire", "B-001"), [])


# ---------- thresholds ----------

def _refs(digests: list[Digest]) -> list[str]:
    return [f"{d.call_id}#c01" for d in digests]


def test_add_needs_three_calls_from_two_seats(ctx, ix):
    ok = _calls_across(["claims adjuster (staff)", "claims adjuster (staff)", "underwriter"])
    one_seat = _calls_across(["pricing actuary"] * 3, start_day=20, quote="Subrogation takes a year to close")
    _write(ctx, ok + one_seat)
    _model(ctx, _reply([_prop("add", statement="Adjusters scrub footage by hand", sources=_refs(ok)),
                        _prop("add", statement="Subrogation is slow", sources=_refs(one_seat)),
                        _prop("add", statement="Too few calls", sources=_refs(ok[:2]))]))
    out = run_rollup(ctx, week="2026-W40")
    data = yaml.safe_load(ctx.repo.read("config/proposals.yml"))
    kept = [p for p in data["proposals"] if p["status"] == "open"]
    assert [p["statement"] for p in kept] == ["Adjusters scrub footage by hand"]
    assert out["proposals"] == [kept[0]["id"]] == ["2026-W40-01"]
    # Every evidence source is recorded, with the verbatim quote from the digest.
    assert kept[0]["sources"] == [{"call_id": d.call_id, "quote": d.claims[0].quote} for d in ok]
    assert kept[0]["type"] == "add" and kept[0]["target"] is None


def test_add_skips_sources_already_in_a_belief(ctx, ix):
    calls = _calls_across(["claims adjuster (staff)", "underwriter", "pricing actuary"])
    held = HistoryLine(date="2026-08-01", action="add", who="Alex", via="slack", ref="r-B-001",
                       sources=[(calls[0].call_id, calls[0].claims[0].quote)])
    b1 = Entry(id="B-001", statement="Footage is scrubbed by hand", scope=Scope(), section="beliefs", history=[held])
    _write(ctx, calls, [b1])
    _model(ctx, _reply([_prop("add", statement="Footage review is manual", sources=_refs(calls))]))
    out = run_rollup(ctx, week="2026-W40")
    assert out["proposals"] == []          # 2 calls left after dropping the one already in B-001


def test_theme_claims_count_toward_an_add(ctx, ix):
    calls = _calls_across(["claims adjuster (staff)", "underwriter", "pricing actuary"])
    _write(ctx, calls)
    theme = {"name": "Manual footage review", "renamed_from": None, "claims": _refs(calls)}
    _model(ctx, _reply([_prop("add", statement="Footage review is manual", sources=_refs(calls[:1]),
                              theme="Manual footage review")], [theme]))
    out = run_rollup(ctx, week="2026-W40")
    assert len(out["proposals"]) == 1
    themes = yaml.safe_load(ctx.repo.read("config/themes.yml"))
    assert themes["themes"][0]["name"] == "Manual footage review"
    assert len(themes["themes"][0]["claims"]) == 3
    assert themes["rollup"]["key"] == "rollup:2026-W40"


def _checked(entry: Entry) -> dict:
    return {entry.id: entry.version}


def test_split_needs_three_scope_mismatches(ctx, ix):
    b1 = _entry("B-001", "Dashcam footage settles liability disputes", "beliefs")
    b2 = _entry("B-002", "A police report comes before any payout", "beliefs")
    seats = ["claims adjuster (staff)", "underwriter", "pricing actuary"]
    digests = []
    for i, seat in enumerate(seats, 1):
        checks = [("B-001", "scope_mismatch", ["c01"], "agree")]
        if i <= 2:
            checks.append(("B-002", "scope_mismatch", ["c01"], "agree"))
        digests.append(_digest(f"2026-09-1{i}_guest-{i}", seat, f"2026-09-1{i}", [_claim(1, f"quote number {i} here")],
                               checks, {**_checked(b1), **_checked(b2)}))
    _write(ctx, digests, [b1, b2])
    halves = [{"statement": "Holds for carriers", "scope": {"line": None, "party": "carrier",
                                                              "segment": None, "seat": None}},
              {"statement": "Fails for TPAs", "scope": {"line": None, "party": "TPA",
                                                          "segment": None, "seat": None}}]
    _model(ctx, _reply([_prop("split", target="B-001", statements=halves),
                        _prop("split", target="B-002", statements=halves)]))
    run_rollup(ctx, week="2026-W40")
    data = yaml.safe_load(ctx.repo.read("config/proposals.yml"))
    splits = [p for p in data["proposals"] if p["type"] == "split"]
    assert [p["target"] for p in splits] == ["B-001"]
    assert splits[0]["target_version"] == "r-B-001"
    assert [s["statement"] for s in splits[0]["statements"]] == ["Holds for carriers", "Fails for TPAs"]
    assert len(splits[0]["sources"]) == 3


def test_split_when_contradictions_come_from_one_seat(ctx, ix):
    b1 = _entry("B-001", "Dashcam footage settles liability disputes", "beliefs")
    digests = [_digest(f"2026-09-1{i}_guest-{i}", "broker / agent", f"2026-09-1{i}", [_claim(1, f"quote {i} x y z")],
                       [("B-001", "contradicts", ["c01"], "agree")], _checked(b1)) for i in (1, 2)]
    _write(ctx, digests, [b1])
    halves = [{"statement": "A", "scope": {}}, {"statement": "B", "scope": {}}]
    _model(ctx, _reply([_prop("split", target="B-001", statements=halves)]))
    out = run_rollup(ctx, week="2026-W40")
    assert len(out["proposals"]) == 1


def test_split_needs_two_statements(ctx, ix):
    b1 = _entry("B-001", "Dashcam footage settles liability disputes", "beliefs")
    digests = [_digest(f"2026-09-1{i}_guest-{i}", "broker / agent", f"2026-09-1{i}", [_claim(1, f"quote {i} x y z")],
                       [("B-001", "scope_mismatch", ["c01"], "agree")], _checked(b1)) for i in (1, 2, 3)]
    _write(ctx, digests, [b1])
    _model(ctx, _reply([_prop("split", target="B-001", statements=[{"statement": "Only one", "scope": {}}])]))
    assert run_rollup(ctx, week="2026-W40")["proposals"] == []


def _contradicted(b: Entry, seats: list[str], *, decided_by="agree", dates=None) -> list[Digest]:
    dates = dates or [f"2026-09-{10 + i:02d}" for i in range(1, len(seats) + 1)]
    return [_digest(f"{dates[i - 1]}_guest-{i}", seat, dates[i - 1], [_claim(1, f"contradicting quote {i} a b c")],
                    [(b.id, "contradicts", ["c01"], decided_by)], _checked(b))
            for i, seat in enumerate(seats, 1)]


def test_retire_proposed_by_code_at_three_calls_in_three_seats(ctx, ix):
    b1 = _entry("B-001", "Carriers pay for footage review tools", "beliefs")
    _write(ctx, _contradicted(b1, ["claims adjuster (staff)", "underwriter", "pricing actuary"]), [b1])
    model = _model(ctx, _reply())          # the model proposes nothing; code still proposes the retire
    out = run_rollup(ctx, week="2026-W40")
    data = yaml.safe_load(ctx.repo.read("config/proposals.yml"))
    assert [(p["type"], p["target"]) for p in data["proposals"]] == [("retire", "B-001")]
    assert len(data["proposals"][0]["sources"]) == 3
    assert out["pressure"] == ["B-001"]
    assert len(model.calls) == 1


def test_retire_not_proposed_with_two_seats(ctx, ix):
    b1 = _entry("B-001", "Carriers pay for footage review tools", "beliefs")
    _write(ctx, _contradicted(b1, ["claims adjuster (staff)", "claims adjuster (staff)", "pricing actuary"]), [b1])
    _model(ctx, _reply([_prop("retire", target="B-001")]))
    out = run_rollup(ctx, week="2026-W40")
    assert out["proposals"] == []
    assert out["pressure"] == ["B-001"]    # still under pressure: 3 calls in 2 seats


def test_retire_ignores_unarbitrated_old_version_and_pre_keep_contradictions(ctx, ix):
    keep = HistoryLine(date="2026-09-12", action="keep", who="Sam", via="slack", ref="r-keep")
    b1 = _entry("B-001", "Carriers pay for footage review tools", "beliefs", extra=[keep])
    seats = ["claims adjuster (staff)", "underwriter", "pricing actuary"]
    before_keep = _contradicted(b1, seats, dates=["2026-09-10", "2026-09-11", "2026-09-12"])
    unarbitrated = _contradicted(b1, seats, decided_by="unarbitrated", dates=["2026-09-20", "2026-09-21", "2026-09-22"])
    old_version = _contradicted(b1, seats, dates=["2026-09-23", "2026-09-24", "2026-09-25"])
    for d in old_version:
        d.checked = {"B-001": "an-older-ref"}
    _write(ctx, before_keep + unarbitrated + old_version, [b1])
    _model(ctx, _reply())
    assert run_rollup(ctx, week="2026-W40")["proposals"] == []


def test_drop_watching_item_with_no_hit_in_six_weeks(ctx, ix):
    stale = _entry("W-001", "Carrier preferences show up in claim guidelines", "watching", date="2026-08-01")
    recent = _entry("W-002", "TPAs want the raw footage", "watching", date="2026-09-25")
    busy = _entry("W-003", "Insureds want monthly claim reports", "watching", date="2026-08-01")
    hit = _digest("2026-09-28_guest-1", "claims adjuster (staff)", "2026-09-28", [_claim(1, "we send reports monthly a b")],
                  [("W-003", "supports", ["c01"], "agree")], {"W-001": "r-W-001", "W-002": "r-W-002", "W-003": "r-W-003"})
    old_hit = _digest("2026-08-05_guest-2", "claims adjuster (staff)", "2026-08-05", [_claim(1, "claim guidelines tell you a b")],
                      [("W-001", "supports", ["c01"], "agree")], {"W-001": "r-W-001"})
    _write(ctx, [hit, old_hit], [stale, recent, busy])
    _model(ctx, _reply())
    run_rollup(ctx, week="2026-W40")
    data = yaml.safe_load(ctx.repo.read("config/proposals.yml"))
    assert [(p["type"], p["target"]) for p in data["proposals"]] == [("drop", "W-001")]
    assert data["proposals"][0]["sources"] == []


def test_promote_watching_item_that_reaches_the_add_bar(ctx, ix):
    w1 = _entry("W-001", "Carrier preferences show up in claim guidelines", "watching")
    seats = ["claims adjuster (staff)", "underwriter", "pricing actuary"]
    digests = [_digest(f"2026-09-1{i}_guest-{i}", seat, f"2026-09-1{i}", [_claim(1, f"claim guideline quote {i} a b")],
                       [("W-001", "supports", ["c01"], "agree")], _checked(w1)) for i, seat in enumerate(seats, 1)]
    _write(ctx, digests, [w1])
    _model(ctx, _reply([_prop("promote", target="W-001", statement="Carrier preferences are written into claim guidelines")]))
    run_rollup(ctx, week="2026-W40")
    data = yaml.safe_load(ctx.repo.read("config/proposals.yml"))
    [p] = data["proposals"]
    assert (p["type"], p["target"], p["target_version"]) == ("promote", "W-001", "r-W-001")
    assert p["statement"] == "Carrier preferences are written into claim guidelines"
    assert len(p["sources"]) == 3


def test_unknown_or_retired_targets_are_dropped(ctx, ix):
    gone = _entry("B-001", "Old belief", "retired")
    _write(ctx, [], [gone])
    _model(ctx, _reply([_prop("reword", target="B-001", statement="New words"),
                        _prop("retire", target="B-009"), _prop("bogus", statement="x")]))
    assert run_rollup(ctx, week="2026-W40")["proposals"] == []


def test_rejected_proposal_is_suppressed(ctx, ix):
    calls = _calls_across(["claims adjuster (staff)", "underwriter", "pricing actuary"])
    _write(ctx, calls)
    ctx.repo.write("config/rejected.yml", yaml.safe_dump({"rejected": [
        {"type": "add", "target": None, "who": "Alex", "reason": "no", "ref": "1.1:prop:x",
         "claims": [{"call_id": d.call_id, "quote": d.claims[0].quote} for d in calls[:2]]}]}))
    _model(ctx, _reply([_prop("add", statement="Footage review is manual", sources=_refs(calls))]))
    assert run_rollup(ctx, week="2026-W40")["proposals"] == []


# ---------- posting ----------

def _busy_week(ctx) -> None:
    """A belief under pressure, an add that passes, and single-call findings."""
    b1 = _entry("B-001", "Carriers pay for footage review tools", "beliefs")
    contra = _contradicted(b1, ["claims adjuster (staff)", "underwriter"], dates=["2026-09-28", "2026-09-29"])
    adds = _calls_across(["claims adjuster (staff)", "pricing actuary", "broker / agent"], start_day=20)
    single = _digest("2026-09-30_guest-9", "reinsurance", "2026-09-30",
                     [_claim(1, "we only take claims with footage from day one", firsthand=True, prompted=False, found_by=2),
                      _claim(2, "the weather was nice", prompted=True)], checked=_checked(b1))
    _write(ctx, contra + adds + [single], [b1])
    _model(ctx, _reply([_prop("add", statement="Footage review is manual", sources=_refs(adds))]))


def test_posts_summary_then_one_reply_per_item(ctx, ix):
    _busy_week(ctx)
    out = run_rollup(ctx, week="2026-W40")
    top, replies = _thread(ctx, "rollup:2026-W40")
    assert top["metadata"] == {"event_type": "discovery_rollup", "event_payload": {"key": "rollup:2026-W40"}}
    assert top["thread_ts"] is None
    assert "Roll-up 2026-W40" in top["text"]
    assert "B-001 went under pressure" in top["text"]
    keys = out["items"]
    assert keys[0] == "pressure:B-001"
    assert "prop:2026-W40-01" in keys
    assert any(k.startswith("finding:2026-09-30_guest-9:") for k in keys)
    assert len(replies) == len(keys) == out["posted"]
    pressure = replies[0]
    assert "pressure:B-001" in _block_ids(pressure)
    actions = next(b for b in pressure["blocks"] if b["type"] == "actions")
    assert [e["action_id"] for e in actions["elements"]] == ["pressure_keep", "pressure_reword", "pressure_split",
                                                             "pressure_retire"]
    assert all(e["value"] == "B-001" for e in actions["elements"])
    prop = next(r for r in replies if "prop:2026-W40-01" in _block_ids(r))
    actions = next(b for b in prop["blocks"] if b["type"] == "actions")
    assert [e["action_id"] for e in actions["elements"]] == ["prop_approve", "prop_edit", "prop_reject"]
    assert all(e["value"] == "2026-W40-01" for e in actions["elements"])
    findings = [r for r in replies if any(b.startswith("item:finding:") for b in _block_ids(r))]
    assert len(findings) == 2                  # c01 (firsthand, unprompted) first, then c02 (scarce seat)
    menu = next(b for b in findings[0]["blocks"] if b.get("accessory"))
    assert menu["block_id"] == "finding:100-140"
    assert [o["value"] for o in menu["accessory"]["options"]] == ["mb|2026-09-30_guest-9|100-140",
                                                                  "w|2026-09-30_guest-9|100-140"]
    assert EM_DASH not in _all_text(ctx)


def test_commits_files_before_posting(ctx, ix):
    _busy_week(ctx)
    order = []
    commit = ctx.remote.commit_and_push
    post = ctx.slack.post
    ctx.remote.commit_and_push = lambda *a, **k: (order.append("commit"), commit(*a, **k))[1]
    ctx.slack.post = lambda *a, **k: (order.append("post"), post(*a, **k))[1]
    out = run_rollup(ctx, week="2026-W40")
    assert order[0] == "commit" and order.count("commit") == 1
    [c] = ctx.remote.commits
    assert {"config/themes.yml", "config/proposals.yml", "findings.md"} <= set(c["paths"])
    assert out["commit"] == "local1"
    assert ix.regenerated


def test_retry_posts_only_missing_items(ctx, ix):
    _busy_week(ctx)
    first = run_rollup(ctx, week="2026-W40")
    model = ctx.llms(ctx.config.pipeline.rollup)
    _, replies = _thread(ctx, "rollup:2026-W40")
    ctx.slack.delete(ctx.channel("calls"), replies[1]["ts"])          # as if that post never happened
    before = len(ctx.slack.messages)

    second = run_rollup(ctx, week="2026-W40")
    assert second["retry"] is True
    assert len(model.calls) == 1                                     # no second model call
    assert len(ctx.remote.commits) == 1                              # nothing new to commit
    assert second["posted"] == 1 and second["already_posted"] == first["posted"] - 1
    assert len(ctx.slack.messages) == before + 1
    _, replies = _thread(ctx, "rollup:2026-W40")
    assert len(replies) == first["posted"]

    third = run_rollup(ctx, week="2026-W40")
    assert third["posted"] == 0 and third["skipped"] is True


def test_last_weeks_buttons_removed_and_open_proposals_roll_forward(ctx, ix):
    calls = _calls_across(["claims adjuster (staff)", "underwriter", "pricing actuary"])
    _write(ctx, calls)
    _model(ctx, _reply([_prop("add", statement="Footage review is manual", sources=_refs(calls))]))
    run_rollup(ctx, week="2026-W39")
    _, old = _thread(ctx, "rollup:2026-W39")
    assert [_has_buttons(r) for r in old] == [True]

    ctx.now = lambda: datetime(2026, 10, 5, 8, 0, tzinfo=PT)
    ctx.llms.register(FakeLLM(ctx.config.pipeline.rollup, lambda s, p, schema: _reply()))
    out = run_rollup(ctx, week="2026-W40")
    assert out["cleared"] == 1
    _, old = _thread(ctx, "rollup:2026-W39")
    assert [_has_buttons(r) for r in old] == [False]
    assert "Rolled into the 2026-W40 roll-up" in repr(old[0]["blocks"])
    # The unanswered proposal keeps its id and is posted again with live buttons.
    assert out["proposals"] == ["2026-W39-01"]
    _, new = _thread(ctx, "rollup:2026-W40")
    assert "prop:2026-W39-01" in _block_ids(new[0]) and _has_buttons(new[0])
    data = yaml.safe_load(ctx.repo.read("config/proposals.yml"))
    assert data["rollup"] == "rollup:2026-W40"
    assert [(p["id"], p["rollup"]) for p in data["proposals"]] == [("2026-W39-01", "rollup:2026-W40")]


def test_new_calls_are_counted_since_the_last_rollup(ctx, ix):
    calls = _calls_across(["claims adjuster (staff)", "underwriter"])
    _write(ctx, calls)
    _model(ctx, _reply())
    first = run_rollup(ctx, week="2026-W39")
    assert first["calls"] == 2
    later = _digest("2026-10-03_guest-7", "claims adjuster (staff)", "2026-10-03", [_claim(1, "a later call quote here")])
    write_digest(ctx.repo, later)
    ctx.now = lambda: datetime(2026, 10, 5, 8, 0, tzinfo=PT)
    second = run_rollup(ctx, week="2026-W40")
    assert second["calls"] == 1
    assert "This week: 1 call" in _thread(ctx, "rollup:2026-W40")[0]["text"]


def test_a_call_digested_after_the_rollup_but_dated_before_it_is_new(ctx, ix):
    """A late Circleback tag, a Re-run or a late backfill drop still counts once, in the next roll-up."""
    calls = _calls_across(["claims adjuster (staff)", "underwriter"])
    _write(ctx, calls)
    _model(ctx, _reply())
    assert run_rollup(ctx, bootstrap=True)["calls"] == 2
    late = _digest("2026-09-25_guest-late", "pricing actuary", "2026-09-25", [_claim(1, "a late tagged call quote")])
    write_digest(ctx.repo, late)
    ctx.now = lambda: datetime(2026, 10, 5, 8, 0, tzinfo=PT)
    out = run_rollup(ctx, week="2026-W41")
    assert out["calls"] == 1
    assert "This week: 1 call" in _thread(ctx, "rollup:2026-W41")[0]["text"]
    state = yaml.safe_load(ctx.repo.read("config/themes.yml"))["rollup"]
    assert state["calls"] == [late.call_id]
    assert state["counted"] == sorted([d.call_id for d in calls] + [late.call_id])
    # counted once: the next week does not count it again
    ctx.now = lambda: datetime(2026, 10, 12, 8, 0, tzinfo=PT)
    assert run_rollup(ctx, week="2026-W42")["calls"] == 0


def test_a_rollup_recorded_without_counted_falls_back_to_its_date(ctx, ix):
    calls = _calls_across(["claims adjuster (staff)", "underwriter"])       # dated 2026-09-11 and 2026-09-12
    _write(ctx, calls)
    ctx.repo.write("config/themes.yml", yaml.safe_dump({"rollup": {
        "key": "rollup:2026-W37", "date": "2026-09-12", "previous": None, "calls": [calls[0].call_id]}}))
    _model(ctx, _reply())
    assert run_rollup(ctx, week="2026-W40")["calls"] == 1
    # from then on `counted` holds every call, so the older ones are not counted again
    state = yaml.safe_load(ctx.repo.read("config/themes.yml"))["rollup"]
    assert state["counted"] == [d.call_id for d in calls]
    ctx.now = lambda: datetime(2026, 10, 12, 8, 0, tzinfo=PT)
    assert run_rollup(ctx, week="2026-W41")["calls"] == 0


def test_findings_left_by_a_wrong_read_reach_the_rollup(ctx, ix):
    d = _digest("2026-09-11_guest-1", "claims adjuster (staff)", "2026-09-11", [_claim(1, "we scrubbed store footage by hand")],
                checks=[("B-001", "contradicts", ["c01"], "agree")], checked={"B-001": "r-B-001"})
    _write(ctx, [d], [_entry("B-001", "Footage is reviewed in software", "beliefs")])
    assert rollup._load_world(ctx, datetime(2026, 10, 1).date()).findings == []
    ctx.repo.write("config/verdict_overrides.yml", yaml.safe_dump({"overrides": [
        {"call_id": d.call_id, "target": "B-001", "quote": "we scrubbed store footage by hand"}]}))
    world = rollup._load_world(ctx, datetime(2026, 10, 1).date())
    assert [(x.call_id, c.id) for x, c in world.findings] == [(d.call_id, "c01")]


def test_dry_run_writes_files_but_skips_git_and_slack(ctx, ix):
    _busy_week(ctx)
    ctx.dry_run = True
    out = run_rollup(ctx, week="2026-W40")
    assert ctx.remote.commits == [] and ctx.slack.messages == []
    assert ctx.repo.exists("config/proposals.yml")
    assert "Roll-up 2026-W40" in out["text"] and "PROPOSAL 2026-W40-01" in out["text"]
    assert EM_DASH not in out["text"]


def test_model_failure_raises_before_anything_is_written(ctx, ix):
    _busy_week(ctx)
    ctx.llms.register(FakeLLM(ctx.config.pipeline.rollup, lambda s, p, schema: LLMError("down")))
    with pytest.raises(LLMError):
        run_rollup(ctx, week="2026-W40")
    assert ctx.remote.commits == [] and ctx.slack.messages == []
    assert "rollup:2026-W40" not in ctx.repo.read("config/themes.yml")


def test_prompt_lists_findings_by_ref_and_the_beliefs(ctx, ix):
    _busy_week(ctx)
    model = ctx.llms(ctx.config.pipeline.rollup)
    run_rollup(ctx, week="2026-W40")
    call = model.calls[0]
    assert "[2026-09-30_guest-9#c01] NEW reinsurance" in call["prompt"]
    assert "B-001 (belief) Carriers pay for footage review tools" in call["prompt"]
    assert call["schema"]["properties"]["proposals"]
    assert "{add_calls}" not in call["prompt"] and "3 or more calls" in call["prompt"]


# ---------- bootstrap ----------

def test_bootstrap_caps_at_forty_and_downgrades_thin_adds(ctx, ix):
    seats = ["claims adjuster (staff)", "underwriter", "pricing actuary", "broker / agent"]
    digests = [_digest(f"2026-08-{i:02d}_guest-{i}", seats[i % 4], f"2026-08-{i:02d}",
                       [_claim(n, f"alpha{n} bravo{n} charlie{n} delta{n} echo{n} call{i}") for n in range(1, 51)])
               for i in range(1, 4)]
    _write(ctx, digests)
    proposals = [_prop("add", statement=f"Point {n} holds", sources=[f"{d.call_id}#c{n:02d}" for d in digests])
                 for n in range(1, 46)]
    proposals.append(_prop("add", statement="Thin point", sources=[f"{digests[0].call_id}#c50"]))
    proposals.append(_prop("retire", target="B-001"))             # not allowed in the bootstrap
    _model(ctx, _reply(proposals))
    out = run_rollup(ctx, bootstrap=True)
    assert out["key"] == "rollup:bootstrap"
    assert len(out["proposals"]) == 40
    data = yaml.safe_load(ctx.repo.read("config/proposals.yml"))
    assert all(p["id"].startswith("boot-") for p in data["proposals"])
    assert {p["type"] for p in data["proposals"]} == {"add"}
    assert out["pressure"] == [] and not any(k.startswith("finding:") for k in out["items"])
    top, replies = _thread(ctx, "rollup:bootstrap")
    assert len(replies) == 40
    assert "Bootstrap roll-up" in top["text"]


def test_bootstrap_turns_a_thin_add_into_a_watch(ctx, ix):
    d = _digest("2026-08-01_guest-1", "claims adjuster (staff)", "2026-08-01", [_claim(1, "a single call point with words")])
    _write(ctx, [d])
    _model(ctx, _reply([_prop("add", statement="Thin point", sources=[f"{d.call_id}#c01"])]))
    out = run_rollup(ctx, bootstrap=True)
    data = yaml.safe_load(ctx.repo.read("config/proposals.yml"))
    assert [(p["type"], p["statement"]) for p in data["proposals"]] == [("watch", "Thin point")]
    assert out["proposals"] == ["boot-01"]


def test_beliefs_over_the_cap_are_flagged(ctx, ix):
    entries = [_entry(f"B-{n:03d}", f"Belief number {n}", "beliefs") for n in range(1, 42)]
    _write(ctx, [], entries)
    _model(ctx, _reply())
    run_rollup(ctx, week="2026-W40")
    top, _ = _thread(ctx, "rollup:2026-W40")
    assert "41 beliefs, over the cap of 40" in top["text"]


def test_week_key_defaults_to_the_iso_week(ctx, ix):
    _write(ctx, [])
    _model(ctx, _reply())
    assert run_rollup(ctx)["key"] == "rollup:2026-W40"          # Ctx.for_tests: 2026-10-01
    with pytest.raises(ValueError):
        run_rollup(ctx, week="last week")


# ---------- merge, reword, file details ----------

def test_merge_needs_mostly_shared_support_and_records_every_version(ctx, ix):
    b1 = _entry("B-001", "Small insureds do not keep footage", "beliefs")
    b2 = _entry("B-002", "Small insureds overwrite footage weekly", "beliefs")
    b3 = _entry("B-003", "Payouts come after a police report", "beliefs")
    checked = {**_checked(b1), **_checked(b2), **_checked(b3)}
    digests = []
    for i, seat in enumerate(["claims adjuster (staff)", "underwriter"], 1):
        digests.append(_digest(f"2026-09-1{i}_guest-{i}", seat, f"2026-09-1{i}",
                               [_claim(1, f"footage gets overwritten here {i}"), _claim(2, f"police report came later {i}")],
                               [("B-001", "supports", ["c01"], "agree"), ("B-002", "supports", ["c01"], "agree"),
                                ("B-003", "supports", ["c02"], "agree")], checked))
    _write(ctx, digests, [b1, b2, b3])
    _model(ctx, _reply([_prop("merge", targets=["B-001", "B-002"], statement="Small insureds overwrite footage weekly"),
                        _prop("merge", targets=["B-001", "B-003"], statement="Unrelated pair")]))
    run_rollup(ctx, week="2026-W40")
    data = yaml.safe_load(ctx.repo.read("config/proposals.yml"))
    [p] = data["proposals"]
    assert (p["type"], p["targets"], p["target"]) == ("merge", ["B-001", "B-002"], None)
    assert p["target_versions"] == {"B-001": "r-B-001", "B-002": "r-B-002"}
    assert len(p["sources"]) == 2


def test_reword_needs_a_new_statement_and_a_cited_claim(ctx, ix):
    b1 = _entry("B-001", "Small insureds do not keep footage", "beliefs")
    d = _digest("2026-09-11_guest-1", "claims adjuster (staff)", "2026-09-11", [_claim(1, "small stores overwrite footage weekly")],
                checked=_checked(b1))
    _write(ctx, [d], [b1])
    _model(ctx, _reply([_prop("reword", target="B-001", statement="Small insureds do not keep footage"),
                        _prop("reword", target="B-001", statement="Small stores overwrite footage weekly"),
                        _prop("reword", target="B-001", statement="Small stores overwrite footage weekly",
                              sources=[f"{d.call_id}#c01"])]))
    out = run_rollup(ctx, week="2026-W40")
    data = yaml.safe_load(ctx.repo.read("config/proposals.yml"))
    assert [(p["type"], p["statement"]) for p in data["proposals"]] == [
        ("reword", "Small stores overwrite footage weekly")]
    assert data["proposals"][0]["target_version"] == "r-B-001"
    top, replies = _thread(ctx, "rollup:2026-W40")
    assert "Now: Small insureds do not keep footage" in replies[0]["text"]
    assert out["proposals"] == ["2026-W40-01"]


def test_bootstrap_records_are_flagged_and_themes_record_the_date(ctx, ix):
    calls = _calls_across(["claims adjuster (staff)", "underwriter", "pricing actuary"])
    _write(ctx, calls)
    _model(ctx, _reply([_prop("add", statement="Footage review is manual", sources=_refs(calls))]))
    run_rollup(ctx, bootstrap=True)
    data = yaml.safe_load(ctx.repo.read("config/proposals.yml"))
    assert data["proposals"][0]["bootstrap"] is True
    themes = yaml.safe_load(ctx.repo.read("config/themes.yml"))
    assert themes["updated"] == "2026-10-01"
    assert themes["rollup"] == {"key": "rollup:bootstrap", "date": "2026-10-01", "previous": None,
                                "calls": [d.call_id for d in calls], "counted": [d.call_id for d in calls],
                                "findings": []}


def test_themes_keep_previous_members_and_follow_renames(ctx, ix):
    calls = _calls_across(["claims adjuster (staff)", "underwriter", "pricing actuary"])
    _write(ctx, calls)
    first = {"name": "Footage by hand", "renamed_from": None, "claims": _refs(calls[:2])}
    _model(ctx, _reply(themes=[first]))
    run_rollup(ctx, week="2026-W39")
    # Next week the model renames the theme and only lists the new member.
    ctx.now = lambda: datetime(2026, 10, 5, 8, 0, tzinfo=PT)
    renamed = {"name": "Manual footage review", "renamed_from": "Footage by hand", "claims": _refs(calls[2:])}
    ctx.llms.register(FakeLLM(ctx.config.pipeline.rollup, lambda s, p, schema: _reply(themes=[renamed])))
    run_rollup(ctx, week="2026-W40")
    themes = yaml.safe_load(ctx.repo.read("config/themes.yml"))["themes"]
    assert [t["name"] for t in themes] == ["Manual footage review"]
    assert [c["call_id"] for c in themes[0]["claims"]] == [d.call_id for d in calls]


def test_runs_against_the_real_indexes(ctx):
    pytest.importorskip("discovery.indexes")
    b1 = _entry("B-001", "Carriers pay for footage review tools", "beliefs")
    contra = _contradicted(b1, ["claims adjuster (staff)", "underwriter"], dates=["2026-09-28", "2026-09-29"])
    adds = _calls_across(["claims adjuster (staff)", "pricing actuary", "broker / agent"], start_day=20)
    _write(ctx, contra + adds, [b1])
    theme = {"name": "Manual footage review", "renamed_from": None, "claims": _refs(adds)}
    _model(ctx, _reply([_prop("add", statement="Footage review is manual", sources=_refs(adds))], [theme]))
    out = run_rollup(ctx, week="2026-W40")
    assert out["pressure"] == ["B-001"]
    assert out["proposals"] == ["2026-W40-01"]
    assert set(ctx.remote.commits[0]["paths"]) >= {"config/themes.yml", "config/proposals.yml", "findings.md",
                                                   "beliefs-status.md"}
    findings = ctx.repo.read("findings.md")
    assert "Manual footage review" in findings
    assert EM_DASH not in findings + ctx.repo.read("config/proposals.yml") + ctx.repo.read("config/themes.yml")


def test_carried_proposal_goes_stale_when_its_target_changed(ctx, ix):
    b1 = _entry("B-001", "Carriers pay for footage review tools", "beliefs")
    digests = _contradicted(b1, ["claims adjuster (staff)", "underwriter", "pricing actuary"])
    _write(ctx, digests, [b1])
    _model(ctx, _reply())
    assert run_rollup(ctx, week="2026-W39")["proposals"] == ["2026-W39-01"]

    # B-001 is reworded and backchecked: the retire still holds, but last week's proposal is stale.
    reword = HistoryLine(date="2026-10-02", action="reword", who="Sam", via="slack", ref="r-reword")
    b1.history.append(reword)
    for d in digests:
        d.checked = {"B-001": "r-reword"}
    _write(ctx, digests, [b1])
    ctx.now = lambda: datetime(2026, 10, 5, 8, 0, tzinfo=PT)
    out = run_rollup(ctx, week="2026-W40")
    assert out["proposals"] == ["2026-W40-01"]
    data = yaml.safe_load(ctx.repo.read("config/proposals.yml"))
    assert [(p["id"], p["status"], p["target_version"]) for p in data["proposals"]] == [
        ("2026-W40-01", "open", "r-reword"), ("2026-W39-01", "expired", "r-B-001")]


def test_malformed_model_output_is_ignored(ctx, ix):
    calls = _calls_across(["claims adjuster (staff)", "underwriter", "pricing actuary"])
    _write(ctx, calls)
    _model(ctx, {"themes": ["loose", {"name": ""}, {"name": "Real", "renamed_from": None,
                                                    "claims": ["nope#c99", 5, f"{calls[0].call_id}#c01"]}],
                 "proposals": ["x", {"type": "add"}, {"type": "split", "target": "B-042", "statements": ["x"]}]})
    out = run_rollup(ctx, week="2026-W40")
    assert out["proposals"] == []
    themes = yaml.safe_load(ctx.repo.read("config/themes.yml"))["themes"]
    assert themes == [{"name": "Real", "claims": [{"call_id": calls[0].call_id, "quote": calls[0].claims[0].quote}]}]


def test_previous_week_is_found_by_key_when_no_rollup_is_recorded(ctx, ix):
    ch = ctx.channel("calls")
    ts = ctx.slack.post(ch, [], "old", metadata={"event_type": "discovery_rollup", "event_payload": {"key": "rollup:2026-W39"}})
    ctx.slack.post(ch, [{"type": "section", "block_id": "item:prop:x", "text": {"type": "mrkdwn", "text": "x"}},
                        {"type": "actions", "block_id": "prop:x", "elements": []}], "x", thread_ts=ts)
    _write(ctx, [])
    _model(ctx, _reply())
    assert run_rollup(ctx, week="2026-W40")["cleared"] == 1
    assert not _has_buttons(ctx.slack.replies(ch, ts)[0])


# ---------- a rejection covers related proposals only ----------

def test_a_rejected_watch_does_not_suppress_an_unrelated_single_source_watch():
    rejected = [{"type": "watch", "target": None, "statement": "Footage is scrubbed by hand",
                 "claims": [{"call_id": "2026-08-01_a-b", "quote": "now I have 45 claims across these stores"}]}]
    unrelated = Proposal(id="x-02", type="watch", statement="Carriers will not pay claims without a police report",
                         scope=Scope(), sources=[("2026-08-15_c-d", "we would never pay claims without a police report")])
    assert not rejection_suppressed(unrelated, rejected)
    same_evidence = Proposal(id="x-03", type="watch", statement="Footage review is manual", scope=Scope(),
                             sources=[("2026-08-01_a-b", "now I have 45 claims across these stores")])
    assert rejection_suppressed(same_evidence, rejected)
    same_statement = Proposal(id="x-04", type="watch", statement="Footage is scrubbed by hand.", scope=Scope(),
                              sources=[("2026-08-20_e-f", "we keep the footage in a shared drive")])
    assert rejection_suppressed(same_statement, rejected)


def test_a_rejected_merge_covers_only_its_target_set():
    rejected = [{"type": "merge", "target": None, "targets": ["B-001", "B-002"],
                 "claims": [{"call_id": "a", "quote": "footage gets overwritten here"}]}]
    same = Proposal(id="x-05", type="merge", statement="S", scope=Scope(), targets=["B-002", "B-001"],
                    sources=[("a", "footage gets overwritten here")])
    other = Proposal(id="x-06", type="merge", statement="S", scope=Scope(), targets=["B-001", "B-003"],
                     sources=[("a", "footage gets overwritten here")])
    assert rejection_suppressed(same, rejected)
    assert not rejection_suppressed(other, rejected)


def test_rejecting_one_bootstrap_watch_leaves_the_others_open(ctx, ix):
    from discovery import apply
    one = _digest("2026-08-01_guest-1", "claims adjuster (staff)", "2026-08-01", [_claim(1, "footage is scrubbed by hand here")])
    two = _digest("2026-08-02_guest-2", "pricing actuary", "2026-08-02", [_claim(1, "carriers never pay for our footage")])
    _write(ctx, [one, two])
    _model(ctx, _reply([_prop("add", statement="Footage review is manual", sources=[f"{one.call_id}#c01"]),
                        _prop("add", statement="Carriers do not pay for footage", sources=[f"{two.call_id}#c01"])]))
    assert run_rollup(ctx, bootstrap=True)["proposals"] == ["boot-01", "boot-02"]
    top, replies = _thread(ctx, "rollup:bootstrap")
    first = next(r for r in replies if "prop:boot-01" in _block_ids(r))
    item = {"channel": ctx.channel("calls"), "ts": first["ts"], "block_id": "prop:boot-01", "thread_ts": top["ts"]}
    out = apply.apply_change(ctx, apply.ChangeRequest(action="reject", requested_by="alex", proposal_id="boot-01"),
                             ref=f"{first['ts']}:prop:boot-01", via="slack", slack_item=item)
    assert out.outcome == "applied"

    ctx.now = lambda: datetime(2026, 10, 5, 8, 0, tzinfo=PT)
    ctx.llms.register(FakeLLM(ctx.config.pipeline.rollup, lambda s, p, schema: _reply()))
    out = run_rollup(ctx, week="2026-W41")
    assert out["proposals"] == ["boot-02"]
    _, old = _thread(ctx, "rollup:bootstrap")
    second = next(r for r in old if "item:prop:boot-02" in _block_ids(r))
    assert "Rolled into the 2026-W41 roll-up" in repr(second["blocks"])


# ---------- sources are matched by span, not by a shared phrase ----------

def test_a_claim_sharing_a_short_phrase_with_a_held_source_is_fresh(ctx, ix):
    calls = _calls_across(["claims adjuster (staff)", "underwriter", "pricing actuary"])
    first = calls[0]
    first.claims = [_claim(1, "I don't think so."),
                    _claim(2, "I don't think so, a carrier would never pay for the footage and we would never ask")]
    held = HistoryLine(date="2026-08-01", action="add", who="Alex", via="slack", ref="r-B-001",
                       sources=[(first.call_id, "I don't think so.")])
    b1 = Entry(id="B-001", statement="Payouts need footage", scope=Scope(), section="beliefs", history=[held])
    _write(ctx, calls, [b1])
    refs = [f"{first.call_id}#c02"] + _refs(calls[1:])
    _model(ctx, _reply([_prop("add", statement="Carriers do not pay for store footage", sources=refs)]))
    out = run_rollup(ctx, week="2026-W40")
    assert out["proposals"] == ["2026-W40-01"]
    [p] = yaml.safe_load(ctx.repo.read("config/proposals.yml"))["proposals"]
    assert p["sources"][0] == {"call_id": first.call_id, "quote": first.claims[1].quote}


def test_identical_short_answers_are_fresh_or_held_as_apply_places_them(ctx, ix):
    """Two "Exactly." answers; W-001 watched the second from its finding menu. The first is still a
    single-call finding worth a look, the second is held."""
    call = _digest("2026-09-30_guest-9", "claims adjuster (staff)", "2026-09-30",
                   [_claim(1, "Exactly.", firsthand=True, prompted=False),
                    _claim(2, "Exactly.", firsthand=True, prompted=False)])
    second = f"{call.claims[1].offset[0]}-{call.claims[1].offset[1]}"
    watched = HistoryLine(date="2026-09-30", action="watch", who="Sam", via="slack", ref=f"1.1:finding:{second}",
                          sources=[(call.call_id, "Exactly.")])
    w1 = Entry(id="W-001", statement="Guests agree when asked", scope=Scope(), section="watching", history=[watched])
    _write(ctx, [call], [w1])
    _model(ctx, _reply())
    out = run_rollup(ctx, week="2026-W40")
    assert [k for k in out["items"] if k.startswith("finding:")] == [f"finding:{call.call_id}:100-140"]
    world = rollup._load_world(ctx, datetime(2026, 10, 1).date())
    assert rollup._fresh(world, [(call.call_id, "Exactly.")] * 2, [(100, 140), (200, 240)]) == [
        (call.call_id, "Exactly.")]


# ---------- unanswered single-call findings roll forward ----------

def test_unanswered_single_call_findings_roll_into_the_next_rollup(ctx, ix):
    _busy_week(ctx)
    first = run_rollup(ctx, week="2026-W40")
    singles = [k for k in first["items"] if k.startswith("finding:")]
    assert singles == ["finding:2026-09-30_guest-9:100-140", "finding:2026-09-30_guest-9:200-240"]
    state = yaml.safe_load(ctx.repo.read("config/themes.yml"))["rollup"]
    assert state["findings"] == singles

    ctx.now = lambda: datetime(2026, 10, 5, 8, 0, tzinfo=PT)
    ctx.llms.register(FakeLLM(ctx.config.pipeline.rollup, lambda s, p, schema: _reply()))
    out = run_rollup(ctx, week="2026-W41")
    assert out["calls"] == 0
    assert [k for k in out["items"] if k.startswith("finding:")] == singles
    _, old = _thread(ctx, "rollup:2026-W40")
    for reply in old:
        if any(b.startswith("item:finding:") for b in _block_ids(reply)):
            assert "Rolled into the 2026-W41 roll-up" in repr(reply["blocks"]) and not _has_buttons(reply)
    _, new = _thread(ctx, "rollup:2026-W41")
    assert sum(1 for r in new if any(b.startswith("item:finding:") for b in _block_ids(r))) == 2
    # a retry posts the same items, read from the record
    again = run_rollup(ctx, week="2026-W41")
    assert again["retry"] is True and again["items"] == out["items"] and again["posted"] == 0


def _queued_approve(ctx, week: str, pid: str):
    """An Approve clicked on last week's item just before the roll-up ran, applied after it."""
    from discovery import apply
    top, replies = _thread(ctx, f"rollup:{week}")
    item = next(r for r in replies if f"item:prop:{pid}" in _block_ids(r))
    slack_item = {"channel": ctx.channel("calls"), "ts": item["ts"], "block_id": f"prop:{pid}",
                  "thread_ts": top["ts"]}
    return apply.apply_change(ctx, apply.ChangeRequest(action="approve", requested_by="alex", proposal_id=pid),
                              ref=f"{item['ts']}:prop:{pid}", via="slack", slack_item=slack_item)


def _assert_rolled_forward(ctx, out: dict, pid: str) -> None:
    assert out["proposals"] == [pid]
    _, old = _thread(ctx, "rollup:2026-W39")
    prop = next(r for r in old if f"item:prop:{pid}" in _block_ids(r))
    assert "Rolled into the 2026-W40 roll-up" in repr(prop["blocks"]) and "Expired" not in repr(prop["blocks"])
    data = yaml.safe_load(ctx.repo.read("config/proposals.yml"))
    assert [(p["id"], p["status"], p["rollup"]) for p in data["proposals"]] == [(pid, "open", "rollup:2026-W40")]


def test_a_retire_that_still_holds_rolls_forward_under_its_id(ctx, ix):
    b1 = _entry("B-001", "Carriers pay for footage review tools", "beliefs")
    _write(ctx, _contradicted(b1, ["claims adjuster (staff)", "underwriter", "pricing actuary"]), [b1])
    _model(ctx, _reply())
    assert run_rollup(ctx, week="2026-W39")["proposals"] == ["2026-W39-01"]
    ctx.now = lambda: datetime(2026, 10, 5, 8, 0, tzinfo=PT)
    out = run_rollup(ctx, week="2026-W40")             # nobody answered; the retire still holds
    _assert_rolled_forward(ctx, out, "2026-W39-01")
    result = _queued_approve(ctx, "2026-W39", "2026-W39-01")
    assert result.outcome == "applied" and result.entry_ids == ["B-001"]
    assert parse_beliefs(ctx.repo.read("beliefs.md")).get("B-001").section == "retired"


def test_a_carried_add_the_model_proposes_again_keeps_its_id_and_content(ctx, ix):
    calls = _calls_across(["claims adjuster (staff)", "underwriter", "pricing actuary"])
    _write(ctx, calls)
    _model(ctx, _reply([_prop("add", statement="Footage review is manual", sources=_refs(calls))]))
    assert run_rollup(ctx, week="2026-W39")["proposals"] == ["2026-W39-01"]
    ctx.now = lambda: datetime(2026, 10, 5, 8, 0, tzinfo=PT)
    # the weekly prompt tells the model to re-propose what still holds, here in other words
    again = _prop("add", statement="Footage is still scrubbed by hand", sources=_refs(calls))
    ctx.llms.register(FakeLLM(ctx.config.pipeline.rollup, lambda s, p, schema: _reply([again])))
    out = run_rollup(ctx, week="2026-W40")
    _assert_rolled_forward(ctx, out, "2026-W39-01")
    [p] = yaml.safe_load(ctx.repo.read("config/proposals.yml"))["proposals"]
    assert p["statement"] == "Footage review is manual"      # what the queued click saw
    result = _queued_approve(ctx, "2026-W39", "2026-W39-01")
    assert result.outcome == "applied"
    assert parse_beliefs(ctx.repo.read("beliefs.md")).get("B-001").statement == "Footage review is manual"


def test_a_digest_date_corrected_by_hand_without_quotes_is_read_as_a_date(ctx, ix):
    """`date: 2026-09-13` (no quotes) loads as a date object, not text; every date comparison still works."""
    keep = HistoryLine(date="2026-09-12", action="keep", who="Sam", via="slack", ref="r-keep")
    b1 = _entry("B-001", "Small insureds keep no footage", "beliefs", extra=[keep])
    w1 = _entry("W-001", "Carrier preferences show up in claim guidelines", "watching", date="2026-07-01")
    checked = {"B-001": "r-B-001", "W-001": "r-W-001"}
    one = _digest("2026-09-10_a-b", "claims adjuster (staff)", "2026-09-10", [_claim(1, "we overwrite footage weekly a b")],
                  [("B-001", "supports", ["c01"], "agree")], checked)
    two = _digest("2026-09-12_c-d", "claims manager / supervisor", "2026-09-12", [_claim(1, "we never needed the footage")],
                  [("B-001", "contradicts", ["c01"], "agree"), ("W-001", "supports", ["c01"], "agree")], checked)
    _write(ctx, [one, two], [b1, w1])
    ctx.repo.write("config/themes.yml", yaml.safe_dump({"rollup": {        # recorded before `counted` existed
        "key": "rollup:2026-W37", "date": "2026-09-11", "previous": None, "calls": []}}))
    text = ctx.repo.read("digests/2026-09-12_c-d.md")
    assert "date: '2026-09-12'" in text
    ctx.repo.write("digests/2026-09-12_c-d.md", text.replace("date: '2026-09-12'", "date: 2026-09-13"))
    model = _model(ctx, _reply())
    out = run_rollup(ctx, week="2026-W40")
    assert out["calls"] == 1                                    # the corrected call, dated after the last roll-up
    assert "last hit 2026-09-13" in repr(model.calls)
    assert out["proposals"] == []                               # W-001 had a hit in the window: no drop


def test_items_that_do_not_roll_forward_say_so(ctx, ix):
    b1 = _entry("B-001", "Carriers pay for footage review tools", "beliefs")
    digests = _contradicted(b1, ["claims adjuster (staff)", "underwriter", "pricing actuary"])
    _write(ctx, digests, [b1])
    _model(ctx, _reply())
    assert run_rollup(ctx, week="2026-W39")["proposals"] == ["2026-W39-01"]
    reword = HistoryLine(date="2026-10-02", action="reword", who="Sam", via="slack", ref="r-reword")
    b1.history.append(reword)
    _write(ctx, digests, [b1])            # the checks are on the old wording: nothing to retire now
    ctx.now = lambda: datetime(2026, 10, 5, 8, 0, tzinfo=PT)
    assert run_rollup(ctx, week="2026-W40")["proposals"] == []
    _, old = _thread(ctx, "rollup:2026-W39")
    prop = next(r for r in old if "item:prop:2026-W39-01" in _block_ids(r))
    assert "No answer. Expired." in repr(prop["blocks"]) and "Rolled into" not in repr(prop["blocks"])
    assert not _has_buttons(prop)
