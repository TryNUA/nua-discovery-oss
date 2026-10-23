"""Quote checks: find a model's quote in the transcript body and map it back to raw offsets.

Matching runs over the turn texts only, so the recorder notes and the turn markup
(`[m:ss] **Name**:`, `Me:`) can never match. Case, curly quotes, punctuation and
whitespace are ignored. Every word keeps its raw-file offsets, so a match points
straight back into the raw file, and the stored quote is the raw text there.

A finding carries 1 to 3 quotes; each is checked on its own, a failed one is repaired once and then
dropped, and the finding survives while any quote does. On a `named` call, a quote that sits in one
turn takes that turn's label as its speaker, whatever the model said. Follow-up quotes get the same
check (no repair).

A short answer needs the turn holding its question: an earlier turn, or, where turns do not separate
speakers (a single_stream call, or a Granola text export that parses to one block), its own turn when
words come before the answer there.
"""
from __future__ import annotations

import bisect
import re
from dataclasses import dataclass, field
from typing import Callable

from discovery.llm import LLMError
from discovery.types import Claim, FollowUp, ParsedTranscript, Quote, Turn, Verification

# A word is a run of letters or digits, with apostrophes allowed between them ("don't").
_APOSTROPHES = "'‘’ʼ"
_WORD = re.compile(rf"[^\W_]+(?:[{_APOSTROPHES}][^\W_]+)*")
_STRAIGHT = str.maketrans({c: "'" for c in _APOSTROPHES[1:]})

# Turn markup a model may copy into a quote: "[12:34]", "[1:02:03]", "[T12]", "[T12 03:41]", "**Name**:".
_TAG = r"\[(?:T\d+(?:[ \t]+\d{1,2}(?::\d{2}){1,2})?|\d{1,2}(?::\d{2}){1,2})\]"
_BOLD_LABEL = r"\*\*[^*\n]{1,80}\*\*:"

NOT_FOUND = "quote not found in transcript"
EMPTY = "empty quote"
SHORT_ANSWER = "short answer needs question_turn"


def _words(text: str) -> list[str]:
    """Normalized words of `text`: lowercase, straight apostrophes, punctuation dropped."""
    return [m.group().translate(_STRAIGHT).lower() for m in _WORD.finditer(text)]


def normalize(text: str) -> str:
    """Lowercase, curly quotes to straight, drop punctuation except in-word apostrophes, collapse whitespace."""
    return " ".join(_words(text))


# Text matching between stored quotes. Every module that compares two quotes uses these, so "the same
# quote" means one thing across the pipeline. discovery/ask.py keeps its own one-way coverage rule on purpose.

def token_overlap(a: str, b: str) -> float:
    """Share of the smaller word set found in the other: |A & B| / min(|A|, |B|). 0.0 if either is empty."""
    wa, wb = set(_words(a)), set(_words(b))
    if not wa or not wb:
        return 0.0
    return len(wa & wb) / min(len(wa), len(wb))


def quote_overlap(a: str, b: str) -> float:
    """Longest run of words two quotes share, over the shorter quote's word count (0.0 if either is empty).

    For two verbatim quotes from one transcript this is the share of the shorter span that the other
    covers, so it stands in for `merge.span_overlap` when only the quote text is stored.
    """
    wa, wb = _words(a), _words(b)
    if not wa or not wb:
        return 0.0
    best = 0
    prev = [0] * (len(wb) + 1)
    for x in wa:
        cur = [0] * (len(wb) + 1)
        for j, y in enumerate(wb, 1):
            if x == y:
                cur[j] = prev[j - 1] + 1
                if cur[j] > best:
                    best = cur[j]
        prev = cur
    return best / min(len(wa), len(wb))


def contains(text: str, quote: str) -> bool:
    """True when `quote`, normalized, is not empty and appears inside `text`, normalized."""
    needle = normalize(quote)
    return bool(needle) and needle in normalize(text)


def _is_punct(ch: str) -> bool:
    return not ch.isalnum() and not ch.isspace()


@dataclass
class Match:
    start: int                 # raw-file offset of the first matched character
    end: int                   # raw-file offset one past the last matched character
    raw_text: str              # raw text over the match with turn markup removed, turns joined by one space
    turns: list[int] = field(default_factory=list)   # Turn.index values covered, in order


class QuoteIndex:
    """Word index over the transcript turns of one parsed file, with raw-file offsets."""

    def __init__(self, parsed: ParsedTranscript):
        self.parsed = parsed
        self._order: list[Turn] = sorted(parsed.turns, key=lambda t: t.start)
        self._by_index: dict[int, Turn] = {t.index: t for t in parsed.turns}
        self._words: list[str] = []
        self._starts: list[int] = []      # raw offset of each word
        self._ends: list[int] = []
        self._turn_pos: list[int] = []    # position in self._order of each word's turn
        self._jpos: list[int] = []        # offset of each word in self._joined
        raw = parsed.raw
        cursor = 0
        for pos, t in enumerate(self._order):
            lo, hi = max(t.start, parsed.body_start), min(t.end, parsed.body_end)
            if lo >= hi:
                continue
            for m in _WORD.finditer(raw, lo, hi):
                word = m.group().translate(_STRAIGHT).lower()
                self._words.append(word)
                self._starts.append(m.start())
                self._ends.append(m.end())
                self._turn_pos.append(pos)
                self._jpos.append(cursor)
                cursor += len(word) + 1
        self._joined = " ".join(self._words)
        names = sorted({t.speaker for t in parsed.turns if t.speaker}, key=len, reverse=True)
        parts = [_TAG, _BOLD_LABEL]
        if names:
            parts.append(r"(?<!\w)(?:" + "|".join(re.escape(n) for n in names) + r"):")
        self._markup = re.compile("|".join(parts))

    def strip_markup(self, quote: str) -> str:
        """Remove turn markup a model copied into a quote (timestamps, turn tags, speaker labels)."""
        return self._markup.sub(" ", quote)

    def find(self, quote: str) -> Match | None:
        """First place `quote` occurs in the transcript body, or None."""
        found = self.find_all(quote, limit=1)
        return found[0] if found else None

    def find_all(self, quote: str, *, limit: int | None = None) -> list[Match]:
        """Every place `quote` occurs in the transcript body, in file order."""
        cleaned = self.strip_markup(quote)
        words = _words(cleaned)
        if not words:
            return []
        needle = " ".join(words)
        hay = self._joined
        out: list[Match] = []
        at = hay.find(needle)
        while at != -1:
            stop = at + len(needle)
            if (at == 0 or hay[at - 1] == " ") and (stop == len(hay) or hay[stop] == " "):
                first = bisect.bisect_left(self._jpos, at)
                out.append(self._match(first, first + len(words) - 1, cleaned.strip()))
                if limit is not None and len(out) >= limit:
                    break
            at = hay.find(needle, at + 1)
        return out

    def _match(self, i: int, j: int, quote: str) -> Match:
        """Build a Match for words i..j, taking in punctuation the quote itself starts or ends with."""
        raw = self.parsed.raw
        first, last = self._turn_pos[i], self._turn_pos[j]
        start, end = self._starts[i], self._ends[j]
        if quote and _is_punct(quote[0]):
            floor = self._order[first].start
            while start > floor and _is_punct(raw[start - 1]):
                start -= 1
        if quote and _is_punct(quote[-1]):
            ceiling = self._order[last].end
            while end < ceiling and _is_punct(raw[end]):
                end += 1
        pieces, turns = [], []
        for pos in range(first, last + 1):
            t = self._order[pos]
            piece = raw[start if pos == first else t.start:end if pos == last else t.end].strip()
            turns.append(t.index)
            if piece:
                pieces.append(piece)
        return Match(start=start, end=end, raw_text=" ".join(pieces), turns=turns)

    def speaker_of(self, index: int) -> str | None:
        """Speaker label of the turn with Turn.index `index`; None if there is none or it is unlabeled."""
        t = self._by_index.get(index)
        return t.speaker if t else None

    def has_turn(self, index: int | None) -> bool:
        """True if `index` is the index of a turn in this transcript."""
        return isinstance(index, int) and not isinstance(index, bool) and index in self._by_index

    def turn_text(self, index: int) -> str:
        """Text of the turn with Turn.index `index`; empty string if there is none."""
        t = self._by_index.get(index)
        return t.text if t else ""

    def text_before(self, index: int, offset: int) -> str:
        """Text of the turn with Turn.index `index` that comes before raw offset `offset`, stripped: the
        question a short answer at `offset` answers in its own turn. Empty if there is no such turn."""
        t = self._by_index.get(index)
        if t is None:
            return ""
        return self.parsed.raw[t.start:max(t.start, min(offset, t.end))].strip()

    @property
    def one_stream(self) -> bool:
        """True when a turn may hold both sides of the call: a single_stream call, or one block of text."""
        return self.parsed.speaker_labels == "single_stream" or len(self.parsed.turns) == 1

    def answers(self, question_turn: int | None, m: Match) -> bool:
        """True when `question_turn` can hold the question a short answer at `m` answers: a turn before
        the answer's first turn, or on a one_stream call that same turn when words come before the answer
        in it (the whole call is one turn in a Granola text export)."""
        if not self.has_turn(question_turn) or not m.turns:
            return False
        first = m.turns[0]
        if question_turn < first:
            return True
        if question_turn != first or not self.one_stream:
            return False
        t = self._by_index[first]
        return _WORD.search(self.parsed.raw, max(t.start, self.parsed.body_start), m.start) is not None

    def context(self, turns: list[int], radius: int = 2) -> str:
        """`[T12] Speaker: text` lines for the given turns and `radius` turns either side.

        Non-adjacent runs are separated by a `...` line. Whitespace inside a turn is collapsed.
        """
        wanted = sorted({i for t in turns for i in range(t - radius, t + radius + 1) if i in self._by_index})
        lines: list[str] = []
        prev = None
        for i in wanted:
            if prev is not None and i != prev + 1:
                lines.append("...")
            t = self._by_index[i]
            text = " ".join(t.text.split())
            lines.append(f"[T{i}] {t.speaker}: {text}" if t.speaker else f"[T{i}] {text}")
            prev = i
        return "\n".join(lines)


def _pick(matches: list[Match], q: Quote) -> Match:
    """Choose among several occurrences: in or after the question turn first, then the model's own turns,
    then the nearest. For a short answer `matches` holds only the occurrences its question_turn answers."""
    if len(matches) == 1:
        return matches[0]
    qt = q.question_turn if isinstance(q.question_turn, int) else None
    hinted = set(q.turns or [])

    def key(m: Match):
        after_q = qt is not None and m.turns[0] >= qt
        return (not after_q, not (hinted & set(m.turns)), m.turns[0] - qt if after_q else 0, m.start)

    return min(matches, key=key)


def check_quote(index: QuoteIndex, q: Quote, short_answer_words: int) -> tuple[Match | None, str]:
    """Return (match, "") or (None, reason) for one quote."""
    if not normalize(index.strip_markup(q.quote or "")):
        return None, EMPTY
    matches = index.find_all(q.quote)
    if not matches:
        return None, NOT_FOUND
    if len(_words(matches[0].raw_text)) <= short_answer_words:
        matches = [m for m in matches if index.answers(q.question_turn, m)]
        if not matches:
            return None, SHORT_ANSWER
    return _pick(matches, q), ""


def _apply(index: QuoteIndex, q: Quote, m: Match,
           canonical: Callable[[str | None], str | None] | None) -> Quote:
    """Copy of `q` with the verified span, raw quote and turns.

    On a `named` call a one-turn quote's speaker is that turn's label (through `canonical`), so a
    quote from the interviewer's turn is never stored as the contact's words.
    """
    speaker = q.speaker
    if index.parsed.speaker_labels == "named" and len(m.turns) == 1:
        label = index.speaker_of(m.turns[0])
        if label:
            speaker = (canonical(label) if canonical else label) or label
    return q.model_copy(update={"offset": (m.start, m.end), "quote": m.raw_text, "turns": list(m.turns),
                                "speaker": speaker,
                                "attribution": "inferred" if len(m.turns) > 1 else q.attribution})


def _overlaps(a: tuple[int, int], b: tuple[int, int]) -> bool:
    """True when two spans share at least half of the shorter one."""
    shorter = min(a[1] - a[0], b[1] - b[0])
    return shorter > 0 and max(0, min(a[1], b[1]) - max(a[0], b[0])) / shorter >= 0.5


def verify_claims(parsed: ParsedTranscript, claims: list[Claim], *,
                  repair: Callable[[Claim, Quote, str], Quote | None] | None = None,
                  short_answer_words: int = 8,
                  canonical: Callable[[str | None], str | None] | None = None) -> tuple[list[Claim], Verification]:
    """Check every quote of every finding against the transcript body, each one on its own.

    A match sets the quote's `offset`, `quote` (the raw text), `turns`, and `attribution = "inferred"`
    when it crosses turns. On a `named` call a quote inside one turn sets its `speaker` to that turn's
    label, mapped through `canonical` (config.canonical) when given. A quote of `short_answer_words`
    words or fewer needs a `question_turn` that holds its question (QuoteIndex.answers: an earlier
    turn, or its own turn on a one_stream call). A miss gets one `repair(finding, quote, reason)` call;
    a second miss, a `None` repair or an LLMError drops that quote into `Verification.dropped`.
    A quote that lands on the span of an earlier quote of the same finding is folded into it. A
    finding keeps the quotes that survive and is dropped (`findings_dropped`) only when none does.
    Returns (kept findings in input order, verification counts). Inputs are not mutated.
    """
    index = QuoteIndex(parsed)
    v = Verification()
    kept: list[Claim] = []
    for claim in claims:
        good: list[Quote] = []
        for q in claim.quotes:
            v.quotes += 1
            m, reason = check_quote(index, q, short_answer_words)
            if m is not None:
                v.matched_first_pass += 1
                fixed_q = _apply(index, q, m, canonical)
            else:
                fixed = None
                if repair is not None:
                    try:
                        fixed = repair(claim, q, reason)
                    except LLMError:
                        fixed = None
                fixed_q = None
                if fixed is not None:
                    m, second = check_quote(index, fixed, short_answer_words)
                    if m is not None:
                        v.repaired += 1
                        fixed_q = _apply(index, fixed, m, canonical)
                    else:
                        reason = second
                if fixed_q is None:
                    v.dropped.append({"model": claim.model, "quote": q.quote, "reason": reason})
                    continue
            if not any(_overlaps(fixed_q.span, g.span) for g in good):
                good.append(fixed_q)
        if good:
            kept.append(claim.model_copy(update={"quotes": good}))
        else:
            v.findings_dropped += 1
    return kept, v


def verify_follow_ups(parsed: ParsedTranscript, follow_ups: list[FollowUp]) -> tuple[list[FollowUp], int]:
    """Check each follow-up's quote against the transcript body, like a claim's quote.

    A match stores the raw transcript text there. A miss (a made-up quote, or one from the recorder
    notes) keeps the follow-up, since `what` is model text anyway, with an empty quote. Returns
    (follow-ups in input order, number of quotes emptied). Inputs are not mutated.
    """
    index = QuoteIndex(parsed)
    out: list[FollowUp] = []
    missed = 0
    for f in follow_ups:
        quote = ""
        if normalize(index.strip_markup(f.quote or "")):
            m = index.find(f.quote)
            if m is None:
                missed += 1
            else:
                quote = m.raw_text
        out.append(f.model_copy(update={"quote": quote}))
    return out, missed
