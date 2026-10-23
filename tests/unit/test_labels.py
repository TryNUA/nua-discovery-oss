"""Golden set and hand labels (Task 10).

Every quote in tests/labels/, tests/golden/ and tests/expected/ must be copied character for
character from its fixture's transcript body, never from the recorder notes above it. The search
lives in this file on purpose: the labels are what discovery/verify.py is scored against, so they must
not depend on it.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

from conftest import FIXTURE_NAMES, ROOT, fixture_text

LABELS = ROOT / "tests" / "labels"
GOLDEN = ROOT / "tests" / "golden"
EXPECTED = ROOT / "tests" / "expected"

TASK_FILES = [
    GOLDEN / "beliefs.md",
    GOLDEN / "decisions.md",
    GOLDEN / "prep_darius-kemp.md",
    LABELS / "wade-pruitt.yml",
    LABELS / "viktor-lang.yml",
    LABELS / "lena-okafor.yml",
    LABELS / "darius-kemp.yml",
    LABELS / "verdict_cases.yml",
    LABELS / "README.md",
    EXPECTED / "brief_darius-kemp.md",
    EXPECTED / "conflicts_first-four.md",
    Path(__file__),
]

EM_DASH = "\u2014"
VERDICTS = {"supports", "contradicts", "scope_mismatch"}
COVERAGE = {"answered", "dodged", "never_asked"}
ROLES = {"carrier", "intermediary", "insured", "vendor", "expert", "competitor"}
SCOPE_KEYS = {"line", "party", "segment", "seat"}
FINDING_KEYS = {"text", "quotes", "scope"}
QUOTE_KEYS = {"quote", "speaker", "question_turn", "attribution"}
SHORT_ANSWER_WORDS = 8
MIN_FINDINGS, MAX_FINDINGS = 8, 25
MAX_QUOTES = 3

TURN_LINE = re.compile(r"^\[(\d+:\d{2}(?::\d{2})?)\] \*\*(.+?)\*\*: ?(.*)$", re.M)
TRANSCRIPT_MARKER = re.compile(r"^Transcript:\s*$", re.M)
GRANOLA_LABEL = re.compile(r"(?<![^\s])(Me|Them|Speaker [A-Z]):(?=\s|$)")
NAMED = ["darius-kemp"]
UNNAMED = ["lena-okafor", "viktor-lang", "wade-pruitt"]

REQUIRED_PREP_QUESTIONS = {
    "Do carriers buy their claims platform or build their own in house?": "dodged",
    "What would a carrier pay per location for a footage preservation tool?": "never_asked",
}


# ---------- transcript helpers (local on purpose, see module docstring) ----------

def transcript_turns(name: str) -> list[tuple[str | None, str | None, str]]:
    """(time, speaker, text) for each turn of the fixture's transcript body, markup removed.

    Circleback copies: one `[m:ss] **Name**: text` line per turn. Granola text export: everything
    after a line that is exactly `Transcript:`, split at each `Me:`, `Them:` or `Speaker X:` label (at a
    line start or after whitespace). A single "Me:" stream is one turn.
    """
    raw = fixture_text(name)
    turns = TURN_LINE.findall(raw)
    if turns:
        return [(time, speaker, text) for time, speaker, text in turns]
    marker = TRANSCRIPT_MARKER.search(raw)
    assert marker, f"{name}: no turn lines and no Transcript: marker"
    body = raw[marker.end():].strip()
    labels = list(GRANOLA_LABEL.finditer(body))
    if not labels:
        return [(None, None, body)]
    ends = [m.start() for m in labels[1:]] + [len(body)]
    return [(None, m.group(1), body[m.end():end].strip()) for m, end in zip(labels, ends)]


def body_text(name: str) -> str:
    """Transcript body with turn markup removed and turns joined by one space."""
    return " ".join(text for _, _, text in transcript_turns(name))


def notes_text(name: str) -> str:
    """Everything above the transcript body: the recorder's overview. Context only."""
    raw = fixture_text(name)
    first = TURN_LINE.search(raw)
    if first:
        return raw[:first.start()]
    return raw[:TRANSCRIPT_MARKER.search(raw).start()]


def normalize(text: str) -> str:
    """Lowercase, straight quotes, collapsed whitespace. Used only to explain a failed exact match."""
    text = text.replace("\u2019", "'").replace("\u2018", "'").replace("\u201c", '"').replace("\u201d", '"')
    return re.sub(r"\s+", " ", text).strip().lower()


def assert_verbatim(name: str, quote: str, where: str) -> None:
    """The quote must occur exactly in the fixture's transcript body."""
    body = body_text(name)
    if quote in body:
        return
    if normalize(quote) in normalize(body):
        pytest.fail(f"{where}: quote only matches after normalizing; copy it exactly: {quote!r}")
    if normalize(quote) in normalize(notes_text(name)):
        pytest.fail(f"{where}: quote comes from the recorder notes, not the transcript: {quote!r}")
    pytest.fail(f"{where}: quote not found in the {name} transcript body: {quote!r}")


def span(name: str, quote: str) -> tuple[int, int]:
    """[start, end) of the quote within body_text(name)."""
    start = body_text(name).find(quote)
    assert start >= 0, f"{name}: quote not in body: {quote!r}"
    return start, start + len(quote)


def turn_of(name: str, quote: str) -> tuple[int, str | None]:
    """(1-based turn index, speaker) of the single turn that contains the quote."""
    for index, (_, speaker, text) in enumerate(transcript_turns(name), start=1):
        if quote in text:
            return index, speaker
    pytest.fail(f"{name}: quote does not sit inside one turn: {quote!r}")


def words(text: str) -> int:
    """Whitespace-separated word count, the unit of the short-answer rule."""
    return len(text.split())


# ---------- loaders ----------

def load_label(name: str) -> dict:
    """tests/labels/<name>.yml as a dict."""
    return yaml.safe_load((LABELS / f"{name}.yml").read_text(encoding="utf-8"))


def label_quotes(label: dict) -> list[dict]:
    """Every quote of every labeled finding, in finding then quote order."""
    return [q for f in label["findings"] for q in f["quotes"]]


def quote_texts(label: dict) -> list[str]:
    return [q["quote"] for q in label_quotes(label)]


def load_cases() -> list[dict]:
    """tests/labels/verdict_cases.yml as a list of cases."""
    return yaml.safe_load((LABELS / "verdict_cases.yml").read_text(encoding="utf-8"))


def seats() -> list[str]:
    """Seat names from config/seats.yml."""
    return yaml.safe_load((ROOT / "config" / "seats.yml").read_text(encoding="utf-8"))


def golden_entries() -> dict[str, dict]:
    """Minimal read of tests/golden/beliefs.md: id -> {section, statement, scope, history}."""
    entries: dict[str, dict] = {}
    section = None
    current = None
    for line in (GOLDEN / "beliefs.md").read_text(encoding="utf-8").splitlines():
        if line.startswith("## "):
            section = line[3:].strip().lower()
            current = None
        elif m := re.match(r"^### ([BW]-\d{3}) (.+)$", line):
            current = {"section": section, "statement": m.group(2).strip(), "scope": None, "history": []}
            entries[m.group(1)] = current
        elif current is not None and line.startswith("- scope: "):
            current["scope"] = line[len("- scope: "):]
        elif current is not None and line.startswith("  - "):
            current["history"].append(line[4:])
    return entries


def golden_decisions() -> dict[str, dict]:
    """Minimal read of tests/golden/decisions.md: id -> {title, rests_on}."""
    decisions: dict[str, dict] = {}
    current = None
    for line in (GOLDEN / "decisions.md").read_text(encoding="utf-8").splitlines():
        if m := re.match(r"^### (D-\d{3}) (.+)$", line):
            current = {"title": m.group(2).strip(), "rests_on": []}
            decisions[m.group(1)] = current
        elif current is not None and (m := re.match(r"^- rests on: (.+)$", line)):
            current["rests_on"] = [x.strip() for x in m.group(1).split(",")]
    return decisions


def prep_parts() -> tuple[dict, str]:
    """(front-matter, body) of tests/golden/prep_darius-kemp.md."""
    text = (GOLDEN / "prep_darius-kemp.md").read_text(encoding="utf-8")
    m = re.match(r"\A---\n(.*?)\n---\n", text, re.S)
    assert m, "prep file needs a front-matter block"
    return yaml.safe_load(m.group(1)), text[m.end():]


def prep_questions() -> list[str]:
    """The numbered `## Questions` items, without their numbers."""
    _, body = prep_parts()
    after = body.split("## Questions", 1)[1]
    return [m.group(1).strip() for m in re.finditer(r"^\d+\.\s+(.+)$", after, re.M)]


def brief_sections() -> dict[str, str]:
    """tests/expected/brief_darius-kemp.md split by `## ` heading, fenced text only."""
    text = (EXPECTED / "brief_darius-kemp.md").read_text(encoding="utf-8")
    sections: dict[str, str] = {}
    for chunk in re.split(r"^## ", text, flags=re.M)[1:]:
        title, _, rest = chunk.partition("\n")
        fence = re.search(r"```text\n(.*?)```", rest, re.S)
        sections[title.strip()] = fence.group(1) if fence else ""
    return sections


def quote_lines(text: str) -> list[str]:
    """Indented lines that start with a quoted string: the quotes a brief shows."""
    return [m.group(1) for m in re.finditer(r'^\s+"(.+?)"(?:\s{2}\(.*\))?\s*$', text, re.M)]


# ---------- file-wide rules ----------

def test_every_task_file_exists():
    missing = [str(p.relative_to(ROOT)) for p in TASK_FILES if not p.exists()]
    assert not missing, missing


@pytest.mark.parametrize("path", TASK_FILES, ids=lambda p: p.name)
def test_no_em_dash(path: Path):
    assert EM_DASH not in path.read_text(encoding="utf-8")


def test_helper_never_searches_the_recorder_notes():
    overview = "Footage is the strongest evidence"
    assert overview in notes_text("darius-kemp")
    assert overview not in body_text("darius-kemp")
    for name, heading in (("lena-okafor", "Dashcams and Evidence in Trucking"),
                          ("viktor-lang", "Video in Property Claims: Little Use")):
        assert heading in notes_text(name)
        assert heading not in body_text(name)


def test_turn_counts_match_the_parser_spec():
    counts = {name: len(transcript_turns(name)) for name in FIXTURE_NAMES}
    assert counts == {"wade-pruitt": 178, "viktor-lang": 1, "lena-okafor": 202, "darius-kemp": 176}


# ---------- per-fixture labels ----------

@pytest.mark.parametrize("name", FIXTURE_NAMES)
def test_label_file_shape(name: str):
    label = load_label(name)
    assert label["fixture"] == name
    assert label["speaker_labels"] in {"named", "me_them", "diarized", "single_stream"}
    who = label["who"]
    assert who["contact"]
    assert who["seat"] in seats()
    assert who["role_in_thesis"] in ROLES
    assert 1 <= len(label["summary"]) <= 5
    assert len(label.get("ask_next") or []) <= 3
    findings = label["findings"]
    assert MIN_FINDINGS <= len(findings) <= MAX_FINDINGS, f"{name}: {len(findings)} findings"
    for f in findings:
        assert set(f) <= FINDING_KEYS, f"unknown keys {set(f) - FINDING_KEYS}"
        assert f["text"].strip()
        assert 1 <= len(f["quotes"]) <= MAX_QUOTES, f"{f['text']!r} needs 1 to {MAX_QUOTES} quotes"
        for q in f["quotes"]:
            assert set(q) <= QUOTE_KEYS, f"unknown keys {set(q) - QUOTE_KEYS}"
            assert q["quote"].strip() and q["speaker"].strip()
        assert isinstance(f["scope"], dict) and f["scope"], f"scope missing for {f['text']!r}"
        assert set(f["scope"]) <= SCOPE_KEYS
        assert f["scope"].get("seat", who["seat"]) in seats()


def test_some_findings_combine_several_quotes():
    sizes = [len(f["quotes"]) for name in FIXTURE_NAMES for f in load_label(name)["findings"]]
    assert max(sizes) == MAX_QUOTES and sum(1 for n in sizes if n > 1) >= 10


@pytest.mark.parametrize("name", FIXTURE_NAMES)
def test_label_quotes_are_verbatim_from_the_body(name: str):
    for i, f in enumerate(load_label(name)["findings"], start=1):
        for q in f["quotes"]:
            assert_verbatim(name, q["quote"], f"{name} finding {i}")


@pytest.mark.parametrize("name", FIXTURE_NAMES)
def test_label_quotes_are_distinct_and_in_transcript_order(name: str):
    findings = load_label(name)["findings"]
    firsts = [span(name, f["quotes"][0]["quote"]) for f in findings]
    assert firsts == sorted(firsts), f"{name}: findings should follow transcript order of their first quote"
    for f in findings:
        own = [span(name, q["quote"]) for q in f["quotes"]]
        assert own == sorted(own), f"{name}: quotes of {f['text']!r} should follow transcript order"
    spans = sorted(span(name, q) for q in quote_texts(load_label(name)))
    for (a_start, a_end), (b_start, b_end) in zip(spans, spans[1:]):
        assert a_end <= b_start, f"{name}: label quotes overlap at {a_start}-{a_end} and {b_start}-{b_end}"


@pytest.mark.parametrize("name", NAMED)
def test_named_speakers_match_the_turn(name: str):
    label = load_label(name)
    assert label["speaker_labels"] == "named"
    for c in label_quotes(label):
        _, speaker = turn_of(name, c["quote"])
        assert c["speaker"] == speaker, f"{c['quote']!r} is said by {speaker}, labeled {c['speaker']}"
        assert c.get("attribution", "span") == "span"


@pytest.mark.parametrize("name", UNNAMED)
def test_unnamed_speakers_are_inferred(name: str):
    label = load_label(name)
    assert label["speaker_labels"] in {"me_them", "single_stream"}
    for c in label_quotes(label):
        assert c["speaker"] == label["who"]["contact"]
        assert c["attribution"] == "inferred"
        if label["speaker_labels"] == "me_them":
            _, speaker = turn_of(name, c["quote"])
            assert speaker == "Them", f"{c['quote']!r} is on a {speaker} turn, which is Alex's microphone"


def test_single_stream_short_answer_follows_its_question_in_the_one_turn():
    label = load_label("viktor-lang")
    assert label["speaker_labels"] == "single_stream"
    [(_, _, text)] = transcript_turns("viktor-lang")
    short = [c for c in label_quotes(label) if words(c["quote"]) <= SHORT_ANSWER_WORDS]
    assert short, "the single-stream fixture should keep a short answer"
    for c in short:
        assert c["question_turn"] == 1
        assert "?" in text[:text.index(c["quote"])]


@pytest.mark.parametrize("name", NAMED + ["wade-pruitt", "lena-okafor"])
def test_short_answers_cite_the_question_turn(name: str):
    turns = transcript_turns(name)
    for c in label_quotes(load_label(name)):
        answer_turn, speaker = turn_of(name, c["quote"])
        if words(c["quote"]) <= SHORT_ANSWER_WORDS:
            q = c.get("question_turn")
            assert isinstance(q, int) and 1 <= q < answer_turn, f"{c['quote']!r} needs question_turn"
            assert turns[q - 1][1] != speaker, "question_turn should be the other side's question"
            assert "?" in turns[q - 1][2]
        else:
            assert "question_turn" not in c, f"only short answers carry question_turn: {c['quote']!r}"


@pytest.mark.parametrize("name", FIXTURE_NAMES)
def test_follow_up_quotes_are_verbatim(name: str):
    for f in load_label(name).get("follow_ups") or []:
        assert f["direction"] in {"we_owe", "they_offered"}
        assert f["what"].strip() and f["who"].strip()
        assert_verbatim(name, f["quote"], f"{name} follow-up {f['what']!r}")


@pytest.mark.parametrize("name", FIXTURE_NAMES)
def test_belief_checks_use_labeled_claims_and_golden_ids(name: str):
    label = load_label(name)
    ids = golden_entries()
    quotes = quote_texts(label)
    targets = [b["target"] for b in label.get("belief_checks") or []]
    assert len(targets) == len(set(targets)), "one check per target"
    for b in label.get("belief_checks") or []:
        assert b["target"] in ids
        assert b["verdict"] in VERDICTS
        assert b["quotes"], "a verdict needs at least one claim"
        for q in b["quotes"]:
            assert q in quotes, f"{b['target']}: {q!r} is not a quote of a labeled finding"


def test_darius_question_coverage_follows_the_prep_file():
    label = load_label("darius-kemp")
    quotes = quote_texts(label)
    coverage = {q["question"]: q for q in label["question_coverage"]}
    assert list(coverage) == prep_questions()
    for question, status in REQUIRED_PREP_QUESTIONS.items():
        assert coverage[question]["status"] == status
    for q in coverage.values():
        assert q["status"] in COVERAGE
        if q["status"] == "never_asked":
            assert not q.get("quotes")
        else:
            assert q["quotes"] and all(x in quotes for x in q["quotes"])


def test_dodged_question_was_asked_at_21_25():
    dodged = next(q for q in load_label("darius-kemp")["question_coverage"] if q["status"] == "dodged")
    answer_turn, _ = turn_of("darius-kemp", dodged["quotes"][0])
    asked = [t for t in transcript_turns("darius-kemp")[:answer_turn - 1] if t[1] == "Alex Rivera"][-1]
    assert asked[0] == "21:25" and "build" in asked[2]


# ---------- verdict cases ----------

def test_verdict_cases_cover_both_sides_of_each_conflict():
    cases = load_cases()
    ids = golden_entries()
    by_target: dict[str, set[str]] = {}
    for case in cases:
        assert {"fixture", "target", "expected", "quote"} <= set(case)
        assert case["fixture"] in FIXTURE_NAMES
        assert case["target"] in ids
        assert case["expected"] in VERDICTS
        assert set(case.get("accept") or []) <= VERDICTS
        by_target.setdefault(case["target"], set()).add(case["expected"])
    assert by_target == {t: {"supports", "contradicts"} for t in ("B-001", "B-002", "B-003")}


def test_verdict_case_quotes_are_verbatim():
    for case in load_cases():
        assert_verbatim(case["fixture"], case["quote"], f"verdict case {case['target']}")


def test_verdict_cases_agree_with_the_fixture_labels():
    for case in load_cases():
        label = load_label(case["fixture"])
        claim = next((q for q in label_quotes(label) if q["quote"] == case["quote"]), None)
        assert claim, f"{case['quote']!r} is not a quote of a labeled finding of {case['fixture']}"
        assert case.get("question_turn") == claim.get("question_turn")
        check = next(b for b in label["belief_checks"] if b["target"] == case["target"])
        assert check["verdict"] == case["expected"]
        assert case["quote"] in check["quotes"]


def test_contradicting_sides_also_accept_scope_mismatch():
    for case in load_cases():
        if case["expected"] == "contradicts":
            assert case["accept"] == ["scope_mismatch"]


# ---------- golden beliefs, decisions, prep ----------

HISTORY = re.compile(
    r'^\d{4}-\d{2}-\d{2} (added|watched) by (Alex|Sam|Jordan) via (slack|agent|bootstrap)'
    r'(?:, source (?P<sources>.+?))?(?:, reason: [^,]+)?, ref (?P<ref>\S+)$'
)
SOURCE = re.compile(r'(\S+) "((?:[^"\\]|\\.)*)"')


def test_golden_beliefs_hold_the_test_entries():
    entries = golden_entries()
    assert {k: v["section"] for k, v in entries.items()} == {
        "B-001": "beliefs", "B-002": "beliefs", "B-003": "beliefs", "B-004": "beliefs", "W-001": "watching"}
    for entry_id, e in entries.items():
        assert e["scope"], f"{entry_id} needs a scope line"
        assert e["history"], f"{entry_id} needs a history line"


def test_golden_history_lines_follow_the_grammar_and_quote_fixtures():
    for entry_id, e in golden_entries().items():
        for line in e["history"]:
            m = HISTORY.match(line)
            assert m, f"{entry_id}: bad history line {line!r}"
            ref = m.group("ref")
            assert re.fullmatch(r"\d{10}\.\d{6}:[\w:-]+|requests/\S+\.yml@[0-9a-f]{40}", ref), ref
            for call_id, quote in SOURCE.findall(m.group("sources") or ""):
                fixture = call_id.split("_", 1)[1]
                assert fixture in FIXTURE_NAMES
                assert_verbatim(fixture, quote, f"{entry_id} history source")


def test_golden_decision_rests_on_three_beliefs():
    assert golden_decisions() == {"D-002": {"title": "Footage review tool for adjusters",
                                            "rests_on": ["B-001", "B-003", "B-004"]}}


def test_prep_file_follows_the_template():
    front, body = prep_parts()
    for key in ("planned_date", "contact", "firm", "seat", "caller"):
        assert front.get(key), f"prep needs {key}"
    assert front["contact"] == "Darius Kemp"
    assert front["seat"] in seats()
    assert "## Context" in body and "## Questions" in body
    questions = prep_questions()
    assert 1 <= len(questions) <= 5
    for q in REQUIRED_PREP_QUESTIONS:
        assert q in questions


# ---------- golden brief ----------

def darius_checks() -> list[dict]:
    """Expected belief checks for the Darius call."""
    return load_label("darius-kemp")["belief_checks"]


def test_brief_has_no_retired_markers():
    text = (EXPECTED / "brief_darius-kemp.md").read_text(encoding="utf-8")
    assert not re.search(r"\b[FQC]-\d{3}\b", text), "no F-, Q- or C- ids in a v2 brief"
    assert "polarity" not in text.lower()
    assert "recall" not in text.lower()


def test_brief_sections_are_the_v2_layout():
    assert list(brief_sections()) == [
        "Top message",
        "Thread 1: Summary",
        "Thread 2: New findings",
        "Thread 3: Questions",
    ]


VERDICT_WORDS = {"contradicts": "contradicts", "supports": "supports", "scope mismatch": "scope_mismatch"}
BELIEF_ITEM = re.compile(r'^(Contradicts|Supports|Scope mismatch) (B-\d{3}) "(.+?)"', re.M)
WATCHING_ITEM = re.compile(r'^Watching (W-\d{3}), (contradicts|supports|scope mismatch) "(.+?)"', re.M)


def what_changed_items(top: str) -> list[tuple[str, str, str]]:
    """(verdict, target, statement) for each what-changed line, in the order they appear."""
    found = [(m.start(), VERDICT_WORDS[m.group(1).lower()], m.group(2), m.group(3)) for m in BELIEF_ITEM.finditer(top)]
    found += [(m.start(), VERDICT_WORDS[m.group(2)], m.group(1), m.group(3)) for m in WATCHING_ITEM.finditer(top)]
    return [(verdict, target, statement) for _, verdict, target, statement in sorted(found)]


def test_brief_what_changed_matches_the_labeled_verdicts():
    top = brief_sections()["Top message"]
    entries = golden_entries()
    decision = golden_decisions()["D-002"]
    assert f"Touches D-002 ({decision['title']})" in top
    assert "WHAT CHANGED" in top
    items = what_changed_items(top)
    got = [(verdict, target) for verdict, target, _ in items]
    assert sorted(got) == sorted((b["verdict"], b["target"]) for b in darius_checks())
    order = {"contradicts": 0, "scope_mismatch": 1, "supports": 2}
    ranks = [(target.startswith("W"), order[verdict]) for verdict, target in got]
    assert ranks == sorted(ranks), "contradictions, scope mismatches, supports, then Watching"
    for _, target, statement in items:
        assert statement == entries[target]["statement"]


def bullets(text: str) -> list[str]:
    """Text of each `- ` bullet line."""
    return re.findall(r"^- (.+)$", text, re.M)


def test_brief_thread_text_follows_the_labels():
    label = load_label("darius-kemp")
    sections = brief_sections()
    assert bullets(sections["Thread 1: Summary"]) == label["summary"]
    # follow-ups and ask-next stay in the digest; the brief does not post them
    text = "\n".join(sections.values())
    for gone in label["ask_next"] + [f["what"] for f in label["follow_ups"]] + [f["quote"] for f in label["follow_ups"]]:
        assert gone not in text


def test_brief_quotes_are_verbatim_and_belong_to_their_items():
    sections = brief_sections()
    checked = {q for b in darius_checks() for q in b["quotes"]}
    for q in quote_lines(sections["Top message"]):
        assert_verbatim("darius-kemp", q, "brief top message")
        assert q in checked, f"top-message quote is not in a belief check: {q!r}"
    for title, text in sections.items():
        for q in quote_lines(text):
            assert_verbatim("darius-kemp", q, f"brief {title}")


def unchecked(label: dict) -> list[dict]:
    """The findings no belief check cites by any of their quotes."""
    checked = {q for b in label["belief_checks"] for q in b["quotes"]}
    return [f for f in label["findings"] if not any(q["quote"] in checked for q in f["quotes"])]


def test_brief_new_findings_are_the_unchecked_claims():
    expected = [f["quotes"][0]["quote"] for f in unchecked(load_label("darius-kemp"))]
    firsts = [m.group(1) for m in re.finditer(r'^- .+\n  "(.+)"$', brief_sections()["Thread 2: New findings"], re.M)]
    assert firsts == expected


def test_brief_count_line_matches_the_labels():
    label = load_label("darius-kemp")
    findings = len(unchecked(label))
    dodged = sum(1 for q in label["question_coverage"] if q["status"] == "dodged")
    top = brief_sections()["Top message"]
    m = re.search(r"^(\d+) new findings · (\d+) dodged questions? · digest on GitHub$", top, re.M)
    assert m, "count line missing"
    assert tuple(int(x) for x in m.groups()) == (findings, dodged)


def test_brief_questions_follow_the_coverage_labels():
    text = brief_sections()["Thread 3: Questions"]
    blocks = dict(re.findall(r"^(Answered|Dodged|Never asked)\n((?:[ -].*\n?)*)", text, re.M))
    status_of = {"Answered": "answered", "Dodged": "dodged", "Never asked": "never_asked"}
    listed = {status_of[k]: re.findall(r"^- (.+)$", v, re.M) for k, v in blocks.items()}
    for q in load_label("darius-kemp")["question_coverage"]:
        assert q["question"] in listed[q["status"]]


# ---------- verdict-case write-up and README ----------

def test_conflicts_file_is_retitled_as_verdict_cases():
    text = (EXPECTED / "conflicts_first-four.md").read_text(encoding="utf-8")
    assert text.splitlines()[0].startswith("# Verdict cases")
    for heading in ("## C-001", "## C-002", "## C-003"):
        assert heading in text
    assert "tests/labels/verdict_cases.yml" in text


def test_conflicts_file_quotes_are_verbatim():
    text = (EXPECTED / "conflicts_first-four.md").read_text(encoding="utf-8")
    quotes = re.findall(r'"([^"\n]{8,})"', text)
    assert len(quotes) >= 11
    for q in quotes:
        assert any(q in body_text(n) for n in FIXTURE_NAMES), f"not verbatim in any fixture: {q!r}"


def test_readme_states_the_matching_rule_and_recall_bar():
    text = (LABELS / "README.md").read_text(encoding="utf-8")
    assert "50%" in text
    assert "0.8" in text
