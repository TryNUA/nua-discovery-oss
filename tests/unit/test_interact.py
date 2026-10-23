"""Tests for nua/interact.py: Slack clicks and forms become locked items and queued change requests.

The last tests feed the queued payloads through the real nua.apply.apply_task, so the payload
contract between the receiver and the processor is checked end to end.
"""
from __future__ import annotations

import json

import pytest
import yaml

from nua import interact
from nua.beliefs import parse_beliefs
from nua.brief import render_brief
from nua.context import Ctx
from nua.digest import write_digest
from nua.tasks import task_name
from nua.types import BeliefCheck, Claim, Digest, Scope

CALL = "2026-09-23_darius-kemp"
# Verbatim spans of tests/fixtures/darius-kemp.md; LONG is the same span read from a richer copy.
PAY = "on the commercial side the carriers buy a lot less of it than they do for personal auto and homeowners."
LONG = "And on the commercial side the carriers buy a lot less of it than they do for personal auto and homeowners."
ATTORNEY = "The minute a lawyer is retained, the claim costs more, period."
TOP_TS = "1700000000.000001"

BELIEFS = """## Beliefs

### B-001 Video footage is the most valuable evidence on a liability claim
- scope: line: general liability, commercial auto; party: carrier
- history:
  - 2026-10-05 added by Alex via bootstrap, source 2026-09-23_darius-kemp "Honestly video is the single best thing you can have on a liability file.", ref 1727560000.000100:prop-07

## Watching

## Retired
"""


@pytest.fixture
def ictx(tmp_repo) -> Ctx:
    people = yaml.safe_load((tmp_repo / "config" / "people.yml").read_text(encoding="utf-8"))
    people["alex"]["slack_id"] = "U_ALEX"
    people["sam"]["slack_id"] = "U_SAM"
    (tmp_repo / "config" / "people.yml").write_text(yaml.safe_dump(people), encoding="utf-8")
    (tmp_repo / "beliefs.md").write_text(BELIEFS, encoding="utf-8")
    interact.reset_state()
    return Ctx.for_tests(tmp_repo)


def darius_digest(checks: list[BeliefCheck] | None = None) -> Digest:
    claims = [
        Claim(id="c01", text="Commercial lines get less claims software",
              quote=PAY, offset=(1000, 1085), turns=[57],
              speaker="Darius Kemp", scope=Scope(party="carrier", segment="small commercial")),
        Claim(id="c02", text="An attorney makes the claim more expensive", quote=ATTORNEY,
              offset=(2000, 2078), turns=[70], speaker="Darius Kemp", scope=Scope(line="general liability")),
    ]
    return Digest(call_id=CALL, transcript="transcripts/circleback_1.md", status="digested", origin="live",
                  claims=claims, belief_checks=checks or [], contact="Darius Kemp", firm="Example Mutual",
                  seat="claims adjuster (staff)")


def post_brief(ctx: Ctx, d: Digest) -> tuple[dict, list[dict]]:
    """Post a rendered brief to the fake Slack; returns (top message, thread replies)."""
    write_digest(ctx.repo, d)
    msgs = render_brief(d, parse_beliefs(BELIEFS), [], counts={})
    top_ts = ctx.slack.post("C123", msgs.top_blocks, msgs.top_text)
    for blocks, text in msgs.thread:
        ctx.slack.post("C123", blocks, text, thread_ts=top_ts)
    top = ctx.slack.messages[0]
    return top, [m for m in ctx.slack.messages if m["thread_ts"] == top_ts]


def findings_reply(replies: list[dict]) -> dict:
    """The thread reply that holds the new findings (found by its title block, not its position)."""
    return next(m for m in replies if any(b.get("block_id") == "findings:title" for b in m["blocks"]))


def click(message: dict, block_id: str, action: dict, *, user: str = "U_ALEX", name: str = "alex") -> dict:
    return {
        "type": "block_actions",
        "user": {"id": user, "username": name, "name": name},
        "trigger_id": "trigger-1",
        "response_url": "https://hooks.slack.com/actions/T1/1/abc",
        "container": {"type": "message", "message_ts": message["ts"], "channel_id": message["channel"],
                      "thread_ts": message["thread_ts"]},
        "channel": {"id": message["channel"]},
        "message": {"ts": message["ts"], "thread_ts": message["thread_ts"], "text": message["text"],
                    "blocks": message["blocks"]},
        "actions": [{**action, "block_id": block_id}],
    }


def menu_pick(value: str) -> dict:
    return {"type": "overflow", "action_id": "finding_menu", "selected_option": {"value": value}}


def button(action_id: str, text: str, value: str = "") -> dict:
    return {"type": "button", "action_id": action_id, "text": {"type": "plain_text", "text": text}, "value": value}


def block(blocks: list[dict], block_id: str) -> dict:
    return next(b for b in blocks if b.get("block_id") == block_id)


def submit(ctx: Ctx, view: dict, values: dict[str, str], *, user: str = "U_ALEX") -> dict | None:
    state = {bid: {"value": {"type": "plain_text_input", "value": v}} for bid, v in values.items()}
    payload = {"type": "view_submission", "user": {"id": user, "username": "alex", "name": "alex"},
               "view": {**view, "id": "V1", "state": {"values": state}}}
    return interact.handle_interaction(ctx, payload)


def proposal_message(ctx: Ctx, pid: str = "p-01") -> dict:
    """A roll-up proposal reply, in nua/rollup.py's layout."""
    blocks = [
        {"type": "section", "block_id": f"item:prop:{pid}",
         "text": {"type": "mrkdwn", "text": f"*Add belief*  `{pid}`\n*Store footage is rarely kept long "
                                            "enough*\nScope: party: insured\n> \"quote\""}},
        {"type": "actions", "block_id": f"prop:{pid}", "elements": [
            button("prop_approve", "Approve", pid), button("prop_edit", "Edit", pid),
            button("prop_reject", "Reject", pid)]},
    ]
    ctx.slack.post("C123", blocks, f"PROPOSAL {pid}", thread_ts=TOP_TS)
    return ctx.slack.messages[-1]


def pressure_message(ctx: Ctx) -> dict:
    """A roll-up under-pressure reply, in nua/rollup.py's layout."""
    blocks = [
        {"type": "section", "block_id": "item:pressure:B-001",
         "text": {"type": "mrkdwn", "text": "*Under pressure: B-001* Video footage is the most valuable "
                                            "evidence on a liability claim\nContradicted by 2 calls, 2 seats:"}},
        {"type": "actions", "block_id": "pressure:B-001", "elements": [
            button("pressure_keep", "Keep", "B-001"), button("pressure_reword", "Reword", "B-001"),
            button("pressure_split", "Split", "B-001"), button("pressure_retire", "Retire", "B-001")]},
    ]
    ctx.slack.post("C123", blocks, "UNDER PRESSURE B-001", thread_ts=TOP_TS)
    return ctx.slack.messages[-1]


def split_message(ctx: Ctx, pid: str, parts: list[tuple[str, str]]) -> dict:
    """A roll-up split proposal reply, in nua/rollup.py's layout: one "- *part* (scope)" line per part."""
    lines = [f"*Split B-001*  `{pid}`", "Now: Video footage is the most valuable evidence on a liability claim"]
    lines += [f"- *{text}* ({scope})" for text, scope in parts]
    blocks = [
        {"type": "section", "block_id": f"item:prop:{pid}", "text": {"type": "mrkdwn", "text": "\n".join(lines)}},
        {"type": "actions", "block_id": f"prop:{pid}", "elements": [
            button("prop_approve", "Approve", pid), button("prop_edit", "Edit", pid),
            button("prop_reject", "Reject", pid)]},
    ]
    ctx.slack.post("C123", blocks, f"PROPOSAL {pid}", thread_ts=TOP_TS)
    return ctx.slack.messages[-1]


def write_proposals(ctx: Ctx, *proposals: dict) -> None:
    ctx.repo.write("config/proposals.yml", yaml.safe_dump({"proposals": list(proposals)}))


# ---------- buttons and menus that enqueue at once ----------

def test_watch_locks_only_the_clicked_finding_and_enqueues_with_ref(ictx):
    _, replies = post_brief(ictx, darius_digest())
    findings = findings_reply(replies)
    assert interact.handle_interaction(ictx, click(findings, "finding:1000-1085",
                                                   menu_pick(f"w|{CALL}|1000-1085"))) is None

    ref = f"{findings['ts']}:finding:1000-1085"
    [task] = ictx.queue.tasks
    assert task["kind"] == "apply_belief_change"
    assert task["name"] == task_name("apply_belief_change", ref)
    assert task["payload"]["ref"] == ref
    assert task["payload"]["via"] == "slack"
    assert task["payload"]["change"] == {"action": "watch", "requested_by": "alex",
                                         "sources": [{"call_id": CALL, "span": "1000-1085", "quote": PAY}]}
    assert task["payload"]["slack_item"] == {"channel": "C123", "ts": findings["ts"],
                                             "block_id": "finding:1000-1085", "thread_ts": findings["thread_ts"]}

    [resp] = ictx.slack.responses
    assert resp["url"] == "https://hooks.slack.com/actions/T1/1/abc"
    assert resp["payload"]["replace_original"] is True
    new_blocks = resp["payload"]["blocks"]
    assert "accessory" not in block(new_blocks, "finding:1000-1085")
    assert block(new_blocks, "finding:1000-1085:status")["elements"][0]["text"] == "Queued by alex"
    assert block(new_blocks, "finding:2000-2078") == block(findings["blocks"], "finding:2000-2078")   # still live


def test_second_click_on_a_locked_item_gets_an_ephemeral(ictx):
    _, replies = post_brief(ictx, darius_digest())
    findings = findings_reply(replies)
    interact.handle_interaction(ictx, click(findings, "finding:1000-1085", menu_pick(f"w|{CALL}|1000-1085")))
    second = click(findings, "finding:1000-1085", menu_pick(f"w|{CALL}|1000-1085"), user="U_SAM", name="sam")
    interact.handle_interaction(ictx, second)
    assert len(ictx.queue.tasks) == 1
    assert len(ictx.slack.responses) == 1
    assert ictx.slack.ephemerals == [{"channel": "C123", "user": "U_SAM", "text": "Already queued by alex."}]


def test_duplicate_task_name_from_another_instance_also_gets_an_ephemeral(ictx):
    _, replies = post_brief(ictx, darius_digest())
    findings = findings_reply(replies)
    interact.handle_interaction(ictx, click(findings, "finding:1000-1085", menu_pick(f"w|{CALL}|1000-1085")))
    interact.reset_state()                      # a different receiver instance has no memory of the lock
    interact.handle_interaction(ictx, click(findings, "finding:1000-1085", menu_pick(f"w|{CALL}|1000-1085")))
    assert len(ictx.queue.tasks) == 1
    assert ictx.slack.ephemerals[0]["text"].startswith("Already queued")


def test_wrong_read_enqueues_a_dismissal_and_replaces_the_button_block(ictx):
    check = BeliefCheck(target="B-001", verdict="contradicts", claims=["c01"], decided_by="agree")
    top, _ = post_brief(ictx, darius_digest([check]))
    actions = next(b for b in top["blocks"] if b["type"] == "actions")
    interact.handle_interaction(ictx, click(top, actions["block_id"], actions["elements"][0]))

    [task] = ictx.queue.tasks
    assert task["payload"]["change"] == {"action": "dismiss_verdict", "target": "B-001", "requested_by": "alex",
                                         "verdict": "contradicts",
                                         "sources": [{"call_id": CALL, "span": "1000-1085", "quote": PAY}]}
    assert task["payload"]["ref"] == f"{top['ts']}:{actions['block_id']}"
    item = task["payload"]["slack_item"]
    assert item["thread_ts"] is None and item["blocks"]           # a top message travels with its blocks
    new = block(ictx.slack.responses[0]["payload"]["blocks"], actions["block_id"])
    assert new["type"] == "context" and new["elements"][0]["text"] == "Queued by alex"


@pytest.mark.parametrize("tail, verdict, version", [
    ("", "contradicts", None),                                          # verdict dropped to fit
    ("|scope_mismatch", "scope_mismatch", None),
    ("|contradicts|1790607600.000100:prop-02", "contradicts", "1790607600.000100:prop-02"),
    ("||requests/x.yml@abc", "contradicts", "requests/x.yml@abc"),       # verdict dropped, version kept
])
def test_a_wrong_read_value_may_carry_the_version_its_read_was_checked_against(ictx, tail, verdict, version):
    """`<call_id>|<target>|<span>[|<verdict>[|<version>]]`: the version travels to apply, which answers
    a click on a read of older wording as stale."""
    check = BeliefCheck(target="B-001", verdict="contradicts", claims=["c01"], decided_by="agree")
    top, _ = post_brief(ictx, darius_digest([check]))
    actions = next(b for b in top["blocks"] if b["type"] == "actions")
    action = {**actions["elements"][0], "value": f"{CALL}|B-001|1000-1085{tail}"}
    interact.handle_interaction(ictx, click(top, actions["block_id"], action))
    [task] = ictx.queue.tasks
    change = task["payload"]["change"]
    assert (change["action"], change["verdict"], change.get("version")) == ("dismiss_verdict", verdict, version)
    assert "version" in change if version else "version" not in change


@pytest.mark.parametrize("version", ["1790607600.000100:prop-02",
                                     "requests/" + "a" * 52 + ".yml@" + "0" * 40])   # fits only without the verdict
def test_the_wrong_read_button_value_round_trips_through_the_parser(ictx, version):
    """The brief's Wrong read value carries the version the read was checked against, and interact reads
    back the call, target, span, verdict and version from it."""
    check = BeliefCheck(target="B-001", verdict="contradicts", claims=["c01"], decided_by="agree")
    d = darius_digest([check])
    d.checked = {"B-001": version}
    top, _ = post_brief(ictx, d)
    actions = next(b for b in top["blocks"] if b["type"] == "actions")
    interact.handle_interaction(ictx, click(top, actions["block_id"], actions["elements"][0]))
    [task] = ictx.queue.tasks
    assert task["payload"]["change"] == {"action": "dismiss_verdict", "target": "B-001", "requested_by": "alex",
                                         "verdict": "contradicts", "version": version,
                                         "sources": [{"call_id": CALL, "span": "1000-1085", "quote": PAY}]}


def test_approve_and_reject_on_the_same_proposal_collide(ictx):
    msg = proposal_message(ictx)
    interact.handle_interaction(ictx, click(msg, "prop:p-01", button("prop_approve", "Approve", "p-01")))
    interact.handle_interaction(ictx, click(msg, "prop:p-01", button("prop_reject", "Reject", "p-01"),
                                            user="U_SAM", name="sam"))
    [task] = ictx.queue.tasks
    assert task["payload"]["change"] == {"action": "approve", "proposal_id": "p-01", "requested_by": "alex"}
    assert task["payload"]["ref"] == f"{msg['ts']}:prop:p-01"
    assert ictx.slack.ephemerals[0]["user"] == "U_SAM"


def test_keep_and_retire_on_pressure_items(ictx):
    msg = pressure_message(ictx)
    interact.handle_interaction(ictx, click(msg, "pressure:B-001", button("pressure_keep", "Keep", "B-001")))
    [task] = ictx.queue.tasks
    assert task["payload"]["change"] == {"action": "keep", "target": "B-001", "requested_by": "alex"}


def test_buttons_are_recognised_by_label_when_the_action_id_is_unknown(ictx):
    msg = pressure_message(ictx)
    interact.handle_interaction(ictx, click(msg, "pressure:B-001", button("btn-7", "Retire", "B-001")))
    assert ictx.queue.tasks[0]["payload"]["change"]["action"] == "retire"


def test_unknown_button_gets_an_ephemeral(ictx):
    msg = pressure_message(ictx)
    interact.handle_interaction(ictx, click(msg, "pressure:B-001", button("mystery", "Mystery", "x")))
    assert ictx.queue.tasks == [] and ictx.slack.ephemerals


def test_rerun_enqueues_the_original_task_forced_under_a_fresh_name(ictx):
    value = json.dumps({"kind": "process_call", "payload": {"transcript": "transcripts/a.md", "force": True}})
    ictx.slack.post("#discovery-ops", [
        {"type": "section", "block_id": "failure", "text": {"type": "mrkdwn", "text": "Task failed"}},
        {"type": "actions", "block_id": "rerun", "elements": [button("rerun", "Re-run", value)]}], "Task failed")
    msg = ictx.slack.messages[-1]
    interact.handle_interaction(ictx, click(msg, "rerun", button("rerun", "Re-run", value)))
    [task] = ictx.queue.tasks
    assert task["kind"] == "process_call"
    assert task["payload"] == {"transcript": "transcripts/a.md", "force": True}
    assert task["name"] and task["name"] != task_name("process_call", "transcripts/a.md")
    assert "Queued by alex" in json.dumps(ictx.slack.responses[0]["payload"]["blocks"])


# ---------- forms ----------

def test_make_belief_opens_a_prefilled_form_with_a_blank_scope(ictx):
    _, replies = post_brief(ictx, darius_digest())
    findings = findings_reply(replies)
    out = interact.handle_interaction(ictx, click(findings, "finding:1000-1085",
                                                  menu_pick(f"mb|{CALL}|1000-1085")))
    assert out is None
    [opened] = ictx.slack.views
    assert opened["trigger_id"] == "trigger-1"
    view = opened["view"]
    assert view["callback_id"] == interact.FORM_CALLBACK_ID
    assert block(view["blocks"], "statement")["element"]["initial_value"] == \
        "Commercial lines get less claims software"
    assert PAY in json.dumps(view["blocks"])
    scope = block(view["blocks"], "scope")
    assert scope["optional"] is True and "initial_value" not in scope["element"]
    assert "Blank keeps the finding's scope" in scope["hint"]["text"]
    assert ictx.queue.tasks == [] and ictx.slack.responses == []     # nothing locks until submit


def test_make_belief_with_a_blank_scope_takes_the_findings_scope(ictx):
    from nua import apply

    _, replies = post_brief(ictx, darius_digest())
    findings = findings_reply(replies)
    interact.handle_interaction(ictx, click(findings, "finding:1000-1085", menu_pick(f"mb|{CALL}|1000-1085")))
    view = ictx.slack.views[0]["view"]
    assert submit(ictx, view, {"statement": "Commercial lines buy less claims software", "scope": ""}) is None
    [task] = ictx.queue.tasks
    assert "scope" not in task["payload"]["change"]
    result = apply.apply_task(ictx, task["payload"])
    assert result["outcome"] == "applied", result
    [added] = [e for e in parse_beliefs(ictx.repo.read("beliefs.md")).entries if e.id != "B-001"]
    assert added.scope == Scope(party="carrier", segment="small commercial")


def test_make_belief_submission_validates_then_enqueues_and_locks(ictx):
    _, replies = post_brief(ictx, darius_digest())
    findings = findings_reply(replies)
    interact.handle_interaction(ictx, click(findings, "finding:1000-1085", menu_pick(f"mb|{CALL}|1000-1085")))
    view = ictx.slack.views[0]["view"]

    errors = submit(ictx, view, {"statement": "  ", "scope": ""})
    assert errors["response_action"] == "errors" and "statement" in errors["errors"]
    errors = submit(ictx, view, {"statement": "Commercial lines get less claims software", "scope": "venue: x"})
    assert "scope" in errors["errors"]
    assert ictx.queue.tasks == []

    assert submit(ictx, view, {"statement": "Commercial lines get\nless claims software",
                               "scope": "party: carrier"}) is None
    [task] = ictx.queue.tasks
    assert task["payload"]["change"] == {
        "action": "add", "statement": "Commercial lines get less claims software", "requested_by": "alex",
        "sources": [{"call_id": CALL, "span": "1000-1085", "quote": PAY}],
        "scope": {"line": None, "party": "carrier", "segment": None, "seat": None}}
    assert task["payload"]["ref"] == f"{findings['ts']}:finding:1000-1085"
    [resp] = ictx.slack.responses
    assert "Queued by alex" in json.dumps(resp["payload"]["blocks"])
    assert "accessory" not in block(resp["payload"]["blocks"], "finding:1000-1085")

    again = submit(ictx, view, {"statement": "Commercial lines get less claims software", "scope": ""})
    assert again["response_action"] == "errors"
    assert len(ictx.queue.tasks) == 1


def test_edit_proposal_prefills_from_the_message_alone(ictx):
    write_proposals(ictx, {"id": "p-01", "type": "add", "statement": "Store footage seldom survives to the claim",
                           "scope": {"party": "insured"}, "sources": [], "status": "open"})
    msg = proposal_message(ictx)          # the form never reads config/proposals.yml
    interact.handle_interaction(ictx, click(msg, "prop:p-01", button("prop_edit", "Edit", "p-01")))
    view = ictx.slack.views[0]["view"]
    assert block(view["blocks"], "statement")["element"]["initial_value"] == "Store footage is rarely kept long enough"
    assert "initial_value" not in block(view["blocks"], "scope")["element"]
    assert submit(ictx, view, {"statement": "Store footage rarely survives to the claim", "scope": "",
                               "reason": "tighter"}) is None
    change = ictx.queue.tasks[0]["payload"]["change"]
    assert (change["action"], change["proposal_id"], change["statement"], change["reason"]) == \
        ("approve", "p-01", "Store footage rarely survives to the claim", "tighter")
    assert "scope" not in change                                     # blank keeps the proposal's


@pytest.mark.parametrize("typed", ["", "Store footage is rarely kept long enough",
                                   "Store footage is rarely\nkept long enough"])
def test_edit_proposal_left_as_shown_or_empty_keeps_the_proposals_statement(ictx, typed):
    msg = proposal_message(ictx)
    interact.handle_interaction(ictx, click(msg, "prop:p-01", button("prop_edit", "Edit", "p-01")))
    view = ictx.slack.views[0]["view"]
    assert submit(ictx, view, {"statement": typed, "scope": ""}) is None
    assert ictx.queue.tasks[0]["payload"]["change"] == {"action": "approve", "proposal_id": "p-01",
                                                        "requested_by": "alex"}


def test_edit_split_proposal_shows_each_part_from_the_message(ictx):
    write_proposals(ictx, {"id": "p-02", "type": "split", "target": "B-001", "status": "open", "sources": [],
                           "statements": [{"statement": "Small stores overwrite their footage",
                                           "scope": {"segment": "small commercial"}},
                                          {"statement": "Large stores keep good video",
                                           "scope": {"segment": "middle market"}}]})
    msg = split_message(ictx, "p-02", [("Small stores overwrite their footage", "segment: small commercial"),
                                       ("Large stores keep good video", "segment: middle market")])
    interact.handle_interaction(ictx, click(msg, "prop:p-02", button("prop_edit", "Edit", "p-02")))
    view = ictx.slack.views[0]["view"]
    assert block(view["blocks"], "statement_2")["element"]["initial_value"] == "Large stores keep good video"
    assert "initial_value" not in block(view["blocks"], "scope_2")["element"]
    submit(ictx, view, {"statement_1": "Small stores overwrite it", "scope_1": "segment: small commercial",
                        "statement_2": "Large stores keep it", "scope_2": ""})
    change = ictx.queue.tasks[0]["payload"]["change"]
    assert change["action"] == "approve" and change["proposal_id"] == "p-02"
    assert change["part_edits"] == [
        {"statement": "Small stores overwrite it",
         "scope": {"line": None, "party": None, "segment": "small commercial", "seat": None}},
        {"statement": "Large stores keep it", "scope": None}]


def test_reword_form_prefills_from_the_message(ictx):
    msg = pressure_message(ictx)
    interact.handle_interaction(ictx, click(msg, "pressure:B-001", button("pressure_reword", "Reword", "B-001")))
    view = ictx.slack.views[0]["view"]
    assert block(view["blocks"], "statement")["element"]["initial_value"] == \
        "Video footage is the most valuable evidence on a liability claim"
    assert "initial_value" not in block(view["blocks"], "scope")["element"]
    submit(ictx, view, {"statement": "Video footage is the best evidence on a liability claim",
                        "scope": "line: general liability", "reason": "sharper"})
    change = ictx.queue.tasks[0]["payload"]["change"]
    assert (change["action"], change["target"], change["reason"]) == ("reword", "B-001", "sharper")
    assert change["scope"]["line"] == "general liability"


def test_reword_left_as_shown_needs_a_scope(ictx):
    from nua import apply

    msg = pressure_message(ictx)
    interact.handle_interaction(ictx, click(msg, "pressure:B-001", button("pressure_reword", "Reword", "B-001")))
    view = ictx.slack.views[0]["view"]
    shown = "Video footage is the most valuable evidence on a liability claim"
    errors = submit(ictx, view, {"statement": shown, "scope": ""})
    assert errors["errors"] == {"statement": interact.NOTHING_TO_REWORD} and ictx.queue.tasks == []
    assert submit(ictx, view, {"statement": shown, "scope": "line: general liability"}) is None
    change = ictx.queue.tasks[0]["payload"]["change"]
    assert "statement" not in change and change["scope"]["line"] == "general liability"
    assert apply.apply_task(ictx, ictx.queue.tasks[0]["payload"])["outcome"] == "applied"
    [b1] = parse_beliefs(ictx.repo.read("beliefs.md")).current()
    assert b1.statement == shown and b1.scope == Scope(line="general liability")


def test_split_form_needs_two_statements(ictx):
    msg = pressure_message(ictx)
    interact.handle_interaction(ictx, click(msg, "pressure:B-001", button("pressure_split", "Split", "B-001")))
    view = ictx.slack.views[0]["view"]
    assert block(view["blocks"], "statement_1")["element"]["initial_value"] == \
        "Video footage is the most valuable evidence on a liability claim"
    errors = submit(ictx, view, {"statement_1": "Store video is the best evidence on a slip and fall", "scope_1": "",
                                 "statement_2": "", "scope_2": ""})
    assert "statement_2" in errors["errors"]
    submit(ictx, view, {"statement_1": "Store video is the best evidence on a slip and fall",
                        "scope_1": "line: general liability",
                        "statement_2": "Dashcam video is the best evidence on an auto claim",
                        "scope_2": "line: commercial auto"})
    change = ictx.queue.tasks[0]["payload"]["change"]
    assert change["action"] == "split" and change["target"] == "B-001"
    assert [s["statement"] for s in change["statements"]] == ["Store video is the best evidence on a slip and fall",
                                                             "Dashcam video is the best evidence on an auto claim"]
    assert change["statements"][1]["scope"]["line"] == "commercial auto"


def test_unknown_payload_types_return_none(ictx):
    assert interact.handle_interaction(ictx, {"type": "view_closed"}) is None
    assert interact.handle_interaction(ictx, {"type": "block_actions", "actions": []}) is None
    assert interact.handle_interaction(ictx, {"type": "view_submission", "view": {"callback_id": "other"}}) is None


# ---------- helpers ----------

def test_locked_blocks_keeps_other_blocks_and_block_ids():
    blocks = [
        {"type": "section", "block_id": "a", "text": {"type": "mrkdwn", "text": "x"},
         "accessory": {"type": "overflow", "options": []}},
        {"type": "actions", "block_id": "b", "elements": []},
        {"type": "section", "block_id": "c", "text": {"type": "mrkdwn", "text": "y"}},
    ]
    out = interact.locked_blocks(blocks, "b", "Queued by Sam")
    assert out[0] == blocks[0] and out[2] == blocks[2]
    assert out[1] == {"type": "context", "block_id": "b", "elements": [{"type": "mrkdwn", "text": "Queued by Sam"}]}
    out = interact.locked_blocks(blocks, "a", "Queued by Sam")
    assert out[0] == {"type": "section", "block_id": "a", "text": {"type": "mrkdwn", "text": "x"}}
    assert out[1]["block_id"] == "a:status" and out[1]["elements"][0]["text"] == "Queued by Sam"
    assert len(out) == 4
    assert interact.locked_blocks(out, "a", "Done") == out[:1] + [
        {"type": "context", "block_id": "a:status", "elements": [{"type": "mrkdwn", "text": "Done"}]}] + out[2:]


# ---------- contract with nua.apply ----------

def test_queued_watch_applies_through_apply_task(ictx):
    from nua import apply

    _, replies = post_brief(ictx, darius_digest())
    findings = findings_reply(replies)
    interact.handle_interaction(ictx, click(findings, "finding:1000-1085", menu_pick(f"w|{CALL}|1000-1085")))
    result = apply.apply_task(ictx, ictx.queue.tasks[0]["payload"])
    assert result["outcome"] == "applied", result
    doc = parse_beliefs(ictx.repo.read("beliefs.md"))
    [watch] = [e for e in doc.entries if e.section == "watching"]
    assert watch.statement == "Commercial lines get less claims software"
    assert watch.scope == Scope(party="carrier", segment="small commercial")
    assert watch.history[-1].sources == [(CALL, PAY)]
    assert watch.history[-1].ref == f"{findings['ts']}:finding:1000-1085"
    marked = next(m for m in ictx.slack.messages if m["ts"] == findings["ts"])
    assert "accessory" not in block(marked["blocks"], "finding:1000-1085")
    assert block(marked["blocks"], "finding:1000-1085:status")


def test_queued_wrong_read_applies_through_apply_task(ictx):
    from nua import apply

    check = BeliefCheck(target="B-001", verdict="contradicts", claims=["c01"], decided_by="agree")
    top, _ = post_brief(ictx, darius_digest([check]))
    actions = next(b for b in top["blocks"] if b["type"] == "actions")
    interact.handle_interaction(ictx, click(top, actions["block_id"], actions["elements"][0]))
    result = apply.apply_task(ictx, ictx.queue.tasks[0]["payload"])
    assert result["outcome"] == "applied", result
    overrides = yaml.safe_load(ictx.repo.read("config/verdict_overrides.yml"))
    [item] = overrides["overrides"]
    assert (item["call_id"], item["target"], item["quote"]) == (CALL, "B-001", PAY)
    assert (item["verdict"], item["offset"]) == ("contradicts", [1000, 1085])
    top_now = next(m for m in ictx.slack.messages if m["ts"] == top["ts"])
    assert block(top_now["blocks"], actions["block_id"])["type"] == "context"


# ---------- who may act ----------

def test_anyone_in_the_channel_may_act_and_is_named_by_slack_handle(ictx):
    """No cofounder list: a click from someone outside config/people.yml is queued under their handle."""
    msg = pressure_message(ictx)
    interact.handle_interaction(ictx, click(msg, "pressure:B-001", button("pressure_keep", "Keep", "B-001"),
                                            user="U0GUEST", name="maria"))
    [task] = ictx.queue.tasks
    assert task["payload"]["change"]["requested_by"] == "maria"
    assert "Queued by maria" in json.dumps(ictx.slack.responses[0]["payload"]["blocks"])
    assert ictx.slack.ephemerals == []


def test_anyone_may_rerun_and_open_a_form(ictx):
    value = json.dumps({"kind": "backcheck", "payload": {}})
    ictx.slack.post("#discovery-ops", [
        {"type": "actions", "block_id": "rerun", "elements": [button("rerun", "Re-run", value)]}], "Task failed")
    ops = ictx.slack.messages[-1]
    interact.handle_interaction(ictx, click(ops, "rerun", button("rerun", "Re-run", value), user="U0GUEST",
                                            name="maria"))
    msg = pressure_message(ictx)
    interact.handle_interaction(ictx, click(msg, "pressure:B-001", button("pressure_reword", "Reword", "B-001"),
                                            user="U0GUEST", name="maria"))
    assert [t["kind"] for t in ictx.queue.tasks] == ["backcheck"] and len(ictx.slack.views) == 1


def test_a_payload_with_no_user_is_refused(ictx):
    msg = pressure_message(ictx)
    payload = click(msg, "pressure:B-001", button("pressure_keep", "Keep", "B-001"))
    payload["user"] = {}
    interact.handle_interaction(ictx, payload)
    assert ictx.queue.tasks == [] and ictx.slack.responses == []


def test_a_form_submitted_by_anyone_is_queued_under_their_handle(ictx):
    msg = pressure_message(ictx)
    interact.handle_interaction(ictx, click(msg, "pressure:B-001", button("pressure_reword", "Reword", "B-001")))
    view = ictx.slack.views[0]["view"]
    out = submit(ictx, view, {"statement": "Video footage is the best evidence on a liability claim"}, user="U0GUEST")
    assert out is None or out.get("response_action") != "errors"
    [task] = ictx.queue.tasks
    assert task["payload"]["change"]["requested_by"]


# ---------- split proposals with more than two parts ----------

def test_edit_split_proposal_keeps_every_part(ictx):
    from nua import apply

    parts = [("Carriers want the store video", "party: carrier"),
             ("Insureds overwrite the store video", "party: insured"),
             ("TPAs rarely get the store video", "party: TPA")]
    write_proposals(ictx, {"id": "p-03", "type": "split", "target": "B-001", "status": "open", "sources": [],
                           "statements": [{"statement": t, "scope": {"party": c.split(": ")[1]}}
                                          for t, c in parts]})
    msg = split_message(ictx, "p-03", parts)
    interact.handle_interaction(ictx, click(msg, "prop:p-03", button("prop_edit", "Edit", "p-03")))
    view = ictx.slack.views[0]["view"]
    fields = [b["block_id"] for b in view["blocks"] if b["type"] == "input"]
    assert fields == ["statement_1", "scope_1", "statement_2", "scope_2", "statement_3", "scope_3", "reason"]
    assert block(view["blocks"], "statement_3")["element"]["initial_value"] == "TPAs rarely get the store video"
    values = {"reason": "tweak one scope", "scope_2": "party: insured, landlord"}
    for i, (text, _) in enumerate(parts, 1):
        values[f"statement_{i}"] = text
    submit(ictx, view, values)
    change = ictx.queue.tasks[0]["payload"]["change"]
    assert [e["statement"] for e in change["part_edits"]] == [None, None, None]      # left as shown
    assert [(e["scope"] or {}).get("party") for e in change["part_edits"]] == [None, "insured, landlord", None]
    assert change["reason"] == "tweak one scope"
    assert apply.apply_task(ictx, ictx.queue.tasks[0]["payload"])["outcome"] == "applied"
    doc = parse_beliefs(ictx.repo.read("beliefs.md"))
    assert [(e.statement, e.scope.party) for e in doc.current()] == [
        ("Carriers want the store video", "carrier"), ("Insureds overwrite the store video", "insured, landlord"),
        ("TPAs rarely get the store video", "TPA")]


def test_form_lock_on_a_top_message_uses_the_message_as_it_is_now(ictx):
    """A form opened from the top message locks its item on the current message, so an item another
    cofounder locked meanwhile stays locked."""
    top_ts = ictx.slack.post("C123", [
        {"type": "actions", "block_id": "pressure:B-001", "elements": [button("pressure_reword", "Reword", "B-001")]},
        {"type": "actions", "block_id": "pressure:B-002", "elements": [button("pressure_keep", "Keep", "B-002")]},
    ], "top")
    top = ictx.slack.messages[-1]
    interact.handle_interaction(ictx, click(top, "pressure:B-001", button("pressure_reword", "Reword", "B-001")))
    view = ictx.slack.views[0]["view"]
    # Sam locks B-002 on the same message while the form is open
    ictx.slack.update("C123", top_ts, interact.locked_blocks(top["blocks"], "pressure:B-002", "Queued by Sam"), "top")

    class Fresh:
        def __init__(self, inner):
            self._inner = inner

        def __getattr__(self, name):
            return getattr(self._inner, name)

        def message(self, channel, ts):
            m = next(m for m in self._inner.messages if m["ts"] == ts)
            return {"ts": ts, "blocks": m["blocks"], "text": m["text"]}

    ictx.slack = Fresh(ictx.slack)
    submit(ictx, view, {"statement": "Video footage is the best evidence on a liability claim"})
    locked = ictx.slack.responses[-1]["payload"]["blocks"]
    assert block(locked, "pressure:B-001")["elements"][0]["text"] == "Queued by alex"
    assert block(locked, "pressure:B-002")["elements"][0]["text"] == "Queued by Sam"


# ---------- a click queued before the call is re-digested ----------

def superseded_digest(checks: list[BeliefCheck] | None = None) -> Digest:
    """Darius re-digested from a richer copy: offsets moved, so 1000-1085 now holds another claim."""
    claims = [
        Claim(id="c01", text="Searching Google is often faster", quote="Half the time it's faster for me to just search the web.",
              offset=(1000, 1055), turns=[50], speaker="Darius Kemp", scope=Scope(party="carrier")),
        Claim(id="c02", text="Commercial lines get less claims software",
              quote=LONG, offset=(1190, 1292), turns=[58],
              speaker="Darius Kemp", scope=Scope(party="carrier", segment="small commercial")),
    ]
    return Digest(call_id=CALL, transcript="transcripts/circleback_2.md", status="digested", origin="live",
                  claims=claims, belief_checks=checks or [], contact="Darius Kemp", firm="Example Mutual",
                  seat="claims adjuster (staff)")


def test_a_watch_queued_before_a_supersede_lands_on_the_quote_it_showed(ictx):
    from nua import apply

    _, replies = post_brief(ictx, darius_digest())
    findings = findings_reply(replies)
    interact.handle_interaction(ictx, click(findings, "finding:1000-1085", menu_pick(f"w|{CALL}|1000-1085")))
    write_digest(ictx.repo, superseded_digest())             # the supersede lands before the task runs
    result = apply.apply_task(ictx, ictx.queue.tasks[0]["payload"])
    assert result["outcome"] == "applied", result
    [watch] = [e for e in parse_beliefs(ictx.repo.read("beliefs.md")).entries if e.section == "watching"]
    assert watch.history[-1].sources == [(CALL, LONG)]
    assert watch.statement == "Commercial lines get less claims software"


def test_a_wrong_read_queued_before_a_supersede_dismisses_the_read_it_showed(ictx):
    from nua import apply

    check = BeliefCheck(target="B-001", verdict="contradicts", claims=["c01"], decided_by="agree")
    top, _ = post_brief(ictx, darius_digest([check]))
    actions = next(b for b in top["blocks"] if b["type"] == "actions")
    interact.handle_interaction(ictx, click(top, actions["block_id"], actions["elements"][0]))
    moved = BeliefCheck(target="B-001", verdict="contradicts", claims=["c02"], decided_by="agree")
    write_digest(ictx.repo, superseded_digest([moved]))
    result = apply.apply_task(ictx, ictx.queue.tasks[0]["payload"])
    assert result["outcome"] == "applied", result
    [item] = yaml.safe_load(ictx.repo.read("config/verdict_overrides.yml"))["overrides"]
    assert item["quote"] == LONG and item["offset"] == [1190, 1292]


def test_make_belief_carries_a_long_quote_within_slacks_metadata_limit(ictx):
    long_quote = " ".join(f"word{n}" for n in range(900))
    d = darius_digest()
    d.claims[0].quotes[0].quote = long_quote
    _, replies = post_brief(ictx, d)
    findings = findings_reply(replies)
    interact.handle_interaction(ictx, click(findings, "finding:1000-1085", menu_pick(f"mb|{CALL}|1000-1085")))
    view = ictx.slack.views[0]["view"]
    assert len(view["private_metadata"]) <= 3000
    shown = json.loads(view["private_metadata"])["quote"]
    assert shown and long_quote.startswith(shown) and long_quote[len(shown)] == " "


# ---------- editing a split proposal ----------

THREE_PARTS = [("Carriers want the store video", "carrier"), ("Insureds overwrite the store video", "insured"),
               ("TPAs rarely get the store video", "TPA")]


def open_split_edit(ictx) -> dict:
    write_proposals(ictx, {"id": "p-03", "type": "split", "target": "B-001", "status": "open", "sources": [],
                           "statements": [{"statement": t, "scope": {"party": c}} for t, c in THREE_PARTS]})
    msg = split_message(ictx, "p-03", [(t, f"party: {c}") for t, c in THREE_PARTS])
    interact.handle_interaction(ictx, click(msg, "prop:p-03", button("prop_edit", "Edit", "p-03")))
    return ictx.slack.views[0]["view"]


def split_values(**changes: str) -> dict[str, str]:
    """The split form as it opens: each statement as shown, every scope blank."""
    values = {}
    for i, (text, _) in enumerate(THREE_PARTS, 1):
        values[f"statement_{i}"] = text
        values[f"scope_{i}"] = ""
    return {**values, **changes}


def test_edit_split_rewrites_one_part_and_drops_a_cleared_one(ictx):
    from nua import apply

    view = open_split_edit(ictx)
    assert submit(ictx, view, split_values(statement_1="Carriers ask for the store video within 48 hours",
                                           statement_3="")) is None
    change = ictx.queue.tasks[0]["payload"]["change"]
    assert change["part_edits"] == [
        {"statement": "Carriers ask for the store video within 48 hours", "scope": None},
        {"statement": None, "scope": None}, {"statement": "", "scope": None}]
    result = apply.apply_task(ictx, ictx.queue.tasks[0]["payload"])
    assert result["outcome"] == "applied", result
    doc = parse_beliefs(ictx.repo.read("beliefs.md"))
    assert [(e.id, e.statement, e.scope.party) for e in doc.current()] == [
        ("B-001", "Carriers ask for the store video within 48 hours", "carrier"),
        ("B-002", "Insureds overwrite the store video", "insured")]


def test_edit_split_keeps_at_least_two_parts(ictx):
    view = open_split_edit(ictx)
    errors = submit(ictx, view, split_values(statement_2="", statement_3=""))
    assert errors["response_action"] == "errors" and set(errors["errors"]) == {"statement_2", "statement_3"}
    assert ictx.queue.tasks == []


def test_edit_split_unchanged_sends_no_edits(ictx):
    view = open_split_edit(ictx)
    assert submit(ictx, view, split_values()) is None
    change = ictx.queue.tasks[0]["payload"]["change"]
    assert "part_edits" not in change and "statements" not in change


def test_edit_split_blank_scopes_keep_each_parts_scope(ictx):
    from nua import apply

    view = open_split_edit(ictx)
    assert submit(ictx, view, split_values(statement_2="Insureds rarely keep the store video")) is None
    change = ictx.queue.tasks[0]["payload"]["change"]
    assert [e["scope"] for e in change["part_edits"]] == [None, None, None]
    assert apply.apply_task(ictx, ictx.queue.tasks[0]["payload"])["outcome"] == "applied"
    doc = parse_beliefs(ictx.repo.read("beliefs.md"))
    assert [(e.statement, e.scope.party) for e in doc.current()] == [
        ("Carriers want the store video", "carrier"), ("Insureds rarely keep the store video", "insured"),
        ("TPAs rarely get the store video", "TPA")]


def test_forms_never_read_the_repo(ictx):
    """Opening and submitting every form works from the payload alone: an AckCtx has no repo."""
    ack = interact.AckCtx.from_ctx(ictx)
    assert not hasattr(ack, "repo") and not hasattr(ack, "config")
    msg = pressure_message(ictx)
    for verb in ("reword", "split"):
        interact.handle_interaction(ack, click(msg, "pressure:B-001",
                                               button(f"pressure_{verb}", verb.title(), "B-001")))
    prop = split_message(ictx, "p-09", [(t, f"party: {c}") for t, c in THREE_PARTS])
    interact.handle_interaction(ack, click(prop, "prop:p-09", button("prop_edit", "Edit", "p-09")))
    _, replies = post_brief(ictx, darius_digest())
    interact.handle_interaction(ack, click(findings_reply(replies), "finding:1000-1085",
                                           menu_pick(f"mb|{CALL}|1000-1085")))
    assert [v["view"]["title"]["text"] for v in ictx.slack.views] == ["Reword B-001", "Split B-001", "Edit proposal",
                                                                     "Make belief"]
    reword = ictx.slack.views[0]["view"]
    state = {"statement": {"value": {"type": "plain_text_input", "value": "Video is the best evidence"}}}
    assert interact.handle_interaction(ack, {"type": "view_submission", "user": {"id": "U_ALEX"},
                                             "view": {**reword, "state": {"values": state}}}) is None
    assert ictx.queue.tasks[0]["payload"]["change"]["statement"] == "Video is the best evidence"
