"""Teammate helpers shared by `nua init` and the setup page (setup-ui/src/lib/people.ts mirrors them).

- `person_key`: a person's id in config/people.yml from their name. It names their env vars
  (CIRCLEBACK_API_KEY_<KEY>) and Circleback URL, so a saved key is never changed.
- `infer_email_pattern` / `email_for`: guess a teammate's email from the first one's pattern.
- `suggested_domains`: internal email domains from teammates' emails, without personal mail domains.
"""
from __future__ import annotations

import re
import unicodedata
from typing import Iterable

# Personal mail domains: never suggested as internal (a call with anyone on gmail.com is not internal).
PERSONAL_DOMAINS = ("gmail.com", "googlemail.com", "outlook.com", "hotmail.com", "live.com", "yahoo.com",
                    "icloud.com", "me.com", "proton.me", "protonmail.com", "aol.com")

# Email local-part formats, in the order they are tried. {f} first name, {l} last name, {i} first initial.
EMAIL_FORMATS = ("{f}.{l}", "{f}_{l}", "{f}{l}", "{i}.{l}", "{i}{l}", "{l}.{f}", "{l}{f}", "{f}")


def name_tokens(name: str | None) -> list[str]:
    """Lowercase ASCII words of a name: "José María-López" -> ["jose", "marialopez"]."""
    text = unicodedata.normalize("NFKD", name or "").encode("ascii", "ignore").decode("ascii").lower()
    return [t for t in (re.sub(r"[^a-z0-9]", "", w) for w in text.split()) if t]


def _render(fmt: str, tokens: list[str]) -> str | None:
    if not tokens or ("{l}" in fmt and len(tokens) < 2):
        return None
    first, last = tokens[0], tokens[-1]
    return fmt.format(f=first, l=last, i=first[0])


def infer_email_pattern(name: str | None, email: str | None) -> tuple[str, str] | None:
    """(format, domain) when `email`'s local part is one of EMAIL_FORMATS for `name`, else None."""
    if not email or email.count("@") != 1:
        return None
    local, domain = email.strip().lower().split("@")
    tokens = name_tokens(name)
    for fmt in EMAIL_FORMATS:
        if local and _render(fmt, tokens) == local:
            return fmt, domain
    return None


def email_for(name: str | None, pattern: tuple[str, str] | None) -> str | None:
    """The email `pattern` gives `name`, or None when the name lacks a needed part."""
    if not pattern:
        return None
    local = _render(pattern[0], name_tokens(name))
    return f"{local}@{pattern[1]}" if local else None


def person_key(name: str | None, taken: Iterable[str]) -> str:
    """A new key from a name: first name, then first name plus last initial, then a digit."""
    taken = set(taken)
    tokens = name_tokens(name)
    first = re.sub(r"[^a-z0-9_]", "", tokens[0]) if tokens else ""
    if not first or not first[0].isalpha():
        first = f"p{first}"
    candidates = [first]
    if len(tokens) > 1:
        candidates.append(first + tokens[-1][0])
    for c in candidates:
        if c not in taken:
            return c
    n = 2
    while f"{first}{n}" in taken:
        n += 1
    return f"{first}{n}"


def suggested_domains(emails: Iterable[str | None]) -> list[str]:
    """Teammates' email domains in first-seen order, personal mail domains left out."""
    out: list[str] = []
    for email in emails:
        if not email or "@" not in email:
            continue
        domain = email.strip().lower().rsplit("@", 1)[1]
        if domain and domain not in PERSONAL_DOMAINS and domain not in out:
            out.append(domain)
    return out
