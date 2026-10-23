"""Tests for nua/apply.py: belief changes from request files, Slack clicks and roll-up proposals. Offline."""
from __future__ import annotations

import hashlib

import pytest
import yaml

from conftest import ROOT, fixture_text
from nua import apply as ap
from nua import beliefs as bl
from nua import digest as dg
from nua.parse import parse
from nua.ports import NullRemote
from nua.types import Claim, Digest, Scope
from nua.verify import QuoteIndex

DARIUS = "2026-09-23_darius-kemp"
VIKTOR = "2026-09-30_viktor-lang"
TWO_HOUR = ("a lot of these stores will just send over the whole afternoon, like three hours of footage, and the "
            "fall is maybe twenty seconds of it.")
ATTORNEY = "The minute a lawyer is retained, the claim costs more, period."
AI_RULES = ("But anything outside the company is off limits for us, because compliance is terrified of claimant "
            "data leaving the building.")
COPILOT = ("I'll have Copilot or ChatGPT pull it out of the policy for me, and every so often they make something "
           "up that just isn't in there.")
CALLS = "#discovery-calls"
EM_DASH = chr(0x2014)


# ---------- helpers ----------

def make_digest(ctx, call_id: str, fixture: str, claims: list[tuple[str, str]], seat: str, speaker: str) -> Digest:
    """Copy a fixture transcript in and write a digest whose claims sit at the quotes' real offsets."""
    rel = f"transcripts/{fixture}.md"
    raw = fixture_text(fixture)
    ctx.repo.write(rel, raw)
    index = QuoteIndex(parse(rel, raw, commit_date="2026-10-04"))
    out = []
    for n, (text, quote) in enumerate(claims, 1):
        m = index.find(quote)
        assert m is not None, quote
        out.append(Claim(id=f"c{n:02d}", text=text, quote=m.raw_text, turns=m.turns, offset=(m.start, m.end),
                         speaker=speaker, scope=Scope(line="general liability", seat=seat)))
    d = Digest(call_id=call_id, transcript=rel, status="digested", origin="backfill", date=call_id.split("_")[0],
               contact=call_id.split("_")[1], seat=seat, prompt_version="v1", claims=out)
    dg.write_digest(ctx.repo, d)
    return d


def span_of(d: Digest, quote: str) -> str:
    c = next(c for c in d.claims if c.quote == quote)
    return f"{c.offset[0]}-{c.offset[1]}"


def blob_sha(text: str) -> str:
    data = text.encode("utf-8")
    return hashlib.sha1(b"blob %d\x00" % len(data) + data).hexdigest()


def write_request(ctx, stem: str, data: dict) -> str:
    rel = f"requests/{stem}.yml"
    ctx.repo.write(rel, yaml.safe_dump(data, sort_keys=False))
    return rel


def doc(ctx) -> bl.BeliefsDoc:
    return bl.parse_beliefs(ctx.repo.read("beliefs.md"))


def notices(ctx) -> list[dict]:
    return [m for m in ctx.slack.live(CALLS) if m["thread_ts"] is None and (m["metadata"] or {}).get("event_type") == "nua_ref"]


def load_yaml(ctx, rel: str):
    return yaml.safe_load(ctx.repo.read(rel)) or {}


@pytest.fixture
def regenerated(monkeypatch):
    """Stub the index regeneration (Task 11) and record each call."""
    calls = []

    def fake(ctx):
        calls.append(1)
        ctx.repo.write("beliefs-status.md", "status\n")
        return ["beliefs-status.md"]

    monkeypatch.setattr(ap, "_regenerate", fake)
    return calls


@pytest.fixture
def base(ctx, regenerated):
    """Golden beliefs plus digests for Darius and Viktor."""
    ctx.repo.write("beliefs.md", (ROOT / "tests" / "golden" / "beliefs.md").read_text(encoding="utf-8"))
    darius = make_digest(ctx, DARIUS, "darius-kemp", [
        ("Stores send hours of footage when only seconds show the fall, an unsolved review problem.", TWO_HOUR),
        ("Once an attorney is retained a claim costs more.", ATTORNEY),
        ("The carrier bars outside AI tools because compliance fears data leaving the company.", AI_RULES),
    ], seat="claims adjuster (staff)", speaker="Darius Kemp")
    viktor = make_digest(ctx, VIKTOR, "viktor-lang", [
        ("General AI tools make up details in claim files.", COPILOT),
    ], seat="independent adjuster / TPA", speaker="Viktor Lang")
    return {"darius": darius, "viktor": viktor}


def thread_item(ctx, block_id: str, text: str = "a finding") -> dict:
    """Post a brief-like parent and a thread reply holding one item block; return the slack_item."""
    parent = ctx.slack.post(CALLS, [], "brief")
    blocks = [
        {"type": "section", "block_id": "findings:title", "text": {"type": "mrkdwn", "text": "*New findings*"}},
        {"type": "section", "block_id": block_id, "text": {"type": "mrkdwn", "text": text},
         "accessory": {"type": "overflow", "action_id": "finding_menu", "options": []}},
    ]
    ts = ctx.slack.post(CALLS, blocks, "findings", thread_ts=parent)
    return {"channel": CALLS, "ts": ts, "block_id": block_id, "thread_ts": parent}


# ---------- request files ----------

def test_add_from_a_request_file(ctx, base, regenerated):
    rel = write_request(ctx, "2026-10-06_sam_footage-scrubbing", {
        "action": "add", "target": None, "targets": [], "statements": [],
        "statement": "Scrubbing long store video for the incident is an unsolved, owned pain",
        "scope": {"party": "carriers with many small commercial insureds"},
        "source": {"call_id": DARIUS, "quote": "A lot of these stores will just send over the whole afternoon, like "
                                              "three hours of footage and the fall is maybe twenty seconds of it."},
        "requested_by": "sam", "reason": None})
    ref = f"{rel}@{blob_sha(ctx.repo.read(rel))}"

    out = ap.apply_task(ctx, {"request_file": rel, "via": "agent"})

    assert out["outcome"] == "applied" and out["entry_ids"] == ["B-005"]
    entry = doc(ctx).get("B-005")
    assert entry.section == "beliefs"
    assert entry.statement == "Scrubbing long store video for the incident is an unsolved, owned pain"
    assert entry.scope == Scope(party="carriers with many small commercial insureds")
    [h] = entry.history
    assert (h.date, h.action, h.who, h.via, h.ref) == ("2026-10-01", "add", "Sam", "agent", ref)
    assert h.sources == [(DARIUS, TWO_HOUR)]              # the raw transcript text, not the request's wording
    assert entry.version == ref
    assert not ctx.repo.exists(rel)
    [commit] = ctx.remote.commits
    assert set(commit["paths"]) == {"beliefs.md", rel, "beliefs-status.md", "config/backcheck.yml"}
    assert regenerated == [1]
    [task] = ctx.queue.of("backcheck")
    assert task["payload"] == {"after": ref} and task["delay_seconds"] == 300 and task["name"]
    # the debounce state names this change as the latest one, with when it was applied
    state = load_yaml(ctx, "config/backcheck.yml")
    assert state == {"last_change": {"ref": ref, "at": "2026-10-01T09:00:00-07:00"}}
    [notice] = notices(ctx)
    assert notice["text"].startswith("Sam added B-005 via agent")
    assert notice["metadata"] == {"event_type": "nua_ref", "event_payload": {"key": ref}}
    assert ap.ref_recorded(ctx, ref)


def test_load_request_returns_the_request_and_its_ref(ctx, base):
    rel = write_request(ctx, "r1", {"action": "keep", "target": "b-2", "requested_by": "alex",
                                    "reason": "one carrier, one claim"})
    req, ref = ap.load_request(ctx, rel)
    assert (req.action, req.target, req.requested_by, req.reason) == ("keep", "B-002", "alex", "one carrier, one claim")
    assert ref == f"{rel}@{blob_sha(ctx.repo.read(rel))}"


def test_load_request_rejects_bad_yaml_and_bad_shapes(ctx, base):
    ctx.repo.write("requests/bad.yml", "action: [add\n")
    with pytest.raises(ValueError):
        ap.load_request(ctx, "requests/bad.yml")
    for data in ({"action": "approve", "requested_by": "alex"},
                 {"action": "add", "requested_by": "alex"},
                 {"action": "merge", "targets": ["B-001"], "requested_by": "alex"},
                 {"action": "split", "target": "B-003", "statements": [{"statement": "one"}], "requested_by": "alex"},
                 {"action": "keep", "requested_by": "alex"},
                 {"action": "keep", "target": "B-002"}):
        rel = write_request(ctx, "shape", data)
        with pytest.raises(ValueError):
            ap.load_request(ctx, rel)


def test_invalid_request_is_deleted_and_answered(ctx, base):
    before = ctx.repo.read("beliefs.md")
    rel = write_request(ctx, "unknown-target", {"action": "retire", "target": "B-099", "requested_by": "jordan"})
    out = ap.apply_task(ctx, {"request": rel})
    assert out["outcome"] == "invalid" and "B-099" in out["message"]
    assert ctx.repo.read("beliefs.md") == before
    assert not ctx.repo.exists(rel)
    assert ctx.remote.commits[-1]["paths"] == [rel]
    [notice] = notices(ctx)
    assert "B-099" in notice["text"]

    ctx.repo.write("requests/bad.yml", "action: [add\n")
    out = ap.apply_task(ctx, {"request": "requests/bad.yml"})
    assert out["outcome"] == "invalid"
    assert not ctx.repo.exists("requests/bad.yml")
    assert ctx.queue.of("backcheck") == []


def test_a_quote_not_in_the_transcript_is_invalid(ctx, base):
    rel = write_request(ctx, "made-up", {"action": "add", "statement": "Adjusters love AI",
                                         "source": {"call_id": DARIUS, "quote": "we would pay anything for an AI tool"},
                                         "requested_by": "alex"})
    out = ap.apply_task(ctx, {"request": rel})
    assert out["outcome"] == "invalid" and "not found" in out["message"]
    assert doc(ctx).get("B-005") is None


def test_a_gone_request_file_is_skipped(ctx, base):
    out = ap.apply_task(ctx, {"request": "requests/gone.yml"})
    assert out["outcome"] == "skipped"
    assert ctx.remote.commits == [] and notices(ctx) == []


# ---------- duplicates ----------

def test_a_source_already_in_a_current_entry_is_a_duplicate(ctx, base):
    before = ctx.repo.read("beliefs.md")
    rel = write_request(ctx, "dup", {"action": "watch", "statement": "Outside AI tools misread claim files",
                                     "source": {"call_id": VIKTOR,
                                                "quote": "every so often they make something up that just "
                                                         "isn't in there"},
                                     "requested_by": "alex"})
    out = ap.apply_task(ctx, {"request": rel})
    assert out["outcome"] == "duplicate" and out["entry_ids"] == ["B-004"]
    assert "already exists as B-004" in out["message"]
    assert ctx.repo.read("beliefs.md") == before
    assert not ctx.repo.exists(rel)
    assert ctx.queue.of("backcheck") == []
    [notice] = notices(ctx)
    assert "already exists as B-004" in notice["text"]


def test_retired_sources_do_not_block(ctx, base):
    retire = ap.ChangeRequest(action="retire", requested_by="sam", target="B-004", reason="no hits")
    assert ap.apply_change(ctx, retire, ref="1.1:pressure:B-004", via="slack").outcome == "applied"
    assert doc(ctx).get("B-004").section == "retired"

    add = ap.ChangeRequest(action="watch", requested_by="alex", statement="Outside AI tools misread claim files",
                           sources=[(VIKTOR, COPILOT)])
    out = ap.apply_change(ctx, add, ref="1.2:finding:x", via="slack")
    assert out.outcome == "applied" and out.entry_ids == ["W-002"]


# ---------- Slack path ----------

def test_watch_from_a_finding_menu_uses_the_claim_and_updates_the_item(ctx, base):
    span = span_of(base["darius"], TWO_HOUR)
    item = thread_item(ctx, f"finding:{span}")
    req = ap.ChangeRequest(action="watch", requested_by="alex", sources=[(DARIUS, span)])
    out = ap.apply_change(ctx, req, ref=f"{item['ts']}:finding:{span}", via="slack", slack_item=item)

    assert out.outcome == "applied" and out.entry_ids == ["W-002"]
    entry = doc(ctx).get("W-002")
    assert entry.section == "watching"
    assert entry.statement == ("Stores send hours of footage when only seconds show the fall, "
                               "an unsolved review problem.")
    assert entry.scope == Scope(line="general liability", seat="claims adjuster (staff)")
    assert entry.history[0].sources == [(DARIUS, TWO_HOUR)]
    msg = next(m for m in ctx.slack.messages if m["ts"] == item["ts"])
    blocks = {b["block_id"]: b for b in msg["blocks"]}
    assert "accessory" not in blocks[f"finding:{span}"]
    assert "W-002" in blocks[f"finding:{span}:status"]["elements"][0]["text"]


def test_a_recorded_ref_skips_and_repairs_the_slack_side(ctx, base):
    span = span_of(base["darius"], TWO_HOUR)
    item = thread_item(ctx, f"finding:{span}")
    req = ap.ChangeRequest(action="add", requested_by="alex", statement="Footage review is a daily pain",
                           sources=[(DARIUS, span)])
    ref = f"{item['ts']}:finding:{span}"
    assert ap.apply_change(ctx, req, ref=ref, via="slack", slack_item=item).outcome == "applied"
    [first] = notices(ctx)
    ctx.slack.delete(CALLS, first["ts"])                      # the Slack side was lost
    commits = len(ctx.remote.commits)

    again = ap.apply_change(ctx, req, ref=ref, via="slack", slack_item=item)
    assert again.outcome == "skipped" and again.entry_ids == ["B-005"]
    assert len(ctx.remote.commits) == commits                 # nothing new to commit
    assert [e.id for e in doc(ctx).entries].count("B-005") == 1
    [repaired] = notices(ctx)
    assert repaired["text"] == first["text"] and repaired["ts"] != first["ts"]

    ap.apply_change(ctx, req, ref=ref, via="slack", slack_item=item)
    assert len(notices(ctx)) == 1                             # already there: not posted twice
    assert len(ctx.queue.of("backcheck")) == 1


def test_keep_writes_a_history_line_without_changing_the_version(ctx, base):
    version = doc(ctx).get("B-002").version
    req = ap.ChangeRequest(action="keep", requested_by="jordan", target="B-002", reason="slip and fall claims only")
    out = ap.apply_change(ctx, req, ref="1.3:pressure:B-002", via="slack")
    assert out.outcome == "applied" and out.entry_ids == ["B-002"]
    entry = doc(ctx).get("B-002")
    assert entry.history[-1].action == "keep" and entry.history[-1].reason == "slip and fall claims only"
    assert entry.version == version
    assert ctx.queue.of("backcheck") == []
    assert notices(ctx)[0]["text"].startswith("Jordan kept B-002")


def test_keep_is_for_beliefs_only(ctx, base):
    req = ap.ChangeRequest(action="keep", requested_by="jordan", target="W-001")
    assert ap.apply_change(ctx, req, ref="1.4:x", via="slack").outcome == "invalid"


def read_on(ctx, d: Digest, target: str, quote: str) -> None:
    """Give the claim holding `quote` a contradicts read on `target` (what a Wrong read dismisses)."""
    from nua.types import BeliefCheck
    claim = next(c for c in d.claims if c.quote == quote)
    d.belief_checks.append(BeliefCheck(target=target, verdict="contradicts", claims=[claim.id], decided_by="agree"))
    dg.write_digest(ctx.repo, d)


def test_dismiss_verdict_records_an_override(ctx, base):
    read_on(ctx, base["darius"], "B-003", ATTORNEY)
    span = span_of(base["darius"], ATTORNEY)
    req = ap.ChangeRequest(action="dismiss_verdict", requested_by="alex", target="B-003",
                           sources=[(DARIUS, span)], reason="about attorneys, not footage")
    ref = "1.5:verdict:B-003:1"
    out = ap.apply_change(ctx, req, ref=ref, via="slack")
    assert out.outcome == "applied" and out.entry_ids == ["B-003"]
    data = load_yaml(ctx, "config/verdict_overrides.yml")
    claim = next(c for c in base["darius"].claims if c.quote == ATTORNEY)
    assert data["overrides"] == [{"call_id": DARIUS, "target": "B-003", "quote": ATTORNEY,
                                  "offset": list(claim.offset), "version": doc(ctx).get("B-003").version,
                                  "who": "Alex", "reason": "about attorneys, not footage", "ref": ref}]
    assert ctx.repo.read("config/verdict_overrides.yml").startswith("# Processor-written")
    assert ap.ref_recorded(ctx, ref)
    assert ctx.queue.of("backcheck") == []
    assert "config/verdict_overrides.yml" in ctx.remote.commits[-1]["paths"]


def test_a_request_file_from_a_non_cofounder_is_invalid(ctx, base):
    req = ap.ChangeRequest(action="keep", requested_by="mallory", target="B-002")
    assert ap.apply_change(ctx, req, ref="requests/x.yml", via="agent").outcome == "invalid"


def test_a_slack_click_from_anyone_is_applied_under_their_handle(ctx, base):
    req = ap.ChangeRequest(action="keep", requested_by="maria", target="B-002")
    assert ap.apply_change(ctx, req, ref="1.6:x", via="slack").outcome == "applied"
    assert "keep by maria via slack" in ctx.repo.read("beliefs.md")


def test_a_slack_handle_matching_a_cofounder_shows_their_first_name(ctx, base):
    req = ap.ChangeRequest(action="keep", requested_by="alex", target="B-002")
    assert ap.apply_change(ctx, req, ref="1.6:x", via="slack").outcome == "applied"
    assert "keep by Alex via slack" in ctx.repo.read("beliefs.md")


# ---------- structural changes ----------

def test_promote_moves_the_watching_item_to_retired(ctx, base):
    watch = ap.ChangeRequest(action="watch", requested_by="alex", statement="Attorneys make liability claims costlier",
                             sources=[(DARIUS, ATTORNEY)])
    assert ap.apply_change(ctx, watch, ref="1.6:x", via="slack").entry_ids == ["W-002"]
    before = len(ctx.queue.of("backcheck"))
    req = ap.ChangeRequest(action="promote", requested_by="sam", target="W-002")
    out = ap.apply_change(ctx, req, ref="1.7:x", via="slack")
    assert out.outcome == "applied" and out.entry_ids == ["B-005", "W-002"]
    d = doc(ctx)
    new, old = d.get("B-005"), d.get("W-002")
    assert new.section == "beliefs" and new.statement == old.statement and new.scope == old.scope
    assert new.history[0].action == "promote" and new.history[0].text == "from W-002"
    assert new.history[0].sources == old.history[0].sources == [(DARIUS, ATTORNEY)]
    assert old.section == "retired"
    assert (old.history[-1].action, old.history[-1].text) == ("promoted", "to B-005")
    assert len(ctx.queue.of("backcheck")) == before + 1


def test_reword_changes_the_statement_and_version(ctx, base):
    req = ap.ChangeRequest(action="reword", requested_by="alex", target="B-001",
                           statement="Video footage is the strongest evidence on a liability claim")
    out = ap.apply_change(ctx, req, ref="1.8:x", via="slack")
    entry = doc(ctx).get("B-001")
    assert out.outcome == "applied"
    assert entry.statement == "Video footage is the strongest evidence on a liability claim"
    assert entry.version == "1.8:x"
    assert entry.scope.line == "general liability, commercial auto"      # kept when not given


def test_split_keeps_the_id_for_the_first_part(ctx, base):
    req = ap.ChangeRequest(action="split", requested_by="alex", target="B-003", statements=[
        {"statement": "Adjusters lose real time scrubbing store footage", "scope": Scope(line="general liability")},
        {"statement": "Adjusters lose real time scrubbing dashcam footage",
         "scope": Scope(line="commercial auto")},
    ])
    out = ap.apply_change(ctx, req, ref="1.9:pressure:B-003", via="slack")
    assert out.outcome == "applied" and out.entry_ids == ["B-003", "B-005"]
    d = doc(ctx)
    first, second = d.get("B-003"), d.get("B-005")
    assert first.statement == "Adjusters lose real time scrubbing store footage"
    assert (first.history[-1].action, first.history[-1].text) == ("split", "into B-003, B-005")
    assert second.section == "beliefs" and second.scope == Scope(line="commercial auto")
    assert (second.history[0].action, second.history[0].text) == ("split", "from B-003")
    assert first.version == second.version == "1.9:pressure:B-003"
    assert "split B-003 into B-003, B-005" in out.message


def test_merge_folds_the_others_into_the_first_belief(ctx, base):
    req = ap.ChangeRequest(action="merge", requested_by="sam", targets=["W-001", "B-002", "B-004"],
                           statement="Adjusters need a sanctioned tool to review footage")
    out = ap.apply_change(ctx, req, ref="2.0:x", via="slack")
    assert out.outcome == "applied" and out.entry_ids == ["B-002", "W-001", "B-004"]
    d = doc(ctx)
    kept, watched, gone = d.get("B-002"), d.get("W-001"), d.get("B-004")
    assert kept.section == "beliefs" and kept.statement == "Adjusters need a sanctioned tool to review footage"
    assert (kept.history[-1].action, kept.history[-1].text) == ("merge", "with W-001, B-004")
    assert gone.history[0].sources[0] in kept.history[-1].sources    # the evidence moves with the merge
    for old in (watched, gone):
        assert old.section == "retired" and (old.history[-1].action, old.history[-1].text) == ("merged", "into B-002")
    assert "merged W-001, B-004 into B-002" in out.message


def test_merge_needs_current_entries(ctx, base):
    ap.apply_change(ctx, ap.ChangeRequest(action="retire", requested_by="sam", target="B-004"),
                    ref="2.1:x", via="slack")
    req = ap.ChangeRequest(action="merge", requested_by="sam", targets=["B-001", "B-004"])
    assert ap.apply_change(ctx, req, ref="2.2:x", via="slack").outcome == "invalid"


# ---------- roll-up proposals ----------

def write_proposals(ctx, proposals: list[dict], **top) -> None:
    ctx.repo.write("config/proposals.yml", yaml.safe_dump({**top, "proposals": proposals}, sort_keys=False))


def test_stale_proposal_is_not_applied(ctx, base):
    before = ctx.repo.read("beliefs.md")
    write_proposals(ctx, [{"id": "p-01", "type": "reword", "statement": "Sharper B-002", "scope": None,
                           "sources": [], "target": "B-002", "target_version": "1790000000.000100:prop-99",
                           "status": "open"}])
    item = thread_item(ctx, "prop:p-01")
    req = ap.ChangeRequest(action="reword", requested_by="alex", proposal_id="p-01")
    out = ap.apply_change(ctx, req, ref=f"{item['ts']}:prop:p-01", via="slack", slack_item=item)
    assert out.outcome == "stale" and "B-002" in out.message
    assert ctx.repo.read("beliefs.md") == before
    assert load_yaml(ctx, "config/proposals.yml")["proposals"][0]["status"] == "stale"
    assert ctx.queue.of("backcheck") == []


def test_approved_proposal_applies_with_every_source(ctx, base):
    write_proposals(ctx, [{"id": "p-02", "type": "add", "statement": "Attorney claims raise the cost of footage review",
                           "scope": {"party": "carrier"},
                           "sources": [{"call_id": DARIUS, "quote": ATTORNEY}, [DARIUS, TWO_HOUR]],
                           "target": None, "target_version": None, "status": "open"}], rollup="rollup:bootstrap")
    req = ap.ChangeRequest(action="add", requested_by="alex", proposal_id="p-02")
    out = ap.apply_change(ctx, req, ref="3.0:prop:p-02", via="slack")
    assert out.outcome == "applied" and out.entry_ids == ["B-005"]
    [h] = doc(ctx).get("B-005").history
    assert h.via == "bootstrap" and h.sources == [(DARIUS, ATTORNEY), (DARIUS, TWO_HOUR)]
    assert load_yaml(ctx, "config/proposals.yml")["proposals"][0]["status"] == "applied"
    assert "config/proposals.yml" in ctx.remote.commits[-1]["paths"]


def test_proposal_request_prefilled_by_the_runner_does_not_double_sources(ctx, base):
    write_proposals(ctx, [{"id": "p-08", "type": "add", "statement": "Attorney claims cost more",
                           "scope": {"party": "carrier"},
                           "sources": [{"call_id": DARIUS, "quote": ATTORNEY}], "target": None, "target_version": None,
                           "status": "open"}])
    req = ap.ChangeRequest(action="add", requested_by="alex", proposal_id="p-08",
                           statement="Attorney claims cost more", scope=Scope(party="carrier"),
                           sources=[(DARIUS, ATTORNEY)])
    assert ap.apply_change(ctx, req, ref="3.8:prop:p-08", via="slack").outcome == "applied"
    assert doc(ctx).get("B-005").history[0].sources == [(DARIUS, ATTORNEY)]


def test_merge_proposal_is_stale_when_its_first_target_moved(ctx, base):
    write_proposals(ctx, [{"id": "p-07", "type": "merge", "statement": "Merged", "scope": {},
                           "sources": [], "target": None, "targets": ["B-002", "B-003"],
                           "target_version": "old-version", "status": "open", "rollup": "rollup:2026-W40"}],
                    rollup="rollup:2026-W40")
    out = ap.apply_change(ctx, ap.ChangeRequest(action="approve", requested_by="alex", proposal_id="p-07"),
                          ref="3.7:prop:p-07", via="slack")
    assert out.outcome == "stale" and "B-002" in out.message
    assert doc(ctx).get("B-003").section == "beliefs"


def test_edit_overrides_the_proposal_statement(ctx, base):
    version = doc(ctx).get("B-002").version
    write_proposals(ctx, [{"id": "p-03", "type": "reword", "statement": "Model wording", "scope": None,
                           "sources": [], "target": "B-002", "target_version": version, "status": "open"}])
    req = ap.ChangeRequest(action="reword", requested_by="alex", proposal_id="p-03", statement="Edited wording")
    assert ap.apply_change(ctx, req, ref="3.1:prop:p-03", via="slack").outcome == "applied"
    assert doc(ctx).get("B-002").statement == "Edited wording"


def test_drop_proposal_retires_the_watching_item(ctx, base):
    version = doc(ctx).get("W-001").version
    write_proposals(ctx, [{"id": "p-04", "type": "drop", "statement": None, "sources": [], "target": "W-001",
                           "target_version": version, "status": "open"}])
    out = ap.apply_change(ctx, ap.ChangeRequest(action="add", requested_by="sam", proposal_id="p-04"),
                          ref="3.2:prop:p-04", via="slack")
    assert out.outcome == "applied"
    entry = doc(ctx).get("W-001")
    assert entry.section == "retired" and entry.history[-1].action == "dropped"


def test_reject_records_the_rejection(ctx, base):
    write_proposals(ctx, [{"id": "p-05", "type": "add", "statement": "Attorneys drive claim cost",
                           "scope": None, "sources": [[DARIUS, ATTORNEY]], "target": None, "target_version": None,
                           "status": "open"}])
    req = ap.ChangeRequest(action="reject", requested_by="jordan", proposal_id="p-05", reason="one adjuster")
    out = ap.apply_change(ctx, req, ref="3.3:prop:p-05", via="slack")
    assert out.outcome == "applied"
    [rej] = load_yaml(ctx, "config/rejected.yml")["rejected"]
    assert rej == {"type": "add", "target": None, "statement": "Attorneys drive claim cost",
                   "claims": [{"call_id": DARIUS, "quote": ATTORNEY}], "who": "Jordan",
                   "reason": "one adjuster", "ref": "3.3:prop:p-05", "proposal": "p-05"}
    assert load_yaml(ctx, "config/proposals.yml")["proposals"][0]["status"] == "rejected"
    assert ap.ref_recorded(ctx, "3.3:prop:p-05")
    assert "rejected" in out.message

    again = ap.apply_change(ctx, ap.ChangeRequest(action="add", requested_by="alex", proposal_id="p-05"),
                            ref="3.4:prop:p-05", via="slack")
    assert again.outcome == "stale"


def test_approve_verb_from_slack_applies_the_proposal(ctx, base):
    write_proposals(ctx, [{"id": "p-06", "type": "watch", "statement": "Long store videos slow every claim",
                           "scope": {"line": "general liability"}, "sources": [[DARIUS, TWO_HOUR]],
                           "target": None, "target_version": None, "status": "open"}])
    out = ap.apply_task(ctx, {"ref": "3.6:prop:p-06", "via": "slack",
                              "change": {"action": "approve", "proposal_id": "p-06", "requested_by": "sam"}})
    assert out["outcome"] == "applied" and out["entry_ids"] == ["W-002"]
    assert doc(ctx).get("W-002").history[0].via == "slack"
    with pytest.raises(ValueError):
        ap.request_from_dict({"action": "approve", "requested_by": "sam"})     # approve needs a proposal


def test_reject_needs_a_proposal(ctx, base):
    req = ap.ChangeRequest(action="reject", requested_by="jordan", target="B-002")
    assert ap.apply_change(ctx, req, ref="3.5:x", via="slack").outcome == "invalid"


# ---------- task payloads, helpers, dry run ----------

def test_apply_task_from_a_slack_payload(ctx, base):
    span = span_of(base["darius"], AI_RULES)
    out = ap.apply_task(ctx, {"ref": "4.0:finding:x", "via": "slack",
                              "change": {"action": "add", "requested_by": "alex",
                                         "statement": "Carriers bar outside AI tools",
                                         "sources": [{"call_id": DARIUS, "span": span}]}})
    assert out["outcome"] == "applied" and out["entry_ids"] == ["B-005"]
    assert doc(ctx).get("B-005").history[0].sources == [(DARIUS, AI_RULES)]


def test_dry_run_writes_files_but_skips_git_and_slack(ctx, base):
    ctx.dry_run = True
    req = ap.ChangeRequest(action="keep", requested_by="alex", target="B-002")
    assert ap.apply_change(ctx, req, ref="5.0:x", via="slack").outcome == "applied"
    assert doc(ctx).get("B-002").history[-1].action == "keep"
    assert ctx.remote.commits == [] and notices(ctx) == []


def test_mark_block_replaces_buttons_and_sections():
    blocks = [{"type": "actions", "block_id": "verdict:B-003:1", "elements": []},
              {"type": "section", "block_id": "finding:1-2", "text": {"type": "mrkdwn", "text": "x"},
               "accessory": {"type": "overflow"}}]
    out = ap.mark_block(blocks, "verdict:B-003:1", "Dismissed by Alex")
    assert out[0] == {"type": "context", "block_id": "verdict:B-003:1",
                      "elements": [{"type": "mrkdwn", "text": "Dismissed by Alex"}]}
    out = ap.mark_block(out, "finding:1-2", "Added as B-005")
    assert "accessory" not in out[1] and out[2]["block_id"] == "finding:1-2:status"
    again = ap.mark_block(out, "finding:1-2", "Added as B-006")
    assert len(again) == 3 and again[2]["elements"][0]["text"] == "Added as B-006"
    assert ap.mark_block(blocks, "missing", "x") is None


def test_em_dashes_in_written_text_are_replaced(ctx, base):
    req = ap.ChangeRequest(action="add", requested_by="alex", statement=f"Footage review {EM_DASH} a daily pain",
                           scope=Scope(party=f"carriers {EM_DASH} large"), reason=f"why {EM_DASH} because",
                           sources=[(DARIUS, TWO_HOUR)])
    assert ap.apply_change(ctx, req, ref="6.2:x", via="slack").outcome == "applied"
    text = ctx.repo.read("beliefs.md")
    assert EM_DASH not in text and "Footage review - a daily pain" in text


def test_no_em_dash_in_messages(ctx, base):
    outs = [ap.apply_change(ctx, ap.ChangeRequest(action="keep", requested_by="alex", target="B-002"),
                            ref="6.0:x", via="slack"),
            ap.apply_change(ctx, ap.ChangeRequest(action="retire", requested_by="alex", target="B-099"),
                            ref="6.1:x", via="slack")]
    assert all(EM_DASH not in o.message for o in outs)
    assert all(EM_DASH not in m["text"] for m in ctx.slack.messages)


def test_dismiss_verdict_records_the_verdict_it_dismisses(ctx, base):
    rel = write_request(ctx, "wrong-read", {"action": "dismiss_verdict", "target": "B-003", "verdict": "contradicts",
                                            "source": {"call_id": DARIUS, "quote": ATTORNEY}, "requested_by": "sam"})
    assert ap.apply_task(ctx, {"request_file": rel})["outcome"] == "applied"
    [record] = load_yaml(ctx, "config/verdict_overrides.yml")["overrides"]
    assert record["verdict"] == "contradicts" and record["offset"] and record["version"]
    bad = write_request(ctx, "bad-verdict", {"action": "dismiss_verdict", "target": "B-003", "verdict": "maybe",
                                             "source": {"call_id": DARIUS, "quote": ATTORNEY}, "requested_by": "sam"})
    assert ap.apply_task(ctx, {"request_file": bad})["outcome"] == "invalid"


def test_a_wrong_read_dismissal_leaves_a_later_supports_read_counted(ctx, base):
    """After a Reword the backcheck's read of the same claim is new evidence (SPEC "Under pressure")."""
    from nua import indexes
    from nua.types import BeliefCheck
    darius = base["darius"]
    attorney = next(c for c in darius.claims if c.quote == ATTORNEY)
    darius.belief_checks = [BeliefCheck(target="B-003", verdict="contradicts", claims=[attorney.id], decided_by="agree")]
    darius.checked = {"B-003": doc(ctx).get("B-003").version}
    dg.write_digest(ctx.repo, darius)
    req = ap.ChangeRequest(action="dismiss_verdict", requested_by="alex", target="B-003", verdict="contradicts",
                           sources=[(DARIUS, span_of(darius, ATTORNEY))])
    assert ap.apply_change(ctx, req, ref="1.5:verdict:B-003:1", via="slack").outcome == "applied"
    overrides = indexes.load_verdict_overrides(ctx.repo)
    assert indexes.is_dismissed(darius.belief_checks[0], darius, overrides)

    reword = ap.ChangeRequest(action="reword", requested_by="alex", target="B-003",
                              statement="Adjusters lose real time scrubbing footage before a liability decision")
    assert ap.apply_change(ctx, reword, ref="1.6:pressure:B-003", via="slack").outcome == "applied"
    entry = doc(ctx).get("B-003")
    darius.belief_checks = [BeliefCheck(target="B-003", verdict="supports", claims=[attorney.id], decided_by="agree",
                                      source="backcheck")]
    darius.checked = {"B-003": entry.version}
    status = indexes.entry_status(entry, [darius], overrides, ctx.config)
    assert [i["quote"] for i in status.supports] == [ATTORNEY]


ATTORNEY_REWORD = "Adjusters lose real time scrubbing footage, most of all on attorney claims"


def checked_everywhere(ctx, base) -> None:
    """Every digest checked against every current entry's version, B-003 read as contradicted by ATTORNEY."""
    versions = {e.id: e.version for e in doc(ctx).current()}
    for d in base.values():
        d.checked = dict(versions)
        dg.write_digest(ctx.repo, d)
    read_on(ctx, base["darius"], "B-003", ATTORNEY)


def attorney_models(ctx, monkeypatch) -> None:
    """Both extractors read the attorney claim as contradicting B-003; nothing else."""
    import re

    from nua import backcheck as bc
    from nua.llm import FakeLLM

    def respond(system, prompt, schema):
        checks = []
        if "B-003 (" in prompt:
            for m in re.finditer(r"^(c\d+) \[[^\]]*\]: (.*)$", prompt, re.M):
                if "attorney" in m.group(2):
                    checks.append({"target": "B-003", "verdict": "contradicts", "claims": [m.group(1)], "reason": "r"})
        return {"belief_checks": checks, "question_coverage": [], "speakers": []}

    for spec in ctx.config.pipeline.extractors:
        ctx.llms.register(FakeLLM(spec, respond))
    ctx.llms.register(FakeLLM(ctx.config.pipeline.arbiter, lambda *a: AssertionError("arbiter not expected")))
    monkeypatch.setattr(bc, "_regenerate", lambda c: [])


def b003_contradictions(ctx) -> list[str]:
    from nua import indexes
    darius = dg.read_digest(ctx.repo, dg.digest_path(DARIUS))
    status = indexes.entry_status(doc(ctx).get("B-003"), [darius], indexes.load_verdict_overrides(ctx.repo), ctx.config)
    return [i["quote"] for i in status.contradicts]


def test_a_wrong_read_between_a_reword_and_its_backcheck_leaves_the_new_read_counted(ctx, base, monkeypatch):
    """The dismissal records the version the read was checked against (SPEC "Under pressure"), not the
    entry's new version, so the backcheck's read of the new wording still counts."""
    from nua import backcheck as bc

    checked_everywhere(ctx, base)
    v1 = doc(ctx).get("B-003").version
    reword = ap.ChangeRequest(action="reword", requested_by="alex", target="B-003", statement=ATTORNEY_REWORD)
    assert ap.apply_change(ctx, reword, ref="1.6:pressure:B-003", via="slack").outcome == "applied"
    wrong = ap.ChangeRequest(action="dismiss_verdict", requested_by="sam", target="B-003", verdict="contradicts",
                             sources=[(DARIUS, span_of(base["darius"], ATTORNEY))])
    assert ap.apply_change(ctx, wrong, ref="1.5:verdict:B-003:1", via="slack").outcome == "applied"
    [record] = load_yaml(ctx, "config/verdict_overrides.yml")["overrides"]
    assert record["version"] == v1

    attorney_models(ctx, monkeypatch)
    assert bc.backcheck(ctx) == {"entries": ["B-003"], "digests_updated": 2}
    darius = dg.read_digest(ctx.repo, dg.digest_path(DARIUS))
    assert darius.checked["B-003"] == "1.6:pressure:B-003"
    assert [(c.target, c.verdict, c.source) for c in darius.belief_checks] == [("B-003", "contradicts", "backcheck")]
    assert b003_contradictions(ctx) == [ATTORNEY]


def test_a_wrong_read_on_a_brief_checked_against_older_wording_is_stale(ctx, base, monkeypatch):
    """A brief posted before a Reword keeps a live Wrong read button carrying the version its read was
    checked against. Once the backcheck re-read the call against the new wording, the click would hide
    the new read, so it is answered as stale and nothing is recorded."""
    from nua import backcheck as bc

    checked_everywhere(ctx, base)
    v1 = doc(ctx).get("B-003").version
    reword = ap.ChangeRequest(action="reword", requested_by="alex", target="B-003", statement=ATTORNEY_REWORD)
    assert ap.apply_change(ctx, reword, ref="1.6:pressure:B-003", via="slack").outcome == "applied"
    attorney_models(ctx, monkeypatch)
    bc.backcheck(ctx)
    assert b003_contradictions(ctx) == [ATTORNEY]

    source = {"call_id": DARIUS, "span": span_of(base["darius"], ATTORNEY), "quote": ATTORNEY}
    before = ctx.repo.read_or("config/verdict_overrides.yml", "")
    old = ap.apply_task(ctx, {"ref": "1.5:verdict:B-003:1", "via": "slack", "change": {
        "action": "dismiss_verdict", "target": "B-003", "verdict": "contradicts", "version": v1,
        "requested_by": "sam", "sources": [source]}})
    assert old["outcome"] == "stale" and "re-checked against the new wording" in old["message"], old
    assert ctx.repo.read_or("config/verdict_overrides.yml", "") == before
    assert not ap.ref_recorded(ctx, "1.5:verdict:B-003:1")
    assert b003_contradictions(ctx) == [ATTORNEY]

    # a Wrong read on a brief showing the new read dismisses it, recording the version it names
    new = ap.apply_task(ctx, {"ref": "1.7:verdict:B-003:1", "via": "slack", "change": {
        "action": "dismiss_verdict", "target": "B-003", "verdict": "contradicts", "version": "1.6:pressure:B-003",
        "requested_by": "sam", "sources": [source]}})
    assert new["outcome"] == "applied", new
    [record] = load_yaml(ctx, "config/verdict_overrides.yml")["overrides"]
    assert record["version"] == "1.6:pressure:B-003"
    assert b003_contradictions(ctx) == []


def test_a_dismissal_on_a_call_never_checked_against_the_entry_records_its_version(ctx, base):
    """No `checked` entry for the target on that call: the entry's current version is recorded."""
    read_on(ctx, base["darius"], "B-003", ATTORNEY)
    assert "B-003" not in base["darius"].checked
    req = ap.ChangeRequest(action="dismiss_verdict", requested_by="alex", target="B-003", verdict="contradicts",
                           sources=[(DARIUS, span_of(base["darius"], ATTORNEY))])
    assert ap.apply_change(ctx, req, ref="1.5:verdict:B-003:1", via="slack").outcome == "applied"
    [record] = load_yaml(ctx, "config/verdict_overrides.yml")["overrides"]
    assert record["version"] == doc(ctx).get("B-003").version


# ---------- Slack: top-level messages and lost notices ----------

class FreshSlack:
    """FakeSlack plus the one-message read (conversations.history on one ts) apply uses for top-level items."""

    def __init__(self, inner):
        self._inner = inner
        self.reads: list[str] = []

    def __getattr__(self, name):
        return getattr(self._inner, name)

    def message(self, channel, ts):
        self.reads.append(ts)
        m = next((m for m in self._inner.messages if m["channel"] == channel and m["ts"] == ts and not m["deleted"]),
                 None)
        return None if m is None else {"ts": m["ts"], "blocks": m["blocks"], "text": m["text"]}


def test_outcomes_on_one_top_message_do_not_revert_each_other(ctx, base):
    """Two Wrong read buttons on a brief's top message: each outcome is written onto the message as it
    is now, not onto the blocks carried from click time."""
    from nua import interact
    darius = base["darius"]
    read_on(ctx, darius, "B-001", AI_RULES)
    read_on(ctx, darius, "B-003", ATTORNEY)
    ctx.slack = FreshSlack(ctx.slack)
    blocks = []
    for n, (target, quote) in enumerate([("B-001", AI_RULES), ("B-003", ATTORNEY)], 1):
        span = span_of(darius, quote)
        blocks += [{"type": "section", "block_id": f"verdict-text:{target}:{n}",
                    "text": {"type": "mrkdwn", "text": f"*Against* {target}"}},
                   {"type": "actions", "block_id": f"verdict:{target}:{n}", "elements": [
                       {"type": "button", "action_id": "wrong_read", "text": {"type": "plain_text", "text": "Wrong read"},
                        "value": f"{DARIUS}|{target}|{span}"}]}]
    top = ctx.slack.post(CALLS, blocks, "brief")

    def click(block_id: str, who: str) -> dict:
        """Lock the item as the receiver does and return the slack_item it queues."""
        now = next(m for m in ctx.slack.messages if m["ts"] == top)
        item = interact._slack_item(CALLS, top, block_id, None, now["blocks"], now["text"])
        ctx.slack.update(CALLS, top, interact.locked_blocks(now["blocks"], block_id, f"Queued by {who}"), "brief")
        return item

    first = click("verdict:B-001:1", "Alex")
    second = click("verdict:B-003:2", "Sam")
    sources = {"verdict:B-001:1": ("B-001", AI_RULES), "verdict:B-003:2": ("B-003", ATTORNEY)}

    def run(item: dict, who: str) -> None:
        target, quote = sources[item["block_id"]]
        req = ap.ChangeRequest(action="dismiss_verdict", requested_by=who, target=target, verdict="contradicts",
                               sources=[(DARIUS, span_of(darius, quote))])
        assert ap.apply_change(ctx, req, ref=f"{top}:{item['block_id']}", via="slack", slack_item=item).outcome == "applied"

    def state() -> dict[str, str]:
        msg = next(m for m in ctx.slack.messages if m["ts"] == top)
        return {b["block_id"]: (b["elements"][0].get("text") or "") if b["type"] == "context" else b["type"]
                for b in msg["blocks"] if b["block_id"].startswith("verdict:")}

    run(first, "alex")
    after_one = state()
    assert after_one["verdict:B-001:1"].startswith("Alex dismissed the B-001 read")
    assert after_one["verdict:B-003:2"] == "Queued by Sam"          # still locked, not live again
    run(second, "sam")
    after_two = state()
    assert after_two["verdict:B-001:1"].startswith("Alex dismissed the B-001 read")
    assert after_two["verdict:B-003:2"].startswith("Sam dismissed the B-003 read")
    assert ctx.slack.reads == [top, top]


def test_top_message_falls_back_to_click_time_blocks_when_the_read_fails(ctx, base):
    blocks = [{"type": "actions", "block_id": "verdict:B-001:1", "elements": []}]
    top = ctx.slack.post(CALLS, blocks, "brief")
    ctx.slack.message = lambda channel, ts: (_ for _ in ()).throw(RuntimeError("slack 503"))
    item = {"channel": CALLS, "ts": top, "block_id": "verdict:B-001:1", "thread_ts": None,
            "blocks": [{"type": "context", "block_id": "verdict:B-001:1", "elements": []}], "text": "brief"}
    ap._update_item(ctx, item, "done")
    msg = next(m for m in ctx.slack.messages if m["ts"] == top)
    assert msg["blocks"][0]["elements"][0]["text"] == "done"


def test_a_lost_notice_is_posted_on_retry_after_the_request_file_is_gone(ctx, base):
    rel = write_request(ctx, "r1", {"action": "watch", "statement": "Footage review is a daily pain",
                                    "source": {"call_id": DARIUS, "quote": TWO_HOUR}, "requested_by": "sam"})
    ref = f"{rel}@{blob_sha(ctx.repo.read(rel))}"
    real_post = ctx.slack.post
    failures = []

    def flaky(*args, **kwargs):
        if not failures:
            failures.append(1)
            raise RuntimeError("slack 503")
        return real_post(*args, **kwargs)

    ctx.slack.post = flaky
    with pytest.raises(RuntimeError):
        ap.apply_task(ctx, {"request_file": rel})
    assert len(ctx.remote.commits) == 1 and not ctx.repo.exists(rel) and notices(ctx) == []

    out = ap.apply_task(ctx, {"request_file": rel})
    assert out["outcome"] == "skipped" and out["entry_ids"] == ["W-002"]
    [notice] = notices(ctx)
    assert notice["text"].startswith("Sam added W-002 to Watching via agent")
    assert notice["metadata"]["event_payload"]["key"] == ref
    assert len(ctx.remote.commits) == 1
    ap.apply_task(ctx, {"request_file": rel})
    assert len(notices(ctx)) == 1                                      # posted once


def test_a_gone_rejection_or_dismissal_request_is_found_by_its_ref(ctx, base):
    rel = write_request(ctx, "r2", {"action": "dismiss_verdict", "target": "B-003",
                                    "source": {"call_id": DARIUS, "quote": ATTORNEY}, "requested_by": "jordan"})
    ap.apply_task(ctx, {"request_file": rel})
    [first] = notices(ctx)
    ctx.slack.delete(CALLS, first["ts"])
    out = ap.apply_task(ctx, {"request_file": rel})
    assert out["outcome"] == "skipped" and out["entry_ids"] == ["B-003"]
    [notice] = notices(ctx)
    assert notice["text"].startswith("Jordan dismissed the B-003 read")


# ---------- a click queued before a supersede (the call re-digested from a richer copy) ----------

FIELD = ("So I get an investigator out to the location inside those three days too, because a lot of these DVR "
         "systems loop over in a week or two.")


def darius_from(ctx, raw: str, claims: list[tuple[str, str, int]], *, checks=()) -> Digest:
    """Re-digest DARIUS from `raw` (a richer copy): claims are (text, quote, occurrence of the quote)."""
    from nua.types import BeliefCheck
    rel = "transcripts/darius-kemp-richer.md"
    ctx.repo.write(rel, raw)
    index = QuoteIndex(parse(rel, raw, commit_date="2026-10-04"))
    out = []
    for n, (text, quote, nth) in enumerate(claims, 1):
        m = index.find_all(quote)[nth]
        out.append(Claim(id=f"c{n:02d}", text=text, quote=m.raw_text, turns=m.turns, offset=(m.start, m.end),
                         speaker="Darius Kemp", scope=Scope(seat="claims adjuster (staff)")))
    d = Digest(call_id=DARIUS, transcript=rel, status="digested", origin="backfill", date="2026-09-23",
               contact="darius-kemp", seat="claims adjuster (staff)", prompt_version="v1", claims=out,
               belief_checks=[BeliefCheck(target=t, verdict="contradicts", claims=[out[i].id], decided_by="agree")
                              for t, i in checks])
    dg.write_digest(ctx.repo, d)
    return d


def richer_copy(onto: str, extra: int = 0) -> str:
    """The Darius transcript with a turn inserted before FIELD, sized so FIELD moves onto the old start
    of `onto` (plus `extra` characters): old offsets now land on the FIELD claim."""
    raw = fixture_text("darius-kemp")
    index = QuoteIndex(parse("transcripts/darius-kemp.md", raw, commit_date="2026-10-04"))
    p, s = index.find(FIELD).start, index.find(onto).start
    shift = s - p + extra
    line = raw.rfind("\n", 0, p) + 1
    head = "[5:10] **Alex Rivera**: "
    filler = ("yeah " * shift)[: shift - len(head) - 2]
    richer = raw[:line] + head + filler + "\n\n" + raw[line:]
    assert len(richer) == len(raw) + shift
    return richer


def test_a_span_that_lands_on_another_claim_after_a_supersede_follows_the_quote_it_showed(ctx, base):
    old_span = span_of(base["darius"], TWO_HOUR)
    new = darius_from(ctx, richer_copy(TWO_HOUR), [("Field investigators pull store video", FIELD, 0),
                                                  ("Long store videos are reviewed by hand", TWO_HOUR, 0)])
    assert new.claims[0].offset[0] == int(old_span.split("-")[0])       # the old span is now FIELD's
    req = ap.request_from_dict({"action": "watch", "requested_by": "alex", "sources": [
        {"call_id": DARIUS, "span": old_span, "quote": TWO_HOUR}]})
    assert req.sources == [(DARIUS, old_span)] and req.shown == {(DARIUS, old_span): TWO_HOUR}
    out = ap.apply_change(ctx, req, ref="7.0:finding:x", via="slack")
    assert out.outcome == "applied", out.message
    entry = doc(ctx).get("W-002")
    assert entry.statement == "Long store videos are reviewed by hand"
    assert entry.history[0].sources == [(DARIUS, TWO_HOUR)]


def test_a_shown_quote_the_new_digest_no_longer_has_is_invalid(ctx, base):
    old_span = span_of(base["darius"], TWO_HOUR)
    darius_from(ctx, richer_copy(TWO_HOUR), [("Field investigators pull store video", FIELD, 0)])
    req = ap.request_from_dict({"action": "watch", "requested_by": "alex", "sources": [
        {"call_id": DARIUS, "span": old_span, "quote": TWO_HOUR}]})
    out = ap.apply_change(ctx, req, ref="7.1:finding:x", via="slack")
    assert out.outcome == "invalid" and "re-digested since this was posted" in out.message
    assert doc(ctx).get("W-002") is None


def test_a_span_needs_half_a_claim_to_resolve(ctx, base):
    """A click queued before the shown quote travelled with it: a span that only grazes a claim is not it."""
    old_span = span_of(base["darius"], TWO_HOUR)
    darius_from(ctx, richer_copy(TWO_HOUR, extra=80), [("Field investigators pull store video", FIELD, 0)])
    out = ap.apply_change(ctx, ap.ChangeRequest(action="watch", requested_by="alex", sources=[(DARIUS, old_span)]),
                          ref="7.2:finding:x", via="slack")
    assert out.outcome == "invalid" and f"no claim at {old_span}" in out.message


def test_a_wrong_read_whose_span_lands_on_a_claim_without_that_read_is_invalid(ctx, base):
    old_span = span_of(base["darius"], ATTORNEY)
    darius_from(ctx, richer_copy(ATTORNEY), [("Field investigators pull store video", FIELD, 0), ("Attorney claims cost more", ATTORNEY, 0)],
              checks=[("B-003", 1)])
    before = ctx.repo.read_or("config/verdict_overrides.yml", "")
    req = ap.ChangeRequest(action="dismiss_verdict", requested_by="alex", target="B-003", verdict="contradicts",
                           sources=[(DARIUS, old_span)])
    out = ap.apply_change(ctx, req, ref="7.3:verdict:B-003:1", via="slack")
    assert out.outcome == "invalid" and "no B-003 read" in out.message
    assert ctx.repo.read_or("config/verdict_overrides.yml", "") == before


def test_a_wrong_read_follows_the_quote_it_showed_after_a_supersede(ctx, base):
    old_span = span_of(base["darius"], ATTORNEY)
    new = darius_from(ctx, richer_copy(ATTORNEY), [("Field investigators pull store video", FIELD, 0),
                                           ("Attorney claims cost more", ATTORNEY, 0)], checks=[("B-003", 1)])
    out = ap.apply_task(ctx, {"ref": "7.4:verdict:B-003:1", "via": "slack", "change": {
        "action": "dismiss_verdict", "target": "B-003", "verdict": "contradicts", "requested_by": "alex",
        "sources": [{"call_id": DARIUS, "span": old_span, "quote": ATTORNEY}]}})
    assert out["outcome"] == "applied", out["message"]
    [record] = load_yaml(ctx, "config/verdict_overrides.yml")["overrides"]
    assert record["quote"] == ATTORNEY and record["offset"] == list(new.claims[1].offset)


# ---------- duplicates are matched by span, not by a shared phrase ----------

SHORT = "I don't think so."
LONGER = "I don't think so, a small store would never keep footage that long and we would never ask them."


def with_answers(second: str) -> str:
    """The Darius transcript with two questions answered just before FIELD: "I don't think so." and `second`."""
    raw = fixture_text("darius-kemp")
    index = QuoteIndex(parse("transcripts/darius-kemp.md", raw, commit_date="2026-10-04"))
    line = raw.rfind("\n", 0, index.find(FIELD).start) + 1
    turns = ("[5:10] **Alex Rivera**: Would a carrier pay for footage review?\n\n"
             f"[5:10] **Darius Kemp**: {SHORT}\n\n"
             "[5:11] **Alex Rivera**: Would a small store keep footage for six months?\n\n"
             f"[5:11] **Darius Kemp**: {second}\n\n")
    return raw[:line] + turns + raw[line:]


def test_a_source_sharing_a_short_phrase_with_a_held_source_is_not_a_duplicate(ctx, base):
    d = darius_from(ctx, with_answers(LONGER), [("No budget for footage review", SHORT, 0),
                                              ("Small stores never keep footage that long", LONGER, 0)])
    first = ap.ChangeRequest(action="add", requested_by="alex", sources=[(DARIUS, span_of(d, SHORT))])
    assert ap.apply_change(ctx, first, ref="8.0:finding:a", via="slack").outcome == "applied"
    second = ap.ChangeRequest(action="watch", requested_by="alex", sources=[(DARIUS, span_of(d, LONGER))])
    out = ap.apply_change(ctx, second, ref="8.1:finding:b", via="slack")
    assert out.outcome == "applied" and out.entry_ids == ["W-002"], out.message
    again = ap.ChangeRequest(action="watch", requested_by="alex", sources=[(DARIUS, LONGER)])
    assert ap.apply_change(ctx, again, ref="8.2:finding:c", via="slack").outcome == "duplicate"


def test_identical_short_answers_are_held_one_each(ctx, base):
    d = darius_from(ctx, with_answers(SHORT), [("No budget for footage review", SHORT, 0),
                                             ("Small stores do not keep footage", SHORT, 1)])
    one, two = (f"{c.offset[0]}-{c.offset[1]}" for c in d.claims)
    assert one != two
    add = ap.ChangeRequest(action="add", requested_by="alex", sources=[(DARIUS, one)])
    assert ap.apply_change(ctx, add, ref="8.3:finding:a", via="slack").outcome == "applied"
    watch = ap.ChangeRequest(action="watch", requested_by="alex", sources=[(DARIUS, two)])
    out = ap.apply_change(ctx, watch, ref="8.4:finding:b", via="slack")
    assert out.outcome == "applied" and out.entry_ids == ["W-002"], out.message
    for n, span in enumerate((one, two)):
        dup = ap.ChangeRequest(action="watch", requested_by="alex", sources=[(DARIUS, span)])
        assert ap.apply_change(ctx, dup, ref=f"8.5:finding:{n}", via="slack").outcome == "duplicate"


def test_identical_short_answers_acted_on_out_of_order_across_beliefs_and_watching(ctx, base):
    """The second answer is watched first from its finding menu: the ref names its span, so the first
    answer stays free and the second stays held, whichever section comes first in beliefs.md."""
    d = darius_from(ctx, with_answers(SHORT), [("No budget for footage review", SHORT, 0),
                                             ("Small stores do not keep footage", SHORT, 1)])
    one, two = (f"{c.offset[0]}-{c.offset[1]}" for c in d.claims)
    watch = ap.ChangeRequest(action="watch", requested_by="sam", sources=[(DARIUS, two)])
    out = ap.apply_change(ctx, watch, ref=f"8.6:finding:{two}", via="slack")
    assert out.outcome == "applied" and out.entry_ids == ["W-002"], out.message
    add = ap.ChangeRequest(action="add", requested_by="alex", sources=[(DARIUS, one)])
    out = ap.apply_change(ctx, add, ref=f"8.7:finding:{one}", via="slack")
    assert out.outcome == "applied" and out.entry_ids == ["B-005"], out.message
    again = ap.ChangeRequest(action="add", requested_by="jordan", sources=[(DARIUS, two)])
    out = ap.apply_change(ctx, again, ref=f"8.8:finding:{two}", via="slack")
    assert out.outcome == "duplicate" and out.message == "Not added: already exists as W-002"
    again = ap.ChangeRequest(action="watch", requested_by="jordan", sources=[(DARIUS, one)])
    out = ap.apply_change(ctx, again, ref=f"8.9:finding:{one}", via="slack")
    assert out.outcome == "duplicate" and out.message == "Not added: already exists as B-005"


def test_identical_short_answers_without_a_span_in_the_ref_are_placed_in_the_order_they_were_held(ctx, base):
    """Request refs name no span: the earlier change holds the earlier answer, although beliefs.md
    lists the later belief before the earlier Watching item."""
    from datetime import datetime
    from zoneinfo import ZoneInfo
    d = darius_from(ctx, with_answers(SHORT), [("No budget for footage review", SHORT, 0),
                                             ("Small stores do not keep footage", SHORT, 1)])
    one, two = (f"{c.offset[0]}-{c.offset[1]}" for c in d.claims)
    ctx.now = lambda: datetime(2026, 10, 1, 9, 0, tzinfo=ZoneInfo("America/Los_Angeles"))
    watch = ap.ChangeRequest(action="watch", requested_by="sam", sources=[(DARIUS, one)])
    assert ap.apply_change(ctx, watch, ref="requests/b.yml@1", via="agent").entry_ids == ["W-002"]
    ctx.now = lambda: datetime(2026, 10, 2, 9, 0, tzinfo=ZoneInfo("America/Los_Angeles"))
    add = ap.ChangeRequest(action="add", requested_by="alex", sources=[(DARIUS, two)])
    assert ap.apply_change(ctx, add, ref="requests/a.yml@2", via="agent").entry_ids == ["B-005"]
    out = ap.apply_change(ctx, ap.ChangeRequest(action="add", requested_by="jordan", sources=[(DARIUS, one)]),
                          ref="requests/c.yml@3", via="agent")
    assert out.message == "Not added: already exists as W-002"
    out = ap.apply_change(ctx, ap.ChangeRequest(action="watch", requested_by="jordan", sources=[(DARIUS, two)]),
                          ref="requests/d.yml@4", via="agent")
    assert out.message == "Not added: already exists as B-005"


def test_a_promoted_watch_keeps_the_span_its_finding_ref_named(ctx, base):
    d = darius_from(ctx, with_answers(SHORT), [("No budget for footage review", SHORT, 0),
                                             ("Small stores do not keep footage", SHORT, 1)])
    one, two = (f"{c.offset[0]}-{c.offset[1]}" for c in d.claims)
    watch = ap.ChangeRequest(action="watch", requested_by="sam", sources=[(DARIUS, two)])
    assert ap.apply_change(ctx, watch, ref=f"9.1:finding:{two}", via="slack").entry_ids == ["W-002"]
    promote = ap.ChangeRequest(action="promote", requested_by="alex", target="W-002")
    assert ap.apply_change(ctx, promote, ref="9.2:prop:x", via="slack").entry_ids[0] == "B-005"
    add = ap.ChangeRequest(action="add", requested_by="jordan", sources=[(DARIUS, one)])
    assert ap.apply_change(ctx, add, ref=f"9.3:finding:{one}", via="slack").outcome == "applied"
    dup = ap.ChangeRequest(action="watch", requested_by="jordan", sources=[(DARIUS, two)])
    out = ap.apply_change(ctx, dup, ref=f"9.4:finding:{two}", via="slack")
    assert out.message == "Not added: already exists as B-005"


# ---------- a lost reply to a duplicate, stale or invalid request ----------

def test_a_lost_reply_to_an_invalid_request_is_posted_on_retry(ctx, base):
    ctx.remote = NullRemote()       # last_commit_message reads its commits, as GitRemote reads GitHub's
    rel = write_request(ctx, "2026-10-06_sam_keep", {"action": "keep", "target": "B-042", "requested_by": "sam"})
    ref = f"{rel}@{blob_sha(ctx.repo.read(rel))}"
    real_post = ctx.slack.post
    failures = []

    def flaky(*args, **kwargs):
        if not failures:
            failures.append(1)
            raise RuntimeError("slack 503")
        return real_post(*args, **kwargs)

    ctx.slack.post = flaky
    with pytest.raises(RuntimeError):
        ap.apply_task(ctx, {"request_file": rel})
    assert len(ctx.remote.commits) == 1 and not ctx.repo.exists(rel) and notices(ctx) == []

    out = ap.apply_task(ctx, {"request_file": rel})
    assert out["outcome"] == "skipped" and "B-042 does not exist" in out["message"]
    [notice] = notices(ctx)
    assert notice["text"] == "2026-10-06_sam_keep.yml: not applied: B-042 does not exist"
    assert notice["metadata"]["event_payload"]["key"] == ref
    ap.apply_task(ctx, {"request_file": rel})
    assert len(notices(ctx)) == 1 and len(ctx.remote.commits) == 1


def test_a_request_file_deleted_by_hand_is_just_gone(ctx, base):
    ctx.remote = NullRemote()       # last_commit_message reads its commits, as GitRemote reads GitHub's
    ctx.remote.commits.append({"message": "Remove a stray request", "paths": ["requests/stray.yml"]})
    out = ap.apply_task(ctx, {"request_file": "requests/stray.yml"})
    assert out["outcome"] == "skipped" and out["message"] == "Already gone: requests/stray.yml"
    assert notices(ctx) == []


# ---------- malformed fields are invalid, never a crash ----------

def test_scalar_targets_are_invalid_not_a_crash(ctx, base):
    ctx.repo.write("requests/2026-10-06_sam_merge.yml", "action: merge\ntargets: 3\nrequested_by: sam\n")
    out = ap.apply_task(ctx, {"request_file": "requests/2026-10-06_sam_merge.yml"})
    assert out["outcome"] == "invalid" and "targets must be a list" in out["message"]
    assert not ctx.repo.exists("requests/2026-10-06_sam_merge.yml")
    assert ctx.remote.commits[-1]["paths"] == ["requests/2026-10-06_sam_merge.yml"]
    for bad in (3, True, "B-001"):
        with pytest.raises(ValueError):
            ap.request_from_dict({"action": "merge", "targets": bad, "requested_by": "sam"})
    out = ap.apply_task(ctx, {"ref": "9.0:x", "via": "slack",
                              "change": {"action": "merge", "targets": 3, "requested_by": "sam"}})
    assert out["outcome"] == "invalid"


def test_a_claim_inside_a_longer_held_passage_is_a_duplicate(ctx, base):
    """An agent quoted a longer passage that holds a claim: that claim is already held."""
    raw = fixture_text("darius-kemp")
    index = QuoteIndex(parse("transcripts/darius-kemp.md", raw, commit_date="2026-10-04"))
    passage = raw[index.find(TWO_HOUR).start - 72: index.find(TWO_HOUR).end]
    darius_from(ctx, raw, [("Field investigators pull store video", FIELD, 0), ("Long store videos are manual review", TWO_HOUR, 0)])
    add = ap.ChangeRequest(action="add", requested_by="sam", statement="Long videos drive the daily work",
                           sources=[(DARIUS, passage)])
    assert ap.apply_change(ctx, add, ref="8.6:x", via="agent").outcome == "applied"
    two_hour = next(c for c in dg.read_digest(ctx.repo, f"digests/{DARIUS}.md").claims if c.quote == TWO_HOUR)
    watch = ap.ChangeRequest(action="watch", requested_by="alex",
                             sources=[(DARIUS, f"{two_hour.offset[0]}-{two_hour.offset[1]}")])
    assert ap.apply_change(ctx, watch, ref="8.7:finding:x", via="slack").outcome == "duplicate"


# ---------- findings with several quotes ----------

def test_a_source_on_a_findings_second_quote_resolves_to_that_quote(ctx, base):
    """A finding carries several quotes; a request quoting its second one names that finding and that span,
    and a duplicate check against the finding's first quote does not swallow it."""
    darius = base["darius"]
    first, second = darius.claims[0], darius.claims[1]
    joined = first.model_copy(update={"quotes": [*first.quotes, *second.quotes]})
    darius = darius.model_copy(update={"claims": [joined, *darius.claims[2:]]})
    dg.write_digest(ctx.repo, darius)
    span = f"{second.offset[0]}-{second.offset[1]}"
    out = ap.apply_change(ctx, ap.ChangeRequest(action="watch", requested_by="alex", sources=[(DARIUS, span)]),
                          ref=f"7.3:finding:{span}", via="slack")
    assert out.outcome == "applied" and out.entry_ids == ["W-002"]
    entry = doc(ctx).get("W-002")
    assert entry.history[0].sources == [(DARIUS, ATTORNEY)]
    assert entry.statement == joined.text                 # the finding's text, reached through its second quote
