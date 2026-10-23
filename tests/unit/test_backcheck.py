"""Tests for nua/backcheck.py: re-running verdicts on past digests for new entry versions. Offline."""
from __future__ import annotations

import re

import pytest

from nua import backcheck as bc
from nua import beliefs as bl
from nua import digest as dg
from nua.llm import FakeLLM, LLMError
from nua.types import BeliefCheck, Claim, Digest, Entry, HistoryLine, Scope

DARIUS = "2026-09-23_darius-kemp"
VIKTOR = "2026-09-30_viktor-lang"
STUB = "granola_internal"


def entry(eid: str, statement: str, ref: str, *, section: str = "beliefs", scope: Scope | None = None) -> Entry:
    action = "watch" if section == "watching" else "add"
    return Entry(id=eid, statement=statement, scope=scope or Scope(), section=section,
                 history=[HistoryLine(date="2026-09-28", action=action, who="Alex", via="slack", ref=ref)])


def claim(cid: str, text: str, quote: str, scope: Scope | None = None) -> Claim:
    return Claim(id=cid, text=text, quote=quote, turns=[1], offset=(0, len(quote)), speaker="Someone",
                 scope=scope or Scope())


SMALL = entry("B-001", "Small shops overwrite their camera footage within days", "ref-b1",
              scope=Scope(party="insured, small businesses"))
SCRUB = entry("B-002", "Scrubbing hours of store video for the incident is an unsolved pain", "ref-b2",
              scope=Scope(line="general liability"))
OLD = entry("B-003", "An entry that was retired", "ref-b3", section="retired")


def digests() -> list[Digest]:
    darius = Digest(call_id=DARIUS, transcript="transcripts/darius-kemp.md", status="digested", origin="backfill",
                   seat="claims adjuster (staff)", claims=[
                       claim("c01", "Stores send three hours of videos when seconds show the fall",
                             "a lot of these stores will just send over the whole afternoon, like three hours of "
                             "footage, and the fall is maybe twenty seconds of it."),
                       claim("c02", "Footage is overwritten months before the claim is reported",
                             "I wish they'd hang onto it longer because a lot of these claims don't get reported "
                             "to us until two or three months later."),
                   ],
                  belief_checks=[BeliefCheck(target="B-001", verdict="supports", claims=["c02"], decided_by="agree"),
                                 BeliefCheck(target="B-002", verdict="supports", claims=["c01"], decided_by="agree")],
                  checked={"B-001": "ref-b1", "B-002": "ref-older"})
    viktor = Digest(call_id=VIKTOR, transcript="transcripts/viktor-lang.md", status="digested", origin="backfill",
                     seat="independent adjuster / TPA", claims=[
                         claim("c01", "Viktor handles only commercial claims", "I only handle commercial."),
                     ])
    stub = Digest(call_id=STUB, transcript="transcripts/granola_internal.md", status="internal", origin="live",
                  reason="internal call")
    return [darius, viktor, stub]


def write_state(ctx, entries: list[Entry], ds: list[Digest]) -> None:
    ctx.repo.write("beliefs.md", bl.render_beliefs(bl.BeliefsDoc(entries=entries)))
    for d in ds:
        dg.write_digest(ctx.repo, d)


def read(ctx, call_id: str) -> Digest:
    return dg.read_digest(ctx.repo, dg.digest_path(call_id))


def responder(rules: list[tuple[str, str, str]], *, fail: bool = False):
    """Verdict replies: for each (target, text needle, verdict), link every claim in the prompt whose text has the needle."""
    def respond(system, prompt, schema):
        if fail:
            return LLMError("down")
        checks = []
        for target, needle, verdict in rules:
            if f"{target} (" not in prompt:
                continue
            for m in re.finditer(r"^(c\d+) \[[^\]]*\]: (.*)$", prompt, re.M):
                if needle in m.group(2):
                    checks.append({"target": target, "verdict": verdict, "claims": [m.group(1)], "reason": "r"})
        return {"belief_checks": checks, "question_coverage": [], "speakers": []}
    return respond


@pytest.fixture
def models(ctx, monkeypatch):
    """Both extractors answer from the same rules (so they agree); the arbiter must not be needed."""
    rules: list[tuple[str, str, str]] = []
    fakes = [FakeLLM(spec, responder(rules)) for spec in ctx.config.pipeline.extractors]
    for f in fakes:
        ctx.llms.register(f)
    ctx.llms.register(FakeLLM(ctx.config.pipeline.arbiter, lambda *a: AssertionError("arbiter not expected")))
    regenerated = []

    def fake_regenerate(c):
        regenerated.append(1)
        return []

    monkeypatch.setattr(bc, "_regenerate", fake_regenerate)
    return {"rules": rules, "fakes": fakes, "regenerated": regenerated}


# ---------- missing_versions ----------

def test_missing_versions_lists_digested_calls_per_current_entry():
    doc = bl.BeliefsDoc(entries=[SMALL, SCRUB, OLD])
    assert bc.missing_versions(digests(), doc) == {"B-001": [VIKTOR], "B-002": [DARIUS, VIKTOR]}


def test_missing_versions_is_empty_when_everything_is_checked():
    ds = digests()
    for d in ds[:2]:
        d.checked = {"B-001": "ref-b1", "B-002": "ref-b2"}
    assert bc.missing_versions(ds, bl.BeliefsDoc(entries=[SMALL, SCRUB])) == {}


# ---------- backcheck ----------

def test_backcheck_writes_verdicts_and_absorbs_every_pending_entry(ctx, models):
    write_state(ctx, [SMALL, SCRUB, OLD], digests())
    models["rules"] += [("B-002", "videos", "contradicts"), ("B-001", "overwrit", "supports")]

    out = bc.backcheck(ctx)

    assert out == {"entries": ["B-001", "B-002"], "digests_updated": 2}
    darius = read(ctx, DARIUS)
    by_target = {c.target: c for c in darius.belief_checks}
    assert set(by_target) == {"B-001", "B-002"}
    assert by_target["B-001"].source == "ingest"                       # B-001 was already current on Darius's call
    new = by_target["B-002"]
    assert (new.verdict, new.claims, new.source, new.decided_by) == ("contradicts", ["c01"], "backcheck", "agree")
    assert darius.checked == {"B-001": "ref-b1", "B-002": "ref-b2"}
    viktor = read(ctx, VIKTOR)
    assert viktor.belief_checks == [] and viktor.checked == {"B-001": "ref-b1", "B-002": "ref-b2"}
    assert read(ctx, STUB).checked == {}
    assert models["regenerated"] == [1]
    [commit] = ctx.remote.commits
    assert set(commit["paths"]) == {dg.digest_path(DARIUS), dg.digest_path(VIKTOR)}

    # one run covered both entries: one verdict call per model for the only digest with relevant claims
    assert [len(f.calls) for f in models["fakes"]] == [1, 1]
    prompt = models["fakes"][0].calls[0]["prompt"]
    assert "B-002 (" in prompt and "B-001 (" not in prompt             # Darius only missed B-002

    again = bc.backcheck(ctx)
    assert again == {"entries": [], "digests_updated": 0}
    assert [len(f.calls) for f in models["fakes"]] == [1, 1]
    assert len(ctx.remote.commits) == 1


def test_backcheck_drops_old_checks_for_the_entry(ctx, models):
    write_state(ctx, [SMALL, SCRUB], digests())
    models["rules"] += [("B-002", "nothing matches this", "supports")]
    bc.backcheck(ctx)
    darius = read(ctx, DARIUS)
    assert [c.target for c in darius.belief_checks] == ["B-001"]           # the old B-002 check is gone
    assert darius.checked["B-002"] == "ref-b2"


def test_backcheck_keeps_going_when_one_digest_fails(ctx, models, monkeypatch):
    ds = digests()
    ds[1].claims.append(claim("c02", "Video length decides review time", "videos"))
    write_state(ctx, [SMALL, SCRUB], ds)
    models["rules"] += [("B-002", "ideo", "supports")]
    real = bc._run_one

    def flaky(ctx_, job):
        if job.digest.call_id == VIKTOR:
            raise LLMError("every verdict model failed")
        return real(ctx_, job)

    monkeypatch.setattr(bc, "_run_one", flaky)
    with pytest.raises(LLMError):
        bc.backcheck(ctx)
    assert read(ctx, DARIUS).checked["B-002"] == "ref-b2"                 # finished work is kept and committed
    assert "B-002" not in read(ctx, VIKTOR).checked
    assert len(ctx.remote.commits) == 1
    doc = bl.parse_beliefs(ctx.repo.read("beliefs.md"))
    assert bc.missing_versions(dg.load_digests(ctx.repo), doc) == {"B-001": [VIKTOR], "B-002": [VIKTOR]}


def test_backcheck_with_nothing_pending_calls_no_model(ctx, models):
    ds = digests()
    for d in ds[:2]:
        d.checked = {"B-001": "ref-b1", "B-002": "ref-b2"}
    write_state(ctx, [SMALL, SCRUB], ds)
    assert bc.backcheck(ctx) == {"entries": [], "digests_updated": 0}
    assert all(not f.calls for f in models["fakes"]) and ctx.remote.commits == []
    assert models["regenerated"] == []


def test_backcheck_reads_the_transcript_for_context_when_present(ctx, models):
    from conftest import fixture_text
    ctx.repo.write("transcripts/darius-kemp.md", fixture_text("darius-kemp"))
    ds = digests()
    ds[0].claims[0].quotes[0].turns = [60]
    write_state(ctx, [SCRUB], ds[:1])
    bc.backcheck(ctx)
    prompt = models["fakes"][0].calls[0]["prompt"]
    assert "[T60 " in prompt                                            # surrounding turns from the transcript


def test_backcheck_does_not_redo_speakers_on_unnamed_calls(ctx, models):
    """Speakers were settled at ingest: a speaker disagreement must not reach the arbiter here."""
    from conftest import fixture_text
    ctx.repo.write("transcripts/viktor-lang.md", fixture_text("viktor-lang"))
    ds = digests()
    ds[1].claims[0].text = "Video length and commercial work steer the review"
    write_state(ctx, [SCRUB], ds[1:2])
    for n, fake in enumerate(models["fakes"]):
        base = fake.responder

        def with_speakers(system, prompt, schema, base=base, n=n):
            out = base(system, prompt, schema)
            out["speakers"] = [{"claim": "c01", "speaker": f"Person {n}"}]
            return out
        fake.responder = with_speakers
    models["rules"] += [("B-002", "ideo", "supports")]
    assert bc.backcheck(ctx) == {"entries": ["B-002"], "digests_updated": 1}
    [check] = read(ctx, VIKTOR).belief_checks
    assert (check.target, check.decided_by) == ("B-002", "agree")


# ---------- pre-filter ----------

def test_relevant_claims_keeps_claims_sharing_a_term_with_the_entry():
    claims = [claim("c01", "Store video runs for hours before the fall", "like three hours of footage"),
              claim("c02", "The founder grew up in Ohio", "I grew up in Ohio"),
              claim("c03", "Something unrelated", "quote", scope=Scope(line="general liability"))]
    assert [c.id for c in bc.relevant_claims(SCRUB, claims)] == ["c01", "c03"]
    assert [c.id for c in bc.relevant_claims(SCRUB, claims, keep={"c02"})] == ["c01", "c02", "c03"]
    empty = entry("B-009", "a b", "r")
    assert len(bc.relevant_claims(empty, claims)) == 3                  # nothing to match on: keep everything


def test_relevant_claims_match_word_forms_across_lengths():
    """Four-letter words meet their longer forms: fund/funding, rate/rated/rating, bind/binding, claim/claimed."""
    rating = entry("B-009", "Carriers require a rating before binding a new insured", "r")
    claims = [claim("c01", "Their broker would not bind the risk until it was rated",
                    "they would not bind us until the account got rated"),
              claim("c02", "The founder grew up in Ohio", "I grew up in Ohio")]
    assert [c.id for c in bc.relevant_claims(rating, claims)] == ["c01"]

    def meets(a: str, b: str) -> bool:
        return bool(bc._shares_term(bc._terms(a), bc._terms(b)))

    for a, b in [("fund", "funding"), ("rate", "rated"), ("rate", "rating"), ("rated", "rating"),
                 ("claim", "claimed"), ("bind", "binding"), ("insure", "insurer"), ("insure", "insuring"),
                 ("insurer", "insuring"), ("loss", "losses"), ("policy", "policies"), ("price", "pricing"),
                 ("claim", "claims"), ("adjust", "adjusting"), ("insure", "insurance"), ("insuring", "insurance")]:
        assert meets(a, b) and meets(b, a), (a, b)
    for a, b in [("rate", "rather"), ("fund", "fun"), ("claim", "clamp"), ("bind", "blind")]:
        assert not meets(a, b), (a, b)


# ---------- cost cap and batches ----------

def many_digests(n: int) -> list[Digest]:
    return [Digest(call_id=f"2026-09-{i + 1:02d}_guest-{i}", transcript=f"transcripts/g{i}.md", status="digested",
                   origin="backfill", seat="claims adjuster (staff)",
                   claims=[claim("c01", "Small shops overwrite their own footage", "we overwrite our footage")])
            for i in range(n)]


def charging(ctx, models, usd: float):
    """Make every verdict call charge `usd` to the task's cost tracker, as real providers do."""
    for fake in models["fakes"]:
        base = fake.responder

        def charged(system, prompt, schema, base=base):
            ctx.llms.cost.add(usd)
            return base(system, prompt, schema)
        fake.responder = charged


def test_cost_cap_commits_finished_digests_stops_new_calls_and_reraises(ctx, models, monkeypatch):
    from nua.llm import CostCapExceeded, CostTracker
    monkeypatch.setattr(bc, "CONCURRENCY", 1)             # one digest at a time, so the count is exact
    write_state(ctx, [SMALL], many_digests(10))
    models["rules"] += [("B-001", "overwrit", "supports")]
    ctx.llms.cost = CostTracker(cap_usd=6.0)
    charging(ctx, models, 0.5)                             # the 13th call goes over the cap: 6 digests finish

    with pytest.raises(CostCapExceeded):
        bc.backcheck(ctx)

    calls = sum(len(f.calls) for f in models["fakes"])
    assert calls <= 14                                     # queued digests made no paid call
    done = [d for d in dg.load_digests(ctx.repo) if d.checked.get("B-001") == "ref-b1"]
    assert len(done) == 6
    assert all(c.source == "backcheck" for d in done for c in d.belief_checks)
    [commit] = ctx.remote.commits
    assert len([p for p in commit["paths"] if p.startswith("digests/")]) == 6

    # a Re-run (fresh cost tracker) only redoes what is still missing
    ctx.llms.cost = CostTracker(cap_usd=100.0)
    out = bc.backcheck(ctx)
    assert out == {"entries": ["B-001"], "digests_updated": 4}
    assert sum(len(f.calls) for f in models["fakes"]) == calls + 8
    assert all(d.checked.get("B-001") == "ref-b1" for d in dg.load_digests(ctx.repo))


def test_digests_with_model_calls_commit_in_batches(ctx, models, monkeypatch):
    monkeypatch.setattr(bc, "BATCH_SIZE", 2)
    ds = many_digests(5)
    ds.append(Digest(call_id="2026-09-30_unrelated", transcript="transcripts/u.md", status="digested",
                     origin="backfill", claims=[claim("c01", "The founder grew up in Ohio", "I grew up in Ohio")]))
    write_state(ctx, [SMALL], ds)
    models["rules"] += [("B-001", "overwrit", "supports")]
    assert bc.backcheck(ctx) == {"entries": ["B-001"], "digests_updated": 6}
    sizes = [len([p for p in c["paths"] if p.startswith("digests/")]) for c in ctx.remote.commits]
    assert sizes == [3, 2, 1]                               # the digest with no relevant claim rides in the first


# ---------- debounce ----------

def test_backcheck_waits_for_a_newer_change_in_the_same_session(ctx, models):
    from datetime import timedelta
    write_state(ctx, [SMALL, SCRUB], digests())
    models["rules"] += [("B-002", "videos", "contradicts")]
    ctx.repo.write(bc.STATE_FILE, bc.change_state("ref-b2", ctx.now() - timedelta(seconds=60)))

    out = bc.backcheck(ctx, {"after": "ref-b1"})           # scheduled after B-001; B-002 came a minute later
    assert out == {"entries": [], "digests_updated": 0, "deferred_to": "ref-b2"}
    assert all(not f.calls for f in models["fakes"]) and ctx.remote.commits == []
    [task] = ctx.queue.of("backcheck")
    assert task["payload"] == {"after": "ref-b2"} and task["name"] == bc.backcheck_task_name("ref-b2")
    assert task["delay_seconds"] == 240

    # the backcheck scheduled after the latest change runs
    assert bc.backcheck(ctx, {"after": "ref-b2"})["entries"] == ["B-001", "B-002"]


def test_backcheck_runs_once_the_newer_change_is_old_enough(ctx, models):
    from datetime import timedelta
    write_state(ctx, [SMALL, SCRUB], digests())
    ctx.repo.write(bc.STATE_FILE, bc.change_state("ref-b2", ctx.now() - timedelta(seconds=600)))
    assert bc.backcheck(ctx, {"after": "ref-b1"})["entries"] == ["B-001", "B-002"]
    assert ctx.queue.of("backcheck") == []
    # the nightly sweep's backcheck carries no `after` and never waits
    ctx.repo.write(bc.STATE_FILE, bc.change_state("ref-b9", ctx.now()))
    assert bc.backcheck(ctx, {}) == {"entries": [], "digests_updated": 0}
