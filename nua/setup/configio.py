"""Read, check and write a team's setup in config/: what `nua init` asks and a setup UI edits.

The setup is one plain dict (see `load_setup`) spread over four hand-maintained files:

- `config/domain.yml`: every key (company, team, about, market, exploring, interviewees, scope, roles,
  institution_examples, bot_name).
- `config/people.yml`: the team, by person key.
- `config/seats.yml`: the seat list.
- `config/pipeline.yml`: only `timezone`, `internal_email_domains`, `channels` and `ingest_cutoff`. Models,
  thresholds and everything else are left as written.

`save_setup` edits the files in place with ruamel.yaml, so comments, key order and quoting survive and a
value that did not change is not rewritten. A list or mapping that changes is rebuilt, and comments between
its items go with it (the comment after its last item is kept). Starting points live in `examples/<name>/`
(`domain.yml`, `seats.yml`, `entities.yml`); `examples/blank/` holds the commented templates config/ ships with.
"""
from __future__ import annotations

import io
import re
import shutil
from datetime import date, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from ruamel.yaml import YAML
from ruamel.yaml.comments import CommentedMap, CommentedSeq
from ruamel.yaml.scalarstring import DoubleQuotedScalarString, FoldedScalarString, PlainScalarString

from nua.frontmatter import load_yaml

CONFIG = "config"
EXAMPLES = "examples"
BLANK = "blank"
EXAMPLE_FILES = ("domain.yml", "seats.yml", "entities.yml")

DOMAIN_TEXT = ("company", "team", "about", "market", "exploring", "interviewees", "institution_examples",
               "bot_name")
REQUIRED_DOMAIN = ("company", "team", "about", "market", "exploring", "interviewees")
SCOPE_KEYS = ("line", "party", "segment")
KEEP_ROLES = ("expert", "competitor")       # nua/prompts/extract.py names both
PERSON_FIELDS = ("name", "email", "slack_id", "github_login", "circleback_key_name")
CHANNEL_KEYS = ("calls", "ops", "test")
DEFAULT_CHANNELS = {"calls": "#discovery-calls", "ops": "#discovery-ops", "test": "#discovery-test"}
DEFAULT_TIMEZONE = "America/Los_Angeles"

_KEY = re.compile(r"^[a-z][a-z0-9_]*$")                 # person keys end up in env var names
_ROLE = re.compile(r"^[a-z][a-z0-9_-]*$")
_EXAMPLE = re.compile(r"^[a-z0-9][a-z0-9_-]*$")
_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_DOMAIN = re.compile(r"^(?=.{1,253}$)([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$")
_SLACK_ID = re.compile(r"^[UW][A-Z0-9]{2,}$")
_CHANNEL = re.compile(r"^#[a-z0-9][a-z0-9._-]{0,79}$")   # Slack: lowercase, no spaces, at most 80
_CHECKOUT = Path(__file__).resolve().parents[2]


class SetupError(ValueError):
    """`save_setup` refused data that `validate_setup` finds problems with; `problems` lists them."""

    def __init__(self, problems: list[str]):
        super().__init__("\n".join(problems))
        self.problems = problems


# ---------- examples ----------

def examples_root(root: str | Path) -> Path:
    """`<root>/examples` when it exists, else the one in this code checkout."""
    here = Path(root) / EXAMPLES
    return here if here.is_dir() else _CHECKOUT / EXAMPLES


def list_examples(root: str | Path) -> list[str]:
    """Names of the starting points in examples/ (folders with a domain.yml), `blank` first."""
    folder = examples_root(root)
    names = sorted(p.name for p in folder.iterdir() if (p / "domain.yml").is_file()) if folder.is_dir() else []
    return sorted(names, key=lambda n: n != BLANK)


def apply_example(root: str | Path, name: str) -> list[Path]:
    """Copy examples/<name>/{domain,seats,entities}.yml over config/. Returns the files whose content changed.
    people.yml and pipeline.yml are never touched. Raises ValueError for an unknown example."""
    if not _EXAMPLE.match(name or "") or name not in list_examples(root):
        raise ValueError(f"no example {name!r}: choose one of {', '.join(list_examples(root)) or 'none'}")
    src = examples_root(root) / name
    changed = []
    for file in EXAMPLE_FILES:
        if not (src / file).is_file():
            continue
        dest = Path(root) / CONFIG / file
        _refuse_link(dest)
        text = (src / file).read_text(encoding="utf-8")
        if not dest.is_file() or dest.read_text(encoding="utf-8") != text:
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(src / file, dest)
            changed.append(dest)
    return changed


def example_setup(root: str | Path, name: str) -> dict:
    """The domain and seats an example would give, in `load_setup` shape, without writing anything."""
    if not _EXAMPLE.match(name or "") or name not in list_examples(root):
        raise ValueError(f"no example {name!r}: choose one of {', '.join(list_examples(root)) or 'none'}")
    folder = examples_root(root) / name
    return {"domain": _domain(_read_plain(folder / "domain.yml", {})),
            "seats": _seats(_read_plain(folder / "seats.yml", []))}


# ---------- load ----------

def load_setup(root: str | Path) -> dict:
    """The setup as written in config/, as plain values:

    {"domain": {company, team, about, market, exploring, interviewees, institution_examples, bot_name: str,
                scope: {line, party, segment: str}, roles: {id: description}},
     "people": {key: {name, email, slack_id, github_login, circleback_key_name}},   # None when unset
     "seats": [str],
     "pipeline": {"timezone": str, "internal_email_domains": [str],
                  "channels": {"calls", "ops", "test": str}, "ingest_cutoff": str | None},
     "examples": [names]}

    Missing files and keys read as empty ("" or None), except the pipeline defaults (time zone, channels).
    """
    cfg = Path(root) / CONFIG
    pipeline = _read_plain(cfg / "pipeline.yml", {})
    pipeline = pipeline if isinstance(pipeline, dict) else {}
    channels = pipeline.get("channels") if isinstance(pipeline.get("channels"), dict) else {}
    cutoff = pipeline.get("ingest_cutoff")
    if isinstance(cutoff, (date, datetime)):
        cutoff = cutoff.isoformat()
    people_raw = _read_plain(cfg / "people.yml", {})
    people = {}
    for key, person in (people_raw.items() if isinstance(people_raw, dict) else []):
        person = person if isinstance(person, dict) else {}
        people[str(key)] = {f: _opt_text(person.get(f)) for f in PERSON_FIELDS}
    return {
        "domain": _domain(_read_plain(cfg / "domain.yml", {})),
        "people": people,
        "seats": _seats(_read_plain(cfg / "seats.yml", [])),
        "pipeline": {
            "timezone": _text(pipeline.get("timezone")) or DEFAULT_TIMEZONE,
            "internal_email_domains": [_text(d) for d in pipeline.get("internal_email_domains") or [] if _text(d)],
            "channels": {k: _text(channels.get(k)) or DEFAULT_CHANNELS[k] for k in CHANNEL_KEYS},
            "ingest_cutoff": None if cutoff in (None, "") else str(cutoff),
        },
        "examples": list_examples(root),
    }


def _read_plain(path: Path, default: Any) -> Any:
    """A YAML file as plain values (read like nua/config.py does); `default` when missing, empty or a link."""
    if path.is_symlink() or not path.is_file():
        return default
    data = load_yaml(path.read_text(encoding="utf-8"))
    return default if data is None else data


def _text(value: Any) -> str:
    return " ".join(str(value).split()) if value is not None else ""


def _opt_text(value: Any) -> str | None:
    return _text(value) or None


def _domain(raw: Any) -> dict:
    raw = raw if isinstance(raw, dict) else {}
    scope = raw.get("scope") if isinstance(raw.get("scope"), dict) else {}
    roles = raw.get("roles") if isinstance(raw.get("roles"), dict) else {}
    out: dict[str, Any] = {k: _text(raw.get(k)) for k in DOMAIN_TEXT}
    out["scope"] = {k: _text(scope.get(k)) for k in SCOPE_KEYS}
    out["roles"] = {str(k).strip(): _text(v) for k, v in roles.items() if str(k).strip()}
    return out


def _seats(raw: Any) -> list[str]:
    return [_text(s) for s in raw if _text(s)] if isinstance(raw, list) else []


# ---------- validate ----------

def validate_setup(data: dict) -> list[str]:
    """Problems with a setup in `load_setup` shape, one human-readable line each; [] when it is fine.
    Only the sections present are checked, so a partial dict (say, just "people") can be validated."""
    if not isinstance(data, dict):
        return ["setup: expected a mapping"]
    problems: list[str] = []
    if "domain" in data:
        problems += _check_domain(data["domain"])
    if "people" in data:
        problems += _check_people(data["people"])
    if "seats" in data:
        problems += _check_seats(data["seats"])
    if "pipeline" in data:
        problems += _check_pipeline(data["pipeline"])
    return problems


def _check_domain(d: Any) -> list[str]:
    where = "config/domain.yml"
    if not isinstance(d, dict):
        return [f"{where}: expected a mapping"]
    out = []
    for key in DOMAIN_TEXT:
        value = d.get(key, "")
        if value is not None and not isinstance(value, str):
            out.append(f"{where}: {key} must be text")
        elif key in REQUIRED_DOMAIN and not _text(value):
            out.append(f"{where}: {key} is empty")
    scope = d.get("scope", {})
    if not isinstance(scope, dict):
        out.append(f"{where}: scope must map line, party and segment to descriptions")
    else:
        out += [f"{where}: scope has unknown key {k!r} (only line, party, segment)" for k in scope if k not in SCOPE_KEYS]
        out += [f"{where}: scope.{k} must be text" for k, v in scope.items()
                if k in SCOPE_KEYS and v is not None and not isinstance(v, str)]
    roles = d.get("roles", {})
    if not isinstance(roles, dict):
        out.append(f"{where}: roles must map role ids to descriptions")
    elif roles:
        out += [f"{where}: role id {k!r} must be a short lowercase word" for k in roles if not _ROLE.match(str(k))]
        out += [f"{where}: role {k!r} needs a description" for k, v in roles.items() if not _text(v)]
        missing = [r for r in KEEP_ROLES if r not in roles]
        if missing:
            out.append(f"{where}: roles must keep {' and '.join(missing)}")
    return out


def _check_people(people: Any) -> list[str]:
    where = "config/people.yml"
    if not isinstance(people, dict):
        return [f"{where}: expected a mapping of person key to details"]
    if not people:
        return [f"{where}: add at least one person who runs calls"]
    out, emails = [], {}
    for key, p in people.items():
        if not _KEY.match(str(key)):
            out.append(f"{where}: key {key!r} must be a short lowercase id (letters, digits, _), e.g. alex")
        if not isinstance(p, dict):
            out.append(f"{where}: {key}: expected name, email and so on")
            continue
        unknown = [f for f in p if f not in PERSON_FIELDS]
        if unknown:
            out.append(f"{where}: {key}: unknown field(s) {', '.join(map(str, unknown))}")
        if not _text(p.get("name")):
            out.append(f"{where}: {key}: name is empty")
        email = _text(p.get("email")).lower()
        if email and not _EMAIL.match(email):
            out.append(f"{where}: {key}: {email!r} is not an email address")
        elif email in emails:
            out.append(f"{where}: {key} and {emails[email]} share the email {email}")
        elif email:
            emails[email] = key
        slack = _text(p.get("slack_id"))
        if slack and not _SLACK_ID.match(slack):
            out.append(f"{where}: {key}: slack_id {slack!r} should look like U0123ABCD (profile > Copy member ID)")
    return out


def _check_seats(seats: Any) -> list[str]:
    where = "config/seats.yml"
    if not isinstance(seats, list) or not all(isinstance(s, str) for s in seats):
        return [f"{where}: expected a list of seat names"]
    out = [f"{where}: seat {i} is empty" for i, s in enumerate(seats, 1) if not _text(s)]
    seen: set[str] = set()
    for s in seats:
        k = _text(s).lower()
        if k and k in seen:
            out.append(f"{where}: seat {s!r} is listed twice")
        seen.add(k)
    return out


def _check_pipeline(p: Any) -> list[str]:
    where = "config/pipeline.yml"
    if not isinstance(p, dict):
        return [f"{where}: expected a mapping"]
    out = []
    if "timezone" in p:
        problem = timezone_problem(p["timezone"])
        if problem:
            out.append(f"{where}: timezone {problem}")
    if "internal_email_domains" in p:
        domains = p["internal_email_domains"]
        if not isinstance(domains, list):
            out.append(f"{where}: internal_email_domains must be a list")
        else:
            out += [f"{where}: internal_email_domains: {d!r} is not a domain like acme.dev" for d in domains
                    if not isinstance(d, str) or not _DOMAIN.match(d.strip().lower())]
    if "channels" in p:
        ch = p["channels"]
        if not isinstance(ch, dict):
            out.append(f"{where}: channels must map calls, ops and test to channel names")
        else:
            for k in CHANNEL_KEYS:
                name = ch.get(k)
                if not isinstance(name, str) or not _CHANNEL.match(name.strip()):
                    out.append(f"{where}: channels.{k} {name!r} must be a Slack channel name like #discovery-calls")
            out += [f"{where}: channels has unknown key {k!r} (only calls, ops, test)" for k in ch if k not in CHANNEL_KEYS]
            if isinstance(ch.get("calls"), str) and ch.get("calls") == ch.get("ops"):
                out.append(f"{where}: channels.calls and channels.ops must differ (errors would bury the briefs)")
    if p.get("ingest_cutoff") not in (None, ""):
        cutoff = p["ingest_cutoff"]
        ok = isinstance(cutoff, (date, datetime))
        if isinstance(cutoff, str):
            try:
                ok = bool(datetime.fromisoformat(cutoff.strip().replace("Z", "+00:00")))
            except ValueError:
                ok = False
        if not ok:
            out.append(f"{where}: ingest_cutoff {cutoff!r} must be an ISO date or time, e.g. 2026-11-02T09:00:00-08:00")
    return out


def timezone_problem(name: Any) -> str | None:
    """Why `name` is not an IANA time zone name (e.g. America/New_York), or None when it is one."""
    if not isinstance(name, str) or not name.strip():
        return "is empty: give an IANA name such as America/New_York"
    try:
        ZoneInfo(name.strip())
    except (ZoneInfoNotFoundError, ValueError):
        return f"{name!r} is not an IANA time zone name such as America/New_York or Europe/Berlin"
    return None


# ---------- save ----------

def save_setup(root: str | Path, data: dict) -> list[Path]:
    """Write the sections of `data` (load_setup shape; "examples" is ignored) into config/, keeping each file's
    comments. Returns the files whose content changed; unchanged files are not written. Raises SetupError,
    writing nothing, when validate_setup finds problems."""
    problems = validate_setup(data)
    if problems:
        raise SetupError(problems)
    cfg = Path(root) / CONFIG
    plans: list[tuple[Path, str]] = []
    if "domain" in data:
        plans.append(_plan(root, "domain.yml", lambda doc: _put_domain(doc, data["domain"]), CommentedMap))
    if "people" in data:
        plans.append(_plan(root, "people.yml", lambda doc: _put_people(doc, data["people"]), CommentedMap))
    if "seats" in data:
        plans.append(_plan(root, "seats.yml", lambda doc: _put_seq(doc, [_text(s) for s in data["seats"]]),
                           CommentedSeq))
    if "pipeline" in data:
        plans.append(_plan(root, "pipeline.yml", lambda doc: _put_pipeline(doc, data["pipeline"]), CommentedMap))
    changed = []
    for path, text in plans:
        if not path.is_file() or path.read_text(encoding="utf-8") != text:
            cfg.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
            changed.append(path)
    return changed


def _yaml(seq_top: bool) -> YAML:
    """A round-trip YAML in the style of the shipped files: two-space maps, `  - x` lists under a key and
    `- x` lists at the top level, nulls as `null`, no line wrapping."""
    y = YAML()
    y.preserve_quotes = True
    y.width = 4096
    y.indent(mapping=2, sequence=2 if seq_top else 4, offset=0 if seq_top else 2)
    y.representer.add_representer(type(None), lambda rep, _: rep.represent_scalar("tag:yaml.org,2002:null", "null"))
    return y


def _plan(root: str | Path, name: str, put, kind: type) -> tuple[Path, str]:
    """(path, new text) for config/<name>: the file (or the blank template, or nothing) loaded, edited by
    `put`, dumped."""
    path = Path(root) / CONFIG / name
    _refuse_link(path)
    y = _yaml(kind is CommentedSeq)
    source = path if path.is_file() else examples_root(root) / BLANK / name
    doc = y.load(source.read_text(encoding="utf-8")) if source.is_file() else None
    if not isinstance(doc, kind):
        doc = kind()
    put(doc)
    if kind is CommentedSeq and not doc:
        # ruamel cannot emit an empty top-level list under comments: keep the file's leading comments by hand
        text = source.read_text(encoding="utf-8") if source.is_file() else ""
        head = []
        for line in text.splitlines():
            if line.strip() and not line.lstrip().startswith("#"):
                break
            head.append(line)
        return path, "\n".join([*head, "[]"]) + "\n"
    buf = io.StringIO()
    y.dump(doc, buf)
    return path, buf.getvalue()


def _refuse_link(path: Path) -> None:
    if path.is_symlink():
        raise SetupError([f"{path.parent.name}/{path.name} is a symbolic link; replace it with a file"])


def _scalar(value: str, old: Any) -> Any:
    """A new text value as ruamel should write it: "" quoted, a folded (>-) value kept folded, else plain
    (quoted only when YAML needs it). ruamel would otherwise copy the old value's quoting."""
    value = _text(value)
    if not value:
        return DoubleQuotedScalarString("")
    if isinstance(old, FoldedScalarString):
        return FoldedScalarString(value)
    return PlainScalarString(value)


def _put_text(doc: CommentedMap, key: str, value: Any) -> None:
    """Set doc[key] to text, leaving the old value (and its formatting) when it reads the same."""
    old = doc.get(key)
    if value is None:
        value = ""
    if old is not None and _text(old) == _text(value) and not (old == "" and value != ""):
        return
    if old is None and key in doc and not _text(value):
        return
    doc[key] = _scalar(str(value), old)


def _put_map(doc: CommentedMap, key: str, new: dict) -> None:
    """Set doc[key] to the mapping `new`, rebuilt only when it changed, keeping the comment after its last item."""
    old = doc.get(key)
    if isinstance(old, dict) and {str(k): _text(v) for k, v in old.items()} == {str(k): _text(v) for k, v in new.items()}:
        return
    if not isinstance(old, CommentedMap):
        old = CommentedMap()
        doc[key] = old
    trailing = old.ca.items.get(list(old)[-1]) if len(old) else None
    for k in list(old):
        del old[k]
    old.ca.items.clear()
    for k, v in new.items():
        old[str(k)] = _scalar(str(v or ""), None)
    if new:
        old.fa.set_block_style()
        if trailing is not None:
            old.ca.items[list(old)[-1]] = trailing
    else:
        old.fa.set_flow_style()


def _put_seq(seq: CommentedSeq, new: list[str]) -> None:
    """Make the sequence `new`: untouched when equal, appended to when `new` extends it, else rebuilt keeping
    the comment after its last item."""
    old = [_text(s) for s in seq]
    if old == new:
        return
    if old and new[:len(old)] == old:
        seq.extend(new[len(old):])
        return
    trailing = seq.ca.items.get(len(seq) - 1) if len(seq) else None
    for i in range(len(seq) - 1, -1, -1):
        del seq[i]
    seq.ca.items.clear()
    seq.extend(new)
    if new:
        seq.fa.set_block_style()
        if trailing is not None:
            seq.ca.items[len(seq) - 1] = trailing
    else:
        seq.fa.set_flow_style()


def _put_domain(doc: CommentedMap, d: dict) -> None:
    for key in DOMAIN_TEXT:
        if key in d:
            _put_text(doc, key, d[key])
    if isinstance(d.get("scope"), dict):
        scope = doc.get("scope")
        if not isinstance(scope, CommentedMap):
            scope = doc["scope"] = CommentedMap()
        for key in SCOPE_KEYS:
            if key in d["scope"]:
                _put_text(scope, key, d["scope"][key])
    if isinstance(d.get("roles"), dict):
        _put_map(doc, "roles", {str(k).strip().lower(): v for k, v in d["roles"].items()})


def _put_people(doc: CommentedMap, people: dict) -> None:
    for key in [k for k in doc if k not in people]:
        del doc[key]
    for key, p in people.items():
        entry = doc.get(key)
        if not isinstance(entry, CommentedMap):
            entry = doc[key] = CommentedMap()
            p = {"circleback_key_name": f"circleback-key-{key}", **p}
        for field in PERSON_FIELDS:
            if field not in p and field in entry:
                continue
            value = _opt_text(p.get(field))
            if field == "email" and value:
                value = value.lower()
            if _opt_text(entry.get(field)) != value or field not in entry:
                entry[field] = value


def _put_pipeline(doc: CommentedMap, p: dict) -> None:
    if "timezone" in p:
        _put_text(doc, "timezone", str(p["timezone"]).strip())
    if "internal_email_domains" in p:
        domains = [d.strip().lower() for d in p["internal_email_domains"]]
        seq = doc.get("internal_email_domains")
        if not isinstance(seq, CommentedSeq):
            seq = doc["internal_email_domains"] = CommentedSeq()
        _put_seq(seq, domains)
    if isinstance(p.get("channels"), dict):
        ch = doc.get("channels")
        if not isinstance(ch, CommentedMap):
            ch = doc["channels"] = CommentedMap()
        for key in CHANNEL_KEYS:
            if key in p["channels"] and _text(ch.get(key)) != _text(p["channels"][key]):
                ch[key] = DoubleQuotedScalarString(p["channels"][key].strip())     # "#" starts a comment
    if "ingest_cutoff" in p:
        cutoff = p["ingest_cutoff"]
        cutoff = cutoff.isoformat() if isinstance(cutoff, (date, datetime)) else _opt_text(cutoff)
        old = doc.get("ingest_cutoff")
        old = old.isoformat() if isinstance(old, (date, datetime)) else _opt_text(old)
        if old != cutoff:
            doc["ingest_cutoff"] = cutoff
