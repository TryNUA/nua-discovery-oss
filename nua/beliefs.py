"""Read and write `beliefs.md` and `decisions.md`, and the history-line grammar.

See SPEC "beliefs.md", "History lines" and "decisions.md".

beliefs.md is written only by the processor, so parsing is strict: anything unexpected raises
ValueError instead of being dropped on the next write. decisions.md is hand-edited, so parsing
is lenient: prose and extra bullets are ignored.

File layout:

    ## Beliefs

    ### B-001 <statement>
    - scope: line: general liability; party: insured; segment: small commercial
    - history:
      - <history line>

    ## Watching

    ## Retired

History line:

    YYYY-MM-DD <action>[ <text>] by <who> via <slack|agent|bootstrap|system>
        [, source <call_id> "<quote>"[; <call_id> "<quote>"...]][, reason: <text>], ref <ref>

Actions are written as stored (`add`, `watch`, `keep`, `promoted`, ...). The past-tense forms in
the SPEC example ("added", "watched") are read as their canonical action. Every field is kept on
one line: newlines inside a statement, quote, reason or scope value become single spaces.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import NamedTuple

from nua.types import SCOPE_KEYS, Decision, Entry, HistoryLine, Scope

EMPTY_BELIEFS = "## Beliefs\n\n## Watching\n\n## Retired\n"

VIAS = ("slack", "agent", "bootstrap", "system")

# Section key (Entry.section) -> heading text, in file order.
_SECTIONS = {"beliefs": "Beliefs", "watching": "Watching", "retired": "Retired"}

# Past-tense words a hand-written file may use, read as the canonical action.
# "promoted", "merged" and "dropped" are actions of their own (notes on the old entry), so they stay.
_ACTION_ALIASES = {"added": "add", "watched": "watch", "reworded": "reword", "retired": "retire", "kept": "keep"}

_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")
_ENTRY_HEAD = re.compile(r"### ([BW]-\d+)\s+(\S.*?)\s*$")
_VIA = re.compile(r" via (" + "|".join(VIAS) + r")(?=, |$)")
_SOURCE_HEAD = re.compile(r'([^\s"]+) "')
_NEWLINES = re.compile(r"\s*[\r\n]\s*")

_HEADING = re.compile(r"#{1,6}\s")
_DECISION_HEAD = re.compile(r"#{2,6}\s+(D-\d+)(?:[:.]?\s+(.*?))?\s*$")
_DECISION_LINE = re.compile(r"(?:[-*]\s+)?(D-\d+)[:.]?\s+(.*\brests on\b.*?)\s*$", re.I)
_RESTS_ON_LINE = re.compile(r"\s*(?:[-*]\s+)?rests on\b\s*:?(.*)$", re.I)
_RESTS_ON_INLINE = re.compile(r"[\s:;,(-]*\brests on\b\s*:?(?P<ids>.*)$", re.I)
_ENTRY_ID = re.compile(r"\b([BW])-(\d+)\b")


# ---------- small helpers ----------

def _one_line(text: str | None) -> str:
    """Collapse newlines (and the whitespace around them) to one space, then strip."""
    return _NEWLINES.sub(" ", text or "").strip()


def _escape(quote: str) -> str:
    """Escape a quote for a history line: backslash and double quote get a backslash; newlines become spaces."""
    return _NEWLINES.sub(" ", quote).replace("\\", "\\\\").replace('"', '\\"')


def _read_quoted(line: str, pos: int) -> tuple[str, int]:
    """Read an escaped quote body starting just after its opening `"`. Returns (text, index after closing `"`)."""
    out: list[str] = []
    i = pos
    while i < len(line):
        ch = line[i]
        if ch == "\\":
            if i + 1 >= len(line):
                break
            out.append(line[i + 1])
            i += 2
            continue
        if ch == '"':
            return "".join(out), i + 1
        out.append(ch)
        i += 1
    raise ValueError(f"unterminated quote in history line: {line!r}")


def _norm_id(prefix: str, number: str) -> str:
    """'B', '3' -> 'B-003'."""
    return f"{prefix.upper()}-{int(number):03d}"


# ---------- scope ----------

def format_scope(s: Scope) -> str:
    """Scope as `key: value; key: value` in field order, empty fields left out.

    Example: "line: general liability, commercial auto; party: carrier". A `;` inside a
    value would end it early, so it is written as `,`. An empty scope gives "".
    """
    parts = []
    for key in SCOPE_KEYS:
        value = _one_line(getattr(s, key)).replace(";", ",").strip()
        if value:
            parts.append(f"{key}: {value}")
    return "; ".join(parts)


def parse_scope(text: str) -> Scope:
    """Inverse of `format_scope`. Unknown keys or parts without `key:` raise ValueError."""
    scope = Scope()
    for part in (text or "").split(";"):
        part = part.strip()
        if not part:
            continue
        key, sep, value = part.partition(":")
        key = key.strip().lower()
        if not sep or key not in SCOPE_KEYS:
            raise ValueError(f"bad scope part {part!r}; expected one of {', '.join(SCOPE_KEYS)}")
        setattr(scope, key, value.strip() or None)
    return scope


# ---------- history lines ----------

def format_history(h: HistoryLine) -> str:
    """One history line in the grammar above. Raises ValueError on a value the grammar cannot hold."""
    if not _DATE.fullmatch(h.date or ""):
        raise ValueError(f"history date must be YYYY-MM-DD, got {h.date!r}")
    action = (h.action or "").strip()
    if not action or any(ch.isspace() for ch in action):
        raise ValueError(f"history action must be one word, got {h.action!r}")
    who = _one_line(h.who)
    if not who or " by " in f" {who} ":
        raise ValueError(f"history line needs a who without ' by ', got {h.who!r}")
    if h.via not in VIAS:
        raise ValueError(f"history via must be one of {', '.join(VIAS)}, got {h.via!r}")
    ref = _one_line(h.ref)
    if not ref or ", ref " in ref:
        raise ValueError(f"history ref must be non-empty and not contain ', ref ', got {h.ref!r}")

    text = _one_line(h.text)
    line = f"{h.date} {action}{' ' + text if text else ''} by {who} via {h.via}"
    if h.sources:
        rendered = []
        for call_id, quote in h.sources:
            call_id = (call_id or "").strip()
            if not call_id or any(ch.isspace() for ch in call_id) or '"' in call_id:
                raise ValueError(f"bad source call id {call_id!r}")
            rendered.append(f'{call_id} "{_escape(quote or "")}"')
        line += ", source " + "; ".join(rendered)
    reason = _one_line(h.reason)
    if reason:
        line += f", reason: {reason}"
    return f"{line}, ref {ref}"


def parse_history(line: str) -> HistoryLine:
    """Parse one history line (a leading `- ` list marker is allowed). Raises ValueError when malformed.

    `ref` is always last, so it is everything after the last ", ref ". The reason is everything
    between ", reason: " and the ref, so it may hold commas.
    """
    line = (line or "").strip()
    if line.startswith("- "):
        line = line[2:].lstrip()
    if not _DATE.match(line) or line[10:11] != " ":
        raise ValueError(f"history line must start with YYYY-MM-DD: {line!r}")
    date, rest = line[:10], line[11:]

    cut = rest.rfind(", ref ")
    if cut < 0:
        raise ValueError(f"history line has no ref: {line!r}")
    ref = rest[cut + len(", ref "):].strip()
    if not ref:
        raise ValueError(f"history line has an empty ref: {line!r}")
    rest = rest[:cut]

    via_match = _VIA.search(rest)
    if not via_match:
        raise ValueError(f"history line needs 'via {'|'.join(VIAS)}': {line!r}")
    head, tail, via = rest[:via_match.start()], rest[via_match.end():], via_match.group(1)

    left, sep, who = head.rpartition(" by ")
    who = who.strip()
    if not sep or not left.strip() or not who:
        raise ValueError(f"history line needs '<action> by <who>': {line!r}")
    action, _, text = left.strip().partition(" ")
    action = _ACTION_ALIASES.get(action, action)

    sources: list[tuple[str, str]] = []
    pos = 0
    if tail.startswith(", source "):
        pos = len(", source ")
        while True:
            m = _SOURCE_HEAD.match(tail, pos)
            if not m:
                raise ValueError(f'history source must be <call_id> "<quote>": {line!r}')
            quote, pos = _read_quoted(tail, m.end())
            sources.append((m.group(1), quote))
            if tail.startswith("; ", pos):
                pos += 2
                continue
            break
    reason = None
    if tail.startswith(", reason: ", pos):
        reason = tail[pos + len(", reason: "):].strip() or None
        pos = len(tail)
    if pos != len(tail):
        raise ValueError(f"unexpected text in history line: {tail[pos:]!r}")

    return HistoryLine(date=date, action=action, who=who, via=via, ref=ref,
                       sources=sources, reason=reason, text=text.strip() or None)


# ---------- beliefs.md ----------

class HeldSource(NamedTuple):
    """A source in the history of a current entry (BeliefsDoc.held_sources)."""
    entry_id: str
    call_id: str
    quote: str
    ref: str       # the ref of the change that first held it
    date: str      # that change's date


@dataclass
class BeliefsDoc:
    """All entries in beliefs.md, grouped by section in file order."""
    entries: list[Entry] = field(default_factory=list)

    def get(self, entry_id: str) -> Entry | None:
        """The entry with this id, in any section, or None."""
        return next((e for e in self.entries if e.id == entry_id), None)

    def current(self) -> list[Entry]:
        """Beliefs then Watching items (Retired left out), each in file order."""
        return ([e for e in self.entries if e.section == "beliefs"]
                + [e for e in self.entries if e.section == "watching"])

    def next_id(self, prefix: str) -> str:
        """Next free id for "B" or "W": highest number in any section (Retired included) plus one."""
        prefix = prefix.rstrip("-").upper()
        if prefix not in ("B", "W"):
            raise ValueError(f"id prefix must be B or W, got {prefix!r}")
        numbers = [int(e.id[2:]) for e in self.entries
                   if e.id.startswith(prefix + "-") and e.id[2:].isdigit()]
        return f"{prefix}-{max(numbers, default=0) + 1:03d}"

    def current_sources(self) -> list[tuple[str, str, str]]:
        """(entry_id, call_id, quote) for every source in the history of a current entry."""
        return [(e.id, call_id, quote)
                for e in self.current()
                for h in e.history
                for call_id, quote in h.sources]

    def held_sources(self) -> list[HeldSource]:
        """Every source in the history of a current entry, with the ref and date of the change that
        first held it. A promote or merge line carries its old entries' sources, so such a source
        keeps the ref and date of the old entry's line (a Watch from a brief's finding menu keeps the
        span its ref names). nua.apply.held_spans places these on the digest claims."""
        out = []
        for e in self.current():
            for h in e.history:
                for call_id, quote in h.sources:
                    ref, day = self._first_held(h, (call_id, quote), {e.id})
                    out.append(HeldSource(e.id, call_id, quote, ref, day))
        return out

    def _first_held(self, h: HistoryLine, source: tuple[str, str], seen: set[str]) -> tuple[str, str]:
        """(ref, date) of the line that first held `source`, following promote and merge lines back to
        the entries they name ("from W-004", "with W-002, B-003")."""
        if h.action in ("promote", "merge") and h.text:
            for letter, number in _ENTRY_ID.findall(h.text):
                old = self.get(f"{letter}-{number}")
                if old is None or old.id in seen:
                    continue
                line = next((x for x in old.history if source in [tuple(s) for s in x.sources]), None)
                if line is not None:
                    return self._first_held(line, source, seen | {old.id})
        return h.ref or "", h.date or ""


def parse_beliefs(text: str) -> BeliefsDoc:
    """Parse beliefs.md. Lines before the first section heading are ignored; anything else unexpected raises ValueError."""
    entries: list[Entry] = []
    seen: set[str] = set()
    section: str | None = None
    entry: Entry | None = None
    in_history = False

    for n, line in enumerate((text or "").splitlines(), start=1):
        stripped = line.strip()
        if not stripped:
            continue
        if line.startswith("## "):
            section = line[3:].strip().lower()
            if section not in _SECTIONS:
                raise ValueError(f"beliefs.md line {n}: unknown section {line.strip()!r}")
            entry, in_history = None, False
            continue
        if section is None:
            if line.startswith("### ") or stripped.startswith("- "):
                raise ValueError(f"beliefs.md line {n}: entry before any '## Beliefs|Watching|Retired' heading")
            continue
        if line.startswith("### "):
            m = _ENTRY_HEAD.match(line)
            if not m:
                raise ValueError(f"beliefs.md line {n}: entry heading must be '### B-### <statement>'")
            if m.group(1) in seen:
                raise ValueError(f"beliefs.md line {n}: duplicate id {m.group(1)}")
            seen.add(m.group(1))
            entry = Entry(id=m.group(1), statement=m.group(2), scope=Scope(), section=section, history=[])
            entries.append(entry)
            in_history = False
            continue
        if entry is None:
            raise ValueError(f"beliefs.md line {n}: text outside an entry: {stripped!r}")
        if line.startswith("- scope:"):
            try:
                entry.scope = parse_scope(line[len("- scope:"):])
            except ValueError as e:
                raise ValueError(f"beliefs.md line {n}: {e}") from None
            in_history = False
            continue
        if line.rstrip() == "- history:":
            in_history = True
            continue
        if in_history and line[0] in " \t" and stripped.startswith("- "):
            try:
                entry.history.append(parse_history(stripped[2:]))
            except ValueError as e:
                raise ValueError(f"beliefs.md line {n}: {e}") from None
            continue
        raise ValueError(f"beliefs.md line {n}: unexpected line {stripped!r}")

    return BeliefsDoc(entries=entries)


def _render_entry(e: Entry) -> str:
    """One entry block, ending in a newline."""
    statement = _one_line(e.statement)
    if not statement:
        raise ValueError(f"{e.id} has no statement")
    scope = format_scope(e.scope)
    lines = [f"### {e.id} {statement}", f"- scope: {scope}" if scope else "- scope:", "- history:"]
    lines += [f"  - {format_history(h)}" for h in e.history]
    return "\n".join(lines) + "\n"


def render_beliefs(doc: BeliefsDoc) -> str:
    """beliefs.md text: the three sections in order, entries in list order within each.

    `parse_beliefs(render_beliefs(doc)) == doc` for a doc whose entries are grouped by section.
    """
    for e in doc.entries:
        if e.section not in _SECTIONS:
            raise ValueError(f"{e.id} has unknown section {e.section!r}")
    blocks: list[str] = []
    for key, title in _SECTIONS.items():
        blocks.append(f"## {title}\n")
        blocks.extend(_render_entry(e) for e in doc.entries if e.section == key)
    return "\n".join(blocks)


# ---------- decisions.md ----------

def _entry_ids(text: str) -> list[str]:
    """B-/W- ids in `text`, normalized to three digits."""
    return [_norm_id(p, num) for p, num in _ENTRY_ID.findall(text)]


def _add_ids(target: list[str], ids: list[str]) -> None:
    for i in ids:
        if i not in target:
            target.append(i)


def _split_inline(title: str) -> tuple[str, list[str]]:
    """'Footage review tool: rests on B-001, B-003' -> ('Footage review tool', ['B-001', 'B-003']).

    A title that only mentions "rests on" without listing any ids is left whole.
    """
    m = _RESTS_ON_INLINE.search(title)
    ids = _entry_ids(m.group("ids")) if m else []
    if not ids:
        return title.strip(), []
    return title[:m.start()].strip(), ids


def parse_decisions(text: str) -> list[Decision]:
    """Parse the hand-edited decisions.md.

    A decision is a `### D-### <title>` heading followed by `- rests on: B-001, B-003`; the ids may
    also sit in indented bullets under a bare `- rests on:`. The SPEC's one-line form
    `D-002 Footage review tool: rests on B-001, B-003` (as a heading, bullet or plain line) works too,
    as long as it lists at least one id.
    Other headings end a decision; prose and other bullets are ignored. Ids are normalized to three digits.
    """
    decisions: list[Decision] = []
    current: Decision | None = None
    collecting = False          # after a `rests on:` line that listed no ids

    for line in (text or "").splitlines():
        if _HEADING.match(line):
            collecting = False
            m = _DECISION_HEAD.match(line)
            if m:
                title, ids = _split_inline(m.group(2) or "")
                current = Decision(id=_norm_id("D", m.group(1)[2:]), title=title, rests_on=[])
                _add_ids(current.rests_on, ids)
                decisions.append(current)
            else:
                current = None
            continue
        one_line = _DECISION_LINE.match(line)
        if one_line:
            title, ids = _split_inline(one_line.group(2))
            if ids:
                decisions.append(Decision(id=_norm_id("D", one_line.group(1)[2:]), title=title, rests_on=ids))
                current, collecting = None, False
                continue
        if current is None:
            continue
        rests = _RESTS_ON_LINE.match(line)
        if rests:
            ids = _entry_ids(rests.group(1))
            _add_ids(current.rests_on, ids)
            collecting = not ids
            continue
        if collecting and line[:1] in (" ", "\t") and line.strip()[:1] in ("-", "*"):
            _add_ids(current.rests_on, _entry_ids(line))
            continue
        collecting = False

    return decisions


def render_decisions(decisions: list[Decision]) -> str:
    """decisions.md text in the canonical form. `parse_decisions(render_decisions(x)) == x`."""
    blocks = ["## Decisions\n"]
    for d in decisions:
        head = f"### {d.id} {_one_line(d.title)}".rstrip()
        rests = ", ".join(d.rests_on)
        blocks.append(f"{head}\n- rests on:{' ' + rests if rests else ''}\n")
    return "\n".join(blocks)
