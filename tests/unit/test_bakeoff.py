"""Bake-off scoring and the label-backed fake models behind `--fake` (Task 19)."""
from __future__ import annotations

import shutil
from pathlib import Path

import pytest
import yaml

from conftest import FIXTURE_NAMES, ROOT, copy_config, fixture_text
from nua import bakeoff, extract, merge, verdicts, verify
from nua.beliefs import parse_beliefs
from nua.context import Ctx
from nua.llm import FakeLLM, LLMError
from nua.parse import parse, write_standard
from nua.prep import parse_prep
from nua.types import Claim, Digest, Quote

GOLDEN = ROOT / "tests" / "golden"
EM_DASH = chr(0x2014)


def parsed_fixture(name: str):
    return parse(f"tests/fixtures/{name}.md", fixture_text(name), commit_date="2026-10-04")


def golden_beliefs():
    return parse_beliefs((GOLDEN / "beliefs.md").read_text(encoding="utf-8"))


def merged_claims(ctx, parsed):
    results, _ = extract.extract(ctx, parsed)
    return merge.merge_claims([verify.verify_claims(parsed, r.claims)[0] for r in results])


def label_claims(parsed, labels: list[dict]) -> list[Claim]:
    """Verified findings built straight from labeled findings, every quote placed."""
    index = verify.QuoteIndex(parsed)
    out = []
    for n, lab in enumerate(labels, 1):
        quotes = []
        for q in lab["quotes"]:
            span = bakeoff.locate(index, q["quote"], q.get("question_turn"))
            assert span is not None, q["quote"]
            quotes.append(Quote(quote=q["quote"], offset=span))
        out.append(Claim(id=f"c{n:02d}", text=lab["text"], quotes=quotes))
    return out


def label_quote_texts(label: dict) -> list[list[str]]:
    return [[q["quote"] for q in f["quotes"]] for f in label["findings"]]


# ---------- labels ----------

def test_load_labels_and_cases():
    labels = bakeoff.load_labels(ROOT)
    assert sorted(labels) == sorted(FIXTURE_NAMES)
    assert all(8 <= len(v["findings"]) <= 25 for v in labels.values())
    cases = bakeoff.load_verdict_cases(ROOT)
    assert len(cases) == 9
    assert {c["fixture"] for c in cases} <= set(FIXTURE_NAMES)


def test_labels_fall_back_to_the_checkout(tmp_path):
    assert sorted(bakeoff.load_labels(tmp_path)) == sorted(FIXTURE_NAMES)
    assert len(bakeoff.load_verdict_cases(tmp_path)) == 9


# ---------- fake models ----------

def test_install_fakes_covers_every_model_role(ctx):
    fakes = bakeoff.install_fakes(ctx)
    p = ctx.config.pipeline
    wanted = {*p.extractors, p.arbiter, p.ask_map, p.ask_reduce, p.rollup}
    assert {f.spec for f in fakes} == wanted
    for spec in wanted:
        assert ctx.llms(spec) in fakes


@pytest.mark.parametrize("name", FIXTURE_NAMES)
def test_fake_extraction_returns_the_labels(ctx, name):
    bakeoff.install_fakes(ctx)
    parsed = parsed_fixture(name)
    results, degraded = extract.extract(ctx, parsed)
    label = bakeoff.load_labels(ROOT)[name]
    assert degraded == []
    assert len(results) == len(ctx.config.pipeline.extractors)
    assert [c.quote_texts for c in results[0].claims] == label_quote_texts(label)
    assert results[0].who.contact == label["who"]["contact"]
    assert results[0].summary == label["summary"]
    kept, v = verify.verify_claims(parsed, results[0].claims)
    assert v.quotes == v.matched_first_pass == sum(len(f["quotes"]) for f in label["findings"])
    assert len(kept) == len(label["findings"])


@pytest.mark.parametrize("name", FIXTURE_NAMES)
def test_fake_consolidation_joins_the_two_readers(ctx, name):
    bakeoff.install_fakes(ctx)
    parsed = parsed_fixture(name)
    results, _ = extract.extract(ctx, parsed)
    kept = [verify.verify_claims(parsed, r.claims)[0] for r in results]
    claims, degraded = merge.consolidate(ctx, parsed, kept)
    label = bakeoff.load_labels(ROOT)[name]
    assert degraded == []
    assert [c.quote_texts for c in claims] == label_quote_texts(label)
    assert {c.found_by for c in claims} == {2}


def test_fake_extraction_on_an_unlabeled_transcript(ctx):
    bakeoff.install_fakes(ctx)
    text = write_standard(
        {"source": "circleback", "source_id": "m1", "start": "2026-10-01T09:00:00-07:00"},
        "Some notes.",
        [("0:01", "Alex Rivera", "Thanks for the time today, how do you get store footage on a claim right now?"),
         ("0:09", "Jane Doe", "We send two field investigators from outside vendors and they want the claim "
                              "number first."),
         ("0:30", "Jane Doe", "Honestly the diary notes on each claim take my team about a week every month "
                              "to finish.")])
    parsed = parse("transcripts/circleback_m1.md", text)
    results, _ = extract.extract(ctx, parsed)
    assert results[0].claims, "a deterministic claim per long turn"
    kept, v = verify.verify_claims(parsed, results[0].claims)
    assert v.matched_first_pass == v.quotes > 0
    assert results[0].who.contact == "Jane Doe"


def test_fake_verdicts_follow_labels_and_cases(ctx):
    bakeoff.install_fakes(ctx)
    parsed = parsed_fixture("darius-kemp")
    claims = merged_claims(ctx, parsed)
    prep = parse_prep("prep/2026-09-23_darius-kemp.md",
                      (GOLDEN / "prep_darius-kemp.md").read_text(encoding="utf-8"))
    out = verdicts.run_verdicts(ctx, parsed, claims, golden_beliefs().current(), prep.questions)
    assert {(c.target, c.verdict) for c in out.belief_checks} == {
        ("B-004", "contradicts"), ("B-001", "supports"), ("B-002", "supports"), ("B-003", "supports"),
        ("W-001", "supports")}
    assert all(c.decided_by == "agree" for c in out.belief_checks)
    b004 = next(c for c in out.belief_checks if c.target == "B-004")
    by_id = {c.id: c for c in claims}
    assert ("But anything outside the company is off limits for us, because compliance is terrified of claimant "
            "data leaving the building.") in {by_id[cid].quote for cid in b004.claims}
    assert [q.status for q in out.question_coverage] == ["answered", "answered", "answered", "dodged", "never_asked"]
    assert out.degraded == []


def test_fake_verdicts_name_speakers_on_single_stream(ctx):
    bakeoff.install_fakes(ctx)
    parsed = parsed_fixture("viktor-lang")
    claims = merged_claims(ctx, parsed)
    out = verdicts.run_verdicts(ctx, parsed, claims, golden_beliefs().current(), [])
    assert ("B-001", "contradicts") in {(c.target, c.verdict) for c in out.belief_checks}
    assert set(out.speakers) == {c.id for c in claims}
    assert set(out.speakers.values()) == {"Viktor Lang"}


def test_fake_arbiter_sides_with_the_first_reader(ctx):
    responder = bakeoff.LabelResponder(bakeoff.load_labels(ROOT), bakeoff.load_verdict_cases(ROOT))
    first, second = ctx.config.pipeline.extractors[:2]
    ctx.llms.register(FakeLLM(first, responder))
    ctx.llms.register(FakeLLM(second, lambda s, p, schema: (
        responder(s, p, schema) if schema is extract.EXTRACT_SCHEMA
        else {"belief_checks": [], "question_coverage": [], "speakers": []})))
    arbiter = FakeLLM(ctx.config.pipeline.arbiter, responder)
    ctx.llms.register(arbiter)
    parsed = parsed_fixture("darius-kemp")
    claims = merged_claims(ctx, parsed)
    prep = parse_prep("prep/p.md", (GOLDEN / "prep_darius-kemp.md").read_text(encoding="utf-8"))
    out = verdicts.run_verdicts(ctx, parsed, claims, golden_beliefs().current(), prep.questions)
    assert len(arbiter.calls) == 1
    assert {(c.target, c.verdict) for c in out.belief_checks} == {
        ("B-004", "contradicts"), ("B-001", "supports"), ("B-002", "supports"), ("B-003", "supports"),
        ("W-001", "supports")}
    assert {c.decided_by for c in out.belief_checks} == {"arbiter"}
    assert [q.status for q in out.question_coverage] == ["answered", "answered", "answered", "dodged", "never_asked"]


def test_fake_generic_replies():
    r = bakeoff.LabelResponder({}, [])
    schema = {"type": "object", "properties": {
        "answer": {"type": "string"},
        "citations": {"type": "array", "items": {"type": "object"}},
        "n": {"type": "integer"},
        "ok": {"type": "boolean"},
        "kind": {"type": "string", "enum": ["x", "y"]},
        "maybe": {"anyOf": [{"type": "string"}, {"type": "null"}]},
        "nested": {"type": "object", "properties": {"label": {"type": "string"}}},
    }}
    assert r("system", "prompt", schema) == {
        "answer": bakeoff.FAKE_TEXT, "citations": [], "n": 0, "ok": False, "kind": "x", "maybe": None,
        "nested": {"label": "fake"}}
    assert r("system", "prompt", None) == bakeoff.FAKE_TEXT
    assert r("system", "no transcript here", extract.REPAIR_SCHEMA) == {"quote": None}
    assert r("system", "no findings here", merge.CONSOLIDATE_SCHEMA) == {"groups": []}
    assert r("system", "nothing", verdicts.VERDICT_SCHEMA) == {
        "belief_checks": [], "question_coverage": [], "speakers": []}


def test_fake_ask_map_and_reduce():
    ask = pytest.importorskip("nua.ask")
    r = bakeoff.LabelResponder({}, [])
    claims = [Claim(id="c01", text="Small stores overwrite their camera footage.", quote="loop over"),
              Claim(id="c02", text="Attorneys make liability claims cost more.", quote="the claim costs more")]
    d = Digest(call_id="2026-09-23_darius-kemp", transcript="transcripts/t.md", status="digested", origin="live",
               claims=claims)
    prompt = "### Question\n\nHow do attorneys shape claim costs?\n\n### Call digests\n\n" + ask.digest_block(d)
    picked = r("system", prompt, ask.MAP_SCHEMA)["relevant"]
    assert [(p["call_id"], p["claim_id"]) for p in picked] == [("2026-09-23_darius-kemp", "c02")]
    evidence = ('### Evidence\n\n[E1] 2026-09-23_darius-kemp | 2026-09-23 | Darius Kemp | '
                'seat: claims adjuster (staff) | speaker: Darius Kemp (span, labels named)\n'
                'claim: Attorneys make liability claims cost more.\n'
                'scope: none; flags: none\nwhy kept: shares words\nquote: "the claim costs more"\n')
    out = r("system", evidence, ask.REDUCE_SCHEMA)
    assert out["citations"] == [{"call_id": "2026-09-23_darius-kemp", "quote": "the claim costs more"}]
    assert out["answer"].startswith(bakeoff.FAKE_TEXT) and "2026-09-23_darius-kemp" in out["answer"]


def test_fake_rollup_cites_one_finding_per_call():
    rollup = pytest.importorskip("nua.rollup")
    r = bakeoff.LabelResponder({}, [])
    prompt = "\n".join(f"[{cid}#c0{n}] NEW claims adjuster (staff); undated; X; unprompted. Text. Quote: \"q\""
                       for cid in ("a_call", "b_call", "c_call", "d_call") for n in (1, 2))
    out = r("system", prompt, rollup.ROLLUP_SCHEMA)
    assert out["themes"][0]["claims"] == ["a_call#c01", "b_call#c01", "c_call#c01", "d_call#c01"]
    [proposal] = out["proposals"]
    assert proposal["type"] == "add" and proposal["sources"] == out["themes"][0]["claims"]
    assert set(proposal) == set(rollup.ROLLUP_SCHEMA["properties"]["proposals"]["items"]["properties"])
    two_calls = "[a_call#c01] x\n[b_call#c01] y"
    assert r("system", two_calls, rollup.ROLLUP_SCHEMA)["proposals"] == []
    assert r("system", "no findings", rollup.ROLLUP_SCHEMA) == {"themes": [], "proposals": []}


# ---------- scoring ----------

def test_match_labels_is_one_to_one_on_span_overlap():
    parsed = parsed_fixture("darius-kemp")
    labels = bakeoff.load_labels(ROOT)["darius-kemp"]["findings"]
    claims = label_claims(parsed, labels)
    assert len(bakeoff.match_labels(parsed, labels, claims)) == len(labels)
    assert len(bakeoff.match_labels(parsed, labels, claims[:5])) == 5
    doubled = claims + [claims[0]]
    assert len(bakeoff.match_labels(parsed, labels, doubled)) == len(labels)
    unverified = [Claim(id="c01", text="x", quote=labels[0]["quotes"][0]["quote"])]
    assert bakeoff.match_labels(parsed, labels, unverified) == []


def test_a_finding_matches_a_label_through_any_quote():
    parsed = parsed_fixture("darius-kemp")
    labels = bakeoff.load_labels(ROOT)["darius-kemp"]["findings"]
    multi = next(i for i, lab in enumerate(labels) if len(lab["quotes"]) == 3)
    full = label_claims(parsed, labels)[multi]
    # a model finding holding only the label's last quote, plus a quote of its own, still matches
    last_only = full.model_copy(update={"quotes": [full.quotes[-1]]})
    assert bakeoff.match_labels(parsed, labels, [last_only]) == [(multi, 0)]
    # and one quoting none of the label's quotes does not
    other = label_claims(parsed, labels)[0]
    assert bakeoff.match_labels(parsed, [labels[multi]], [other]) == []


def test_locate_short_answer_uses_the_question_turn():
    parsed = parsed_fixture("darius-kemp")
    index = verify.QuoteIndex(parsed)
    span = bakeoff.locate(index, "Nowhere close to that.", 109)
    assert span is not None
    turn = next(t for t in parsed.turns if t.start <= span[0] < t.end)
    assert turn.index > 109
    assert (turn.index, turn.speaker) == (110, "Participant 2")
    yeah = bakeoff.locate(index, "Yeah.", 140)               # many "Yeah." turns: the first after T140 wins
    assert yeah is not None
    assert next(t for t in parsed.turns if t.start <= yeah[0] < t.end).index == 143
    assert bakeoff.locate(index, "words that are not in this call at all", None) is None


def test_one_block_label_resolves_to_the_occurrence_verify_picks():
    """A one-block Granola export: the label's question_turn is its own turn, and the first "No." opens
    the block with no question before it, so verify keeps the second. The bake-off must place it there too."""
    raw = "Notes.\n\nTranscript:\nMe: No. Do you keep the footage? No.\n"
    parsed = parse("transcripts/x.md", raw, commit_date="2026-10-04")
    [turn] = parsed.turns
    [kept], _ = verify.verify_claims(parsed, [Claim(id="c01", text="x", quote="No.", question_turn=turn.index)])
    assert kept.offset[0] == raw.rindex("No.")
    assert bakeoff.locate(verify.QuoteIndex(parsed), "No.", turn.index) == kept.offset
    case = {"fixture": "x", "quote": "No.", "question_turn": turn.index}
    assert bakeoff._case_claim(parsed, case, None).offset == kept.offset


# ---------- the whole bake-off ----------

@pytest.fixture
def bake_root(tmp_path: Path) -> Path:
    copy_config(tmp_path)
    for sub in ("fixtures", "labels", "golden"):
        shutil.copytree(ROOT / "tests" / sub, tmp_path / "tests" / sub)
    return tmp_path


def weak(good):
    """Finds every other labeled claim and reads every verdict backwards."""
    flip = {"supports": "contradicts", "contradicts": "supports", "scope_mismatch": "supports"}

    def reply(system, prompt, schema):
        out = good(system, prompt, schema)
        if schema is extract.EXTRACT_SCHEMA:
            out = {**out, "findings": out["findings"][::2]}
        elif schema is verdicts.VERDICT_SCHEMA:
            out = {**out, "belief_checks": [{**c, "verdict": flip[c["verdict"]]} for c in out["belief_checks"]]}
        return out
    return reply


def test_run_bakeoff_scores_models_and_writes_the_report(bake_root):
    ctx = Ctx.for_tests(bake_root)
    good = bakeoff.LabelResponder(bakeoff.load_labels(bake_root), bakeoff.load_verdict_cases(bake_root))
    ctx.llms.register(FakeLLM("fake:good", good))
    ctx.llms.register(FakeLLM("fake:weak", weak(good)))
    ctx.llms.register(FakeLLM("fake:down", lambda s, p, schema: LLMError("provider down")))

    report = bakeoff.run_bakeoff(ctx, ["fake:good", "fake:weak", "fake:down"], root=bake_root)

    ex = {s.model: s for s in report.extractors}
    assert ex["fake:good"].recall == 1.0
    assert ex["fake:good"].first_pass_rate == 1.0
    assert ex["fake:good"].passes_bar
    assert 0.3 < ex["fake:weak"].recall < 0.7
    assert not ex["fake:weak"].passes_bar
    assert ex["fake:down"].recall == 0.0
    assert all(run.error for run in ex["fake:down"].runs.values())

    judges = {j.model: j for j in report.judges}
    assert judges["fake:good"].accuracy == 1.0
    assert judges["fake:good"].passes_p1
    assert judges["fake:weak"].accuracy < 0.5
    assert judges["fake:down"].accuracy == 0.0

    pair = next(p for p in report.pairs if set(p.models) == {"fake:good", "fake:weak"})
    assert pair.recall == 1.0
    roles = report.suggested_roles()
    assert roles["extractors"] == ["fake:good", "fake:weak"], "a model that never answered is not an extractor"
    assert roles["arbiter"] == "fake:down"
    assert roles["best_judge"] == "fake:good" and not roles["best_judge_is_arbiter"]

    assert report.path == "docs/research/bakeoff-2026-10-01.md"
    text = (bake_root / report.path).read_text(encoding="utf-8")
    assert "fake:good" in text and "Recall bar" in text
    assert "`fake:good` judged best (1.00)" in text and "had extraction errors" in text
    assert EM_DASH not in text
    table = report.table()
    assert "fake:weak" in table and EM_DASH not in table


def test_run_bakeoff_does_not_overwrite_an_earlier_report(bake_root):
    ctx = Ctx.for_tests(bake_root)
    good = bakeoff.LabelResponder(bakeoff.load_labels(bake_root), bakeoff.load_verdict_cases(bake_root))
    ctx.llms.register(FakeLLM("fake:good", good))
    first = bakeoff.run_bakeoff(ctx, ["fake:good"], root=bake_root)
    second = bakeoff.run_bakeoff(ctx, ["fake:good"], root=bake_root)
    assert first.path == "docs/research/bakeoff-2026-10-01.md"
    assert second.path == "docs/research/bakeoff-2026-10-01-2.md"
    assert first.suggested_roles() == {}


def _scored_report(accuracy: dict[str, float], pair_recall: dict[tuple[str, str], float]) -> bakeoff.BakeoffReport:
    """A report over one 10-label fixture with the given judge accuracies and pair recalls."""
    models = list(accuracy)
    extractors = [bakeoff.ExtractorScore(m, {"f1": bakeoff.FixtureRun("f1", labels=10, matched=8)}) for m in models]
    pairs = [bakeoff.PairScore(p, {"f1": (round(r * 10), 10)}) for p, r in pair_recall.items()]
    judges = [bakeoff.JudgeScore(m, [bakeoff.JudgeResult({"expected": "supports"}, "supports", i < round(a * 10))
                                     for i in range(10)]) for m, a in accuracy.items()]
    return bakeoff.BakeoffReport(date="2026-10-01", models=models, fixtures=["f1"], extractors=extractors,
                                 pairs=pairs, judges=judges)


def test_suggested_roles_say_when_the_best_judge_is_not_the_arbiter():
    """SPEC: the best judge becomes arbiter. When the other two cannot make a pair above the bar, the ranking
    makes the best judge an extractor instead, and the report must say so rather than hide it."""
    report = _scored_report({"a": 1.0, "b": 0.6, "c": 0.5},
                            {("a", "b"): 0.9, ("a", "c"): 0.8, ("b", "c"): 0.7})
    roles = report.suggested_roles()
    assert roles["arbiter"] == "b" and "a" in roles["extractors"]
    assert roles["best_judge"] == "a" and roles["best_judge_accuracy"] == 1.0
    assert roles["best_judge_is_arbiter"] is False
    assert roles["best_judge_pair"] == ["b", "c"] and roles["best_judge_pair_recall"] == pytest.approx(0.7)
    for text in (report.table(), report.markdown()):
        assert "`a` judged best (1.00)" in text
        assert "`b + c` recall 0.70 misses the bar" in text
        assert "SPEC says the best judge becomes arbiter" in text
        assert EM_DASH not in text


def test_suggested_roles_quiet_when_the_best_judge_is_the_arbiter():
    report = _scored_report({"a": 1.0, "b": 0.6, "c": 0.5},
                            {("a", "b"): 0.9, ("a", "c"): 0.8, ("b", "c"): 0.85})
    roles = report.suggested_roles()
    assert roles["arbiter"] == "a" and roles["best_judge"] == "a" and roles["best_judge_is_arbiter"] is True
    for text in (report.table(), report.markdown()):
        assert "judged best" not in text


def test_best_judge_skips_a_judge_that_errored():
    report = _scored_report({"a": 1.0, "b": 0.6, "c": 0.5},
                            {("a", "b"): 0.9, ("a", "c"): 0.8, ("b", "c"): 0.85})
    report.judges[0].results[-1] = bakeoff.JudgeResult({"expected": "supports"}, "error", False, error="timeout")
    assert report.suggested_roles()["best_judge"] == "b"


def test_module_has_no_em_dash():
    assert EM_DASH not in (ROOT / "nua" / "bakeoff.py").read_text(encoding="utf-8")


# ---------- a team's own labels ----------

def own_labels(root: Path, names: list[str]) -> Path:
    """`labels/` under root: the given shipped labels, each pointing at its transcript in `calls/`, and no
    verdict cases."""
    folder = root / "labels"
    folder.mkdir()
    (root / "calls").mkdir()
    for name in names:
        shutil.copy(ROOT / "tests" / "fixtures" / f"{name}.md", root / "calls" / f"{name}.md")
        label = bakeoff.load_labels(ROOT)[name]
        (folder / f"{name}.yml").write_text(yaml.safe_dump({**label, "transcript": f"calls/{name}.md"},
                                                           sort_keys=False), encoding="utf-8")
    return folder


def test_run_bakeoff_scores_a_labels_folder(tmp_path):
    copy_config(tmp_path)
    own_labels(tmp_path, ["lena-okafor", "viktor-lang"])
    ctx = Ctx.for_tests(tmp_path)
    bakeoff.install_fakes(ctx, ["fake:good"], root=tmp_path, labels="labels")
    report = bakeoff.run_bakeoff(ctx, ["fake:good"], root=tmp_path, labels="labels")
    assert report.fixtures == ["lena-okafor", "viktor-lang"]
    assert report.extractors[0].recall == 1.0
    assert not report.has_cases and "no verdict cases" in report.table()
    assert "judging was skipped" in (tmp_path / report.path).read_text(encoding="utf-8")
    with pytest.raises(FileNotFoundError):
        bakeoff.run_bakeoff(ctx, ["fake:good"], root=tmp_path, labels="nowhere")


def test_fixture_path_order(tmp_path):
    folder = tmp_path / "labels"
    folder.mkdir()
    (tmp_path / "transcripts").mkdir()
    (tmp_path / "transcripts" / "x.md").write_text("t")
    assert bakeoff.fixture_path(tmp_path, "x", {}, folder) == tmp_path / "transcripts" / "x.md"
    (folder / "x.md").write_text("t")
    assert bakeoff.fixture_path(tmp_path, "x", {}, folder) == folder / "x.md"
    (tmp_path / "y.md").write_text("t")
    assert bakeoff.fixture_path(tmp_path, "x", {"transcript": "y.md"}, folder) == tmp_path / "y.md"
    assert bakeoff.fixture_path(tmp_path, "z", {}, folder) is None


@pytest.mark.parametrize("name", FIXTURE_NAMES)
def test_draft_label_round_trips_the_labels(ctx, name):
    """With the label-backed fakes, a draft label holds the labeled findings, in the labels format."""
    bakeoff.install_fakes(ctx)
    ctx.repo.write(f"transcripts/{name}.md", fixture_text(name))
    fixture, text = bakeoff.draft_label(ctx, f"transcripts/{name}.md")
    assert fixture == name and text.startswith("# DRAFT label")
    draft = yaml.safe_load(text)
    label = bakeoff.load_labels(ROOT)[name]
    assert draft["speaker_labels"] == label["speaker_labels"]
    assert draft["transcript"] == f"transcripts/{name}.md"
    assert len(draft["findings"]) == len(label["findings"])
    assert all(1 <= len(f["quotes"]) <= 3 and all(q["speaker"] for q in f["quotes"]) for f in draft["findings"])
    if label["speaker_labels"] in ("me_them", "single_stream"):
        assert all(q["attribution"] == "inferred" for f in draft["findings"] for q in f["quotes"])
    parsed = parsed_fixture(name)
    claims = label_claims(parsed, draft["findings"])
    assert len(bakeoff.match_labels(parsed, label["findings"], claims)) == len(label["findings"])
    assert EM_DASH not in text


def test_label_command_writes_a_draft_and_bakeoff_scores_it(tmp_path, capsys):
    from nua import __main__ as cli

    copy_config(tmp_path)
    for sub in ("fixtures", "labels", "golden"):
        shutil.copytree(ROOT / "tests" / sub, tmp_path / "tests" / sub)
    (tmp_path / "transcripts").mkdir()
    args = ["--root", str(tmp_path), "label", str(tmp_path / "tests" / "fixtures" / "wade-pruitt.md"), "--fake"]
    assert cli.main(args) == 0
    draft = tmp_path / "labels" / "wade-pruitt.yml"
    assert draft.exists() and "bakeoff --labels labels" in capsys.readouterr().out
    assert cli.main(args) == 1, "an existing draft is never overwritten without --force"
    assert cli.main([*args, "--force"]) == 0
    code = cli.main(["--root", str(tmp_path), "bakeoff", "--fake", "--labels", "labels", "--models", "fake:a"])
    out = capsys.readouterr().out
    assert code == 0 and "wade-pruitt" in out and "1.00" in out
