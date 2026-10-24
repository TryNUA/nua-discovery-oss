"""Tests for service/mcp.py: every tool through FastMCP's in-memory client over a temp repo.

The collaborators written in parallel (nua.indexes, nua.ask) are stubbed with small fake modules
placed in sys.modules, so these tests only exercise the MCP layer.
"""
from __future__ import annotations

import asyncio
import json
import shutil
import sys
import types
from dataclasses import dataclass
from pathlib import Path

import pytest
import yaml
from fastmcp import Client
from fastmcp.exceptions import ToolError

from nua import config as config_mod
from nua import frontmatter
from nua.context import Ctx
from nua.digest import make_stub, write_digest
from nua.ports import NullRemote
from nua.types import BeliefCheck, Claim, Digest, FollowUp, QuestionCoverage, Scope
from service import mcp as mcp_mod
from conftest import ROOT, fixture_text

TOOLS = {"ask", "list_calls", "get_call", "get_beliefs", "get_belief", "get_findings",
         "get_open_questions", "get_prep_context", "request_change", "save_prep"}

# Verbatim spans of tests/fixtures/wade-pruitt.md and tests/fixtures/viktor-lang.md (transcript bodies).
Q_AUTO = ("Commercial auto, we're running maybe 3 or 4 a week, so call it 150 to 200 a year. That's where "
          "the real dollars are")
Q_NOT_SO_MUCH = "On the yard and warehouse stuff, the workers' comp, the slip and fall stuff, hardly ever."
Q_LYTX = "Every truck has a Lytx camera, and we lean on that footage a lot and keep all of it."
Q_FLOORS = ("mostly it's people walking the yards and our site safety leads keeping us in the loop here at "
            "headquarters.")
Q_ATTORNEY = "they hire a lawyer and an $8,000 soft tissue claim turns into a $250,000 claim."
Q_NEVER_MATTERS = "Honestly, in my line of work, video almost never makes a difference."
Q_COPILOT = ("I'll have Copilot or ChatGPT pull it out of the policy for me, and every so often they make "
             "something up that just isn't in there.")    # B-004's source in tests/golden/beliefs.md
NOTES_ONLY = "the employer takes employees at their word and rarely pulls video"      # Granola notes above the transcript

OPEN_QUESTIONS = """# Open questions

## Prep questions not yet answered

- Who pulls the CCTV footage on a workers' comp claim? (never asked, 2026-10-01_wade-pruitt, risk manager (insured))
- How long does a TPA keep site photos? (dodged, 2026-09-30_viktor-lang, independent adjuster / TPA)

## Beliefs under pressure

### B-001 Video footage is the most valuable evidence on a liability claim
- contradicting seats: risk manager (insured), independent adjuster / TPA
- not yet heard from: underwriter, reinsurance

## Manual

<!-- manual:start -->
- Ask an underwriter how they price a fleet with cameras.
- Ask a risk manager (insured) about near-miss tracking tools.
<!-- manual:end -->
"""

FINDINGS = "# Findings\n\n## Auto claim exposure\n\n- 2026-10-01_wade-pruitt: auto liability\n"

GOOD_PREP = """---
planned_date: 2026-10-02
contact: Jane Doe
firm: Example Mutual
attendee_email: jane@example.com
seat: underwriter
caller: alex
---

## Context
First call with Jane.

## Questions
1. Would you cut the premium for a fleet that keeps dashcam footage on every claim? (B-001)
2. What goes in a submission for a new fleet?
"""


# ---------- fakes for collaborators written in parallel ----------

@dataclass
class FakeEntryStatus:
    entry_id: str
    supports: list
    contradicts: list
    scope_mismatch: list
    under_pressure: bool
    checked: int
    total: int


def _fake_indexes() -> types.ModuleType:
    mod = types.ModuleType("nua.indexes")

    def entry_status(entry, digests, overrides, config):
        buckets = {"supports": [], "contradicts": [], "scope_mismatch": []}
        live = [d for d in digests if d.status == "digested"]
        for d in live:
            for b in d.belief_checks:
                if b.target != entry.id:
                    continue
                for cid in b.claims:
                    c = d.claim(cid)
                    buckets[b.verdict].append({"call_id": d.call_id, "seat": d.seat, "quote": c.quote if c else "",
                                               "date": d.date, "decided_by": b.decided_by})
        seats = {i["seat"] for i in buckets["contradicts"]}
        return FakeEntryStatus(entry.id, buckets["supports"], buckets["contradicts"], buckets["scope_mismatch"],
                               under_pressure=entry.section == "beliefs" and len(seats) >= 2,
                               checked=sum(1 for d in live if entry.id in d.checked), total=len(live))

    def finding_claims(digests, beliefs, overrides=None):
        dismissed = {(o.get("call_id"), o.get("target")) for o in overrides or []}
        touched = {(d.call_id, cid) for d in digests for b in d.belief_checks for cid in b.claims
                   if (d.call_id, b.target) not in dismissed}
        return [(d, c) for d in digests if d.status == "digested" for c in d.claims
                if (d.call_id, c.id) not in touched]

    def load_themes(repo):
        data = yaml.safe_load(repo.read_or("config/themes.yml", "")) or {}
        return data.get("themes") or []

    def load_verdict_overrides(repo):
        return yaml.safe_load(repo.read_or("config/verdict_overrides.yml", "")) or []

    def group_findings(digests, findings, themes):
        from nua.verify import token_overlap
        refs = [(t["name"], c.get("call_id"), c.get("quote") or "") for t in themes for c in t.get("claims") or []]
        grouped, ungrouped = {}, []
        for d, c in findings:
            name = next((n for n, cid, q in refs if cid == d.call_id and token_overlap(q, c.quote) >= 0.5), None)
            if name is None:
                ungrouped.append((d, c))
            else:
                grouped.setdefault(name, []).append((d, c))
        return grouped, ungrouped

    mod.entry_status = entry_status
    mod.finding_claims = finding_claims
    mod.group_findings = group_findings
    mod.load_themes = load_themes
    mod.load_verdict_overrides = load_verdict_overrides
    return mod


def _fake_apply() -> types.ModuleType:
    """nua.apply stand-in: the processor's request check accepts everything; one test rejects."""
    mod = types.ModuleType("nua.apply")

    def request_from_dict(data):
        if data.get("reason") == "processor says no":
            raise ValueError("the processor rejects this shape")
        return data

    mod.request_from_dict = request_from_dict
    return mod


@dataclass
class FakeAskResult:
    answer: str
    citations: list
    read: int
    total: int

    @property
    def coverage(self) -> str:
        return f"read {self.read}/{self.total} digests"


def _fake_ask(calls: list) -> types.ModuleType:
    mod = types.ModuleType("nua.ask")

    def ask(ctx, question, *, since=None, seat=None, batch_size=20, concurrency=8, progress=None,
            deadline_seconds=None):
        calls.append({"question": question, "since": since, "seat": seat, "deadline_seconds": deadline_seconds})
        return FakeAskResult(answer="Carriers want footage on every liability claim.",
                             citations=[{"call_id": "2026-10-01_wade-pruitt", "quote": Q_ATTORNEY, "verified": True},
                                        {"call_id": "2026-10-01_wade-pruitt", "quote": "made up", "verified": False}],
                             read=2, total=2)

    mod.ask = ask
    return mod


class FileRemote(NullRemote):
    """NullRemote that really creates files (create-only) and counts syncs."""

    def __init__(self, root: Path):
        super().__init__()
        self.root = root
        self.syncs = 0

    def sync(self) -> None:
        self.syncs += 1

    def create_file_only(self, rel, content, message):
        p = self.root / rel
        if p.exists():
            return False
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
        self.created.append(rel)
        return True


# ---------- repo and server ----------

def _claim(cid: str, text: str, quote: str, speaker: str, **kw) -> Claim:
    return Claim(id=cid, text=text, quote=quote, speaker=speaker, turns=[1], found_by=kw.pop("found_by", 2), **kw)


@pytest.fixture
def repo(tmp_repo: Path) -> Path:
    shutil.copy(ROOT / "tests" / "golden" / "beliefs.md", tmp_repo / "beliefs.md")
    shutil.copy(ROOT / "tests" / "golden" / "decisions.md", tmp_repo / "decisions.md")
    (tmp_repo / "transcripts" / "wade-pruitt.md").write_text(fixture_text("wade-pruitt"), encoding="utf-8")
    (tmp_repo / "transcripts" / "viktor-lang.md").write_text(fixture_text("viktor-lang"), encoding="utf-8")
    (tmp_repo / "open-questions.md").write_text(OPEN_QUESTIONS, encoding="utf-8")
    (tmp_repo / "findings.md").write_text(FINDINGS, encoding="utf-8")
    (tmp_repo / "config" / "themes.yml").write_text(yaml.safe_dump({"themes": [
        {"name": "Safety walks", "claims": [{"call_id": "2026-10-01_wade-pruitt", "quote": Q_FLOORS}]}]}),
        encoding="utf-8")

    from nua.ports import RepoFS
    fs = RepoFS(tmp_repo)
    wade = Digest(
        call_id="2026-10-01_wade-pruitt", transcript="transcripts/wade-pruitt.md", status="digested",
        origin="backfill", date="2026-10-01", date_source="footer", contact="Wade Pruitt", firm="Example Foods",
        seat="risk manager (insured)", speaker_labels="me_them", prompt_version="v1",
        attendees=[],
        summary=["Auto liability is the highest exposure.", "Footage rarely comes up on workers' comp claims."],
        claims=[
            _claim("c01", "Auto liability runs 2 to 3 claims a week and is the highest exposure.", Q_AUTO,
                   "Wade Pruitt", scope=Scope(line="commercial auto", seat="risk manager (insured)")),
            _claim("c02", "Footage is rarely used on warehouse workers' comp claims.", Q_NOT_SO_MUCH, "Wade Pruitt",
                   question_turn=71),
            _claim("c03", "Truck camera footage is used heavily on auto claims.", Q_LYTX, "Wade Pruitt"),
            _claim("c04", "Safety managers walk the floors and report up.", Q_FLOORS, "Wade Pruitt", found_by=1),
            _claim("c05", "An attorney turns a small auto claim into a large one.", Q_ATTORNEY, "Wade Pruitt"),
        ],
        belief_checks=[
            BeliefCheck(target="B-001", verdict="contradicts", claims=["c02"], decided_by="agree"),
            BeliefCheck(target="B-001", verdict="supports", claims=["c03"], decided_by="agree"),
        ],
        checked={"B-001": "x", "B-003": "y"},
        question_coverage=[
            QuestionCoverage(question="Who pulls the CCTV footage on a workers' comp claim?",
                             status="never_asked"),
            QuestionCoverage(question="How long does Lytx keep footage after an accident?",
                             status="dodged"),
        ],
        follow_ups=[FollowUp(direction="we_owe", what="answer his AI and technical questions", who="Wade Pruitt",
                             quote="I'm glad to answer anything on that front.")],
        ask_next=["Who reviews the Lytx video after an accident?"],
        mentions={"people": ["Jane Doe"], "firms": ["Lytx", "Keystone Casualty"]},
    )
    viktor = Digest(
        call_id="2026-09-30_viktor-lang", transcript="transcripts/viktor-lang.md", status="digested",
        origin="backfill", date="2026-09-30", date_source="footer", contact="Viktor Lang", firm="Corrigan Hale",
        seat="independent adjuster / TPA", speaker_labels="single_stream", prompt_version="v1",
        summary=["Video rarely matters on property claims."],
        claims=[_claim("c01", "Video rarely matters in independent adjusting.", Q_NEVER_MATTERS, "Viktor Lang"),
                _claim("c02", "He uses outside AI tools on claim files.", Q_COPILOT, "Viktor Lang")],
        belief_checks=[BeliefCheck(target="B-001", verdict="contradicts", claims=["c01"], decided_by="arbiter"),
                       BeliefCheck(target="B-004", verdict="supports", claims=["c02"], decided_by="agree")],
        mentions={"people": [], "firms": ["guide wire"]},
    )
    stub = make_stub(call_id="granola_abc", transcript="transcripts/granola_abc.md", status="internal",
                     origin="live", reason="all attendees have Acme emails")
    for d in (wade, viktor, stub):
        write_digest(fs, d)
    return tmp_repo


@pytest.fixture
def ask_calls() -> list:
    return []


@pytest.fixture
def env(repo: Path, monkeypatch, ask_calls):
    monkeypatch.setitem(sys.modules, "nua.indexes", _fake_indexes())
    monkeypatch.setitem(sys.modules, "nua.ask", _fake_ask(ask_calls))
    monkeypatch.setitem(sys.modules, "nua.apply", _fake_apply())
    monkeypatch.delenv("MCP_DEV_USER", raising=False)
    ctx = Ctx.for_tests(repo)
    ctx.remote = FileRemote(repo)
    return ctx


def _server(ctx: Ctx, **kw):
    return mcp_mod.build_server(lambda: ctx, **kw)


async def _acall(server, name: str, args: dict):
    async with Client(server) as c:
        res = await c.call_tool(name, args)
        return res.content[0].text if res.content else ""


def call(server, name: str, args: dict | None = None) -> str:
    return asyncio.run(_acall(server, name, args or {}))


def call_json(server, name: str, args: dict | None = None) -> dict:
    return json.loads(call(server, name, args))


# ---------- server, instructions, identity ----------

def test_lists_all_tools_and_sends_instructions(env):
    server = _server(env)

    async def run():
        async with Client(server) as c:
            return {t.name for t in await c.list_tools()}, c.instructions

    names, instructions = asyncio.run(run())
    assert names == TOOLS
    assert instructions == mcp_mod.INSTRUCTIONS


def test_every_tool_declares_its_result_size_for_claude_code(env):
    """Claude Code refuses a tool result over 25,000 tokens unless the tool declares a larger size in
    _meta["anthropic/maxResultSizeChars"]; an unfiltered list_calls at 300 calls is about 136k characters."""
    server = _server(env)

    async def run():
        async with Client(server) as c:
            return {t.name: (t.meta or {}) for t in await c.list_tools()}

    metas = asyncio.run(run())
    assert set(metas) == TOOLS
    for name, meta in metas.items():
        assert meta.get("anthropic/maxResultSizeChars") == mcp_mod.MAX_RESULT_CHARS, name


def test_instructions_carry_the_hard_rules():
    text = mcp_mod.INSTRUCTIONS.lower()
    for phrase in ("verbatim", "`ask`", "coverage", "sample", "not evidence", "speaker label", "stub",
                   "request_change"):
        assert phrase in text, phrase
    assert "\u2014" not in mcp_mod.INSTRUCTIONS


def test_allowed_identity(env):
    cfg = env.config
    cfg.people[1].github_login = "SamGH"
    assert mcp_mod.allowed_identity(cfg, "Alex.Rivera@Gmail.com", None) == "alex"
    assert mcp_mod.allowed_identity(cfg, None, "samgh") == "sam"
    assert mcp_mod.allowed_identity(cfg, "stranger@example.com", "stranger") is None
    assert mcp_mod.allowed_identity(cfg, None, None) is None
    assert mcp_mod.allowed_identity(cfg, "", "") is None


def test_signed_in_account_must_be_a_cofounder(env, monkeypatch):
    from fastmcp.server.auth.providers.jwt import StaticTokenVerifier
    auth = StaticTokenVerifier(tokens={})
    people = env.repo.root / "config" / "people.yml"
    people.write_text(people.read_text().replace("github_login: null\n  circleback_key_name: circleback-key-sam",
                                                 "github_login: samgh\n  circleback_key_name: circleback-key-sam"))
    env.config = config_mod.load(env.repo.root)
    assert env.config.person("sam").github_login == "samgh"
    server = _server(env, auth=auth)

    monkeypatch.setattr(mcp_mod, "_access_token", lambda: None)
    with pytest.raises(ToolError, match="Sign-in required"):
        call(server, "get_beliefs")

    monkeypatch.setattr(mcp_mod, "_access_token", lambda: types.SimpleNamespace(claims={"login": "stranger"}))
    with pytest.raises(ToolError, match="not one of the teammates"):
        call(server, "list_calls")

    # An unverified email never counts; a GitHub login does.
    monkeypatch.setattr(mcp_mod, "_access_token", lambda: types.SimpleNamespace(
        claims={"email": "alex.rivera@gmail.com", "email_verified": False}))
    with pytest.raises(ToolError, match="not one of the teammates"):
        call(server, "list_calls")

    monkeypatch.setattr(mcp_mod, "_access_token", lambda: types.SimpleNamespace(
        claims={"email": "alex.rivera@gmail.com", "email_verified": True}))
    assert call_json(server, "list_calls")["matched"] == 3

    monkeypatch.setattr(mcp_mod, "_access_token", lambda: types.SimpleNamespace(claims={"login": "SamGH"}))
    out = call_json(server, "request_change", {"action": "retire", "target": "B-004", "reason": "no support"})
    assert yaml.safe_load((env.repo.root / out["request_file"]).read_text())["requested_by"] == "sam"


# ---------- repo sync ----------

def test_syncs_only_when_older_than_60_seconds(env, monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(mcp_mod, "_clock", lambda: clock[0])
    server = _server(env)
    call(server, "get_beliefs")
    call(server, "get_findings")
    assert env.remote.syncs == 1
    clock[0] += 30
    call(server, "get_open_questions")
    assert env.remote.syncs == 1
    clock[0] += 31
    call(server, "get_beliefs")
    assert env.remote.syncs == 2


def test_no_pull_while_another_tool_call_is_reading(env, monkeypatch):
    """A pull resets the working tree, so it waits until no other call is in flight (an ask runs for
    minutes); the stale tree is served meanwhile and the next lone call pulls."""
    clock = [1000.0]
    monkeypatch.setattr(mcp_mod, "_clock", lambda: clock[0])
    state = mcp_mod._State(lambda: env, auth_enabled=False)
    with state.call() as (ctx, _):
        assert env.remote.syncs == 1
        clock[0] += 120
        with state.call() as (inner, _):       # a second call while the first still reads
            assert env.remote.syncs == 1
            assert [d.call_id for d in state.digests(inner)]
        assert env.remote.syncs == 1
    with state.call():
        assert env.remote.syncs == 2
    assert state.active == 0


def test_in_flight_count_drops_when_a_call_fails(env, monkeypatch):
    state = mcp_mod._State(lambda: env, auth_enabled=False)
    with pytest.raises(ToolError):
        with state.call(write=True):           # no MCP_DEV_USER: refused
            pass
    with pytest.raises(RuntimeError):
        with state.call():
            raise RuntimeError("tool bug")
    assert state.active == 0


def test_digest_cache_skips_a_file_removed_under_it(env, monkeypatch):
    cache = mcp_mod._DigestCache()
    real_list = env.repo.list
    monkeypatch.setattr(env.repo, "list", lambda pattern: real_list(pattern) + ["digests/2026-09-30_gone.md"])
    assert [d.call_id for d in cache.load(env.repo)] == ["2026-09-30_viktor-lang", "2026-10-01_wade-pruitt",
                                                         "granola_abc"]


def test_failed_sync_still_serves_the_last_tree(env, monkeypatch):
    def boom():
        raise RuntimeError("github down")
    monkeypatch.setattr(env.remote, "sync", boom)
    assert "B-003" in call(_server(env), "get_beliefs")


def test_failed_sync_is_not_retried_on_every_call(env, monkeypatch):
    """Every tool call waits on the sync lock while a pull runs. During a GitHub outage a failed pull must
    back off like a good one, not re-run the (timing out) fetch under the lock on every call."""
    clock = [1000.0]
    monkeypatch.setattr(mcp_mod, "_clock", lambda: clock[0])
    attempts = []

    def stalled():
        attempts.append(clock[0])
        clock[0] += 180                       # the fetch runs into gitops' network timeout
        raise RuntimeError("git fetch timed out after 180 seconds")

    monkeypatch.setattr(env.remote, "sync", stalled)
    server = _server(env)
    call(server, "get_beliefs")
    call(server, "get_findings")
    clock[0] += 30
    call(server, "list_calls")
    assert len(attempts) == 1
    clock[0] += 31
    call(server, "get_beliefs")
    assert len(attempts) == 2


def test_ask_deadline_counts_the_time_spent_syncing(env, ask_calls, monkeypatch):
    """The pull before `ask` runs inside the client's 240 seconds too, so it comes off ask's time limit."""
    clock = [1000.0]
    monkeypatch.setattr(mcp_mod, "_clock", lambda: clock[0])

    def slow_sync():
        clock[0] += 100

    monkeypatch.setattr(env.remote, "sync", slow_sync)
    call_json(_server(env), "ask", {"question": "Q?"})
    assert ask_calls[-1]["deadline_seconds"] == mcp_mod.ASK_DEADLINE_SECONDS - 100

    clock[0] += 61                            # a pull that eats the whole limit still leaves ask a moment
    monkeypatch.setattr(env.remote, "sync", lambda: clock.__setitem__(0, clock[0] + 400))
    call_json(_server(env), "ask", {"question": "Q?"})
    assert 0 < ask_calls[-1]["deadline_seconds"] <= mcp_mod.ASK_MIN_SECONDS


# ---------- list_calls ----------

def test_list_calls_from_digests_before_first_reindex(env):
    server = _server(env)
    out = call_json(server, "list_calls")
    assert out["source"].startswith("digests")
    assert out["matched"] == 3
    assert [r["call_id"] for r in out["rows"]][:2] == ["2026-10-01_wade-pruitt", "2026-09-30_viktor-lang"]
    wade = out["rows"][0]
    assert wade["claims"] == "5" and wade["seat"] == "risk manager (insured)"

    assert [r["call_id"] for r in call_json(server, "list_calls", {"seat": "Risk Manager (Insured)"})["rows"]] == \
        ["2026-10-01_wade-pruitt"]
    assert [r["call_id"] for r in call_json(server, "list_calls", {"status": "internal"})["rows"]] == ["granola_abc"]
    assert [r["call_id"] for r in call_json(server, "list_calls", {"since": "2026-10-01"})["rows"]] == \
        ["2026-10-01_wade-pruitt"]
    assert [r["call_id"] for r in call_json(server, "list_calls", {"until": "2026-09-30"})["rows"]] == \
        ["2026-09-30_viktor-lang"]
    assert [r["call_id"] for r in call_json(server, "list_calls", {"person": "wade"})["rows"]] == \
        ["2026-10-01_wade-pruitt"]
    # "Carrigan Hail" is an ASR alias of Corrigan Hale in config/entities.yml.
    assert [r["call_id"] for r in call_json(server, "list_calls", {"firm": "Carrigan Hail"})["rows"]] == \
        ["2026-09-30_viktor-lang"]


def test_list_calls_reads_calls_csv(env):
    (env.repo.root / "calls.csv").write_text(
        "call_id,transcript,status,origin,date,date_source,contact,firm,seat,institution_type,role_in_thesis,"
        "recorded_by,source,speaker_labels,claims,prompt_version\n"
        "2026-09-20_ann-lee,transcripts/a.md,digested,live,2026-09-20,api,Ann Lee,Acme,underwriter,,,"
        "sam,granola,me_them,12,v1\n", encoding="utf-8")
    out = call_json(_server(env), "list_calls", {"person": "sam"})
    assert out["source"] == "calls.csv"
    assert [r["call_id"] for r in out["rows"]] == ["2026-09-20_ann-lee"]


def test_list_calls_rejects_bad_dates(env):
    with pytest.raises(ToolError, match="YYYY-MM-DD"):
        call(_server(env), "list_calls", {"since": "last week"})


# ---------- get_call ----------

def test_get_call_returns_the_digest_and_page_count(env):
    out = call(_server(env), "get_call", {"call_id": "2026-10-01_wade-pruitt"})
    fm, body, _ = frontmatter.split(out)
    assert fm["call_id"] == "2026-10-01_wade-pruitt"
    assert "Auto liability runs 2 to 3 claims a week" in body
    assert "transcript_page" in out and "1 page" in out


def test_get_call_unknown_or_unsafe_id(env):
    server = _server(env)
    with pytest.raises(ToolError, match="list_calls"):
        call(server, "get_call", {"call_id": "2026-01-01_nobody"})
    (env.repo.root / "secret.md").write_text("---\ncall_id: x\n---\nnot a digest\n", encoding="utf-8")
    for bad in ("../config/people", "../secret", "..", "a/../../secret", "sub/granola_abc", "sub\\granola_abc",
                ".granola_abc", "granola\x00abc", "", "   "):
        with pytest.raises(ToolError, match="not a call id"):
            call(server, "get_call", {"call_id": bad})


def test_get_call_opens_a_stub_named_after_a_dropped_export(env):
    """A dropped export keeps its own file name, so its stub's call id can hold spaces and parentheses.
    list_calls shows it; get_call must open it so the stub's reason can be read."""
    from nua.callid import stub_call_id
    from nua.ports import RepoFS
    rel = "transcripts/Alex _ Darius (Acme Mutual) intro call.md"
    (env.repo.root / rel).write_text("Alex: hi\n", encoding="utf-8")
    call_id = stub_call_id(rel)
    write_digest(RepoFS(env.repo.root), make_stub(
        call_id=call_id, transcript=rel, status="parse_failed", origin="live",
        reason="no speaker turns found"))
    server = _server(env)
    listed = [r["call_id"] for r in call_json(server, "list_calls", {"status": "parse_failed"})["rows"]]
    assert listed == [call_id]
    out = call(server, "get_call", {"call_id": f" {call_id} "})
    fm, _, _ = frontmatter.split(out)
    assert fm["call_id"] == call_id and fm["status"] == "parse_failed"
    assert "no speaker turns found" in out
    assert "Alex: hi" in call(server, "get_call", {"call_id": call_id, "transcript_page": 1})

    # Dots inside a name are one file name, not a path step, so they open too.
    dotted = "transcripts/Darius... follow up v1.2.md"
    (env.repo.root / dotted).write_text("Darius: sure\n", encoding="utf-8")
    write_digest(RepoFS(env.repo.root), make_stub(
        call_id=stub_call_id(dotted), transcript=dotted, status="failed", origin="live", reason="model error"))
    assert "Darius: sure" in call(server, "get_call", {"call_id": "Darius... follow up v1.2", "transcript_page": 1})


def test_get_call_never_follows_a_transcript_path_out_of_transcripts(env):
    from nua.ports import RepoFS
    (env.repo.root / "secret.md").write_text("do not show\n", encoding="utf-8")
    write_digest(RepoFS(env.repo.root), make_stub(
        call_id="odd-stub", transcript="transcripts/../secret.md", status="failed", origin="live", reason="x"))
    server = _server(env)
    assert "do not show" not in call(server, "get_call", {"call_id": "odd-stub"})
    with pytest.raises(ToolError, match="not in the repo"):
        call(server, "get_call", {"call_id": "odd-stub", "transcript_page": 1})


def test_get_call_pages_a_long_transcript(env, monkeypatch):
    monkeypatch.setattr(mcp_mod, "TRANSCRIPT_PAGE_CHARS", 7_500)
    server = _server(env)
    raw = fixture_text("wade-pruitt")
    head = call(server, "get_call", {"call_id": "2026-10-01_wade-pruitt"})
    assert "4 pages" in head

    pages = [call(server, "get_call", {"call_id": "2026-10-01_wade-pruitt", "transcript_page": n}) for n in (1, 2, 3, 4)]
    assert all(len(p) <= 7_500 + 2_000 for p in pages)
    assert "page 1 of 4" in pages[0] and "page 4 of 4" in pages[3]
    assert "not evidence" in pages[0]
    texts = [p.split(mcp_mod.PAGE_TEXT_MARKER, 1)[1] for p in pages]
    assert "".join(texts) == raw
    with pytest.raises(ToolError, match="1 to 4"):
        call(server, "get_call", {"call_id": "2026-10-01_wade-pruitt", "transcript_page": 5})


def test_get_call_on_a_non_utf8_transcript_shows_the_stub(env):
    """A Windows-1252 export becomes a parse_failed stub. get_call must still show the stub and its reason,
    and must never page replacement-character text that could be quoted as verbatim."""
    from nua.ports import RepoFS
    (env.repo.root / "transcripts" / "pat-lee-export.md").write_bytes(
        b"Pat Lee: we\x92re fine with a \x93total loss\x94 call\n")
    write_digest(RepoFS(env.repo.root), make_stub(
        call_id="pat-lee-export", transcript="transcripts/pat-lee-export.md", status="parse_failed",
        origin="backfill", reason="not valid UTF-8"))
    server = _server(env)
    out = call(server, "get_call", {"call_id": "pat-lee-export"})
    fm, _, _ = frontmatter.split(out)
    assert fm["status"] == "parse_failed"
    assert "not valid UTF-8" in out and "UTF-8" in out.split("---")[-1]
    with pytest.raises(ToolError, match="UTF-8"):
        call(server, "get_call", {"call_id": "pat-lee-export", "transcript_page": 1})


# ---------- beliefs ----------

def test_get_beliefs_adds_a_status_line_per_current_entry(env):
    out = call(_server(env), "get_beliefs")
    lines = out.splitlines()
    b1 = lines.index("### B-001 Video footage is the most valuable evidence on a liability claim")
    status = lines[b1 + 1]
    assert status.startswith("- status: ")
    assert "contradicts 2 calls (2 seats)" in status and "UNDER PRESSURE" in status
    assert "checked 1/2 calls" in status
    b4 = lines.index("### B-004 Adjusters can use outside AI tools on claim files")
    assert "supports 1 call (1 seat)" in lines[b4 + 1] and "UNDER PRESSURE" not in lines[b4 + 1]
    assert "### W-001 Commercial lines get less claims software than personal lines" in out
    assert "\u2014" not in out


def test_get_beliefs_when_empty(env):
    (env.repo.root / "beliefs.md").write_text("## Beliefs\n\n## Watching\n\n## Retired\n", encoding="utf-8")
    assert "No beliefs" in call(_server(env), "get_beliefs")


def test_get_belief_shows_evidence_and_decisions(env):
    server = _server(env)
    out = call(server, "get_belief", {"id": "b-1"})
    assert out.startswith("### B-001 ")
    assert "D-002" in out
    assert "## Contradicts (2 calls, 2 seats)" in out
    assert f"> {Q_NOT_SO_MUCH}" in out and f"> {Q_NEVER_MATTERS}" in out
    assert "decided by arbiter" in out
    assert "## History" in out and "ref 1790607600.000100:prop-01" in out
    with pytest.raises(ToolError, match="get_beliefs"):
        call(server, "get_belief", {"id": "B-099"})


# ---------- findings and open questions ----------

def test_get_findings_whole_file_and_recent_ungrouped(env):
    server = _server(env)
    assert call(server, "get_findings") == FINDINGS
    out = call(server, "get_findings", {"since": "2026-10-01"})
    assert Q_AUTO in out and Q_ATTORNEY in out
    assert Q_FLOORS not in out          # already in a theme
    assert Q_NOT_SO_MUCH not in out     # touched by a belief check
    assert Q_NEVER_MATTERS not in out   # call before `since`
    assert "2026-10-01_wade-pruitt" in out


def test_get_findings_counts_a_dismissed_verdict_as_a_finding(env):
    """Same rule as findings.md: a Wrong read leaves the claim a finding (config/verdict_overrides.yml)."""
    (env.repo.root / "config" / "verdict_overrides.yml").write_text(yaml.safe_dump([
        {"call_id": "2026-10-01_wade-pruitt", "target": "B-001", "quote": Q_NOT_SO_MUCH,
         "verdict": "contradicts"}]),
        encoding="utf-8")
    out = call(_server(env), "get_findings", {"since": "2026-10-01"})
    assert Q_NOT_SO_MUCH in out


def test_get_findings_before_first_reindex(env):
    (env.repo.root / "findings.md").unlink()
    out = call(_server(env), "get_findings")
    assert "not been generated" in out and Q_AUTO in out


def test_get_open_questions_whole_file_and_by_seat(env):
    server = _server(env)
    assert call(server, "get_open_questions") == OPEN_QUESTIONS
    out = call(server, "get_open_questions", {"seat": "risk manager (insured)"})
    assert "CCTV footage on a workers' comp claim" in out
    assert "TPA keep site photos" not in out
    assert "### B-001" in out and "contradicting seats: risk manager (insured)" in out
    assert "near-miss tracking tools" in out
    assert "underwriter how they price" not in out
    out = call(server, "get_open_questions", {"seat": "SIU / fraud investigator"})
    assert "No open questions" in out


# ---------- prep ----------

def test_get_prep_context_for_a_known_contact(env):
    out = call(_server(env), "get_prep_context", {"contact": "Wade"})
    assert "## Prior calls with Wade" in out
    assert "### 2026-10-01_wade-pruitt" in out
    assert Q_AUTO in out
    assert "never asked: Who pulls the CCTV footage" in out
    assert "dodged: How long does Lytx keep footage" in out
    assert "we owe: answer his AI and technical questions" in out
    assert "## Beliefs under pressure" in out and "B-001" in out
    assert "W-001" in out
    assert "prep/<planned-date>_wade-pruitt.md" in out and "# Prep context: Wade Pruitt" in out
    assert "risk manager (insured)" in out      # seat inferred from the prior call
    assert "CCTV footage on a workers' comp claim" in out


def test_get_prep_context_finds_mentions(env):
    out = call(_server(env), "get_prep_context", {"contact": "Jane Doe", "firm": "Example Mutual",
                                                  "seat": "underwriter"})
    assert "No prior calls" in out
    assert "## Mentions in other calls" in out and "2026-10-01_wade-pruitt" in out
    assert "Not yet heard from underwriter" in out


def test_get_prep_context_asks_for_a_full_name_for_a_first_time_contact(env):
    """process_call links a prep by attendee email, then by the digest's contact name ("Jane Doe"). A prep
    saved as `contact: Jane` never matches, so a first name with no prior call must prompt for the full name."""
    server = _server(env)
    out = call(server, "get_prep_context", {"contact": "Jane", "firm": "Example Mutual"})
    assert "No prior calls" in out
    assert "Ask the user for Jane's full name" in out
    [contact_line] = [line for line in out.splitlines() if line.startswith("contact:")]
    assert contact_line.startswith("contact: Jane") and "full name" in contact_line
    known = call(server, "get_prep_context", {"contact": "Wade"})    # prior call gives "Wade Pruitt"
    assert "Ask the user for" not in known
    assert "full name" in mcp_mod.INSTRUCTIONS


def test_save_prep_warns_when_only_a_first_name_is_given(env, monkeypatch):
    from nua.prep import load_preps, match_prep
    monkeypatch.setenv("MCP_DEV_USER", "alex")
    first_name_only = (GOOD_PREP.replace("contact: Jane Doe", "contact: Jane")
                       .replace("attendee_email: jane@example.com\n", ""))
    out = call_json(_server(env), "save_prep", {"markdown": first_name_only})
    assert out["prep_file"] == "prep/2026-10-02_jane.md"
    assert "Jane" in out["warning"] and "full name" in out["warning"]
    assert "links this prep once the call is processed" not in out["next"]
    # the warning is real: the digest's contact is the full name, so the prep does not match
    assert match_prep(load_preps(env.repo), date="2026-10-02", contact="Jane Doe", attendee_emails=[],
                      config=env.config) is None


def test_save_prep_rejects_an_invalid_template(env, monkeypatch):
    monkeypatch.setenv("MCP_DEV_USER", "alex")
    bad = GOOD_PREP.replace("## Questions", "## Qs")
    with pytest.raises(ToolError, match="Questions"):
        call(_server(env), "save_prep", {"markdown": bad})
    assert not list((env.repo.root / "prep").glob("*.md"))


def test_save_prep_commits_a_valid_file_once(env, monkeypatch):
    monkeypatch.setenv("MCP_DEV_USER", "alex")
    server = _server(env)
    out = call_json(server, "save_prep", {"markdown": GOOD_PREP})
    assert out["prep_file"] == "prep/2026-10-02_jane-doe.md"
    assert (env.repo.root / out["prep_file"]).read_text() == GOOD_PREP
    assert "warning" not in out
    with pytest.raises(ToolError, match="already exists"):
        call(server, "save_prep", {"markdown": GOOD_PREP})


# ---------- ask ----------

def test_ask_passes_filters_and_reports_coverage(env, ask_calls):
    out = call_json(_server(env), "ask", {"question": "What do carriers need?", "seat": "risk manager (insured)",
                                          "since": "2026-09-01"})
    assert ask_calls == [{"question": "What do carriers need?", "since": "2026-09-01", "seat": "risk manager (insured)",
                          "deadline_seconds": mcp_mod.ASK_DEADLINE_SECONDS}]
    assert out["coverage"] == "read 2/2 digests"
    assert out["answer"].startswith("Carriers")
    assert [c["verified"] for c in out["citations"]] == [True, False]


def test_ask_time_limit_fits_inside_the_tightest_client_timeout(env, ask_calls, monkeypatch):
    """claude.ai and Desktop give up on a tool call after 240 seconds (docs/research/2026-09-27_ai-clients.md),
    tighter than Cloud Run's 300. After the limit ask still checks citations against transcripts and encodes
    the result, so the limit leaves room for that: the out-of-time answer with its coverage must arrive
    before claude.ai stops waiting."""
    assert 0 < mcp_mod.ASK_DEADLINE_SECONDS <= 210
    env_example = (ROOT / ".env.example").read_text(encoding="utf-8")
    assert f"# MCP_ASK_DEADLINE_SECONDS={mcp_mod.ASK_DEADLINE_SECONDS:g}\n" in env_example
    assert f"default {mcp_mod.ASK_DEADLINE_SECONDS:g}" in env_example
    monkeypatch.setenv("MCP_ASK_DEADLINE_SECONDS", "120")
    call_json(_server(env), "ask", {"question": "Q?"})
    assert ask_calls[-1]["deadline_seconds"] == 120.0
    monkeypatch.setenv("MCP_ASK_DEADLINE_SECONDS", "soon")
    call_json(_server(env), "ask", {"question": "Q?"})
    assert ask_calls[-1]["deadline_seconds"] == mcp_mod.ASK_DEADLINE_SECONDS


# ---------- request_change ----------

def test_request_change_writes_a_valid_request_and_enqueues(env, monkeypatch):
    monkeypatch.setenv("MCP_DEV_USER", "sam")
    out = call_json(_server(env), "request_change", {
        "action": "add",
        "statement": "Weekly auto liability claims are a fleet's highest exposure, an owned pain",
        "scope": {"party": "insureds with truck fleets"},
        "source": {"call_id": "2026-10-01_wade-pruitt",
                   "quote": "commercial auto we're running maybe 3 or 4 a week so, call it 150 to 200 a "
                            "year. That's where the real dollars are"},
    })
    rel = out["request_file"]
    assert rel == "requests/2026-10-01_sam_weekly-auto-liability-claims-are-a.yml"
    assert out["queued"] is True
    data = yaml.safe_load((env.repo.root / rel).read_text())
    assert data == {
        "action": "add", "target": None, "targets": [], "statements": [],
        "statement": "Weekly auto liability claims are a fleet's highest exposure, an owned pain",
        "scope": {"party": "insureds with truck fleets"},
        "source": {"call_id": "2026-10-01_wade-pruitt", "quote": Q_AUTO},   # stored as the raw transcript text
        "requested_by": "sam", "reason": None,
        "requested_at": "2026-10-01T09:00:00-07:00",    # makes each request's blob sha, and so its ref, new
    }
    tasks = env.queue.of("apply_belief_change")
    assert len(tasks) == 1 and tasks[0]["payload"]["request_file"] == rel
    assert tasks[0]["name"]


def test_request_change_same_name_gets_a_suffix(env, monkeypatch):
    monkeypatch.setenv("MCP_DEV_USER", "alex")
    server = _server(env)
    args = {"action": "keep", "target": "B-003", "reason": "TPA view, not staff adjuster"}
    first = call_json(server, "request_change", args)["request_file"]
    second = call_json(server, "request_change", args)["request_file"]
    assert first == "requests/2026-10-01_alex_keep-b-003.yml"
    assert second == "requests/2026-10-01_alex_keep-b-003-2.yml"


def test_request_change_reused_file_name_still_queues(env, monkeypatch):
    """Once the processor has applied and deleted a request file, the next request of the same kind on the
    same target that day gets the same file name. Its task name must still be new: Cloud Tasks (and
    RecordingQueue) refuse a reused name for about an hour, and the tool would claim it was queued."""
    monkeypatch.setenv("MCP_DEV_USER", "alex")
    server = _server(env)
    first = call_json(server, "request_change", {"action": "dismiss_verdict", "target": "B-001",
                                                  "source": {"call_id": "2026-10-01_wade-pruitt",
                                                             "quote": Q_NOT_SO_MUCH}})
    (env.repo.root / first["request_file"]).unlink()         # applied and deleted by the processor
    second = call_json(server, "request_change", {"action": "dismiss_verdict", "target": "B-001",
                                                   "source": {"call_id": "2026-10-01_wade-pruitt",
                                                              "quote": Q_LYTX}})
    assert first["request_file"] == second["request_file"] == "requests/2026-10-01_alex_dismiss-verdict-b-001.yml"
    tasks = env.queue.of("apply_belief_change")
    assert len(tasks) == 2 and tasks[0]["name"] != tasks[1]["name"]
    assert first["queued"] is True and second["queued"] is True


def test_request_change_repeated_after_apply_is_applied_again(env, monkeypatch):
    """The processor's idempotency key is `requests/<file>@<blob sha>`. A request identical to one already
    applied and deleted that day gets the same file name, so its content must differ or the processor
    skips it as recorded while the tool has said `queued: true`. Reword B-003 to X, to Y, then to X again:
    all three apply, each posts its own notice, and B-003 ends at X."""
    from datetime import datetime, timedelta
    from zoneinfo import ZoneInfo
    monkeypatch.delitem(sys.modules, "nua.apply")
    apply = pytest.importorskip("nua.apply")
    monkeypatch.setattr(apply, "_regenerate", lambda ctx: [])
    from nua.beliefs import parse_beliefs
    monkeypatch.setenv("MCP_DEV_USER", "sam")
    server = _server(env)
    start = datetime(2026, 10, 1, 9, 0, tzinfo=ZoneInfo("America/Los_Angeles"))
    x = "Adjusters lose real time scrubbing long footage only on store theft claims"
    y = "Adjusters never scrub long footage"
    rels, outcomes = [], []
    for n, statement in enumerate([x, y, x]):
        env.now = lambda n=n: start + timedelta(minutes=n)
        out = call_json(server, "request_change", {"action": "reword", "target": "B-003", "statement": statement})
        assert out["queued"] is True
        rels.append(out["request_file"])
        result = apply.apply_task(env, {"request_file": out["request_file"], "via": "agent"})
        outcomes.append(result["outcome"])
        assert not (env.repo.root / out["request_file"]).exists()
    assert rels[0] == rels[2]                       # the same file name, reused after the first was deleted
    assert outcomes == ["applied", "applied", "applied"]
    entry = parse_beliefs((env.repo.root / "beliefs.md").read_text(encoding="utf-8")).get("B-003")
    assert entry.statement == x
    refs = [h.ref for h in entry.history if h.action == "reword"]
    assert len(refs) == 3 and len(set(refs)) == 3
    notices = [m for m in env.slack.live() if (m["metadata"] or {}).get("event_type") == "nua_ref"]
    assert len(notices) == 3


def test_request_change_refused_by_the_queue_says_not_queued(env, monkeypatch):
    monkeypatch.setenv("MCP_DEV_USER", "alex")
    monkeypatch.setattr(env.queue, "enqueue", lambda *a, **k: False)    # name taken (Cloud Tasks AlreadyExists)
    out = call_json(_server(env), "request_change", {"action": "keep", "target": "B-003"})
    assert out["queued"] is False
    assert "not queued" in out["next"] and "sweep" in out["next"]
    assert (env.repo.root / out["request_file"]).exists()


def test_request_change_split_and_merge_and_dismiss(env, monkeypatch):
    monkeypatch.setenv("MCP_DEV_USER", "jordan")
    server = _server(env)
    split = call_json(server, "request_change", {"action": "split", "target": "B-003", "statements": [
        {"statement": "Staff adjusters scrub long store footage", "scope": {"party": "carrier"}},
        {"statement": "Independent adjusters get clips under a minute",
         "scope": {"party": "independent adjusting firm"}},
    ]})
    data = yaml.safe_load((env.repo.root / split["request_file"]).read_text())
    assert data["statements"][1]["scope"] == {"party": "independent adjusting firm"}

    merge = call_json(server, "request_change", {"action": "merge", "targets": ["B-001", "B-004"],
                                                 "statement": "Outside adjusters rarely need the footage"})
    assert yaml.safe_load((env.repo.root / merge["request_file"]).read_text())["targets"] == ["B-001", "B-004"]

    dismiss = call_json(server, "request_change", {"action": "dismiss_verdict", "target": "B-001",
                                                   "source": {"call_id": "2026-10-01_wade-pruitt",
                                                              "quote": Q_NOT_SO_MUCH},
                                                   "reason": "answered a different question"})
    data = yaml.safe_load((env.repo.root / dismiss["request_file"]).read_text())
    assert data["source"] == {"call_id": "2026-10-01_wade-pruitt", "quote": Q_NOT_SO_MUCH}
    assert "verdict" not in data
    assert len(env.queue.of("apply_belief_change")) == 3

    one = call_json(server, "request_change", {"action": "dismiss_verdict", "target": "B-001", "verdict": "Contradicts",
                                               "source": {"call_id": "2026-10-01_wade-pruitt",
                                                          "quote": Q_NOT_SO_MUCH}})
    assert yaml.safe_load((env.repo.root / one["request_file"]).read_text())["verdict"] == "contradicts"


@pytest.mark.parametrize("args, message", [
    ({"action": "launch"}, "action must be one of"),
    ({"action": "retire", "target": "B-099"}, "unknown target B-099"),
    ({"action": "retire"}, "target is required"),
    ({"action": "add"}, "needs a statement or a source"),
    ({"action": "reword", "target": "B-001"}, "reword needs a new statement or scope"),
    ({"action": "dismiss_verdict", "target": "B-003"}, "source is required for dismiss_verdict"),
    ({"action": "dismiss_verdict", "target": "B-001", "verdict": "wrong",
      "source": {"call_id": "2026-10-01_wade-pruitt", "quote": Q_NOT_SO_MUCH}}, "verdict must be one of"),
    ({"action": "retire", "target": "B-004", "verdict": "supports"}, "verdict is only used by dismiss_verdict"),
    ({"action": "retire", "target": "B-004", "statement": "x"}, "statement is not used by retire"),
    ({"action": "add", "statement": "x", "source": {"call_id": "2026-10-01_wade-pruitt", "quote": NOTES_ONLY}},
     "not found in call 2026-10-01_wade-pruitt"),
    ({"action": "add", "statement": "x", "source": {"call_id": "2026-01-01_nobody", "quote": "hi"}},
     "no digest for call 2026-01-01_nobody"),
    ({"action": "add", "statement": "x", "scope": {"planet": "mars"},
      "source": {"call_id": "2026-10-01_wade-pruitt", "quote": Q_AUTO}}, "scope keys"),
    ({"action": "promote", "target": "B-001"}, "promote takes a Watching item"),
    ({"action": "watch", "statement": "x", "source": {"call_id": "2026-09-30_viktor-lang", "quote": Q_COPILOT}},
     "already backs B-004"),
    ({"action": "merge", "targets": ["B-001"], "statement": "x"}, "at least two"),
    ({"action": "split", "target": "B-003", "statements": [{"statement": "only one"}]}, "at least two"),
])
def test_request_change_rejects_invalid_requests(env, monkeypatch, args, message):
    monkeypatch.setenv("MCP_DEV_USER", "alex")
    with pytest.raises(ToolError, match=message):
        call(_server(env), "request_change", args)
    assert not list((env.repo.root / "requests").glob("*.yml"))
    assert env.queue.of("apply_belief_change") == []


def test_write_tools_need_an_identity_when_sign_in_is_off(env):
    with pytest.raises(ToolError, match="MCP_DEV_USER"):
        call(_server(env), "request_change", {"action": "retire", "target": "B-004"})


def test_request_text_has_em_dashes_removed_but_quotes_kept(env, monkeypatch):
    monkeypatch.setenv("MCP_DEV_USER", "alex")
    out = call_json(_server(env), "request_change", {"action": "reword", "target": "B-001",
                                                     "statement": "Small fleets \u2014 food distributors \u2014 keep dashcam footage"})
    data = yaml.safe_load((env.repo.root / out["request_file"]).read_text())
    assert "\u2014" not in data["statement"]


# ---------- result size ----------

def test_results_are_capped(env, monkeypatch):
    monkeypatch.setattr(mcp_mod, "MAX_RESULT_CHARS", 2_000)
    (env.repo.root / "findings.md").write_text("- a finding line\n" * 1_000, encoding="utf-8")
    server = _server(env)
    out = call(server, "get_findings")
    assert len(out) <= 2_000 and "Truncated" in out
    for i in range(60):
        write_digest_row = Digest(call_id=f"2026-08-{i % 28 + 1:02d}_person-{i}", transcript=f"transcripts/p{i}.md",
                                  status="digested", origin="backfill", date=f"2026-08-{i % 28 + 1:02d}",
                                  contact=f"Person {i}", firm="Firm", seat="other")
        from nua.ports import RepoFS
        write_digest(RepoFS(env.repo.root), write_digest_row)
    rows = call(server, "list_calls")
    assert len(rows) <= 2_000
    data = json.loads(rows)
    assert data["truncated"] and len(data["rows"]) < data["matched"]


# ---------- auth from env ----------

def test_auth_from_env(monkeypatch):
    assert mcp_mod.auth_from_env({"MCP_AUTH": "none"}) is None
    assert mcp_mod.auth_from_env({"MCP_AUTH": " None "}) is None
    with pytest.raises(ValueError, match="MCP_AUTH"):
        mcp_mod.auth_from_env({"MCP_AUTH": "okta"})
    with pytest.raises(ValueError, match="MCP_OAUTH_CLIENT_ID"):
        mcp_mod.auth_from_env({"MCP_AUTH": "github"})

    seen = {}

    class FakeProvider:
        def __init__(self, **kw):
            seen.update(kw)

    import fastmcp.server.auth.providers.github as gh
    import fastmcp.server.auth.providers.google as gg
    monkeypatch.setattr(gh, "GitHubProvider", FakeProvider)
    monkeypatch.setattr(gg, "GoogleProvider", FakeProvider)
    env = {"MCP_AUTH": "github", "MCP_OAUTH_CLIENT_ID": "id", "MCP_OAUTH_CLIENT_SECRET": "secret",
           "MCP_BASE_URL": "https://mcp.example.run.app"}
    assert isinstance(mcp_mod.auth_from_env(env), FakeProvider)
    assert seen["client_id"] == "id" and seen["base_url"] == "https://mcp.example.run.app"
    # Only the public login is used, so GitHub is asked for read:user, not FastMCP's default "user"
    # (read/write to the profile, private emails and follows).
    assert seen["required_scopes"] == ["read:user"]
    seen.clear()
    assert isinstance(mcp_mod.auth_from_env({**env, "MCP_AUTH": "google"}), FakeProvider)
    assert "email" in seen["required_scopes"]


@pytest.mark.parametrize("env_vars", [{}, {"MCP_AUTH": ""}, {"MCP_AUTH": "  "}, {"MCP_AUTH": "off"}])
def test_sign_in_fails_closed_unless_explicitly_off(env_vars):
    """nua-mcp is public (allUsers invoker). A revision deployed without MCP_AUTH must refuse to start,
    not serve every digest and transcript to anyone. Only an explicit MCP_AUTH=none turns sign-in off."""
    with pytest.raises(ValueError, match="MCP_AUTH"):
        mcp_mod.auth_from_env(env_vars)


def test_create_app_refuses_to_start_without_mcp_auth(env, monkeypatch):
    fake_settings = types.ModuleType("nua.settings")
    fake_settings.build_ctx = lambda role: env
    monkeypatch.setitem(sys.modules, "nua.settings", fake_settings)
    monkeypatch.delenv("MCP_AUTH", raising=False)
    with pytest.raises(ValueError, match="MCP_AUTH"):
        mcp_mod.create_app()


def test_env_example_turns_sign_in_off_for_local_runs():
    lines = (ROOT / ".env.example").read_text(encoding="utf-8").splitlines()
    assert "MCP_AUTH=none" in lines


def test_sign_in_state_lives_under_fastmcp_home(tmp_path, monkeypatch, caplog):
    """Client registrations and token mappings live in FastMCP's store under FASTMCP_HOME. On Cloud Run
    that must be the Cloud Storage volume infra/terraform/mcp.tf mounts, or every restart signs everyone out."""
    import fastmcp
    monkeypatch.setattr(fastmcp.settings, "home", tmp_path / "oauth")
    env = {"MCP_AUTH": "github", "MCP_OAUTH_CLIENT_ID": "id", "MCP_OAUTH_CLIENT_SECRET": "secret",
           "MCP_BASE_URL": "https://mcp.example.run.app", "MCP_JWT_SIGNING_KEY": "k" * 40}
    with caplog.at_level("WARNING", logger="nua.mcp"):
        mcp_mod.auth_from_env(env)
    assert list((tmp_path / "oauth" / "oauth-proxy").iterdir())
    assert "FASTMCP_HOME" in caplog.text               # no durable store configured: say so
    caplog.clear()
    with caplog.at_level("WARNING", logger="nua.mcp"):
        mcp_mod.auth_from_env({**env, "FASTMCP_HOME": str(tmp_path / "oauth")})
    assert "FASTMCP_HOME" not in caplog.text



def test_request_change_runs_the_processor_check(env, monkeypatch):
    monkeypatch.setenv("MCP_DEV_USER", "alex")
    with pytest.raises(ToolError, match="the processor rejects this shape"):
        call(_server(env), "request_change", {"action": "retire", "target": "B-004", "reason": "processor says no"})
    assert not list((env.repo.root / "requests").glob("*.yml"))


def test_committed_requests_load_in_the_real_processor(env, monkeypatch):
    """Integration check against nua.apply when it is present: every file the tool writes loads."""
    monkeypatch.delitem(sys.modules, "nua.apply")
    apply = pytest.importorskip("nua.apply")
    monkeypatch.setenv("MCP_DEV_USER", "sam")
    server = _server(env)
    requests = [
        {"action": "add", "statement": "Auto liability exposure is an owned pain",
         "source": {"call_id": "2026-10-01_wade-pruitt", "quote": Q_AUTO}},
        {"action": "watch", "source": {"call_id": "2026-10-01_wade-pruitt", "quote": Q_ATTORNEY}},
        {"action": "promote", "target": "W-001"},
        {"action": "reword", "target": "B-001", "scope": {"line": "commercial auto", "party": "insured"}},
        {"action": "split", "target": "B-003", "statements": [{"statement": "a"}, {"statement": "b"}]},
        {"action": "merge", "targets": ["B-001", "B-004"]},
        {"action": "retire", "target": "B-004", "reason": "no support"},
        {"action": "keep", "target": "B-003"},
        {"action": "dismiss_verdict", "target": "B-001",
         "source": {"call_id": "2026-10-01_wade-pruitt", "quote": Q_NOT_SO_MUCH}},
        {"action": "dismiss_verdict", "target": "B-001", "verdict": "contradicts",
         "source": {"call_id": "2026-10-01_wade-pruitt", "quote": Q_NOT_SO_MUCH}},
        {"action": "add", "statement": "Two sources", "sources": [
            {"call_id": "2026-10-01_wade-pruitt", "quote": Q_AUTO},
            {"call_id": "2026-09-30_viktor-lang", "quote": Q_NEVER_MATTERS}]},
    ]
    for args in requests:
        rel = call_json(server, "request_change", args)["request_file"]
        req = apply.request_from_dict(yaml.safe_load((env.repo.root / rel).read_text()))
        assert req.action == args["action"] and req.requested_by == "sam"
    two = apply.request_from_dict(yaml.safe_load((env.repo.root / rel).read_text()))
    assert len(two.sources) == 2


def test_create_app_builds_the_http_app(env, monkeypatch):
    fake_settings = types.ModuleType("nua.settings")
    fake_settings.build_ctx = lambda role: env
    monkeypatch.setitem(sys.modules, "nua.settings", fake_settings)
    monkeypatch.setenv("MCP_AUTH", "none")
    app = mcp_mod.create_app()
    assert any(getattr(r, "path", None) == "/mcp" for r in app.routes)


def test_seat_filter_keeps_everything_under_a_matching_heading():
    text = ("# Open questions\n\n## claims adjuster (staff)\n\n- How long is store footage kept?\n"
            "- Who orders the footage?\n\n## underwriter\n\n- What data do you need?\n")
    out = mcp_mod._filter_markdown(text, "Claims Adjuster (Staff)")
    assert "## claims adjuster (staff)" in out and "Who orders the footage?" in out
    assert "underwriter" not in out and "What data do you need?" not in out
