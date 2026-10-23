"""`nua init`: write a team's config/ by asking a few questions, or from flags.

    init [--example NAME] [--company ...] [--team ...] [--about ...] [--market ...] [--exploring ...]
         [--interviewees ...] [--person KEY,NAME,EMAIL[,SLACK_ID]]... [--internal-domain D]...
         [--timezone ZONE] [--calls-channel C] [--ops-channel C] [--test-channel C] [--seat S]... [--yes]

It starts from what config/ holds now, or from `--example` (examples/<name>/, `blank` for the empty
templates), asks for each answer with the current value as the default, and writes config/domain.yml,
people.yml, seats.yml and the team keys of pipeline.yml through nua.setup.configio, which keeps the
files' comments. A flag answers its question; `--yes` takes the defaults for everything else and asks
nothing. Nothing is written until every answer passes `configio.validate_setup`.

When the time zone changes, the GCP `infra/terraform/terraform.tfvars` gets the same `time_zone` (when that
file exists), so the scheduled jobs run on the team's clock; the AWS tfvars are left to the user.
"""
from __future__ import annotations

import argparse
import re
from pathlib import Path
from typing import Callable

from nua.setup import configio

TFVARS = "infra/terraform/terraform.tfvars"
AWS_TFVARS = "infra/aws/terraform/terraform.tfvars"
CURRENT = "current"

DOMAIN_QUESTIONS = [
    ("company", "Company name, as the models should call you"),
    ("team", "Who reads the findings and decides (e.g. founders)"),
    ("about", "One line: who you are and what you are deciding"),
    ("market", "The market in a few words"),
    ("exploring", "The problem areas you are exploring"),
    ("interviewees", "Who you interview, as a plain list"),
]


def add_arguments(p: argparse.ArgumentParser) -> None:
    p.add_argument("--example", help="start from examples/<NAME> (blank, insurance, freight-brokerage...) "
                                     "instead of what config/ holds now")
    for key, question in DOMAIN_QUESTIONS:
        p.add_argument(f"--{key}", help=question)
    p.add_argument("--person", action="append", metavar="KEY,NAME,EMAIL[,SLACK_ID]",
                   help="a teammate (repeat for each); replaces the team in config/people.yml")
    p.add_argument("--internal-domain", action="append", metavar="DOMAIN",
                   help="your team's email domain (repeat for each); replaces internal_email_domains")
    p.add_argument("--timezone", help="IANA time zone name, e.g. America/New_York")
    p.add_argument("--calls-channel", help="Slack channel for briefs (default #discovery-calls)")
    p.add_argument("--ops-channel", help="Slack channel for errors and Re-run buttons (default #discovery-ops)")
    p.add_argument("--test-channel", help="Slack channel for staging (default #discovery-test)")
    p.add_argument("--seat", action="append", metavar="SEAT", help="a seat (repeat for each); replaces the seat list")
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

    def lines(self, question: str) -> list[str]:
        """Answers one per line until a blank line."""
        self.say(f"{question} (one per line, blank line to finish)")
        out = []
        while True:
            line = self.ask("  > ").strip()
            if not line:
                return out
            out.append(line)


def parse_person(text: str) -> tuple[str, dict]:
    """`KEY,NAME,EMAIL[,SLACK_ID]` as (key, person). EMAIL and SLACK_ID may be empty."""
    parts = [p.strip() for p in text.split(",")]
    if len(parts) < 2 or len(parts) > 4 or not parts[0] or not parts[1]:
        raise ValueError(f"--person {text!r}: expected KEY,NAME,EMAIL[,SLACK_ID], e.g. maya,Maya Ortiz,maya@acme.dev")
    parts += [""] * (4 - len(parts))
    key, name, email, slack = parts
    return key.lower(), {"name": name, "email": email or None, "slack_id": slack or None}


def _key_problem(key: str) -> str | None:
    return None if re.fullmatch(r"[a-z][a-z0-9_]*", key) else "a short lowercase id such as alex (letters, digits, _)"


def _channel_problem(name: str) -> str | None:
    return None if re.fullmatch(r"#[a-z0-9][a-z0-9._-]{0,79}", name) else "a Slack channel such as #discovery-calls"


def _domain_problem(text: str) -> str | None:
    bad = [d for d in _split(text) if configio.validate_setup({"pipeline": {"internal_email_domains": [d]}})]
    return f"not a domain: {', '.join(bad)}" if bad else None


def _split(text: str) -> list[str]:
    return [d.strip().lower() for d in re.split(r"[,\s]+", text or "") if d.strip()]


def _ask_people(asker: _Asker, people: dict) -> dict:
    if people and asker.yes(f"Keep the team in config/people.yml ({', '.join(people)})?", default=True):
        return people
    asker.say("Teammates: everyone who runs calls. Leave the key blank to finish.")
    out: dict = {}
    while True:
        key = asker.ask("  key (e.g. alex): ").strip().lower()
        if not key:
            if out:
                return out
            asker.say("  add at least one teammate")
            continue
        if _key_problem(key):
            asker.say(f"  {_key_problem(key)}")
            continue
        old = people.get(key, {})
        name = asker.text("  name", old.get("name") or "")
        email = asker.text("  email their calls are recorded under", old.get("email") or "")
        slack = asker.text("  Slack member id (optional, e.g. U0123ABCD)", old.get("slack_id") or "")
        out[key] = {**old, "name": name, "email": email or None, "slack_id": slack or None}


# ---------- the run ----------

def run_init(root: Path, args: argparse.Namespace, *, ask: Callable[[str], str] = input,
             say: Callable[[str], None] = print) -> int:
    """Collect the answers, validate them, then write the files. Returns the exit code."""
    asker = _Asker(ask, say, quiet=args.yes)
    examples = configio.list_examples(root)
    setup = configio.load_setup(root)
    old_timezone = setup["pipeline"]["timezone"]

    start = args.example
    if start is None and not args.yes:
        start = asker.text(f"Start from ({', '.join([CURRENT, *examples])})", CURRENT,
                           check=lambda a: None if a in (CURRENT, *examples) else f"choose one of {CURRENT}, {', '.join(examples)}")
    if start and start != CURRENT:
        if start not in examples:
            say(f"error: no example {start!r}: choose one of {', '.join(examples)}")
            return 1
        setup.update(configio.example_setup(root, start))

    d = setup["domain"]
    for key, question in DOMAIN_QUESTIONS:
        given = getattr(args, key, None)
        d[key] = given.strip() if given is not None else asker.text(question, d.get(key) or ("founders" if key == "team" else ""))

    if args.person:
        try:
            setup["people"] = dict(parse_person(p) for p in args.person)
        except ValueError as e:
            say(f"error: {e}")
            return 1
    else:
        setup["people"] = _ask_people(asker, setup["people"])

    p = setup["pipeline"]
    if args.internal_domain:
        p["internal_email_domains"] = [x for d in args.internal_domain for x in _split(d)]
    else:
        p["internal_email_domains"] = _split(asker.text("Internal email domains, comma separated (a call with only "
                                                        "these attendees is internal)", ", ".join(p["internal_email_domains"]),
                                                        check=_domain_problem))
    p["timezone"] = args.timezone.strip() if args.timezone else asker.text(
        "Time zone (IANA name, e.g. America/New_York)", p["timezone"], check=configio.timezone_problem)
    for key, label in (("calls", "briefs"), ("ops", "errors and Re-run buttons"), ("test", "staging")):
        given = getattr(args, f"{key}_channel")
        p["channels"][key] = given.strip() if given else asker.text(f"Slack channel for {label}", p["channels"][key],
                                                                     check=_channel_problem)

    if args.seat:
        setup["seats"] = [s.strip() for s in args.seat if s.strip()]
    elif not asker.quiet:
        current = setup["seats"]
        summary = f"{len(current)} seats: {', '.join(current[:4])}{', ...' if len(current) > 4 else ''}" if current else "none yet"
        if not asker.yes(f"Keep the seat list ({summary})?", default=bool(current)):
            setup["seats"] = asker.lines("Seats: the role someone speaks from on a call, e.g. head of operations")

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
    if not setup["seats"]:
        say("Note: config/seats.yml is empty, so seats stay as the models name them. Add seats to count evidence by.")
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
