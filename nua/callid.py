"""Call ids: `YYYY-MM-DD_firstname-lastname`, `undated_firstname-lastname`, and stub ids.

See SPEC "Call id". A call id names the digest and never changes once assigned.
"""
from __future__ import annotations

import re
import unicodedata
from datetime import date as date_cls
from pathlib import PurePosixPath

_ISO_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")
_APOSTROPHES = "'’‘`"
_NON_ALNUM = re.compile(r"[^a-z0-9]+")
# Letters NFKD leaves without an ascii base, which the ascii fold would otherwise drop.
_TRANSLITERATE = str.maketrans({
    "Ø": "O", "ø": "o", "Ł": "L", "ł": "l", "ß": "ss", "ẞ": "SS", "Đ": "D", "đ": "d", "Ð": "D", "ð": "d",
    "Æ": "AE", "æ": "ae", "Œ": "OE", "œ": "oe", "Þ": "TH", "þ": "th", "ı": "i",
})


def slug_name(contact: str | None) -> str:
    """Lowercase ascii slug of a person's name: "Jane Doe" -> "jane-doe".

    Accents are folded ("José" -> "jose"), letters with no ascii base are transliterated
    ("Strauß" -> "strauss", "Łukasz" -> "lukasz"), apostrophes dropped ("O'Brien" -> "obrien"),
    and every other run of non-alphanumerics becomes one hyphen. None, or a name with
    nothing left after folding, gives "unknown".
    """
    if not contact:
        return "unknown"
    folded = unicodedata.normalize("NFKD", contact.translate(_TRANSLITERATE))
    folded = folded.encode("ascii", "ignore").decode("ascii")
    folded = folded.lower()
    for ch in _APOSTROPHES:
        folded = folded.replace(ch, "")
    slug = _NON_ALNUM.sub("-", folded).strip("-")
    return slug or "unknown"


def make_call_id(date: str | None, contact: str | None, existing: set[str]) -> str:
    """Call id for a new digest, unique against `existing` call ids.

    `date` is the call date in the configured time zone (YYYY-MM-DD) or None when the date is
    unknown, which gives "undated_<slug>". A second call with the same person on the same day gets
    "-2", then "-3".
    Raises ValueError when `date` is not a valid YYYY-MM-DD date.
    """
    if date is None:
        prefix = "undated"
    else:
        if not _ISO_DATE.fullmatch(date):
            raise ValueError(f"call date must be YYYY-MM-DD, got {date!r}")
        date_cls.fromisoformat(date)   # rejects impossible dates like 2026-13-40
        prefix = date
    base = f"{prefix}_{slug_name(contact)}"
    if base not in existing:
        return base
    n = 2
    while f"{base}-{n}" in existing:
        n += 1
    return f"{base}-{n}"


def stub_call_id(transcript_path: str) -> str:
    """Call id for a stub digest: the transcript's filename stem ("transcripts/granola_abc.md" -> "granola_abc")."""
    return PurePosixPath(transcript_path).stem
