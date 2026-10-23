"""Digest and stub files: `digests/<call_id>.md`.

Structured data lives in the YAML front-matter (`Digest.to_dict()`); the body is readable
markdown rendered from the same data, so a person or an agent can read a digest without
parsing YAML. The front-matter is the record; the body is regenerated on every write.
"""
from __future__ import annotations

import copy
import datetime as dt
import re
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any, Iterable

import yaml

from discovery import frontmatter
from discovery.ports import RepoFS
from discovery.types import Claim, Digest, ParsedTranscript, Quote, iso_date

DIGEST_DIR = "digests"
STUB_STATUSES = ("internal", "parse_failed", "failed")
ORIGINS = ("live", "backfill")
UNREADABLE = "unreadable"   # status of the stand-in for a digest file that cannot be read; never written
REQUIRED = ("call_id", "transcript", "status", "origin")

# Fields a hand correction in config/overrides.yml may set. The call id never changes.
OVERRIDE_FIELDS = ("contact", "firm", "seat", "role_in_thesis", "institution_type", "date")

_EM_DASH = re.compile(r"\s*\u2014\s*")
_SEP = " · "   # middle dot, as in the SPEC's brief and digest titles


# ---------- paths, reading, writing ----------

def digest_path(call_id: str) -> str:
    """Repo-relative path of a call's digest: `digests/<call_id>.md`."""
    return f"{DIGEST_DIR}/{call_id}.md"


class DigestError(ValueError):
    """A digest file that cannot be read: not UTF-8, front-matter that is not valid YAML, no digest
    front-matter, or a field of the wrong shape. The message starts with the file's path."""


@dataclass
class Unreadable:
    """A file in digests/ that load_digests could not read (a YAML slip in a hand correction, a stray
    non-digest .md). It still owns its call id (the file stem), and the transcript its raw text names on
    a `transcript:` line, when there is one, so no writer ever reuses the id or re-digests the call."""
    path: str
    call_id: str
    transcript: str | None
    error: str

    def stand_in(self) -> Digest:
        """The Digest digest_for_transcript returns for this file: status `unreadable` (a stub to every
        reader), which write_digest refuses to write."""
        return Digest(call_id=self.call_id, transcript=self.transcript or "", status=UNREADABLE,
                      origin="backfill", reason=self.error)


class DigestList(list):
    """load_digests' result: the readable digests and stubs, sorted by call_id, which every reader
    iterates as a plain list, plus `unreadable`: the files in digests/ that could not be read."""

    def __init__(self, digests: Iterable[Digest] = (), unreadable: Iterable[Unreadable] = ()):
        super().__init__(digests)
        self.unreadable: list[Unreadable] = list(unreadable)


def write_digest(repo: RepoFS, d: Digest) -> str:
    """Write `d` to `digests/<call_id>.md` (front-matter = `d.to_dict()`, body = readable markdown).

    Model-written text (summary, claim text, notes, follow-up text, ask next, reason) is written
    with em dashes rewritten as commas; quotes are written verbatim. `d` itself is not modified.
    Returns the repo-relative path. The stand-in for an unreadable file raises DigestError: that
    file is fixed by hand, never overwritten.
    """
    rel = digest_path(d.call_id)
    if is_unreadable(d):
        raise DigestError(f"{rel}: cannot be read, so it is never overwritten; fix it by hand ({d.reason})")
    clean = without_em_dashes(d)
    repo.write(rel, frontmatter.join(clean.to_dict(), render_body(clean)))
    return rel


def read_digest(repo: RepoFS, rel: str) -> Digest:
    """Read a digest file back into a `Digest`.

    Raises DigestError (a ValueError naming `rel`) when the file is not UTF-8, its front-matter is not
    valid YAML, it has no digest front-matter, or a field has the wrong shape. A missing file raises
    FileNotFoundError.
    """
    try:
        text = repo.read(rel)
    except UnicodeDecodeError as e:
        raise DigestError(f"{rel}: not UTF-8 text ({e.reason} at byte {e.start})") from e
    try:
        fm, _, _ = frontmatter.split(text)
    except yaml.YAMLError as e:
        raise DigestError(f"{rel}: front-matter is not valid YAML ({_one_line(e)})") from e
    missing = [k for k in REQUIRED if not fm.get(k)]
    if missing:
        raise DigestError(f"{rel}: not a digest, front-matter lacks {', '.join(missing)}")
    odd = [k for k in REQUIRED if not isinstance(fm[k], str)]
    if odd:
        raise DigestError(f"{rel}: {', '.join(odd)} must be text")
    try:
        return Digest.from_dict(fm)
    except Exception as e:     # a hand edit gave a field the wrong shape (claims: [1, 2])
        raise DigestError(f"{rel}: a front-matter field has the wrong shape ({type(e).__name__}: "
                          f"{_one_line(e)})") from e


def load_digests(repo: RepoFS) -> DigestList:
    """Every readable `digests/*.md`, stubs included, sorted by call_id.

    A file that cannot be read is left out, so one YAML slip never stops the processor, the indexes or
    `ask`, and is listed in the result's `unreadable` instead (see Unreadable for what it still owns:
    digest_for_transcript finds its transcript, and process_call treats its stem as a taken call id).
    Writers never overwrite it.
    """
    digests: list[Digest] = []
    unreadable: list[Unreadable] = []
    for rel in repo.list(f"{DIGEST_DIR}/*.md"):
        try:
            digests.append(read_digest(repo, rel))
        except (DigestError, OSError) as e:
            unreadable.append(Unreadable(path=rel, call_id=PurePosixPath(rel).stem,
                                         transcript=_named_transcript(repo, rel), error=_one_line(e)))
    return DigestList(sorted(digests, key=lambda d: d.call_id), unreadable)


def unreadable_of(digests: Iterable[Digest]) -> list[Unreadable]:
    """The unreadable files load_digests found; [] for any other list."""
    return list(getattr(digests, "unreadable", None) or [])


def unreadable_paths(digests: Iterable[Digest]) -> list[str]:
    """Repo paths of the digest files load_digests could not read, sorted."""
    return sorted(u.path for u in unreadable_of(digests))


_TRANSCRIPT_LINE = re.compile(r"^transcript:[ \t]*(.*?)[ \t]*$", re.M)


def _named_transcript(repo: RepoFS, rel: str) -> str | None:
    """The transcript an unreadable digest names on a top-level `transcript:` line of its front-matter,
    read with a plain regex (the YAML does not load), or None."""
    try:
        text = repo.read_bytes(rel).decode("utf-8", errors="replace")
    except OSError:
        return None
    if text.startswith("---\n"):
        end = text.find("\n---", 3)
        text = text[: end if end != -1 else len(text)]
    m = _TRANSCRIPT_LINE.search(text)
    if not m:
        return None
    value = m.group(1).strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
        value = value[1:-1].replace("''", "'") if value[0] == "'" else value[1:-1]
    value = _norm_path(value.strip())
    return value if value and value not in ("~", "null") else None


def _one_line(e: object) -> str:
    return " ".join(str(e).split())


def is_stub(d: Digest) -> bool:
    """True for `internal`, `parse_failed` and `failed` stubs (and an unreadable file's stand-in).
    `ask` and the indexes skip these."""
    return d.status != "digested"


def is_unreadable(d: Digest) -> bool:
    """True for the stand-in digest_for_transcript returns for a digest file that cannot be read."""
    return d.status == UNREADABLE


def _norm_path(path: str) -> str:
    return path[2:] if path.startswith("./") else path


def digest_for_transcript(digests: list[Digest], transcript: str) -> Digest | None:
    """The digest that points to `transcript`, through its `transcript` field or its `duplicates`.

    A digest naming the file as its own transcript wins over one listing it as a duplicate, and a
    real digest wins over a stub. When `digests` came from load_digests, an unreadable digest file
    naming the transcript counts too, after readable ones naming it as their own: the result is then
    its stand-in (is_unreadable), so the transcript is never digested again under a new id.
    None when no digest points to the file.
    """
    target = _norm_path(transcript)
    own = [d for d in digests if _norm_path(d.transcript) == target]
    if own:
        return next((d for d in own if not is_stub(d)), own[0])
    for u in unreadable_of(digests):
        if u.transcript and _norm_path(u.transcript) == target:
            return u.stand_in()
    return next((d for d in digests if target in {_norm_path(p) for p in d.duplicates}), None)


def without_em_dashes(d: Digest) -> Digest:
    """A copy of `d` with em dashes removed from model-written text. Quotes are left verbatim."""
    out = copy.deepcopy(d)
    out.summary = [_plain(s) for s in out.summary]
    out.ask_next = [_plain(q) for q in out.ask_next]
    if out.reason:
        out.reason = _plain(out.reason)
    for c in out.claims:
        c.text = _plain(c.text)
    for b in out.belief_checks:
        b.note = _plain(b.note)
    for f in out.follow_ups:
        f.what = _plain(f.what)
    return out


# ---------- stubs ----------

def make_stub(*, call_id: str, transcript: str, status: str, origin: str, reason: str,
              parsed: ParsedTranscript | None = None) -> Digest:
    """Build a stub digest (`internal`, `parse_failed` or `failed`).

    A stub carries only call_id, transcript, duplicates, status, origin, the parsed metadata
    (copied from `parsed` when given) and a reason. Its call_id is the transcript's filename stem, or
    `<stem>-2` and up when another transcript's digest already has that id (process._stub_id).
    """
    if status not in STUB_STATUSES:
        raise ValueError(f"stub status must be one of {STUB_STATUSES}, got {status!r}")
    if origin not in ORIGINS:
        raise ValueError(f"origin must be one of {ORIGINS}, got {origin!r}")
    d = Digest(call_id=call_id, transcript=transcript, status=status, origin=origin, reason=reason)
    if parsed is not None:
        d.source = parsed.source
        d.source_id = parsed.source_id
        d.date = parsed.date
        d.date_source = parsed.date_source
        d.recorded_by = parsed.recorded_by
        d.attendees = copy.deepcopy(parsed.attendees)
        d.speaker_labels = parsed.speaker_labels
    return d


# ---------- overrides ----------

def _as_date(value: Any) -> str | None:
    """YYYY-MM-DD from an override value (YAML turns an unquoted 2026-09-18 into a date), or None when it
    is not a real date. frontmatter.load_yaml keeps an impossible date such as 2026-09-31 as text."""
    if isinstance(value, (dt.date, str)):
        try:
            return dt.date.fromisoformat(iso_date(value).strip()).isoformat()
        except (AttributeError, ValueError):
            return None
    return None


def _fix_for(overrides: Any, call_id: str) -> dict:
    fix = overrides.get(call_id) if isinstance(overrides, dict) else None
    return fix if isinstance(fix, dict) else {}


def override_problems(overrides: dict[str, dict], call_id: str | None = None) -> list[str]:
    """Hand corrections in `config/overrides.yml` that apply_overrides skips, one line each: a date that
    is not a real YYYY-MM-DD date. Only `call_id`'s entry when given, else every entry."""
    if call_id is not None:
        ids = [call_id]
    else:
        ids = list(overrides) if isinstance(overrides, dict) else []
    out = []
    for cid in ids:
        value = _fix_for(overrides, cid).get("date")
        if value not in (None, "") and _as_date(value) is None:
            out.append(f"config/overrides.yml: {cid} date {str(value)!r} is not a real YYYY-MM-DD date; ignored")
    return out


def apply_overrides(d: Digest, overrides: dict[str, dict]) -> Digest:
    """Return a copy of `d` with the hand corrections for its call_id from `config/overrides.yml`.

    Sets contact, firm, seat, role_in_thesis, institution_type and date when given (empty values
    are ignored). A firm override sets `firm_source: manual`; a date override sets
    `date_source: manual`. A date that is not a real YYYY-MM-DD date is skipped (override_problems
    lists it), so no reader ever meets an impossible date. Other keys are ignored and the call_id
    never changes. `d` is not modified.
    """
    out = copy.deepcopy(d)
    fix = _fix_for(overrides, d.call_id)
    for key in OVERRIDE_FIELDS:
        value = fix.get(key)
        if value is None or value == "":
            continue
        if key == "date":
            day = _as_date(value)
            if day is not None:
                out.date, out.date_source = day, "manual"
        else:
            setattr(out, key, str(value))
            if key == "firm":
                out.firm_source = "manual"
    return out


# ---------- readable body ----------

def _plain(text: str | None) -> str:
    """Model-written text with em dashes rewritten as commas. Text without one is returned as is.
    Never used on quotes."""
    if not text:
        return ""
    if "\u2014" not in text:
        return text
    return _EM_DASH.sub(", ", text).strip(" ,")


def _blockquote(quote: str, indent: str = "") -> str:
    """A verbatim quote as a markdown blockquote, one `> ` per line."""
    lines = quote.split("\n") if quote else [""]
    return "\n".join(f"{indent}> {line}" if line else f"{indent}>" for line in lines)


def digest_title(d: Digest) -> str:
    """`contact · firm · seat · date` with missing parts omitted. Led by the call_id when there is
    no contact (stubs), so a title never reads as a bare date."""
    lead = d.contact or d.call_id
    parts = [p for p in (lead, d.firm, d.seat, d.date) if p]
    return _SEP.join(_plain(str(p)) for p in parts)


def _turns(turns: list[int]) -> str:
    if not turns:
        return ""
    label = "turn" if len(turns) == 1 else "turns"
    return f"{label} {', '.join(str(t) for t in turns)}"


def _quote_meta(q: Quote) -> str:
    """Speaker, turns and question turn of one quote."""
    parts = [q.speaker + (" (inferred)" if q.attribution == "inferred" else "") if q.speaker else "speaker unknown"]
    if q.turns:
        parts.append(_turns(q.turns))
    if q.question_turn is not None:
        parts.append(f"asked at turn {q.question_turn}")
    return _SEP.join(parts)


def _claim_meta(c: Claim) -> str:
    parts = [f"found by {c.found_by} model{'' if c.found_by == 1 else 's'}"]
    parts.extend(name for name in ("firsthand", "hedged", "prompted") if getattr(c.flags, name))
    scope = [f"{k}: {v}" for k, v in c.scope.to_dict().items() if v]
    if scope:
        parts.append("scope " + ", ".join(scope))
    return _SEP.join(parts)


def _claims_section(d: Digest) -> list[str]:
    out = ["## Findings", ""]
    for c in d.claims:
        label = f"**{c.id}.** " if c.id else ""
        new = " (new finding)" if c.id and c.id in d.new_findings else ""
        out += [f"{label}{_plain(c.text)}{new}", ""]
        for q in c.quotes:
            out += [_blockquote(q.quote), "", _quote_meta(q), ""]
        out += [_claim_meta(c), ""]
    return out


def _checks_section(d: Digest) -> list[str]:
    out = ["## Belief checks", ""]
    for b in d.belief_checks:
        parts = [f"**{b.target}** {b.verdict}"]
        if b.claims:
            parts.append(", ".join(b.claims))
        parts.append(f"decided by {b.decided_by}")
        if b.source != "ingest":
            parts.append(b.source)
        line = "- " + _SEP.join(parts)
        note = _plain(b.note)
        out.append(f"{line}. {note}" if note else line)
    return out + [""]


def _coverage_section(d: Digest) -> list[str]:
    out = ["## Prep questions", ""]
    for q in d.question_coverage:
        status = q.status.replace("_", " ")
        refs = f" ({', '.join(q.claims)})" if q.claims else ""
        out.append(f"- {status}: {_plain(q.question)}{refs}")
    return out + [""]


def _follow_ups_section(d: Digest) -> list[str]:
    out = ["## Follow-ups", ""]
    for f in d.follow_ups:
        label = "We owe" if f.direction == "we_owe" else "They offered"
        who = f" ({f.who})" if f.who else ""
        out.append(f"- {label}: {_plain(f.what)}{who}")
        if f.quote:
            out.append(_blockquote(f.quote, indent="  "))
    return out + [""]


def _verification_line(d: Digest) -> str:
    v = d.verification
    line = (f"Verification: {v.quotes} quotes, {v.matched_first_pass} matched first pass, "
            f"{v.repaired} repaired, {len(v.dropped)} dropped.")
    if v.findings_dropped:
        line += f" {v.findings_dropped} finding(s) left with no verified quote, left out."
    if v.follow_up_quotes_dropped:
        line += f" {v.follow_up_quotes_dropped} follow-up quote(s) not found in the transcript, left out."
    return line


def _models_line(d: Digest) -> str:
    parts = []
    if d.prompt_version:
        parts.append(f"prompt {d.prompt_version}")
    for role, value in (d.models or {}).items():
        names = ", ".join(str(v) for v in value) if isinstance(value, (list, tuple)) else str(value)
        if names:
            parts.append(f"{role}: {names}")
    return ("Processed with " + _SEP.join(parts) + ".") if parts else ""


def _header_lines(d: Digest) -> list[str]:
    lines = [f"Transcript: {d.transcript}"]
    if d.duplicates:
        lines.append(f"Other copies: {', '.join(d.duplicates)}")
    meta = [f"origin {d.origin}"]
    if d.source:
        meta.append(f"source {d.source}")
    if d.recorded_by:
        meta.append(f"recorded by {d.recorded_by}")
    if d.speaker_labels:
        meta.append(f"speaker labels {d.speaker_labels}")
    if d.duration_min:
        meta.append(f"{d.duration_min} min")
    if d.date is None or d.date_source == "unknown":
        meta.append("date unknown")
    lines.append(_SEP.join(meta))
    who = [p for p in (d.institution_type, d.role_in_thesis.replace("_", " ") if d.role_in_thesis else None) if p]
    if who:
        lines.append(_SEP.join(who))
    if d.prep:
        lines.append(f"Prep: {d.prep}")
    if d.degraded:
        lines.append(f"Degraded: {', '.join(d.degraded)}")
    return [line + "  " for line in lines[:-1]] + lines[-1:]   # markdown hard breaks between lines


def render_body(d: Digest) -> str:
    """Readable markdown for a digest or stub.

    Digest: title, header, summary, findings (text, then each verbatim quote as a blockquote with its
    speaker and turns, then found_by, flags and scope), belief checks, prep questions, follow-ups, ask next, verification line.
    Stub: title plus status and reason. Quotes are never altered; other text has em dashes removed.
    """
    lines = [f"# {digest_title(d)}", ""]
    if is_stub(d):
        lines += [f"Status: {d.status}  ", f"Reason: {_plain(d.reason) or 'none given'}  "]
        lines += _header_lines(d)
        return "\n".join(lines).rstrip() + "\n"

    lines += _header_lines(d) + [""]
    if d.summary:
        lines += ["## Summary", ""] + [f"- {_plain(s)}" for s in d.summary] + [""]
    if d.claims:
        lines += _claims_section(d)
    if d.belief_checks:
        lines += _checks_section(d)
    if d.question_coverage:
        lines += _coverage_section(d)
    if d.follow_ups:
        lines += _follow_ups_section(d)
    if d.ask_next:
        lines += ["## Ask next", ""] + [f"- {_plain(q)}" for q in d.ask_next] + [""]
    lines += [_verification_line(d), ""]
    models = _models_line(d)
    if models:
        lines += [models, ""]
    if d.reprocess_failed:
        lines += [f"Re-run failed at prompt {d.reprocess_failed}.", ""]
    return "\n".join(lines).rstrip() + "\n"
