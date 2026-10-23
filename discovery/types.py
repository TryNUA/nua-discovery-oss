"""Shared data types. Every module passes these around; nothing else defines call data.

Conventions:
- All offsets are character offsets into the raw transcript file text.
- Dates are ISO strings (YYYY-MM-DD) in America/Los_Angeles unless stated.
- `to_dict` / `from_dict` round-trip through YAML front-matter.
- Records are pydantic models (`Record`): reading a dict validates and coerces it, unknown keys are
  ignored, and a null list, dict or nested record reads as its default. Copy with
  `model_copy(update=...)`; nothing is frozen.
"""
from __future__ import annotations

import datetime as dt
import functools
import typing
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

SpeakerLabels = Literal["named", "me_them", "diarized", "single_stream"]
Shape = Literal["standard", "circleback_copy", "granola_export"]
Status = Literal["digested", "internal", "parse_failed", "failed", "unreadable"]
Origin = Literal["live", "backfill"]
DateSource = Literal["api", "filename", "footer", "manual", "unknown"]
Verdict = Literal["supports", "contradicts", "scope_mismatch"]
DecidedBy = Literal["agree", "arbiter", "unarbitrated", "single"]
CoverageStatus = Literal["answered", "dodged", "never_asked"]
Attribution = Literal["span", "inferred"]
Section = Literal["beliefs", "watching", "retired"]

VERDICTS = ("supports", "contradicts", "scope_mismatch")
COVERAGE = ("answered", "dodged", "never_asked")
# Claim scope keys. What `line`, `party` and `segment` mean for your market is described in
# config/domain.yml `scope` (for the insurance example: line of business, kind of firm, market segment);
# `seat` comes from config/seats.yml.
SCOPE_KEYS = ("line", "party", "segment", "seat")


def iso_date(value: Any) -> str | None:
    """A date as YYYY-MM-DD text. YAML hands back a date (or datetime) object for an unquoted
    `2026-09-18`, and readers compare dates as text. None and "" give None; any other value its text."""
    if value is None or value == "":
        return None
    if isinstance(value, dt.datetime):
        return value.date().isoformat()
    if isinstance(value, dt.date):
        return value.isoformat()
    return str(value)


@functools.cache
def _defaults_for_none(cls: type[BaseModel]) -> frozenset[str]:
    """Fields of `cls` that hold a list, a dict or a nested record and do not accept None."""
    names = set()
    for name, f in cls.model_fields.items():
        if f.is_required():
            continue
        ann = f.annotation
        args = typing.get_args(ann)
        if ann is Any or type(None) in args:
            continue
        origin = typing.get_origin(ann) or ann
        if origin in (list, dict) or (isinstance(origin, type) and issubclass(origin, BaseModel)):
            names.add(name)
    return frozenset(names)


class Record(BaseModel):
    """Base for the shared records: unknown keys are ignored, and None for a list, dict or nested
    record field reads as its default (a hand edit or an older file may leave `claims:` empty)."""
    model_config = ConfigDict(extra="ignore", coerce_numbers_to_str=True)

    @model_validator(mode="before")
    @classmethod
    def _none_is_default(cls, data: Any) -> Any:
        if isinstance(data, dict):
            drop = _defaults_for_none(cls)
            if any(data.get(k, 0) is None for k in drop):
                data = {k: v for k, v in data.items() if not (k in drop and v is None)}
        return data

    @classmethod
    def from_dict(cls, d: dict | None):
        """Build from a dict, ignoring unknown keys. None gives None."""
        return None if d is None else cls.model_validate(d)


class Attendee(Record):
    name: str | None = None
    email: str | None = None


class Turn(Record):
    index: int            # 1-based turn number within the transcript body
    time: str | None      # timestamp as written, e.g. "12:34"; None for text-block exports
    speaker: str | None   # as labeled in the file ("Jane Doe", "Me", "Speaker A")
    text: str             # turn text without markup
    start: int            # raw-file offset of the first character of `text`
    end: int              # raw-file offset one past the last character of `text`


class ParsedTranscript(Record):
    path: str                          # repo-relative path, e.g. transcripts/circleback_123.md
    raw: str                           # full file text
    shape: Shape
    front_matter: dict[str, Any]       # {} for dropped files
    notes: str                         # recorder AI overview: context only, never evidence
    body_start: int                    # raw offset where the transcript body starts
    body_end: int                      # raw offset where it ends
    turns: list[Turn]
    speaker_labels: SpeakerLabels
    origin: Origin                     # live iff front-matter has source granola|circleback
    source: str | None = None          # granola | circleback | None for dropped files
    source_id: str | None = None
    ical_uid: str | None = None
    start: str | None = None           # ISO datetime with offset
    start_source: str | None = None    # calendar | transcript | created
    date: str | None = None            # YYYY-MM-DD, Pacific
    date_source: DateSource = "unknown"
    recorded_by: str | None = None
    attendees: list[Attendee] = Field(default_factory=list)
    title: str | None = None

    @property
    def body(self) -> str:
        return self.raw[self.body_start:self.body_end]

    @property
    def stem(self) -> str:
        name = self.path.rsplit("/", 1)[-1]
        return name[:-3] if name.endswith(".md") else name


class Scope(Record):
    line: str | None = None            # line of business or coverage: workers' comp, commercial auto, cyber
    party: str | None = None           # the kind of organization (config/domain.yml scope.party)
    segment: str | None = None         # small commercial, middle market, E&S, personal lines, specialty
    seat: str | None = None

    @classmethod
    def from_dict(cls, d: dict | None) -> "Scope":
        return cls.model_validate(d or {})

    def to_dict(self) -> dict:
        return self.model_dump()

    def is_empty(self) -> bool:
        return not any(self.model_dump().values())


class Flags(Record):
    hedged: bool = False
    firsthand: bool = False
    prompted: bool = False

    @classmethod
    def from_dict(cls, d: dict | None) -> "Flags":
        return cls.model_validate(d or {})


MAX_QUOTES = 3     # quotes per finding
_QUOTE_KEYS = ("quote", "turns", "offset", "question_turn", "speaker", "attribution")


class Quote(Record):
    """One verbatim span supporting a finding."""
    quote: str                         # after verify: the raw transcript text at `offset`, markup removed
    turns: list[int] = Field(default_factory=list)
    offset: tuple[int, int] | None = None   # raw-file [start, end); set by verify
    question_turn: int | None = None   # short answers: the turn holding the question
    speaker: str | None = None
    attribution: Attribution = "span"

    @field_validator("offset", mode="before")
    @classmethod
    def _no_offset(cls, v: Any) -> Any:
        return v if v else None

    @property
    def span(self) -> tuple[int, int] | None:
        return (int(self.offset[0]), int(self.offset[1])) if self.offset else None


class Claim(Record):
    """A finding: one point stated in `text`, supported by 1 to MAX_QUOTES verbatim quotes, possibly from
    different parts of the call. Scope, flags and found_by belong to the finding.

    A dict in the older one-quote shape (`quote`, `turns`, `offset`, ... at the top level) reads as a
    finding with that one quote. `quote`, `offset` and `speaker` read the first quote."""
    id: str | None                     # "c01".. assigned after consolidation; None straight from a model
    text: str
    quotes: list[Quote] = Field(default_factory=list)
    scope: Scope = Field(default_factory=Scope)
    flags: Flags = Field(default_factory=Flags)
    found_by: int = 1
    model: str | None = None           # which extraction model produced it (dropped after consolidation)

    @model_validator(mode="before")
    @classmethod
    def _one_quote_shape(cls, data: Any) -> Any:
        if isinstance(data, dict) and not data.get("quotes") and data.get("quote") is not None:
            data = dict(data)
            data["quotes"] = [{k: data.pop(k) for k in _QUOTE_KEYS if k in data}]
        return data

    @property
    def quote(self) -> str:
        """The first quote's text, or ""."""
        return self.quotes[0].quote if self.quotes else ""

    @property
    def offset(self) -> tuple[int, int] | None:
        """The first quote's span."""
        return self.quotes[0].span if self.quotes else None

    @property
    def speaker(self) -> str | None:
        """The first quote's speaker that is known."""
        return next((q.speaker for q in self.quotes if q.speaker), None)

    @property
    def spans(self) -> list[tuple[int, int]]:
        """Every verified quote span, in quote order."""
        return [q.span for q in self.quotes if q.offset]

    @property
    def quote_texts(self) -> list[str]:
        return [q.quote for q in self.quotes if q.quote]

    @property
    def all_turns(self) -> list[int]:
        """Turns of every quote, in order, without repeats."""
        out: list[int] = []
        for q in self.quotes:
            out.extend(t for t in q.turns if t not in out)
        return out

    def to_dict(self) -> dict:
        return self.model_dump(mode="json", exclude={"model"})


class Who(Record):
    contact: str | None = None
    firm: str | None = None
    firm_source: str | None = None     # transcript | attendee_email | manual
    seat: str | None = None
    institution_type: str | None = None
    role_in_thesis: str | None = None

    @classmethod
    def from_dict(cls, d: dict | None) -> "Who":
        return cls.model_validate(d or {})


class FollowUp(Record):
    direction: Literal["we_owe", "they_offered"]
    what: str
    who: str | None = None
    quote: str = ""


class ExtractionResult(Record):
    model: str
    who: Who
    summary: list[str]
    claims: list[Claim]
    follow_ups: list[FollowUp]
    mentions: dict[str, list[str]]     # {"people": [...], "firms": [...]}
    ask_next: list[str] = Field(default_factory=list)


class BeliefCheck(Record):
    target: str                        # B-### or W-###
    verdict: Verdict
    claims: list[str]                  # claim ids in this digest
    decided_by: DecidedBy
    votes: dict[str, str] = Field(default_factory=dict)   # model spec -> verdict or "none"
    source: Literal["ingest", "backcheck"] = "ingest"
    note: str = ""


class QuestionCoverage(Record):
    question: str
    status: CoverageStatus
    claims: list[str] = Field(default_factory=list)
    decided_by: DecidedBy = "agree"


class BriefRef(Record):
    channel: str | None = None
    ts: str | None = None
    thread: list[str] = Field(default_factory=list)

    @classmethod
    def from_dict(cls, d: dict | None) -> "BriefRef | None":
        return cls.model_validate(d) if d else None


class Verification(Record):
    quotes: int = 0                    # quotes checked, across every finding
    matched_first_pass: int = 0
    repaired: int = 0
    dropped: list[dict] = Field(default_factory=list)   # quotes: [{"model", "quote", "reason"}]
    findings_dropped: int = 0                            # findings left with no verified quote
    follow_up_quotes_dropped: int = 0                    # follow-up quotes not in the transcript body, emptied

    @classmethod
    def from_dict(cls, d: dict | None) -> "Verification":
        return cls.model_validate(d or {})


def _no_mentions() -> dict[str, list[str]]:
    return {"people": [], "firms": []}


class Digest(Record):
    call_id: str
    transcript: str                    # repo-relative transcript path this digest was built from
    status: Status
    origin: Origin
    duplicates: list[str] = Field(default_factory=list)
    source: str | None = None
    source_id: str | None = None
    date: str | None = None
    date_source: DateSource = "unknown"
    recorded_by: str | None = None
    attendees: list[Attendee] = Field(default_factory=list)
    contact: str | None = None
    firm: str | None = None
    firm_source: str | None = None
    seat: str | None = None
    institution_type: str | None = None
    role_in_thesis: str | None = None
    speaker_labels: SpeakerLabels | None = None
    duration_min: int | None = None                         # call length in minutes, from the last turn timestamp
    prompt_version: str | None = None
    models: dict[str, Any] = Field(default_factory=dict)   # {"extract": [...], "arbiter": "..."}
    degraded: list[str] = Field(default_factory=list)       # one_model | no_arbiter | no_consolidation
    brief: BriefRef | None = None
    prep: str | None = None
    summary: list[str] = Field(default_factory=list)
    claims: list[Claim] = Field(default_factory=list)
    belief_checks: list[BeliefCheck] = Field(default_factory=list)
    checked: dict[str, str] = Field(default_factory=dict)  # entry id -> entry version (a ref)
    new_findings: list[str] = Field(default_factory=list)  # claim ids; snapshot at brief time
    question_coverage: list[QuestionCoverage] = Field(default_factory=list)
    follow_ups: list[FollowUp] = Field(default_factory=list)
    ask_next: list[str] = Field(default_factory=list)       # suggested next questions, from the primary extractor
    mentions: dict[str, list[str]] = Field(default_factory=_no_mentions)
    verification: Verification = Field(default_factory=Verification)
    reason: str | None = None          # stubs only
    reprocess_failed: str | None = None  # prompt_version a stale re-run failed at

    @field_validator("date", mode="before")
    @classmethod
    def _iso(cls, v: Any) -> Any:      # a hand edit left `date: 2026-09-18` unquoted
        return v if isinstance(v, str) else iso_date(v)

    @field_validator("brief", mode="before")
    @classmethod
    def _no_brief(cls, v: Any) -> Any:
        return v if v else None

    @field_validator("mentions", mode="before")
    @classmethod
    def _mentions(cls, v: Any) -> Any:
        return v if v else _no_mentions()

    def to_dict(self) -> dict:
        return self.model_dump(mode="json", exclude={"claims": {"__all__": {"model"}}})

    def claim(self, claim_id: str) -> Claim | None:
        return next((c for c in self.claims if c.id == claim_id), None)


# ---------- beliefs ----------

class HistoryLine(Record):
    date: str                          # YYYY-MM-DD
    action: str                        # add | watch | promote | reword | split | merge | retire | keep | promoted | merged | dropped | ...
    who: str                           # Alex | Sam | Jordan | system
    via: Literal["slack", "agent", "bootstrap", "system"]
    ref: str                           # "<message_ts>:<block_id>" or "requests/<file>@<blob sha>" or "system:<...>"
    sources: list[tuple[str, str]] = Field(default_factory=list)   # (call_id, quote)
    reason: str | None = None
    text: str | None = None            # free text after the action for promoted/merged notes, e.g. "promoted to B-012"


VERSION_ACTIONS = ("add", "watch", "promote", "reword", "split", "merge")


class Entry(Record):
    id: str                            # B-### or W-###
    statement: str
    scope: Scope
    section: Section
    history: list[HistoryLine] = Field(default_factory=list)

    @property
    def version(self) -> str | None:
        """Ref of the latest add/watch/promote/reword/split/merge history line."""
        for h in reversed(self.history):
            if h.action in VERSION_ACTIONS:
                return h.ref
        return None

    @property
    def is_current(self) -> bool:
        return self.section in ("beliefs", "watching")


class Decision(Record):
    id: str                            # D-###
    title: str
    rests_on: list[str] = Field(default_factory=list)   # B-### ids


# ---------- prep ----------

class PrepFile(Record):
    path: str
    planned_date: str
    contact: str
    firm: str | None = None
    attendee_email: str | None = None
    seat: str | None = None
    caller: str | None = None
    questions: list[str] = Field(default_factory=list)
