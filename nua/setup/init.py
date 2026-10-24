"""`nua init`: write a team's config/ by asking a few questions, or from flags.

    init [--example NAME] [--company ...] [--market ...] [--exploring ...]
         [--person NAME[,EMAIL]]... [--internal-domain D]... [--timezone ZONE]
         [--calls-channel C] [--ops-channel C] [--test-channel C] [--seat S]... [--yes]

It asks six things, each with the current value as the default: company, market (who you sell to),
what you are exploring, teammates (name and email; keys come from the names and the first email's
pattern suggests the rest, see nua.setup.people), other internal email domains (default: teammates'
work domains) and time zone. Channels and seed seats are
flags only (channels keep their defaults; seats grow from the calls). It starts from what config/
holds now, or from `--example` (examples/<name>/, `blank` for the empty templates), and writes
config/domain.yml, people.yml, seats.yml and the team keys of pipeline.yml through nua.setup.configio,
which keeps the files' comments. A flag answers its question; `--yes` takes the defaults for everything
else and asks nothing. Nothing is written until every answer passes `configio.validate_setup`.

When the time zone changes, the GCP `infra/terraform/terraform.tfvars` gets the same `time_zone` (when that
file exists), so the scheduled jobs run on the team's clock; the AWS tfvars are left to the user.
"""
from __future__ import annotations

import argparse
import re
from pathlib import Path
from typing import Callable

from nua.setup import configio
from nua.setup import people as teammates

TFVARS = "infra/terraform/terraform.tfvars"
AWS_TFVARS = "infra/aws/terraform/terraform.tfvars"
CURRENT = "current"

DOMAIN_QUESTIONS = [
    ("company", "Company name, as the models should call you"),
    ("market", "Who you sell to, in a few words (e.g. independent dental practices)"),
    ("exploring", "The problems or questions you want your calls to answer"),
]


def add_arguments(p: argparse.ArgumentParser) -> None:
    p.add_argument("--example", help="start from examples/<NAME> (blank, insurance, freight-brokerage...) "
                                     "instead of what config/ holds now")
    for key, question in DOMAIN_QUESTIONS:
        p.add_argument(f"--{key}", help=question)
    p.add_argument("--person", action="append", metavar="NAME[,EMAIL]",
                   help="a teammate (repeat for each); replaces the team in config/people.yml. The key comes "
                        "from the name; KEY,NAME,EMAIL[,SLACK_ID] still works")
    p.add_argument("--internal-domain", action="append", metavar="DOMAIN",
                   help="your team's email domain (repeat for each); replaces internal_email_domains")
    p.add_argument("--timezone", help="IANA time zone name, e.g. America/New_York")
    p.add_argument("--calls-channel", help="Slack channel for briefs (default #discovery-calls)")
    p.add_argument("--ops-channel", help="Slack channel for errors and Re-run buttons (default #discovery-ops)")
    p.add_argument("--test-channel", help="Slack channel for staging (default #discovery-test)")
    p.add_argument("--seat", action="append", metavar="SEAT",
                   help="a seed seat (repeat for each); replaces config/seats.yml. Optional: seats grow from the calls")
    p.add_argument("--yes", "-y", action="store_true", help="ask nothing: take the defaults for every answer not given")


def command(args: argparse.Namespace, root: Path) -> int:
    return run_init(root, args, ask=input, say=print)


# ---------- the questions ----------

class _Asker:
    """Asks one question at a time with a default; with `quiet` (--yes) it only returns the default."""

    def __init__(self, ask: Callable[[str], str], say: Callable[[str], None], quiet: bool):
        self.ask, self.say, self.quiet = ask, say, quiet

    def text(self, question: str, default: str = "", check: Callable[[str], str | None] | None = None) -> str:
        if self.quiet:
            return default
        while True:
            shown = f" [{default}]" if default else ""
            answer = self.ask(f"{question}{shown}: ").strip() or default
            problem = check(answer) if check else None
            if not problem:
                return answer
            self.say(f"  {problem}")

    def yes(self, question: str, default: bool = True) -> bool:
        if self.quiet:
            return default
        answer = self.ask(f"{question} [{'Y/n' if default else 'y/N'}]: ").strip().lower()
        return default if not answer else answer.startswith("y")


def parse_person(text: str, taken: list[str] | None = None) -> tuple[str, dict]:
    """`NAME[,EMAIL]` as (key, person), the key made from the name (teammates.person_key) and unique among
    `taken`. The older `KEY,NAME,EMAIL[,SLACK_ID]` form (three or four parts) is still read."""
    parts = [p.strip() for p in text.split(",")]
    if len(parts) in (3, 4):
        parts += [""] * (4 - len(parts))
        key, name, email, slack = parts
        if not key or not name:
            raise ValueError(f"--person {text!r}: expected NAME[,EMAIL], e.g. Maya Ortiz,maya@acme.dev")
        return key.lower(), {"name": name, "email": email or None, "slack_id": slack or None}
    if len(parts) > 4 or not parts[0]:
        raise ValueError(f"--person {text!r}: expected NAME[,EMAIL], e.g. Maya Ortiz,maya@acme.dev")
    name, email = parts[0], parts[1] if len(parts) > 1 else ""
    return teammates.person_key(name, taken or []), {"name": name, "email": email or None}


def parse_people(texts: list[str]) -> dict:
    out: dict = {}
    for text in texts:
        key, person = parse_person(text, list(out))
        out[key] = person
    return out


def _domain_problem(text: str) -> str | None:
    bad = [d for d in _split(text) if configio.validate_setup({"pipeline": {"internal_email_domains": [d]}})]
    return f"not a domain: {', '.join(bad)}" if bad else None


def _split(text: str) -> list[str]:
    return [d.strip().lower() for d in re.split(r"[,\s]+", text or "") if d.strip()]


def _ask_people(asker: _Asker, saved: dict) -> dict:
    """Ask for each teammate's name and email. Keys come from the names; a saved person (same name)
    keeps their key and other fields, since env var names and Circleback URLs depend on it."""
    if saved and asker.yes(f"Keep the team in config/people.yml ({', '.join(saved)})?", default=True):
        return saved
    by_name = {(p.get("name") or "").strip().lower(): (k, p) for k, p in saved.items() if p.get("name")}
    asker.say("Teammates: everyone who runs calls. Leave the name blank to finish.")
    out: dict = {}
    pattern = None
    while True:
        name = asker.ask("  name: ").strip()
        if not name:
            if out:
                return out
            asker.say("  add at least one teammate")
            continue
        key, old = by_name.get(name.lower(), (None, {}))
        if key is None or key in out:
            key, old = teammates.person_key(name, [*out, *saved]), {}
        guess = old.get("email") or teammates.email_for(name, pattern) or ""
        email = asker.text("  email their calls are recorded under", guess)
        pattern = pattern or teammates.infer_email_pattern(name, email)
        out[key] = {**old, "name": name, "email": email or None}


# ---------- the run ----------

def run_init(root: Path, args: argparse.Namespace, *, ask: Callable[[str], str] = input,
             say: Callable[[str], None] = print) -> int:
    """Collect the answers, validate them, then write the files. Returns the exit code."""
    asker = _Asker(ask, say, quiet=args.yes)
    examples = configio.list_examples(root)
    setup = configio.load_setup(root)
    old_timezone = setup["pipeline"]["timezone"]

    start = args.example
    if start and start != CURRENT:
        if start not in examples:
            say(f"error: no example {start!r}: choose one of {', '.join(examples)}")
            return 1
        setup.update(configio.example_setup(root, start))

    d = setup["domain"]
    for key, question in DOMAIN_QUESTIONS:
        given = getattr(args, key, None)
        d[key] = given.strip() if given is not None else asker.text(question, d.get(key) or "")

    if args.person:
        try:
            setup["people"] = parse_people(args.person)
        except ValueError as e:
            say(f"error: {e}")
            return 1
    else:
        setup["people"] = _ask_people(asker, setup["people"])

    p = setup["pipeline"]
    if args.internal_domain:
        p["internal_email_domains"] = [x for d in args.internal_domain for x in _split(d)]
    else:
        default = p["internal_email_domains"] or teammates.suggested_domains(
            person.get("email") for person in setup["people"].values())
        p["internal_email_domains"] = _split(asker.text("Also internal: email domains whose people count as your team "
                                                        "(teammates' own emails always count; optional, comma separated)",
                                                        ", ".join(default), check=_domain_problem))
    p["timezone"] = args.timezone.strip() if args.timezone else asker.text(
        "Time zone (IANA name, e.g. America/New_York)", p["timezone"], check=configio.timezone_problem)
    for key in configio.CHANNEL_KEYS:
        given = getattr(args, f"{key}_channel")
        if given:
            p["channels"][key] = given.strip()
    if args.seat:
        setup["seats"] = [s.strip() for s in args.seat if s.strip()]

    problems = configio.validate_setup(setup)
    if problems:
        say("Nothing written. Fix these and run `nua init` again:")
        for line in problems:
            say(f"  {line}")
        return 1

    changed = configio.apply_example(root, start) if start and start != CURRENT else []
    changed += [path for path in configio.save_setup(root, setup) if path not in changed]
    for path in changed:
        say(f"wrote {_rel(root, path)}")
    if not changed:
        say("config/ already matches; nothing written.")
    if p["timezone"] != old_timezone:
        _timezone_note(root, p["timezone"], say)
    say("Review the changes (git diff config/), then commit them.")
    return 0


def _rel(root: Path, path: Path) -> str:
    try:
        return path.relative_to(root).as_posix()
    except ValueError:
        return str(path)


def _timezone_note(root: Path, zone: str, say: Callable[[str], None]) -> None:
    """Keep the deployed schedules on the same clock: write `time_zone` into the GCP tfvars when present,
    and tell the user about the rest."""
    tfvars = root / TFVARS
    if tfvars.is_file() and not tfvars.is_symlink():
        text = tfvars.read_text(encoding="utf-8")
        line = f'time_zone = "{zone}"'
        new, n = re.subn(r'(?m)^\s*time_zone\s*=.*$', line, text)
        if not n:
            new = text + ("" if text.endswith("\n") or not text else "\n") + line + "\n"
        if new != text:
            tfvars.write_text(new, encoding="utf-8")
            say(f"wrote {TFVARS} (time_zone = \"{zone}\"); run terraform apply so the schedules follow it.")
    else:
        say(f"Time zone is now {zone}: set time_zone = \"{zone}\" in {TFVARS} when you deploy.")
    say(f"On AWS, set time_zone = \"{zone}\" in {AWS_TFVARS} too.")
