"""Command line and day-one repo files (Task 19).

The wave-2 collaborators (`nua.process`, `nua.indexes`, `nua.ask`, `nua.rollup`, `nua.sweep`,
`nua.backcheck`, `nua.settings`) are stubbed in `sys.modules`, so these tests cover the CLI's own
plumbing. The `stub_process_call` below runs the real parse, extract, verify, merge and verdict
steps with the `--fake` models, so the brief it prints is the real rendering of the labels. Tests
marked "real pipeline" use the actual modules and skip until they exist.
"""
from __future__ import annotations

import re
import shutil
import sys
import types
from pathlib import Path

import pytest
import yaml

from conftest import FIXTURE_NAMES, FIXTURES, PROCESSOR_CONFIG, ROOT, copy_config, fixture_text
from nua import __main__ as cli
from nua.beliefs import EMPTY_BELIEFS, parse_decisions
from nua.context import Ctx
from nua.digest import digest_path, read_digest, write_digest
from nua.types import Digest

GOLDEN = ROOT / "tests" / "golden"
EM_DASH = chr(0x2014)
GENERATED = ["calls.csv", "claims.csv", "findings.md", "beliefs-status.md", "open-questions.md"]


# ---------- stubs ----------

def stub(monkeypatch, name: str, **attrs) -> types.ModuleType:
    mod = types.ModuleType(f"nua.{name}")
    for key, value in attrs.items():
        setattr(mod, key, value)
    monkeypatch.setitem(sys.modules, f"nua.{name}", mod)
    return mod


def stub_process_call(ctx, transcript, *, force=False, batch=None, beliefs_path="beliefs.md",
                      decisions_path="decisions.md", prep_path=None):
    """A small process_call over the real Task 1-7 modules: enough to render a real brief."""
    from nua import callid, digest, extract, merge, parse, verdicts, verify
    from nua.beliefs import parse_beliefs
    from nua.prep import parse_prep
    from nua.types import Verification

    parsed = parse.parse(transcript, ctx.repo.read(transcript), commit_date="2026-10-04")
    results, degraded = extract.extract(ctx, parsed)
    kept, total = [], Verification()
    for r in results:
        claims, v = verify.verify_claims(parsed, r.claims)
        kept.append(claims)
        total.quotes += v.quotes
        total.matched_first_pass += v.matched_first_pass
    claims = merge.merge_claims(kept)
    beliefs = parse_beliefs(ctx.repo.read(beliefs_path))
    questions = parse_prep(prep_path, ctx.repo.read(prep_path)).questions if prep_path else []
    out = verdicts.run_verdicts(ctx, parsed, claims, beliefs.current(), questions)
    who = results[0].who
    existing = {d.call_id for d in digest.load_digests(ctx.repo)}
    d = Digest(call_id=callid.make_call_id(parsed.date, who.contact, existing), transcript=transcript,
               status="digested", origin=parsed.origin, date=parsed.date, date_source=parsed.date_source,
               contact=who.contact, firm=who.firm, seat=who.seat, speaker_labels=parsed.speaker_labels,
               prep=prep_path, summary=results[0].summary, claims=claims, belief_checks=out.belief_checks,
               question_coverage=out.question_coverage, follow_ups=results[0].follow_ups,
               ask_next=results[0].ask_next, verification=total, degraded=degraded)
    digest.write_digest(ctx.repo, d)
    return types.SimpleNamespace(transcript=transcript, outcome="digested", call_id=d.call_id, briefed=False)


def stub_counts(digests, beliefs, overrides=None):
    dismissed = {(o.get("call_id"), o.get("target")) for o in overrides or []}
    counts: dict[str, dict] = {}
    for d in digests:
        for check in d.belief_checks:
            if (d.call_id, check.target) in dismissed:
                continue
            c = counts.setdefault(check.target, {"calls": set(), "seat_list": []})
            c["calls"].add(d.call_id)
            if d.seat and d.seat not in c["seat_list"]:
                c["seat_list"].append(d.seat)
    return {k: {"calls": len(v["calls"]), "seats": len(v["seat_list"]), "seat_list": v["seat_list"]}
            for k, v in counts.items()}


def stub_overrides(repo):
    """config/verdict_overrides.yml as a list of rows, like indexes.load_verdict_overrides."""
    return list(yaml.safe_load(repo.read_or("config/verdict_overrides.yml", "")) or [])


def stub_regenerate(ctx):
    for rel in GENERATED:
        ctx.repo.write(rel, "generated\n")
    return list(GENERATED)


@pytest.fixture
def pipeline(monkeypatch):
    stub(monkeypatch, "process", process_call=stub_process_call)
    stub(monkeypatch, "indexes", evidence_counts=stub_counts, load_verdict_overrides=stub_overrides,
         regenerate=stub_regenerate, GENERATED=GENERATED)


def tree(root: Path) -> list[str]:
    return sorted(str(p.relative_to(root)) for p in root.rglob("*") if "__pycache__" not in p.parts)


def dry_run_args(root: Path, name: str, *extra: str) -> list[str]:
    return ["--root", str(root), "run", str(FIXTURES / f"{name}.md"), "--dry-run", "--fake",
            "--beliefs", str(GOLDEN / "beliefs.md"), "--decisions", str(GOLDEN / "decisions.md"), *extra]


# ---------- argument parsing ----------

def test_parse_run_arguments():
    a = cli.build_parser().parse_args(["run", "x.md", "--dry-run", "--beliefs", "b.md", "--decisions", "d.md",
                                       "--prep", "p.md", "--fake"])
    assert (a.command, a.transcript, a.dry_run, a.beliefs, a.decisions, a.prep, a.fake) == \
        ("run", "x.md", True, "b.md", "d.md", "p.md", True)
    a = cli.build_parser().parse_args(["run", "x.md"])
    assert (a.dry_run, a.fake, a.beliefs, a.prep, a.force) == (False, False, None, None, False)


def test_parse_other_commands():
    p = cli.build_parser()
    assert p.parse_args(["backfill", "a.md", "b.md"]).paths == ["a.md", "b.md"]
    a = p.parse_args(["ask", "what do adjusters want?", "--since", "2026-09-01", "--seat", "underwriter"])
    assert (a.question, a.since, a.seat) == ("what do adjusters want?", "2026-09-01", "underwriter")
    assert p.parse_args(["reindex"]).command == "reindex"
    assert p.parse_args(["rollup", "--bootstrap"]).bootstrap is True
    assert p.parse_args(["rollup"]).bootstrap is False
    assert p.parse_args(["sweep"]).command == "sweep"
    assert p.parse_args(["backcheck"]).command == "backcheck"
    assert p.parse_args(["bakeoff", "--models", "a:x, b:y,c:z"]).models == ["a:x", "b:y", "c:z"]
    assert p.parse_args(["bakeoff"]).models is None
    assert p.parse_args(["--root", "/tmp/r", "reindex"]).root == "/tmp/r"
    assert p.parse_args(["reindex", "--root", "/tmp/r"]).root == "/tmp/r"
    assert p.parse_args(["reindex"]).root is None


def test_no_command_prints_help(capsys):
    assert cli.main([]) == 2
    assert "usage" in capsys.readouterr().out.lower()


def test_unknown_command_exits_2():
    with pytest.raises(SystemExit) as e:
        cli.main(["nope"])
    assert e.value.code == 2


def test_find_root_walks_up(tmp_repo, monkeypatch):
    deep = tmp_repo / "digests" / "sub"
    deep.mkdir()
    monkeypatch.chdir(deep)
    monkeypatch.delenv("NUA_ROOT", raising=False)
    monkeypatch.delenv("NUA_REPO_ROOT", raising=False)
    assert cli.find_root(None) == tmp_repo.resolve()
    monkeypatch.setenv("NUA_ROOT", str(tmp_repo))
    assert cli.find_root(None) == tmp_repo.resolve()


def test_find_root_accepts_internal_repo_root(tmp_repo, tmp_path_factory, monkeypatch):
    monkeypatch.chdir(tmp_path_factory.mktemp("elsewhere"))      # not inside tmp_repo
    monkeypatch.delenv("NUA_ROOT", raising=False)
    monkeypatch.setenv("NUA_REPO_ROOT", str(tmp_repo))       # the name nua/settings.py reads
    assert cli.find_root(None) == tmp_repo.resolve()


def test_no_repo_found_is_an_error_not_an_empty_answer(tmp_path, monkeypatch, capsys):
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    monkeypatch.delenv("NUA_ROOT", raising=False)
    monkeypatch.delenv("NUA_REPO_ROOT", raising=False)
    with pytest.raises(cli.CliError, match="no repo found"):
        cli.find_root(None)
    assert cli.main(["ask", "anything?", "--fake"]) == 1
    captured = capsys.readouterr()
    assert "no repo found" in captured.err and "read 0/0" not in captured.out
    with pytest.raises(cli.CliError, match="config/pipeline.yml"):
        cli.find_root(str(elsewhere))


# ---------- run ----------

@pytest.mark.parametrize("name", FIXTURE_NAMES)
def test_run_dry_run_fake_prints_a_brief(tmp_repo, pipeline, capsys, name):
    before = tree(tmp_repo)
    code = cli.main(dry_run_args(tmp_repo, name))
    out = capsys.readouterr().out
    assert code == 0
    assert "WHAT CHANGED" in out
    assert "--- thread reply 1 ---" in out
    assert EM_DASH not in out
    assert tree(tmp_repo) == before, "a dry run leaves the working tree alone"


def test_run_dry_run_darius_kemp_matches_the_golden_brief(tmp_repo, pipeline, capsys):
    code = cli.main(dry_run_args(tmp_repo, "darius-kemp", "--prep", str(GOLDEN / "prep_darius-kemp.md")))
    out = capsys.readouterr().out
    assert code == 0
    assert out.startswith("Darius Kemp · claims adjuster (staff)\n")
    assert "Touches D-002 (Footage review tool for adjusters)" in out
    assert 'Contradicts B-004 "Adjusters can use outside AI tools on claim files"' in out
    assert "[2/2 agree]" in out
    assert "Supports B-001" in out and "Supports B-002" in out and "Supports B-003" in out
    assert "Watching W-001, supports" in out
    assert "12 new findings · 1 dodged question\n" in out and "follow-up" not in out
    replies = out.split("--- thread reply ")[1:]
    assert [r.split("\n", 1)[1].split("\n", 1)[0] for r in replies] == [
        "SUMMARY", "NEW FINDINGS (12)", "QUESTIONS FROM PREP"]
    assert "FOLLOW-UPS" not in out and "ASK NEXT" not in out
    assert "Dodged\n- Do carriers buy their claims platform" in out
    assert "Never asked\n- What would a carrier pay per location" in out


def test_dry_run_brief_leaves_out_dismissed_verdicts(tmp_repo, pipeline, capsys):
    args = dry_run_args(tmp_repo, "darius-kemp", "--prep", str(GOLDEN / "prep_darius-kemp.md"))
    def w001(out: str) -> str:
        [line] = [x for x in out.splitlines() if x.startswith("Watching W-001, supports")]
        return line

    assert cli.main(args) == 0
    assert w001(capsys.readouterr().out).endswith("(now 1 call, 1 seat)")
    (tmp_repo / "config" / "verdict_overrides.yml").write_text(
        "- {call_id: undated_darius-kemp, target: W-001, quote: x, who: alex, reason: wrong read}\n",
        encoding="utf-8")
    assert cli.main(args) == 0
    assert "(now" not in w001(capsys.readouterr().out)


def test_run_dry_run_prints_the_digest_too(tmp_repo, pipeline, capsys):
    """SPEC: the dry run prints the digest and brief. The brief alone stays on stdout (golden
    comparisons read it); the digest follows on stderr."""
    code = cli.main(dry_run_args(tmp_repo, "lena-okafor"))
    captured = capsys.readouterr()
    assert code == 0
    assert "WHAT CHANGED" in captured.out and "=== digests/" not in captured.out
    assert "=== digests/2026-09-30_lena-okafor.md ===" in captured.err
    assert "call_id: 2026-09-30_lena-okafor" in captured.err


def test_run_dry_run_digest_flags(tmp_repo, pipeline, capsys):
    code = cli.main(dry_run_args(tmp_repo, "lena-okafor", "--digest"))
    out = capsys.readouterr().out
    assert code == 0
    assert "=== digests/2026-09-30_lena-okafor.md ===" in out
    assert "call_id: 2026-09-30_lena-okafor" in out

    code = cli.main(dry_run_args(tmp_repo, "lena-okafor", "--no-digest"))
    captured = capsys.readouterr()
    assert code == 0
    assert "=== digests/" not in captured.out + captured.err


def test_run_writes_into_the_working_tree(tmp_repo, pipeline, capsys):
    code = cli.main(["--root", str(tmp_repo), "run", str(FIXTURES / "darius-kemp.md"), "--fake"])
    captured = capsys.readouterr()
    assert code == 0
    assert (tmp_repo / "transcripts" / "darius-kemp.md").read_text(encoding="utf-8") == fixture_text("darius-kemp")
    assert (tmp_repo / "digests" / "undated_darius-kemp.md").exists()
    assert "No belief touched." in captured.out
    assert "fake" in captured.err.lower()


def test_run_refuses_to_replace_a_different_transcript(tmp_repo, pipeline, capsys):
    (tmp_repo / "transcripts" / "darius-kemp.md").write_text("something else\n", encoding="utf-8")
    code = cli.main(["--root", str(tmp_repo), "run", str(FIXTURES / "darius-kemp.md"), "--fake"])
    assert code == 1
    assert "already exists" in capsys.readouterr().err
    assert (tmp_repo / "transcripts" / "darius-kemp.md").read_text(encoding="utf-8") == "something else\n"


def test_dry_run_renames_around_a_different_transcript(tmp_repo, pipeline, capsys):
    (tmp_repo / "transcripts" / "darius-kemp.md").write_text("another call\n", encoding="utf-8")
    code = cli.main(dry_run_args(tmp_repo, "darius-kemp"))
    captured = capsys.readouterr()
    assert code == 0
    assert "transcripts/darius-kemp-2.md (dry run): digested" in captured.err
    assert (tmp_repo / "transcripts" / "darius-kemp.md").read_text(encoding="utf-8") == "another call\n"


def test_run_uses_a_transcript_already_in_the_repo(tmp_repo, pipeline, capsys, monkeypatch):
    shutil.copy(FIXTURES / "wade-pruitt.md", tmp_repo / "transcripts" / "wade-pruitt.md")
    seen = {}

    def process_call(ctx, transcript, **kw):
        seen.update(transcript=transcript, **kw)
        return stub_process_call(ctx, transcript, **kw)

    stub(monkeypatch, "process", process_call=process_call)
    monkeypatch.chdir(tmp_repo)
    code = cli.main(["--root", str(tmp_repo), "run", "transcripts/wade-pruitt.md", "--fake", "--force"])
    assert code == 0
    assert seen["transcript"] == "transcripts/wade-pruitt.md"
    assert seen["force"] is True
    assert seen["beliefs_path"] == "beliefs.md" and seen["prep_path"] is None


def test_run_reports_a_stub(tmp_repo, monkeypatch, capsys):
    def process_call(ctx, transcript, **kw):
        d = Digest(call_id="odd", transcript=transcript, status="parse_failed", origin="backfill",
                   reason="unrecognized transcript shape")
        write_digest(ctx.repo, d)
        return types.SimpleNamespace(transcript=transcript, outcome="stub:parse_failed", call_id="odd", briefed=False)

    stub(monkeypatch, "process", process_call=process_call)
    stub(monkeypatch, "indexes", evidence_counts=stub_counts, load_verdict_overrides=stub_overrides)
    src = tmp_repo / "odd.md"
    src.write_text("not a transcript\n", encoding="utf-8")
    code = cli.main(["--root", str(tmp_repo), "run", str(src), "--dry-run"])
    captured = capsys.readouterr()
    assert code == 1
    assert "parse_failed" in captured.out and "unrecognized transcript shape" in captured.out


BROKEN_DIGEST = ("---\ncall_id: 2026-09-01_jane-doe\ntranscript: transcripts/jane.md\nstatus: digested\n"
                 "origin: live\nseat: [unclosed\n---\n")


@pytest.mark.parametrize("show", [False, True])
def test_run_names_an_unreadable_digest_instead_of_a_traceback(tmp_repo, capsys, monkeypatch, show):
    pytest.importorskip("nua.process")
    shutil.copy(FIXTURES / "darius-kemp.md", tmp_repo / "transcripts" / "jane.md")
    (tmp_repo / "digests" / "2026-09-01_jane-doe.md").write_text(BROKEN_DIGEST, encoding="utf-8")
    monkeypatch.chdir(tmp_repo)
    code = cli.main(["--root", str(tmp_repo), "run", "transcripts/jane.md", "--fake",
                     *(["--digest"] if show else [])])
    captured = capsys.readouterr()
    assert code == 0
    assert "skipped:unreadable (2026-09-01_jane-doe)" in captured.err
    assert "Unreadable digest digests/2026-09-01_jane-doe.md: front-matter is not valid YAML" in captured.out
    assert "by hand" in captured.out
    assert "Traceback" not in captured.out + captured.err
    assert ("=== digests/2026-09-01_jane-doe.md ===\n" + BROKEN_DIGEST in captured.out) is show
    assert (tmp_repo / "digests" / "2026-09-01_jane-doe.md").read_text(encoding="utf-8") == BROKEN_DIGEST


def test_dry_run_over_a_digested_transcript_that_no_longer_parses_fails(tmp_repo, capsys):
    """AGENTS.md: dry run any pipeline change. A forced re-run that now fails to parse keeps the existing
    digest (skipped:parse_failed); the dry run must not print that old digest's brief as if it had worked."""
    pytest.importorskip("nua.process")
    pytest.importorskip("nua.indexes")
    assert cli.main(["--root", str(tmp_repo), "run", str(FIXTURES / "darius-kemp.md"), "--fake", "--no-digest"]) == 0
    assert (tmp_repo / "digests" / "undated_darius-kemp.md").exists()
    capsys.readouterr()
    (tmp_repo / "transcripts" / "darius-kemp.md").write_text("not a transcript\n", encoding="utf-8")   # a parser regression
    code = cli.main(["--root", str(tmp_repo), "run", str(tmp_repo / "transcripts" / "darius-kemp.md"),
                     "--dry-run", "--fake", "--no-digest"])
    captured = capsys.readouterr()
    assert code == 1
    assert "skipped:parse_failed" in captured.err
    assert "WHAT CHANGED" not in captured.out and "Darius Kemp" not in captured.out
    assert "parse_failed on re-run" in captured.out and "digests/undated_darius-kemp.md kept" in captured.out


@pytest.mark.parametrize("outcome", ["skipped:parse_failed", "skipped:internal", "skipped:missing"])
@pytest.mark.parametrize("force", [True, False])
def test_run_that_kept_an_old_digest_prints_no_brief(tmp_repo, pipeline, monkeypatch, capsys, outcome, force):
    """process_call kept the existing digest (process._KEPT): this run produced nothing, so no brief and exit 1."""
    shutil.copy(FIXTURES / "darius-kemp.md", tmp_repo / "transcripts" / "darius-kemp.md")
    assert cli.main(["--root", str(tmp_repo), "run", str(FIXTURES / "darius-kemp.md"), "--fake"]) == 0
    capsys.readouterr()

    def process_call(ctx, transcript, **kw):
        return types.SimpleNamespace(transcript=transcript, outcome=outcome, call_id="undated_darius-kemp",
                                     briefed=False)

    stub(monkeypatch, "process", process_call=process_call)
    code = cli.main(["--root", str(tmp_repo), "run", str(FIXTURES / "darius-kemp.md"), "--fake",
                     *(["--force"] if force else [])])
    captured = capsys.readouterr()
    assert code == 1
    assert "WHAT CHANGED" not in captured.out
    assert f"{outcome[8:]} on re-run" in captured.out and "digests/undated_darius-kemp.md kept" in captured.out


@pytest.mark.parametrize("outcome", ["skipped:current", "skipped:stub"])
def test_run_that_found_a_current_digest_prints_its_brief(tmp_repo, pipeline, monkeypatch, capsys, outcome):
    shutil.copy(FIXTURES / "darius-kemp.md", tmp_repo / "transcripts" / "darius-kemp.md")
    assert cli.main(["--root", str(tmp_repo), "run", str(FIXTURES / "darius-kemp.md"), "--fake"]) == 0
    capsys.readouterr()

    def process_call(ctx, transcript, **kw):
        return types.SimpleNamespace(transcript=transcript, outcome=outcome, call_id="undated_darius-kemp",
                                     briefed=False)

    stub(monkeypatch, "process", process_call=process_call)
    code = cli.main(["--root", str(tmp_repo), "run", str(FIXTURES / "darius-kemp.md"), "--fake"])
    assert code == 0
    assert "WHAT CHANGED" in capsys.readouterr().out


def test_run_reports_an_error(tmp_repo, monkeypatch, capsys):
    def process_call(ctx, transcript, **kw):
        raise RuntimeError("model down")

    stub(monkeypatch, "process", process_call=process_call)
    code = cli.main(dry_run_args(tmp_repo, "darius-kemp"))
    assert code == 1
    assert "model down" in capsys.readouterr().err


# ---------- real pipeline (skips until tasks 11 and 12 exist) ----------

@pytest.mark.parametrize("name", FIXTURE_NAMES)
def test_run_dry_run_fake_real_pipeline(tmp_repo, capsys, name):
    pytest.importorskip("nua.process")
    pytest.importorskip("nua.indexes")
    extra = ["--prep", str(GOLDEN / "prep_darius-kemp.md")] if name == "darius-kemp" else []
    code = cli.main(dry_run_args(tmp_repo, name, *extra))
    captured = capsys.readouterr()
    assert code == 0, captured.err
    assert "WHAT CHANGED" in captured.out
    assert EM_DASH not in captured.out


def test_reindex_real_pipeline(tmp_repo, capsys):
    indexes = pytest.importorskip("nua.indexes")
    assert cli.main(["--root", str(tmp_repo), "reindex"]) == 0
    for rel in indexes.GENERATED:
        assert (tmp_repo / rel).exists(), rel


# ---------- backfill ----------

def test_backfill_processes_each_path_and_prints_a_summary(tmp_repo, pipeline, capsys):
    folder = tmp_repo.parent / "drop"
    folder.mkdir()
    for name in ("darius-kemp", "viktor-lang"):
        shutil.copy(FIXTURES / f"{name}.md", folder / f"{name}.md")
    code = cli.main(["--root", str(tmp_repo), "backfill", str(folder), "--fake"])
    out = capsys.readouterr().out
    assert code == 0
    assert (tmp_repo / "transcripts" / "darius-kemp.md").exists()
    assert (tmp_repo / "transcripts" / "viktor-lang.md").exists()
    assert "transcripts/darius-kemp.md: digested undated_darius-kemp" in out
    assert "digested: 2" in out
    assert "transcripts/viktor-lang.md: digested 2026-09-30_viktor-lang" in out
    assert "undated_darius-kemp: date and firm unknown" in out      # Darius never names his carrier
    assert not any(t for t in tree(tmp_repo) if t.startswith("config/batches/") and t.endswith(".yml"))


def test_backfill_names_an_unreadable_digest_in_the_fix_up_list(tmp_repo, capsys):
    pytest.importorskip("nua.process")
    shutil.copy(FIXTURES / "darius-kemp.md", tmp_repo / "transcripts" / "jane.md")
    (tmp_repo / "digests" / "2026-09-01_jane-doe.md").write_text(BROKEN_DIGEST, encoding="utf-8")
    code = cli.main(["--root", str(tmp_repo), "backfill", str(tmp_repo / "transcripts" / "jane.md"), "--fake"])
    captured = capsys.readouterr()
    assert code == 0
    assert "transcripts/jane.md: skipped:unreadable 2026-09-01_jane-doe" in captured.out
    assert ("2026-09-01_jane-doe: digests/2026-09-01_jane-doe.md cannot be read (front-matter is not valid YAML"
            in captured.out)
    assert "error" not in captured.out.lower() and "Traceback" not in captured.err


def test_backfill_keeps_going_after_an_error(tmp_repo, monkeypatch, capsys):
    def process_call(ctx, transcript, **kw):
        if "darius" in transcript:
            raise RuntimeError("boom")
        return types.SimpleNamespace(transcript=transcript, outcome="skipped:current", call_id=None, briefed=False)

    stub(monkeypatch, "process", process_call=process_call)
    code = cli.main(["--root", str(tmp_repo), "backfill", str(FIXTURES / "darius-kemp.md"),
                     str(FIXTURES / "lena-okafor.md")])
    captured = capsys.readouterr()
    assert code == 1
    assert "transcripts/darius-kemp.md: error: boom" in captured.out
    assert "transcripts/lena-okafor.md: skipped:current" in captured.out


# ---------- ask ----------

def test_ask_prints_answer_citations_and_coverage(tmp_repo, monkeypatch, capsys):
    seen = {}

    def ask(ctx, question, *, since=None, seat=None, batch_size=20, concurrency=8, progress=None):
        seen.update(question=question, since=since, seat=seat)
        if progress:
            progress(3, 3)
        return types.SimpleNamespace(
            answer="Adjusters want the store footage.", read=3, total=3, coverage="read 3/3 digests",
            citations=[{"call_id": "2026-09-23_darius-kemp", "quote": "single best thing you can have", "verified": True},
                       {"call_id": "2026-09-30_viktor-lang", "quote": "made up", "verified": False}])

    stub(monkeypatch, "ask", ask=ask)
    code = cli.main(["--root", str(tmp_repo), "ask", "what do adjusters want?", "--since", "2026-09-01",
                     "--seat", "underwriter"])
    captured = capsys.readouterr()
    assert code == 0
    assert seen == {"question": "what do adjusters want?", "since": "2026-09-01", "seat": "underwriter"}
    assert captured.out.startswith("Adjusters want the store footage.")
    assert '- 2026-09-23_darius-kemp: "single best thing you can have"' in captured.out
    assert '- 2026-09-30_viktor-lang: "made up"  (not verified)' in captured.out
    assert "read 3/3 digests" in captured.out
    assert "3/3" in captured.err


def test_ask_that_read_nothing_exits_1(tmp_repo, monkeypatch, capsys):
    def ask(ctx, question, *, since=None, seat=None, batch_size=20, concurrency=8, progress=None):
        if progress:
            progress(1, 1)
        return types.SimpleNamespace(answer="Could not read any of the 1 digests: every map batch failed.",
                                     read=0, total=1, coverage="read 0/1 digests", citations=[])

    stub(monkeypatch, "ask", ask=ask)
    code = cli.main(["--root", str(tmp_repo), "ask", "anything?"])
    captured = capsys.readouterr()
    assert code == 1
    assert "read 0/1 digests" in captured.out
    assert "read 1/1" not in captured.err and "processed 1/1" in captured.err


def test_ask_that_read_part_of_the_corpus_exits_0(tmp_repo, monkeypatch, capsys):
    stub(monkeypatch, "ask", ask=lambda ctx, q, **kw: types.SimpleNamespace(
        answer="Partial.", read=1, total=2, coverage="read 1/2 digests", citations=[]))
    assert cli.main(["--root", str(tmp_repo), "ask", "anything?"]) == 0
    assert "read 1/2 digests" in capsys.readouterr().out


def test_ask_fake_answers_without_keys(tmp_repo, capsys):
    pytest.importorskip("nua.ask")
    code = cli.main(["--root", str(tmp_repo), "ask", "anything?", "--fake"])
    assert code == 0
    assert "read 0/0 digests" in capsys.readouterr().out


def test_run_then_ask_fake_real_pipeline(tmp_repo, capsys):
    """Real pipeline: process a fixture into the working tree, then ask across it with fakes."""
    pytest.importorskip("nua.process")
    pytest.importorskip("nua.ask")
    assert cli.main(["--root", str(tmp_repo), "run", str(FIXTURES / "darius-kemp.md"), "--fake"]) == 0
    capsys.readouterr()
    assert cli.main(["--root", str(tmp_repo), "ask", "How often does Darius get usable video?", "--fake"]) == 0
    out = capsys.readouterr().out
    assert "read 1/1 digests" in out
    assert "undated_darius-kemp" in out
    assert "(not verified)" not in out


# ---------- reindex, rollup, sweep, backcheck ----------

def test_reindex_applies_overrides_and_regenerates(tmp_repo, pipeline, capsys):
    ctx = Ctx.for_tests(tmp_repo)
    write_digest(ctx.repo, Digest(call_id="2026-09-23_darius-kemp", transcript="transcripts/g.md",
                                  status="digested", origin="backfill", firm="Exampel Mutual"))
    (tmp_repo / "config" / "overrides.yml").write_text(
        yaml.safe_dump({"2026-09-23_darius-kemp": {"firm": "Example Mutual"}}), encoding="utf-8")
    code = cli.main(["--root", str(tmp_repo), "reindex"])
    out = capsys.readouterr().out
    assert code == 0
    d = read_digest(ctx.repo, digest_path("2026-09-23_darius-kemp"))
    assert (d.firm, d.firm_source) == ("Example Mutual", "manual")
    assert "digests/2026-09-23_darius-kemp.md" in out
    for rel in GENERATED:
        assert (tmp_repo / rel).read_text(encoding="utf-8") == "generated\n"
        assert rel in out


def test_reindex_prints_the_overrides_it_skips(tmp_repo, pipeline, capsys):
    ctx = Ctx.for_tests(tmp_repo)
    write_digest(ctx.repo, Digest(call_id="2026-09-23_darius-kemp", transcript="transcripts/g.md",
                                  status="digested", origin="backfill", firm="Exampel Mutual"))
    (tmp_repo / "config" / "overrides.yml").write_text(
        "2026-09-23_darius-kemp: {date: 2026-09-31, firm: Example Mutual}\n", encoding="utf-8")
    assert cli.main(["--root", str(tmp_repo), "reindex"]) == 0
    out = capsys.readouterr().out
    assert ("config/overrides.yml: 2026-09-23_darius-kemp date '2026-09-31' is not a real YYYY-MM-DD date; "
            "ignored") in out.splitlines()
    assert "digests/2026-09-23_darius-kemp.md" in out            # the firm correction still applies


def test_rollup_prints_the_slack_preview(tmp_repo, monkeypatch, capsys):
    seen = {}

    def run_rollup(ctx, *, bootstrap=False, week=None):
        seen.update(bootstrap=bootstrap, week=week)
        ctx.slack.post(ctx.channel("calls"), [], "Roll-up: 2 proposals")
        return {"proposals": 2}

    stub(monkeypatch, "rollup", run_rollup=run_rollup)
    code = cli.main(["--root", str(tmp_repo), "rollup", "--bootstrap"])
    captured = capsys.readouterr()
    assert code == 0
    assert seen == {"bootstrap": True, "week": None}
    assert "proposals: 2" in captured.out
    assert "Roll-up: 2 proposals" in captured.err
    assert "not sent" in captured.err


def test_sweep_builds_recorder_clients_from_env(tmp_repo, monkeypatch, capsys):
    pytest.importorskip("nua.settings")
    from nua.ingest.circleback import CirclebackClient
    from nua.ingest.granola import GranolaClient
    seen = {}

    def run_sweep(ctx, *, granola=None, circleback=None, granola_only=False, last_sweep=None):
        seen.update(granola=granola, circleback=circleback, granola_only=granola_only)
        return types.SimpleNamespace(ingested=["transcripts/granola_a.md"], enqueued=[], rebriefed=[], requests=[],
                                     reminders=[], stale=[], batches_checked=[])

    stub(monkeypatch, "sweep", run_sweep=run_sweep)
    monkeypatch.setenv("GRANOLA_API_KEY", "grn_test")
    monkeypatch.setenv("CIRCLEBACK_API_KEY_ALEX", "cb_test")
    code = cli.main(["--root", str(tmp_repo), "sweep", "--granola-only"])
    out = capsys.readouterr().out
    assert code == 0
    assert isinstance(seen["granola"], GranolaClient)
    assert set(seen["circleback"]) == {"alex"}
    assert isinstance(seen["circleback"]["alex"], CirclebackClient)
    assert seen["granola_only"] is True
    assert "ingested: transcripts/granola_a.md" in out

    monkeypatch.delenv("GRANOLA_API_KEY")
    monkeypatch.delenv("CIRCLEBACK_API_KEY_ALEX")
    assert cli.main(["--root", str(tmp_repo), "sweep"]) == 0
    assert seen["granola"] is None and seen["circleback"] == {}


def test_backcheck_prints_its_result(tmp_repo, monkeypatch, capsys):
    stub(monkeypatch, "backcheck", backcheck=lambda ctx: {"entries": ["B-001"], "digests_updated": 3})
    assert cli.main(["--root", str(tmp_repo), "backcheck"]) == 0
    out = capsys.readouterr().out
    assert "entries: B-001" in out and "digests_updated: 3" in out


def test_sweep_errors_exit_1(tmp_repo, monkeypatch, capsys):
    stub(monkeypatch, "settings", granola_client=lambda: None, circleback_clients=lambda config: {})
    stub(monkeypatch, "sweep", run_sweep=lambda ctx, **kw: types.SimpleNamespace(
        ingested=[], enqueued=[], errors=["granola: 503"]))
    assert cli.main(["--root", str(tmp_repo), "sweep"]) == 1
    assert "errors: granola: 503" in capsys.readouterr().out


def test_verbose_prints_the_pipeline_log(tmp_repo, monkeypatch, capsys):
    def backcheck(ctx):
        ctx.info("backcheck: nothing pending")
        return {"entries": [], "digests_updated": 0}

    stub(monkeypatch, "backcheck", backcheck=backcheck)
    assert cli.main(["--root", str(tmp_repo), "backcheck", "-v"]) == 0
    assert "backcheck: nothing pending" in capsys.readouterr().err


# ---------- bakeoff ----------

def test_bakeoff_fake_prints_the_table_and_writes_the_report(tmp_path, capsys):
    copy_config(tmp_path)
    for sub in ("fixtures", "labels", "golden"):
        shutil.copytree(ROOT / "tests" / sub, tmp_path / "tests" / sub)
    code = cli.main(["--root", str(tmp_path), "bakeoff", "--fake", "--models", "fake:a,fake:b,fake:c"])
    out = capsys.readouterr().out
    assert code == 0
    assert "fake:a" in out and "fake:c" in out
    reports = list((tmp_path / "docs" / "research").glob("bakeoff-*.md"))
    assert len(reports) == 1
    assert str(reports[0].relative_to(tmp_path)) in out


# ---------- day-one files ----------

def test_day_one_files(ctx):
    """The templates a fresh repo starts from, rendered in a temp repo; not the live repo's data."""
    from nua import indexes
    from nua.beliefs import parse_beliefs, render_beliefs

    assert parse_beliefs(EMPTY_BELIEFS).entries == []
    assert render_beliefs(parse_beliefs(EMPTY_BELIEFS)) == EMPTY_BELIEFS
    assert ctx.repo.read("beliefs.md") == EMPTY_BELIEFS
    for name in PROCESSOR_CONFIG:
        assert ctx.repo.read(f"config/{name}").startswith("# Processor-written."), name
    assert indexes.load_themes(ctx.repo) == [] and indexes.load_verdict_overrides(ctx.repo) == []

    ctx.repo.delete("beliefs.md")              # a missing beliefs.md reads as the template
    assert indexes.regenerate(ctx) == GENERATED
    status = ctx.repo.read("beliefs-status.md")
    assert "Under pressure: none." in status
    assert "## Beliefs\n\nNone." in status and "## Watching\n\nNone." in status
    oq = ctx.repo.read("open-questions.md")
    assert oq.index("<!-- manual:start -->") < oq.index("<!-- manual:end -->")

    # The live decisions.md is hand-edited: it must keep its header and parse, whatever it holds.
    text = (ROOT / "decisions.md").read_text(encoding="utf-8")
    assert text.startswith("<!-- Hand-edited.")
    ids = [d.id for d in parse_decisions(text)]
    assert len(ids) == len(set(ids)) and all(re.fullmatch(r"D-\d{3}", i) for i in ids)
    for folder in ("transcripts", "digests", "prep", "requests"):
        assert (ROOT / folder / ".gitkeep").exists(), folder


def _blank_quotes(doc):
    """`doc` with every history source quote emptied: verbatim transcript text is exempt from the
    em-dash rule, everything else the processor writes is not."""
    import copy
    doc = copy.deepcopy(doc)
    for e in doc.entries:
        for h in e.history:
            h.sources = [(call_id, "") for call_id, _ in h.sources]
    return doc


def test_owned_files_have_no_em_dash(ctx):
    from nua import indexes
    from nua.beliefs import parse_beliefs, render_beliefs
    from nua.types import Entry, HistoryLine, Scope

    # Code and hand-edited files hold no quotes, so they are checked whole.
    for rel in ("nua/__main__.py", "decisions.md"):
        assert EM_DASH not in (ROOT / rel).read_text(encoding="utf-8"), rel

    # The live beliefs.md: everything outside the verbatim source quotes. The round trip proves the
    # parsed doc covers every byte of the file, so blanking the quotes hides nothing else.
    live = (ROOT / "beliefs.md").read_text(encoding="utf-8")
    doc = parse_beliefs(live)
    assert render_beliefs(doc) == live
    assert EM_DASH not in render_beliefs(_blank_quotes(doc))

    # Generated output from a synthetic repo whose quotes carry em dashes: the quote stays verbatim in
    # beliefs.md, and the generated indexes carry no em dash at all.
    quote = f"we pull the footage {EM_DASH} every single time"
    entry = Entry(id="B-001", statement="Footage review is manual", scope=Scope(party="carrier"),
                  section="beliefs", history=[HistoryLine(date="2026-10-01", action="add", who="Alex",
                                                          via="slack", ref="r-1",
                                                          sources=[("2026-09-30_jane-doe", quote)])])
    ctx.repo.write("beliefs.md", render_beliefs(type(doc)(entries=[entry])))
    written = ctx.repo.read("beliefs.md")
    assert quote in written
    assert EM_DASH not in render_beliefs(_blank_quotes(parse_beliefs(written)))
    for rel in indexes.regenerate(ctx):
        assert EM_DASH not in ctx.repo.read(rel), rel
