"""Prep files in prep/: template check, parsing, loading, and matching a call to its prep.

See SPEC "Pre-call prep" and process_call step 9. Only the `## Questions` list is used downstream.
"""
from __future__ import annotations

import re
from datetime import date as date_cls, datetime
from typing import Any

import yaml

from discovery import frontmatter
from discovery.callid import slug_name
from discovery.config import Config
from discovery.ports import RepoFS
from discovery.types import PrepFile

_ISO_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")
_EMAIL = re.compile(r"[^@\s]+@[^@\s]+\.[^@\s]+")
_HEADING = re.compile(r"^##\s+(.+?)\s*$")
_ITEM = re.compile(r"^(\s*)(?:\d+[.)]|[-*])(?:\s+|$)(.*)$")    # "1. ", "1) ", "- ", "* "
_OPTIONAL_TEXT = ("firm", "attendee_email", "seat", "caller")


def _planned_date(value: Any) -> str | None:
    """planned_date as YYYY-MM-DD, or None when missing or not a real date. YAML may give a date object."""
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date_cls):
        return value.isoformat()
    if isinstance(value, str) and _ISO_DATE.fullmatch(value.strip()):
        try:
            return date_cls.fromisoformat(value.strip()).isoformat()
        except ValueError:
            return None
    return None


def _questions(body: str) -> list[str] | None:
    """Items under the `## Questions` heading, or None when there is no such heading.

    Items are numbered ("1." or "1)") or bulleted ("-", "*"), the marker followed by a space. A new
    item starts only at a marker indented no deeper than the first item; deeper lines (sub-bullets
    included) and unmarked lines ("3.5-year paper?" wrapped onto its own line) continue the item
    above. The section ends at the next heading.
    """
    lines = body.splitlines()
    start = next((i for i, line in enumerate(lines)
                  if (m := _HEADING.match(line)) and m.group(1).lower() == "questions"), None)
    if start is None:
        return None
    items: list[str] = []
    base: int | None = None           # indent of the first item
    for line in lines[start + 1:]:
        if line.startswith("#"):
            break
        if not line.strip():
            continue
        m = _ITEM.match(line)
        indent = len(m.group(1).expandtabs(4)) if m else None
        if m and (base is None or indent <= base):
            base = indent if base is None else base
            items.append(m.group(2).strip())
        elif items:
            items[-1] = f"{items[-1]} {line.strip()}".strip()
    return [q for q in items if q]


def _check(text: str) -> tuple[list[str], dict[str, Any], list[str]]:
    """Validate a prep file. Returns (errors, front_matter, questions)."""
    text = text.replace("\r\n", "\n")
    if not text.startswith("---\n"):
        return ["missing front-matter block (--- ... ---) at the top"], {}, []
    try:
        fm, body, _ = frontmatter.split(text)
    except yaml.YAMLError as e:
        return [f"front-matter is not valid YAML: {e}"], {}, []
    if not fm:
        return ["front-matter block is empty, unclosed or not a mapping"], {}, []

    errors: list[str] = []
    if "planned_date" not in fm or fm.get("planned_date") in (None, ""):
        errors.append("planned_date is required (YYYY-MM-DD)")
    elif _planned_date(fm["planned_date"]) is None:
        errors.append(f"planned_date must be a YYYY-MM-DD date, got {fm['planned_date']!r}")

    contact = fm.get("contact")
    if not isinstance(contact, str) or not contact.strip():
        errors.append("contact is required (the person the call is with)")

    for key in _OPTIONAL_TEXT:
        value = fm.get(key)
        if value is not None and not isinstance(value, str):
            errors.append(f"{key} must be text, got {value!r}")
    email = fm.get("attendee_email")
    if isinstance(email, str) and email.strip() and not _EMAIL.fullmatch(email.strip()):
        errors.append(f"attendee_email is not an email address: {email!r}")

    questions = _questions(body)
    if questions is None:
        errors.append("missing a ## Questions section")
        questions = []
    elif not questions:
        errors.append("## Questions has no questions")
    return errors, fm, questions


def validate_prep(text: str) -> list[str]:
    """Check a prep file against the template. Returns error messages; [] when valid.

    Requires front-matter with `planned_date` (YYYY-MM-DD) and `contact`, optional text fields
    `firm`, `attendee_email` (an email address), `seat`, `caller`, and a `## Questions`
    section with at least one numbered or bulleted question.
    """
    return _check(text)[0]


def _text(value: Any) -> str | None:
    """Stripped string, or None for missing or blank values."""
    if value is None:
        return None
    s = str(value).strip()
    return s or None


def parse_prep(path: str, text: str) -> PrepFile:
    """Parse a prep file. Raises ValueError listing every template error when it is invalid."""
    errors, fm, questions = _check(text)
    if errors:
        raise ValueError(f"{path}: " + "; ".join(errors))
    return PrepFile(
        path=path,
        planned_date=_planned_date(fm["planned_date"]),
        contact=fm["contact"].strip(),
        firm=_text(fm.get("firm")),
        attendee_email=_text(fm.get("attendee_email")),
        seat=_text(fm.get("seat")),
        caller=_text(fm.get("caller")),
        questions=questions,
    )


def load_preps(repo: RepoFS) -> list[PrepFile]:
    """Every valid prep/*.md file, sorted by path. Invalid files are skipped."""
    preps = []
    for rel in repo.list("prep/*.md"):
        try:
            preps.append(parse_prep(rel, repo.read(rel)))
        except ValueError:
            continue
    return preps


def _name_key(name: str | None, config: Config) -> str | None:
    """Comparable form of a contact name: entity-mapped, then slugged. None when there is no name."""
    canon = config.canonical(name.strip()) if name and name.strip() else None
    slug = slug_name(canon)
    return None if slug == "unknown" else slug


def _days_apart(a: str, b: str) -> int:
    return abs((date_cls.fromisoformat(a) - date_cls.fromisoformat(b)).days)


def _closest(candidates: list[PrepFile], date: str) -> PrepFile:
    """Nearest planned_date; a tie goes to the prep planned on or before the call, then by path."""
    return min(candidates, key=lambda p: (_days_apart(p.planned_date, date), p.planned_date > date, p.path))


def match_prep(preps: list[PrepFile], *, date: str | None, contact: str | None,
               attendee_emails: list[str], config: Config, window_days: int = 7) -> PrepFile | None:
    """The prep file for a call, or None.

    Attendee email is tried first, then contact name (case-insensitive, after config.canonical).
    A match must fall within `window_days` of the prep's planned_date; among several, the nearest
    planned_date wins. With no call date there is no window: a name never matches, and an email
    matches only when exactly one prep file carries it.
    """
    emails = {e.strip().lower() for e in attendee_emails if e and e.strip()}
    by_email = [p for p in preps if p.attendee_email and p.attendee_email.strip().lower() in emails]

    if date is None:
        return by_email[0] if len(by_email) == 1 else None

    def in_window(p: PrepFile) -> bool:
        return _days_apart(p.planned_date, date) <= window_days

    hits = [p for p in by_email if in_window(p)]
    if hits:
        return _closest(hits, date)

    key = _name_key(contact, config)
    if key is None:
        return None
    hits = [p for p in preps if in_window(p) and _name_key(p.contact, config) == key]
    return _closest(hits, date) if hits else None
