"""Model bake-off, and the label-backed fake models behind `--fake`.

Both are driven by the hand labels in `tests/labels/` (rules in `tests/labels/README.md`):

- `install_fakes(ctx)` registers a deterministic fake for every model role in
  `config/pipeline.yml`. On the four fixtures, extraction returns the labeled findings, consolidation
  groups findings with the same text, verdicts return the labeled belief checks, verdict cases and
  question coverage, and the arbiter sides with the first reader. Any other prompt (ask, roll-up, an unlabeled transcript) gets a small
  deterministic answer. So the whole pipeline runs offline with no keys.
- `run_bakeoff(ctx, models)` scores each model as extractor (recall of labeled findings on merged
  findings, first-pass quote match rate) and as judge (verdict accuracy on `verdict_cases.yml`),
  scores every extractor pair on merged recall, suggests roles, and writes
  `docs/research/bakeoff-<date>.md`.

Matching rule: a model finding matches a labeled finding when any of its quotes overlaps any of the
labeled finding's quotes by at least 50% (`merge.span_overlap`, intersection over the shorter span).
Matching is one to one: each labeled finding and each model finding is used at most once, best
overlaps first. The bake-off merges by quote overlap (`merge.merge_claims`), with no consolidation
call, so its numbers need no extra model calls.
"""
from __future__ import annotations

import itertools
import re
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Callable

import yaml

from discovery import extract, merge, verdicts, verify
from discovery.beliefs import BeliefsDoc, parse_beliefs
from discovery.context import Ctx
from discovery.llm import FakeLLM, LLMError
from discovery.parse import ParseError, parse
from discovery.types import SCOPE_KEYS, Claim, ParsedTranscript, Quote, Scope, Verification

LABELS_DIR = "tests/labels"
FIXTURES_DIR = "tests/fixtures"
GOLDEN_BELIEFS = "tests/golden/beliefs.md"
REPORT_DIR = "docs/research"
CASES_FILE = "verdict_cases.yml"

MATCH_OVERLAP = 0.5      # README "Matching rule"
RECALL_BAR = 0.8         # README "Recall bar": overall, on merged findings
FIXTURE_FLOOR = 0.7      # README "Recall bar": no fixture below this
FIXTURE_MATCH = 0.5      # share of a label file's quotes a prompt must contain to count as that fixture

FAKE_TEXT = "Fake answer (--fake mode): no model was called."
FAKE_WORD = "fake"
FAKE_THEME = "Fake theme"
_CHECKOUT = Path(__file__).resolve().parents[1]


# ---------- labels ----------

def labels_root(root: str | Path | None = None) -> Path:
    """The directory holding `tests/labels/`: `root` when it has one, else this code checkout."""
    if root is not None and (Path(root) / LABELS_DIR).is_dir():
        return Path(root)
    return _CHECKOUT


def load_labels(root: str | Path | None = None) -> dict[str, dict]:
    """Label files by fixture name (`tests/labels/<fixture>.yml`, verdict cases excluded)."""
    folder = labels_root(root) / LABELS_DIR
    out: dict[str, dict] = {}
    for path in sorted(folder.glob("*.yml")):
        if path.name == CASES_FILE:
            continue
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        if isinstance(data, dict) and isinstance(data.get("findings"), list):
            out[str(data.get("fixture") or path.stem)] = data
    return out


def load_verdict_cases(root: str | Path | None = None) -> list[dict]:
    """The judge test set, `tests/labels/verdict_cases.yml`, each case with an `accept` list."""
    path = labels_root(root) / LABELS_DIR / CASES_FILE
    if not path.exists():
        return []
    cases = yaml.safe_load(path.read_text(encoding="utf-8")) or []
    return [{**c, "accept": list(c.get("accept") or [])} for c in cases if isinstance(c, dict)]


# ---------- fake models ----------

def _same_quote(a: str, b: str) -> bool:
    return bool(a) and verify.normalize(a) == verify.normalize(b)


def _contains(a: str, b: str) -> bool:
    """One normalized quote inside the other, when they are of comparable length."""
    na, nb = verify.normalize(a), verify.normalize(b)
    if not na or not nb:
        return False
    short, long = sorted((na, nb), key=len)
    return len(short) * 2 >= len(long) and f" {short} " in f" {long} "


def label_findings(label: dict | None) -> list[dict]:
    """The labeled findings of one label file."""
    return [f for f in (label or {}).get("findings") or [] if isinstance(f, dict)]


def label_quotes(finding: dict) -> list[dict]:
    """The quotes of one labeled finding, each a dict with `quote` and optional `speaker`,
    `question_turn` and `attribution`."""
    return [q for q in finding.get("quotes") or [] if isinstance(q, dict) and q.get("quote")]


def _find_claim(quote: str, claims: dict[str, list[str]]) -> str | None:
    """Id of the finding with a quote that is `quote`: exact (normalized) first, then containment."""
    for cid, qs in claims.items():
        if any(_same_quote(q, quote) for q in qs):
            return cid
    for cid, qs in claims.items():
        if any(_contains(q, quote) for q in qs):
            return cid
    return None


def _schema_value(schema: dict | None, name: str = "") -> Any:
    """The smallest value that fits a JSON Schema: empty lists, zeros, first enum values."""
    schema = schema or {}
    if "enum" in schema:
        return schema["enum"][0]
    if "anyOf" in schema:
        options = schema["anyOf"]
        if any(o.get("type") == "null" for o in options):
            return None
        return _schema_value(options[0], name)
    kind = schema.get("type")
    if isinstance(kind, list):
        kind = next((k for k in kind if k != "null"), None)
    if kind == "object":
        props = schema.get("properties") or {}
        return {key: _schema_value(sub, key) for key, sub in props.items()}
    if kind == "array":
        return []
    if kind == "string":
        return FAKE_TEXT if name == "answer" else FAKE_WORD
    if kind in ("integer", "number"):
        return 0
    if kind == "boolean":
        return False
    return None


# The fakes read the filled prompts: these patterns match lines written by discovery/prompts/ (the
# "- <company> people" line of extract.py, the "### Question" heading of ask.py) and by the code that fills
# them (claim, entry, turn and dispute lines). Keep them in step when either changes.
_CLAIM_HEAD = re.compile(r"^(c\d+) \[[^\n]*\]: ", re.M)
_CLAIM_QUOTE = re.compile(r'^  quote \([^\n]*?\): "(.*)"$', re.M)
_FINDING = re.compile(r'^<finding id="([^"]+)" reader="\d+">\ntext: (.*)$', re.M)
_ENTRY_LINE = re.compile(r"^([BW]-\d+) \((?:belief|watching)\):", re.M)
_TURN_LINE = re.compile(r"^\[T(\d+)(?: [^\]]*)?\] (.*)$", re.M)
_SPEAKER = re.compile(r"^([^:\n]{1,60}): (.*)$")
_DISCOVERY_PEOPLE = re.compile(r"^- .{1,80}? people \(the interviewers[^:\n]*: (.*)$", re.M)
_DISPUTE = re.compile(r"^#### ([bqs]\d+):", re.M)
_READER = re.compile(r"^Reader (\d+): (.*)$", re.M)


class LabelResponder:
    """A `FakeLLM` responder that answers from the hand labels.

    Calls are told apart by their schema: extraction, quote repair, verdicts and arbiter get
    label-backed answers; `ask` map and reduce and the roll-up get small answers built from the ids
    and refs in their prompts; any other schema gets its smallest valid value (strings named
    `answer` get `FAKE_TEXT`); plain text calls get `FAKE_TEXT`. The fixture is recognized from the
    quotes in the prompt, so the fakes work whatever the transcript file is called.
    """

    def __init__(self, labels: dict[str, dict], cases: list[dict]):
        self.labels = labels
        self.cases = cases

    def __call__(self, system: str, prompt: str, schema: dict | None) -> Any:
        if schema is None:
            return FAKE_TEXT
        if schema == extract.EXTRACT_SCHEMA:
            return self.extraction(prompt)
        if schema == extract.REPAIR_SCHEMA:
            return {"quote": None}
        if schema == merge.CONSOLIDATE_SCHEMA:
            return self.consolidate(prompt)
        if schema == verdicts.VERDICT_SCHEMA:
            return self.verdicts(prompt)
        if schema == verdicts.ARBITER_SCHEMA:
            return self.arbiter(prompt)
        ask_map, ask_reduce = _ask_schemas()
        if ask_map is not None and schema == ask_map:
            return self.ask_map(prompt)
        if ask_reduce is not None and schema == ask_reduce:
            return self.ask_reduce(prompt)
        if {"themes", "proposals"} <= set(schema.get("properties") or {}):
            return self.rollup(prompt, schema)
        return _schema_value(schema)

    # which fixture

    def _quotes(self, fixture: str) -> list[str]:
        label = self.labels.get(fixture) or {}
        quotes = [q["quote"] for f in label_findings(label) for q in label_quotes(f)]
        quotes += [c.get("quote", "") for c in self.cases if c.get("fixture") == fixture]
        return [q for q in quotes if q]

    def fixture_in_text(self, text: str) -> str | None:
        """The fixture most of whose labeled quotes appear in `text`, if any."""
        hay = f" {verify.normalize(text)} "
        best, best_share = None, 0.0
        for fixture in sorted(self.labels):
            quotes = self._quotes(fixture)
            if not quotes:
                continue
            share = sum(1 for q in quotes if f" {verify.normalize(q)} " in hay) / len(quotes)
            if share > best_share:
                best, best_share = fixture, share
        return best if best_share >= FIXTURE_MATCH else None

    def fixture_for_claims(self, claims: dict[str, list[str]]) -> str | None:
        """The fixture whose labeled quotes match the most of these claim quotes."""
        best, best_hits = None, 0
        for fixture in sorted(self.labels):
            hits = sum(1 for q in self._quotes(fixture) if _find_claim(q, claims))
            if hits > best_hits:
                best, best_hits = fixture, hits
        return best

    # extraction

    def extraction(self, prompt: str) -> dict:
        """The fixture's labels as an extraction reply; a generic reply for any other transcript."""
        fixture = self.fixture_in_text(prompt)
        if fixture is None:
            return self._generic_extraction(prompt)
        label = self.labels[fixture]
        who = label.get("who") or {}
        claims = []
        for c in label_findings(label):
            scope = c.get("scope") or {}
            claims.append({
                "text": c.get("text", ""),
                "quotes": [{"quote": q["quote"], "turns": [], "question_turn": q.get("question_turn"),
                            "speaker": q.get("speaker")} for q in label_quotes(c)],
                "scope": {k: scope.get(k) for k in SCOPE_KEYS},
                "flags": {"hedged": False, "firsthand": False, "prompted": False},
            })
        mentions = label.get("mentions") or {}
        return {
            "who": {"contact": who.get("contact"), "firm": who.get("firm"), "firm_source": "transcript",
                    "seat": who.get("seat"), "institution_type": who.get("institution_type"),
                    "role_in_thesis": who.get("role_in_thesis")},
            "summary": list(label.get("summary") or []),
            "findings": claims,
            "follow_ups": [{"direction": f.get("direction"), "what": f.get("what", ""), "who": f.get("who"),
                            "quote": f.get("quote", "")} for f in label.get("follow_ups") or []],
            "mentions": {"people": list(mentions.get("people") or []), "firms": list(mentions.get("firms") or [])},
            "ask_next": list(label.get("ask_next") or []),
        }

    def _generic_extraction(self, prompt: str) -> dict:
        """One claim for each of the three longest turns, quoting their first 20 words."""
        discovery = set()
        m = _DISCOVERY_PEOPLE.search(prompt)
        if m:
            discovery = {n.strip().lower() for n in m.group(1).split(",")}
        turns = []
        inside = prompt.split("<transcript>", 1)[-1].split("</transcript>", 1)[0]   # not the prompt's examples
        for t in _TURN_LINE.finditer(inside):
            sm = _SPEAKER.match(t.group(2))
            speaker, text = (sm.group(1), sm.group(2)) if sm else (None, t.group(2))
            turns.append((int(t.group(1)), speaker, text))
        external = [t for t in turns if not t[1] or t[1].lower() not in discovery] or turns
        longest = sorted(external, key=lambda t: (-len(t[2].split()), t[0]))[:3]
        claims = [{
            "text": f"Fake finding from turn {n}.",
            "quotes": [{"quote": " ".join(text.split()[:20]), "turns": [n], "question_turn": None,
                        "speaker": speaker}],
            "scope": dict.fromkeys(SCOPE_KEYS),
            "flags": {"hedged": False, "firsthand": False, "prompted": False},
        } for n, speaker, text in sorted(longest)]
        contact = longest[0][1] if longest else None
        return {
            "who": {"contact": contact, "firm": None, "firm_source": None, "seat": None,
                    "institution_type": None, "role_in_thesis": None},
            "summary": ["Fake extraction (--fake mode): no labels for this transcript."],
            "findings": claims, "follow_ups": [], "mentions": {"people": [], "firms": []}, "ask_next": [],
        }

    # verdicts

    def verdicts(self, prompt: str) -> dict:
        """Belief checks from the verdict cases and label checks, coverage and speakers from the labels,
        for the claims in the prompt that match labeled quotes."""
        claims = _prompt_claims(prompt)
        entries = set(_ENTRY_LINE.findall(prompt))
        fixture = self.fixture_for_claims(claims) if claims else None
        empty = {"belief_checks": [], "question_coverage": [], "speakers": []}
        if fixture is None:
            return empty
        label = self.labels.get(fixture) or {}

        # (target, claim id) -> (verdict, reason). Verdict cases decide the verdict; label notes the reason.
        found: dict[tuple[str, str], tuple[str, str]] = {}
        notes: dict[tuple[str, str], str] = {}
        for check in label.get("belief_checks") or []:
            if check.get("target") not in entries:
                continue
            for quote in check.get("quotes") or []:
                cid = _find_claim(quote, claims)
                if cid:
                    notes.setdefault((check["target"], cid), check.get("note") or "")
                    found.setdefault((check["target"], cid), (check["verdict"], ""))
        for case in self.cases:
            cid = _find_claim(case.get("quote", ""), claims) if case.get("fixture") == fixture else None
            if cid and case.get("target") in entries:
                found[(case["target"], cid)] = (case["expected"], "")
        found = {key: (verdict, notes.get(key) or "From the labels.") for key, (verdict, _) in found.items()}
        grouped: dict[tuple[str, str], dict] = {}
        for (target, cid), (verdict, reason) in sorted(found.items()):
            g = grouped.setdefault((target, verdict), {"target": target, "verdict": verdict, "claims": [],
                                                       "reason": reason})
            g["claims"].append(cid)

        coverage = []
        for qc in label.get("question_coverage") or []:
            m = re.search(r"^(\d+)\. " + re.escape(str(qc.get("question", "")).strip()) + r"\s*$", prompt, re.M)
            if not m:
                continue
            ids = [cid for cid in (_find_claim(q, claims) for q in qc.get("quotes") or []) if cid]
            coverage.append({"question": int(m.group(1)), "status": qc.get("status"), "claims": ids,
                             "reason": "From the labels."})

        speakers = []
        for cid, quotes in claims.items():
            said = next((q.get("speaker") for f in label_findings(label) for q in label_quotes(f)
                         if q.get("speaker") and any(_same_quote(q["quote"], x) for x in quotes)), None)
            if said:
                speakers.append({"claim": cid, "speaker": said})
        return {"belief_checks": list(grouped.values()), "question_coverage": coverage, "speakers": speakers}

    # consolidation (discovery/merge.py)

    def consolidate(self, prompt: str) -> dict:
        """Group the findings in the prompt that have the same text (both fakes return the same labels)."""
        groups: dict[str, dict] = {}
        for fid, text in _FINDING.findall(prompt):
            g = groups.setdefault(verify.normalize(text) or fid, {
                "ids": [], "text": text.strip(), "scope": {"line": "", "party": "", "segment": "", "seat": ""},
                "flags": {"hedged": False, "firsthand": False, "prompted": False}})
            g["ids"].append(fid)
        return {"groups": list(groups.values())}

    # arbiter

    def arbiter(self, prompt: str) -> dict:
        """Side with Reader 1, or Reader 2 when Reader 1 gave no answer."""
        out: dict[str, list] = {"belief_checks": [], "question_coverage": [], "speakers": []}
        heads = list(_DISPUTE.finditer(prompt))
        for i, head in enumerate(heads):
            end = heads[i + 1].start() if i + 1 < len(heads) else len(prompt)
            block = prompt[head.end():end].split("\n### ", 1)[0]
            said = [r.group(2).strip() for r in _READER.finditer(block)]
            did = head.group(1)
            if did[0] == "b":
                words = [s.split(".")[0].strip() for s in said]
                verdict = next((w for w in words if w in verdicts.VERDICTS), "none")
                out["belief_checks"].append({"id": did, "verdict": verdict, "reason": "Fake arbiter."})
            elif did[0] == "q":
                pick = next((s for s in said if not s.startswith("no answer")), "")
                status = re.split(r"[,.]", pick, maxsplit=1)[0].strip() if pick else "never_asked"
                ids = re.findall(r"\bc\d+\b", pick.split("Reason:", 1)[0])
                out["question_coverage"].append({"id": did, "status": status, "claims": ids,
                                                 "reason": "Fake arbiter."})
            else:
                name = next((s for s in said if s != "no answer"), "")
                out["speakers"].append({"id": did, "speaker": name})
        return out

    # ask (discovery/ask.py)

    def ask_map(self, prompt: str) -> dict:
        """Per digest, the claim sharing the most words with the question; the first claim if none do."""
        m = _ASK_QUESTION.search(prompt)
        wanted = _content_words(m.group(1) if m else "")
        picks, first = [], None
        for block in _ASK_DIGEST.split(prompt)[1:]:
            call_id, _, rest = block.partition("\n")
            best, best_score = None, 0
            for cm in _ASK_CLAIM.finditer(rest):
                first = first or (call_id.strip(), cm.group(1))
                score = len(wanted & _content_words(cm.group(2) + " " + cm.group(3)))
                if score > best_score:
                    best, best_score = cm.group(1), score
            if best:
                picks.append({"call_id": call_id.strip(), "claim_id": best, "why": "Shares words with the question."})
        if not picks and first:
            picks.append({"call_id": first[0], "claim_id": first[1], "why": "First claim (fake map)."})
        return {"relevant": picks}

    def ask_reduce(self, prompt: str) -> dict:
        """An answer naming the calls, citing up to three evidence quotes as given."""
        items = [(m.group(1), m.group(2)) for m in _ASK_EVIDENCE.finditer(prompt)]
        calls = sorted({call_id for call_id, _ in items})
        answer = (f"{FAKE_TEXT} The map step kept {len(items)} claim(s) from {len(calls)} call(s)"
                  + (f": {', '.join(calls)}." if calls else "."))
        return {"answer": answer, "citations": [{"call_id": c, "quote": q} for c, q in items[:3]]}

    # roll-up (discovery/rollup.py)

    def rollup(self, prompt: str, schema: dict) -> dict:
        """One theme and, with three or more calls, one `add` citing the first finding from each call
        (up to five). Claims are cited by the `<call_id>#<claim id>` refs in the prompt."""
        firsts: dict[str, str] = {}
        for call_id, cid in _ROLLUP_REF.findall(prompt):
            firsts.setdefault(call_id, f"{call_id}#{cid}")
        refs = list(firsts.values())[:5]
        out = {key: [] for key in schema["properties"]}
        if not refs:
            return out
        theme = _schema_value(schema["properties"]["themes"].get("items"))
        out["themes"] = [{**theme, "name": FAKE_THEME, "claims": refs}]
        if len(refs) >= 3:
            proposal = _schema_value(schema["properties"]["proposals"].get("items"))
            wanted = {"type": "add", "statement": f"Fake proposal (--fake mode) from {len(refs)} calls",
                      "sources": refs, "theme": FAKE_THEME, "rationale": "Fake roll-up (--fake mode)."}
            out["proposals"] = [{**proposal, **{k: v for k, v in wanted.items() if k in proposal}}]
        return out


def _prompt_claims(prompt: str) -> dict[str, list[str]]:
    """Finding id -> its quotes, from the finding blocks a verdict prompt lists (discovery/verdicts.py _claim_text)."""
    heads = list(_CLAIM_HEAD.finditer(prompt))
    out: dict[str, list[str]] = {}
    for i, head in enumerate(heads):
        end = heads[i + 1].start() if i + 1 < len(heads) else len(prompt)
        quotes = _CLAIM_QUOTE.findall(prompt, head.end(), end)
        if quotes:
            out.setdefault(head.group(1), quotes)
    return out


_ROLLUP_REF = re.compile(r"\[([^\]\s#]+)#(c\d+)\]")
_ASK_QUESTION = re.compile(r"^### Question\s*\n+(.+?)\s*$", re.M)
_ASK_DIGEST = re.compile(r"^=== ", re.M)
_ASK_CLAIM = re.compile(r'^\[(c\d+)\][^\n]*\n  claim: ([^\n]*)\n  quote \([^\n]*?\): "(.*?)"$', re.M | re.S)
_ASK_EVIDENCE = re.compile(r'^\[E\d+\] (\S+) \|.*?^quote: "(.*?)"$', re.M | re.S)


def _content_words(text: str) -> set[str]:
    return {w for w in verify.normalize(text).split() if len(w) > 3}


def _ask_schemas() -> tuple[dict | None, dict | None]:
    """ask.py's map and reduce schemas, or (None, None) when that module is not available."""
    try:
        from discovery import ask
    except ImportError:
        return None, None
    return getattr(ask, "MAP_SCHEMA", None), getattr(ask, "REDUCE_SCHEMA", None)


def fake_specs(ctx: Ctx) -> list[str]:
    """Every model spec named in `config/pipeline.yml`, once each, in role order."""
    p = ctx.config.pipeline
    out: list[str] = []
    for spec in [*p.extractors, p.arbiter, p.ask_map, p.ask_reduce, p.rollup]:
        if spec and spec not in out:
            out.append(spec)
    return out


def install_fakes(ctx: Ctx, specs: list[str] | None = None, *, root: str | Path | None = None) -> list[FakeLLM]:
    """Register a label-backed fake for each spec (default: every role in pipeline.yml). Returns them."""
    responder = LabelResponder(load_labels(root), load_verdict_cases(root))
    fakes = [FakeLLM(spec, responder) for spec in (specs or fake_specs(ctx))]
    for fake in fakes:
        ctx.llms.register(fake)
    return fakes


# ---------- scoring helpers ----------

def _occurrence(index: verify.QuoteIndex, quote: str, question_turn: int | None = None) -> verify.Match | None:
    """The occurrence of a labeled quote that verify picks: the first one `question_turn` can hold the
    question of (QuoteIndex.answers: a later turn, or on a one-block call its own turn with words before
    the answer), else the first."""
    matches = index.find_all(quote)
    if not matches:
        return None
    answered = [m for m in matches if index.answers(question_turn, m)]
    return (answered or matches)[0]


def locate(index: verify.QuoteIndex, quote: str, question_turn: int | None = None) -> tuple[int, int] | None:
    """Raw-file span of a labeled quote, at the occurrence verify picks for `question_turn` when given."""
    m = _occurrence(index, quote, question_turn)
    return (m.start, m.end) if m is not None else None


def match_labels(parsed: ParsedTranscript, label_claims: list[dict], claims: list[Claim]) -> list[tuple[int, int]]:
    """(label index, finding index) pairs that match, one to one, best overlaps first. A finding matches a
    labeled finding when any of its quotes overlaps any labeled quote by MATCH_OVERLAP or more."""
    index = verify.QuoteIndex(parsed)
    spans = [[s for s in (locate(index, q["quote"], q.get("question_turn")) for q in label_quotes(lab)) if s]
             for lab in label_claims]
    pairs = []
    for li, lspans in enumerate(spans):
        if not lspans:
            continue
        for ci, claim in enumerate(claims):
            overlap = merge.best_overlap(lspans, claim.spans)
            if overlap >= MATCH_OVERLAP:
                pairs.append((-overlap, li, ci))
    pairs.sort()
    used_labels: set[int] = set()
    used_claims: set[int] = set()
    out = []
    for _, li, ci in pairs:
        if li in used_labels or ci in used_claims:
            continue
        used_labels.add(li)
        used_claims.add(ci)
        out.append((li, ci))
    return sorted(out)


def _solo_ctx(ctx: Ctx, spec: str) -> Ctx:
    """A copy of ctx whose only extraction and verdict model is `spec`, with no arbiter."""
    pipeline = replace(ctx.config.pipeline, extractors=[spec], arbiter="")
    return replace(ctx, config=replace(ctx.config, pipeline=pipeline))


def _ratio(num: int, den: int) -> float:
    return num / den if den else 0.0


# ---------- results ----------

@dataclass
class FixtureRun:
    fixture: str
    labels: int
    matched: int = 0
    verification: Verification = field(default_factory=Verification)
    claims: list[Claim] = field(default_factory=list)     # verified claims, before merge
    error: str | None = None

    @property
    def recall(self) -> float:
        return _ratio(self.matched, self.labels)


@dataclass
class ExtractorScore:
    model: str
    runs: dict[str, FixtureRun] = field(default_factory=dict)

    @property
    def recall(self) -> float:
        return _ratio(sum(r.matched for r in self.runs.values()), sum(r.labels for r in self.runs.values()))

    @property
    def first_pass_rate(self) -> float:
        v = [r.verification for r in self.runs.values()]
        return _ratio(sum(x.matched_first_pass for x in v), sum(x.quotes for x in v))

    @property
    def repaired(self) -> int:
        return sum(r.verification.repaired for r in self.runs.values())

    @property
    def dropped(self) -> int:
        return sum(len(r.verification.dropped) for r in self.runs.values())

    @property
    def passes_bar(self) -> bool:
        return _passes(self.recall, [r.recall for r in self.runs.values()])


@dataclass
class PairScore:
    models: tuple[str, str]
    matched: dict[str, tuple[int, int]] = field(default_factory=dict)   # fixture -> (matched, labels)

    @property
    def recall(self) -> float:
        return _ratio(sum(m for m, _ in self.matched.values()), sum(n for _, n in self.matched.values()))

    @property
    def passes_bar(self) -> bool:
        return _passes(self.recall, [_ratio(m, n) for m, n in self.matched.values()])


@dataclass
class JudgeResult:
    case: dict
    got: str                      # a verdict, "none" when the claim was not linked, "error"
    correct: bool
    error: str | None = None


@dataclass
class JudgeScore:
    model: str
    results: list[JudgeResult] = field(default_factory=list)

    @property
    def accuracy(self) -> float:
        return _ratio(sum(1 for r in self.results if r.correct), len(self.results))

    @property
    def passes_p1(self) -> bool:
        """README P1 rule: every contradicting side comes out contradicts or scope_mismatch."""
        sides = [r for r in self.results if r.case.get("expected") == "contradicts"]
        return bool(sides) and all(r.correct for r in sides)


def _passes(overall: float, per_fixture: list[float]) -> bool:
    return overall >= RECALL_BAR and all(r >= FIXTURE_FLOOR for r in per_fixture)


@dataclass
class BakeoffReport:
    date: str
    models: list[str]
    fixtures: list[str]
    extractors: list[ExtractorScore]
    pairs: list[PairScore]
    judges: list[JudgeScore]
    cost_usd: float | None = None
    path: str | None = None

    def suggested_roles(self) -> dict:
        """Extractor pair and arbiter, for three or more models; {} otherwise.

        Every split of three distinct models into an extractor pair and an arbiter is ranked:
        pairs that pass the recall bar first, then extractors that ran without errors, then an
        arbiter that ran without errors, then the arbiter's verdict accuracy, then pair recall.
        The primary extractor is the pair member with the higher recall.

        SPEC says the best judge becomes arbiter, but the bar comes first here, so the best judge
        can lose the arbiter seat when the models left to extract cannot clear the bar. The result
        therefore also names `best_judge` (highest accuracy among judges that ran without errors,
        the suggested arbiter on a tie; None when every judge errored), `best_judge_is_arbiter`, and
        the pair the best judge would have as arbiter (`best_judge_pair`, its recall, whether it
        passes the bar and whether its extractors ran without errors). `best_judge_note` puts it in
        words when the two rules disagree.
        """
        if len(self.models) < 3:
            return {}
        ex = {s.model: s for s in self.extractors}
        judge = {j.model: j for j in self.judges}
        pairs = {frozenset(p.models): p for p in self.pairs}
        clean_extractor = {m: not any(r.error for r in ex[m].runs.values()) for m in self.models}
        clean_judge = {m: not any(r.error for r in judge[m].results) for m in self.models}
        by_arbiter: dict[str, tuple] = {}      # arbiter -> (key, a, b) of its best split
        for arbiter in self.models:
            for a, b in itertools.combinations([m for m in self.models if m != arbiter], 2):
                pair = pairs[frozenset((a, b))]
                clean_pair = clean_extractor[a] and clean_extractor[b]
                key = (pair.passes_bar, clean_pair, clean_judge[arbiter], judge[arbiter].accuracy, pair.recall)
                if arbiter not in by_arbiter or key > by_arbiter[arbiter][0]:
                    by_arbiter[arbiter] = (key, a, b)
        arbiter = max(self.models, key=lambda m: (by_arbiter[m][0], -self.models.index(m)))
        _, a, b = by_arbiter[arbiter]
        primary, second = sorted((a, b), key=lambda m: (-ex[m].recall, -ex[m].first_pass_rate, self.models.index(m)))
        pair = pairs[frozenset((a, b))]
        roles: dict[str, Any] = {"extractors": [primary, second], "arbiter": arbiter, "pair_recall": pair.recall,
                                 "pair_passes_bar": pair.passes_bar, "arbiter_accuracy": judge[arbiter].accuracy,
                                 "best_judge": None, "best_judge_is_arbiter": False}
        clean = [m for m in self.models if clean_judge[m]]
        if clean:
            top = max(clean, key=lambda m: (judge[m].accuracy, m == arbiter, -self.models.index(m)))
            _, ta, tb = by_arbiter[top]
            top_pair = pairs[frozenset((ta, tb))]
            roles.update({"best_judge": top, "best_judge_accuracy": judge[top].accuracy,
                          "best_judge_is_arbiter": top == arbiter,
                          "best_judge_pair": sorted((ta, tb), key=self.models.index),
                          "best_judge_pair_recall": top_pair.recall,
                          "best_judge_pair_passes_bar": top_pair.passes_bar,
                          "best_judge_pair_clean": clean_extractor[ta] and clean_extractor[tb]})
        return roles

    def table(self) -> str:
        """Plain-text tables for the terminal."""
        rows = [["model", *self.fixtures, "overall", "first pass", "repaired", "dropped", "bar"]]
        for s in self.extractors:
            rows.append([s.model, *[_frac(s.runs[f]) for f in self.fixtures], f"{s.recall:.2f}",
                         f"{s.first_pass_rate:.2f}", str(s.repaired), str(s.dropped), _ok(s.passes_bar)])
        parts = ["Extraction recall (merged findings, span overlap >= 50%)", _grid(rows)]
        if self.pairs:
            rows = [["pair", *self.fixtures, "overall", "bar"]]
            for p in self.pairs:
                rows.append([" + ".join(p.models), *[f"{m}/{n}" for m, n in (p.matched[f] for f in self.fixtures)],
                             f"{p.recall:.2f}", _ok(p.passes_bar)])
            parts += ["Extractor pairs", _grid(rows)]
        rows = [["model", "correct", "accuracy", "P1 rule"]]
        for j in self.judges:
            rows.append([j.model, f"{sum(r.correct for r in j.results)}/{len(j.results)}", f"{j.accuracy:.2f}",
                         _ok(j.passes_p1)])
        parts += ["Judging (verdict_cases.yml)", _grid(rows)]
        roles = self.suggested_roles()
        if roles:
            suggested = (f"Suggested: extractors {roles['extractors'][0]} (primary), {roles['extractors'][1]}; "
                         f"arbiter {roles['arbiter']}")
            note = best_judge_note(roles)
            parts.append(suggested + (f"\n{note}" if note else ""))
        return "\n\n".join(parts) + "\n"

    def markdown(self) -> str:
        """The report written to docs/research/bakeoff-<date>.md."""
        labels = {f: next((r.labels for s in self.extractors for n, r in s.runs.items() if n == f), 0)
                  for f in self.fixtures}
        cases = len(self.judges[0].results) if self.judges else 0
        lines = [
            f"# Bake-off {self.date}",
            "",
            f"Models: {', '.join(self.models)}. Fixtures: {len(self.fixtures)} "
            f"({sum(labels.values())} labeled findings). Verdict cases: {cases}.",
            "",
            f"Recall bar (fixed in `tests/labels/README.md` before this run): {RECALL_BAR:.1f} overall on merged "
            f"claims, no fixture below {FIXTURE_FLOOR:.1f}.",
            "",
            "Method: each model extracts alone; quotes are verified (one repair each) and merged, then matched to "
            "the labels one to one when their spans overlap by at least 50% of the shorter span. First pass is "
            "the share of quotes matched before any repair. As judge, each model gets one verdict call per case "
            "with that single claim and target, and no arbiter; a case is correct when the verdict equals "
            "`expected` or is in `accept`.",
        ]
        if self.cost_usd is not None:
            lines += ["", f"Estimated model cost: ${self.cost_usd:.2f}."]
        lines += ["", "## Extraction", "",
                  _md_table(["Model", *self.fixtures, "Overall", "First pass", "Repaired", "Dropped", "Bar"],
                            [[f"`{s.model}`", *[_frac(s.runs[f]) for f in self.fixtures], f"{s.recall:.2f}",
                              f"{s.first_pass_rate:.2f}", str(s.repaired), str(s.dropped), _ok(s.passes_bar)]
                             for s in self.extractors])]
        if self.pairs:
            lines += ["", "## Extractor pairs", "", "Merged recall when both models extract, as in production.", "",
                      _md_table(["Pair", *self.fixtures, "Overall", "Bar"],
                                [[" + ".join(f"`{m}`" for m in p.models),
                                  *[f"{m}/{n}" for m, n in (p.matched[f] for f in self.fixtures)],
                                  f"{p.recall:.2f}", _ok(p.passes_bar)] for p in self.pairs])]
        if self.judges:
            lines += ["", "## Judging", "",
                      _md_table(["Model", "Correct", "Accuracy", "P1 rule"],
                                [[f"`{j.model}`", f"{sum(r.correct for r in j.results)}/{len(j.results)}",
                                  f"{j.accuracy:.2f}", _ok(j.passes_p1)] for j in self.judges]),
                      "", "Per case (a trailing `x` marks a wrong verdict):", "",
                      _md_table(["Case", "Fixture", "Target", "Expected", *[f"`{j.model}`" for j in self.judges]],
                                [[str(r0.case.get("conflict", "")), str(r0.case.get("fixture", "")),
                                  str(r0.case.get("target", "")),
                                  " or ".join([r0.case.get("expected", ""), *r0.case.get("accept", [])]),
                                  *[j.results[i].got + ("" if j.results[i].correct else " x") for j in self.judges]]
                                 for i, r0 in enumerate(self.judges[0].results)])]
        roles = self.suggested_roles()
        if roles:
            lines += ["", "## Suggested roles", "",
                      f"- Extractors: `{roles['extractors'][0]}` (primary), `{roles['extractors'][1]}`. Pair recall "
                      f"{roles['pair_recall']:.2f}, {'passes' if roles['pair_passes_bar'] else 'misses'} the bar.",
                      f"- Arbiter: `{roles['arbiter']}`, verdict accuracy {roles['arbiter_accuracy']:.2f}.",
                      *([f"- {note}"] if (note := best_judge_note(roles)) else []),
                      "",
                      "A person decides. Record the roles in `config/pipeline.yml` and bump `prompt_version`."]
        errors = [f"- `{s.model}` on {r.fixture}: {r.error}" for s in self.extractors for r in s.runs.values()
                  if r.error]
        errors += [f"- `{j.model}` judging {r.case.get('conflict', '')} ({r.case.get('fixture', '')}): {r.error}"
                   for j in self.judges for r in j.results if r.error]
        if errors:
            lines += ["", "## Errors", "", *errors]
        return "\n".join(lines) + "\n"


def best_judge_note(roles: dict) -> str | None:
    """One line when the best judge is not the suggested arbiter (SPEC: the best judge becomes arbiter)."""
    top = roles.get("best_judge")
    if not top or roles.get("best_judge_is_arbiter"):
        return None
    pair = " + ".join(roles["best_judge_pair"])
    why = []
    if not roles["best_judge_pair_passes_bar"]:
        why.append(f"recall {roles['best_judge_pair_recall']:.2f} misses the bar")
    if not roles["best_judge_pair_clean"]:
        why.append("had extraction errors")
    reason = f"`{pair}` " + (" and ".join(why) if why else f"ranks lower (recall {roles['best_judge_pair_recall']:.2f})")
    seat = "is suggested as an extractor" if top in roles["extractors"] else "is not suggested as arbiter"
    return (f"`{top}` judged best ({roles['best_judge_accuracy']:.2f}) but {seat} because {reason}; "
            f"SPEC says the best judge becomes arbiter, so decide which rule wins.")


def _frac(run: FixtureRun) -> str:
    return "error" if run.error else f"{run.matched}/{run.labels}"


def _ok(flag: bool) -> str:
    return "pass" if flag else "fail"


def _grid(rows: list[list[str]]) -> str:
    widths = [max(len(r[i]) for r in rows) for i in range(len(rows[0]))]
    return "\n".join("  ".join(cell.ljust(w) for cell, w in zip(r, widths)).rstrip() for r in rows)


def _md_table(head: list[str], rows: list[list[str]]) -> str:
    out = ["| " + " | ".join(head) + " |", "|" + "---|" * len(head)]
    out += ["| " + " | ".join(c.replace("|", "/") for c in r) + " |" for r in rows]
    return "\n".join(out)


# ---------- scoring ----------

def score_extractor(ctx: Ctx, spec: str, fixtures: dict[str, ParsedTranscript],
                    labels: dict[str, dict], *, workers: int = 4) -> ExtractorScore:
    """Run `spec` alone as extractor on every fixture and score recall and first-pass matches."""
    solo = _solo_ctx(ctx, spec)
    words = ctx.config.pipeline.t("short_answer_words")

    def one(name: str) -> FixtureRun:
        parsed = fixtures[name]
        wanted = label_findings(labels[name])
        run = FixtureRun(fixture=name, labels=len(wanted))
        try:
            results, _ = extract.extract(solo, parsed)
        except LLMError as e:
            run.error = str(e)
            return run
        kept, run.verification = verify.verify_claims(
            parsed, results[0].claims, short_answer_words=words,
            repair=lambda claim, quote, reason: extract.repair_quote(solo, parsed, claim, quote, reason))
        run.claims = kept
        run.matched = len(match_labels(parsed, wanted, merge.merge_claims([kept])))
        return run

    names = [n for n in fixtures if n in labels]
    with ThreadPoolExecutor(max_workers=max(1, min(workers, len(names)))) as pool:
        runs = list(pool.map(one, names))
    return ExtractorScore(model=spec, runs={r.fixture: r for r in runs})


def score_pair(a: ExtractorScore, b: ExtractorScore, fixtures: dict[str, ParsedTranscript],
               labels: dict[str, dict]) -> PairScore:
    """Merged recall of two extractors' verified claims, with no extra model calls."""
    out = PairScore(models=(a.model, b.model))
    for name, parsed in fixtures.items():
        if name not in labels:
            continue
        wanted = label_findings(labels[name])
        merged = merge.merge_claims([a.runs[name].claims, b.runs[name].claims])
        out.matched[name] = (len(match_labels(parsed, wanted, merged)), len(wanted))
    return out


def _case_claim(parsed: ParsedTranscript, case: dict, label: dict | None) -> Claim | None:
    """The verdict case as a verified claim `c01`, with the label's wording and scope when labeled."""
    qt = case.get("question_turn")
    m = _occurrence(verify.QuoteIndex(parsed), case.get("quote", ""), qt)
    if m is None:
        return None
    lab = next((f for f in label_findings(label)
                if any(_same_quote(q["quote"], case["quote"]) for q in label_quotes(f))), {})
    speaker = next((q.get("speaker") for q in label_quotes(lab) if _same_quote(q["quote"], case["quote"])), None)
    quote = Quote(quote=m.raw_text, turns=list(m.turns), question_turn=qt if isinstance(qt, int) else None,
                  offset=(m.start, m.end), speaker=speaker, attribution="inferred" if len(m.turns) > 1 else "span")
    return Claim(id="c01", text=lab.get("text") or m.raw_text, quotes=[quote], scope=Scope.from_dict(lab.get("scope")))


def score_judge(ctx: Ctx, spec: str, cases: list[dict], fixtures: dict[str, ParsedTranscript],
                labels: dict[str, dict], beliefs: BeliefsDoc, *, workers: int = 4) -> JudgeScore:
    """Run `spec` alone as verdict model on every case: one claim against its target entry."""
    solo = _solo_ctx(ctx, spec)

    def one(case: dict) -> JudgeResult:
        parsed = fixtures.get(case.get("fixture", ""))
        entry = beliefs.get(case.get("target", ""))
        claim = _case_claim(parsed, case, labels.get(case["fixture"])) if parsed else None
        if parsed is None or entry is None or claim is None:
            why = "fixture missing" if parsed is None else "target missing" if entry is None else "quote not found"
            return JudgeResult(case=case, got="error", correct=False, error=why)
        try:
            out = verdicts.run_verdicts(solo, parsed, [claim], [entry], [])
        except LLMError as e:
            return JudgeResult(case=case, got="error", correct=False, error=str(e))
        got = next((c.verdict for c in out.belief_checks if c.target == entry.id and claim.id in c.claims), "none")
        return JudgeResult(case=case, got=got, correct=got == case.get("expected") or got in case.get("accept", []))

    with ThreadPoolExecutor(max_workers=max(1, min(workers, len(cases) or 1))) as pool:
        return JudgeScore(model=spec, results=list(pool.map(one, cases)))


def load_fixtures(root: Path, names: list[str], *, commit_date: str | None = None) -> dict[str, ParsedTranscript]:
    """Parse `tests/fixtures/<name>.md` for each labeled fixture that exists and parses."""
    out = {}
    for name in names:
        path = root / FIXTURES_DIR / f"{name}.md"
        if not path.exists():
            continue
        try:
            out[name] = parse(f"{FIXTURES_DIR}/{name}.md", path.read_text(encoding="utf-8"), commit_date=commit_date)
        except ParseError:
            continue
    return out


def _report_path(ctx: Ctx, date: str) -> str:
    rel = f"{REPORT_DIR}/bakeoff-{date}.md"
    n = 2
    while ctx.repo.exists(rel):
        rel = f"{REPORT_DIR}/bakeoff-{date}-{n}.md"
        n += 1
    return rel


def run_bakeoff(ctx: Ctx, models: list[str] | None = None, *, root: str | Path | None = None,
                write: bool = True, progress: Callable[[str], None] | None = None) -> BakeoffReport:
    """Score each model as extractor and judge on the golden set; write the report unless `write=False`.

    `models` defaults to the extractors and arbiter in `config/pipeline.yml`. Fixtures, labels and
    the test beliefs come from `root` (default: the repo root), falling back to this checkout.
    """
    base = labels_root(root if root is not None else ctx.repo.root)
    models = list(models or [m for m in [*ctx.config.pipeline.extractors, ctx.config.pipeline.arbiter] if m])
    models = list(dict.fromkeys(models))
    date = ctx.now().astimezone(ctx.tz).date().isoformat()
    labels = load_labels(base)
    cases = load_verdict_cases(base)
    fixtures = load_fixtures(base, sorted(labels), commit_date=date)
    beliefs = parse_beliefs((base / GOLDEN_BELIEFS).read_text(encoding="utf-8")) \
        if (base / GOLDEN_BELIEFS).exists() else BeliefsDoc(entries=[])
    say = progress or (lambda msg: None)
    spent_before = ctx.llms.cost.spent_usd if ctx.llms.cost else None

    def one(spec: str) -> tuple[ExtractorScore, JudgeScore]:
        say(f"{spec}: extracting {len(fixtures)} fixtures")
        ex = score_extractor(ctx, spec, fixtures, labels)
        say(f"{spec}: judging {len(cases)} verdict cases")
        return ex, score_judge(ctx, spec, cases, fixtures, labels, beliefs)

    with ThreadPoolExecutor(max_workers=max(1, min(3, len(models)))) as pool:
        scored = list(pool.map(one, models))
    extractors = [ex for ex, _ in scored]
    judges = [j for _, j in scored]
    pairs = [score_pair(a, b, fixtures, labels) for a, b in itertools.combinations(extractors, 2)]
    cost = ctx.llms.cost.spent_usd - spent_before if spent_before is not None else None
    report = BakeoffReport(date=date, models=models, fixtures=list(fixtures), extractors=extractors,
                           pairs=pairs, judges=judges, cost_usd=cost)
    if write:
        report.path = _report_path(ctx, date)
        ctx.repo.write(report.path, report.markdown())
    return report
