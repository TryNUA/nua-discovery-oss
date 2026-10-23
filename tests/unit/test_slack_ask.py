"""@bot questions in Slack: parsing the mention, the answer message, and the processor task."""
from __future__ import annotations

import json

import pytest

from nua import runner, slack_ask
from nua.ask import AskResult
from nua.context import Ctx
from nua.llm import FakeLLM, LLMError
from nua.slack_ask import Question, answer_blocks, parse_mention


def _ctx(tmp_repo, router=None) -> Ctx:
    """A test Ctx whose `ask_map` model (the router) answers with `router(prompt)`."""
    ctx = Ctx.for_tests(tmp_repo)
    if router is not None:
        ctx.llms.register(FakeLLM(ctx.config.pipeline.ask_map, lambda system, prompt, schema: router(prompt)))
    return ctx


def _asks(monkeypatch, result=None):
    seen = []

    def fake_ask(c, question, *, since=None, seat=None):
        seen.append({"question": question, "since": since, "seat": seat})
        return result or _result()

    monkeypatch.setattr("nua.ask.ask", fake_ask)
    return seen


def test_parse_mention_strips_mentions_links_and_filters():
    q = parse_mention('<@U0BOT|discovery> what does <https://guidewire.com|Guidewire> do? seat:underwriter since:2026-09-01')
    assert (q.text, q.since, q.seat) == ("what does Guidewire do?", "2026-09-01", "underwriter")
    q = parse_mention("<@U0BOT> seat:“claims adjuster (staff)” how bad is fraud &amp; abuse")
    assert (q.text, q.seat) == ("how bad is fraud & abuse", "claims adjuster (staff)")
    assert parse_mention("<@U0BOT>   ").text == ""


def _result(answer="**Two** seats.\n* underwriters\n* adjusters", citations=None) -> AskResult:
    cites = citations if citations is not None else [
        {"call_id": "2026-09-24_jane-doe", "quote": "price ourselves out", "verified": True},
        {"call_id": "2026-09-30_viktor", "quote": "video never matters", "verified": False}]
    return AskResult(answer=answer, citations=cites, read=34, total=34)


def test_answer_blocks_convert_markdown_and_list_quotes_and_coverage():
    blocks, text = answer_blocks(Question("q", since="2026-09-01"), _result())
    body = blocks[0]["text"]["text"]
    assert "*Two* seats." in body and "• underwriters" in body
    quotes = json.dumps(blocks)
    assert "2026-09-24_jane-doe" in quotes and "(not verified)" in quotes
    assert blocks[-1]["type"] == "context"
    assert blocks[-1]["elements"][0]["text"] == "read 34/34 digests (since 2026-09-01)"
    assert "read 34/34 digests" in text


def test_a_long_answer_is_split_within_slack_limits():
    long = "\n\n".join("word " * 400 for _ in range(30))
    blocks, _ = answer_blocks(Question("q"), _result(answer=long, citations=[]))
    assert len(blocks) <= 50
    assert all(len(b["text"]["text"]) <= 3000 for b in blocks if b["type"] == "section")


def test_a_question_is_routed_to_ask_and_answered_in_the_thread(tmp_repo, monkeypatch):
    ctx = _ctx(tmp_repo, lambda p: {"route": "ask", "question": "which seats have we talked to?", "reply": ""})
    seen = _asks(monkeypatch)
    out = runner.run_task(ctx, "ask", {"channel": "C1", "thread_ts": "1.2", "ts": "1.2", "question": "which seats?",
                                       "since": None, "seat": "underwriter"})
    assert out["ok"] and seen == [{"question": "which seats have we talked to?", "since": None, "seat": "underwriter"}]
    on_it, answer = ctx.slack.messages
    assert "On it" in on_it["text"] and "which seats have we talked to?" in on_it["text"]
    assert (answer["channel"], answer["thread_ts"]) == ("C1", "1.2") and "read 34/34" in answer["text"]


def test_a_greeting_gets_a_short_reply_and_no_search(tmp_repo, monkeypatch):
    ctx = _ctx(tmp_repo, lambda p: {"route": "reply", "question": "",
                                    "reply": "Hi! Ask me about the calls, e.g. which seats have we talked to?"})
    seen = _asks(monkeypatch)
    out = runner.run_task(ctx, "ask", {"channel": "C1", "thread_ts": "1.2", "ts": "1.2", "question": "hi"})
    assert out["ok"] and seen == []
    [msg] = ctx.slack.messages
    assert msg["text"].startswith("Hi!") and msg["thread_ts"] == "1.2"


def test_a_follow_up_in_a_thread_gives_the_router_the_thread(tmp_repo, monkeypatch):
    prompts = []

    def router(prompt):
        prompts.append(prompt)
        return {"route": "ask", "question": "what do brokers say about video evidence?", "reply": ""}

    ctx = _ctx(tmp_repo, router)
    parent = ctx.slack.post("C1", [], "<@U0BOT> what do adjusters say about video evidence?")
    ctx.slack.post("C1", [], "Adjusters say video settles liability fast.", thread_ts=parent)
    mention = ctx.slack.post("C1", [], "<@U0BOT> what about brokers?", thread_ts=parent)
    seen = _asks(monkeypatch)
    runner.run_task(ctx, "ask", {"channel": "C1", "thread_ts": parent, "ts": mention,
                                 "question": "what about brokers?"})
    assert "person: @bot what do adjusters say about video evidence?" in prompts[0]
    assert "bot: Adjusters say video settles liability fast." in prompts[0]
    assert "what about brokers?" not in prompts[0].split("### The message")[0]     # the mention itself is the message
    assert seen[0]["question"] == "what do brokers say about video evidence?"


@pytest.mark.parametrize("text, searched", [("hi", False), ("thanks!", False), ("which seats?", True),
                                             ("tell me about pricing actuaries", True)])
def test_when_the_router_fails_a_plain_rule_decides(tmp_repo, monkeypatch, text, searched):
    ctx = _ctx(tmp_repo, lambda p: LLMError("model down"))
    seen = _asks(monkeypatch)
    out = runner.run_task(ctx, "ask", {"channel": "C1", "thread_ts": "1.2", "ts": "1.2", "question": text})
    assert out["ok"] and bool(seen) == searched
    if not searched:
        assert "Mention me with a question" in ctx.slack.messages[0]["text"]


def test_a_bad_since_gets_a_reply_not_a_retry(tmp_repo, monkeypatch):
    ctx = _ctx(tmp_repo, lambda p: {"route": "ask", "question": "q about calls", "reply": ""})

    def fake_ask(*a, **k):
        raise ValueError("since must be YYYY-MM-DD")

    monkeypatch.setattr("nua.ask.ask", fake_ask)
    out = runner.run_task(ctx, "ask", {"channel": "C1", "thread_ts": "1.2", "question": "q about calls"})
    assert out["ok"] and "could not run that" in ctx.slack.messages[-1]["text"]


def test_an_ask_task_without_a_thread_is_invalid(tmp_repo):
    ctx = Ctx.for_tests(tmp_repo)
    out = runner.run_task(ctx, "ask", {"question": "q"})
    assert out["ok"] is False and out["final"] is True


def test_route_prompt_follows_the_house_style():
    from nua.prompts import route as prompt

    for text in (prompt.SYSTEM, prompt.ROUTE):
        assert chr(0x2014) not in text
    assert "{thread}" in prompt.ROUTE and "{message}" in prompt.ROUTE


STATUS = """# Beliefs status

Under pressure: B-002.

## Beliefs

### B-001 Video settles liability fast.

- scope: party: carrier

**For** (3 calls, 2 seats)

- claims adjuster (staff)
  - 2026-09-30_x [2/2 agree]: "video settles it"

**Against**: none

**Scope mismatch** (1 call, 1 seat)

### B-002 Adjusters drown in paperwork.

**For**: none

**Against** (2 calls, 2 seats)

**Scope mismatch**: none

## Watching

### W-001 Carriers mine public filings.

**For** (1 call, 1 seat)

**Against**: none

**Scope mismatch**: none
"""

FINDINGS = """# Findings

Claims no current belief or Watching item touches: 12 claims from 5 calls. Generated by nua/indexes.py.

## Attorneys inflate payouts (2 calls, 2 seats)

- 2026-10-02_m · claims adjuster (staff): Attorneys push settlements up.
  > quote

## Ungrouped since 2026-10-04

3 claims, newest first.

- 2026-10-05_c · claims executive: FNOL is manual and missing details.
  > quote
"""


def _route_to(route, target=""):
    return lambda p: {"route": route, "question": "", "reply": "", "target": target}


def test_list_beliefs_reads_the_status_file_without_a_search(tmp_repo, monkeypatch):
    ctx = _ctx(tmp_repo, _route_to("beliefs"))
    ctx.repo.write("beliefs-status.md", STATUS)
    seen = _asks(monkeypatch)
    runner.run_task(ctx, "ask", {"channel": "C1", "thread_ts": "1.2", "question": "list beliefs"})
    assert seen == []
    [msg] = ctx.slack.messages
    text = json.dumps(msg["blocks"], ensure_ascii=False)
    assert "B-001" in text and "3 for \u00b7 0 against \u00b7 1 scope mismatch" in text
    assert "0 for \u00b7 2 against" in text and "*Watching* (1)" in text and "Under pressure: B-002." in text


def test_show_one_belief_gives_its_evidence(tmp_repo, monkeypatch):
    ctx = _ctx(tmp_repo, _route_to("beliefs", "b-001"))
    ctx.repo.write("beliefs-status.md", STATUS)
    runner.run_task(ctx, "ask", {"channel": "C1", "thread_ts": "1.2", "question": "show b-001"})
    body = ctx.slack.messages[0]["blocks"][0]["text"]["text"]
    assert body.startswith("*B-001* Video settles liability fast.") and "video settles it" in body
    assert "B-002" not in body


def test_an_unknown_belief_id_says_so(tmp_repo, monkeypatch):
    ctx = _ctx(tmp_repo, _route_to("beliefs", "B-099"))
    ctx.repo.write("beliefs-status.md", STATUS)
    runner.run_task(ctx, "ask", {"channel": "C1", "thread_ts": "1.2", "question": "show B-099"})
    assert "There is no B-099" in ctx.slack.messages[0]["text"]


def test_list_findings_shows_themes_and_the_newest_ungrouped(tmp_repo, monkeypatch):
    ctx = _ctx(tmp_repo, _route_to("findings"))
    ctx.repo.write("findings.md", FINDINGS)
    seen = _asks(monkeypatch)
    runner.run_task(ctx, "ask", {"channel": "C1", "thread_ts": "1.2", "question": "list findings"})
    assert seen == []
    text = ctx.slack.messages[0]["blocks"][0]["text"]["text"]
    assert text.startswith("*Findings no belief covers yet*  12 claims from 5 calls")
    assert "\u2022 Attorneys inflate payouts (2 calls, 2 seats)" in text
    assert "FNOL is manual and missing details.  _2026-10-05_c \u00b7 claims executive_" in text
