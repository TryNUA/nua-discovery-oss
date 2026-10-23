"""Markdown files with a YAML front-matter block."""
from __future__ import annotations

import re
from typing import Any

import yaml

_FM = re.compile(r"\A---\n(.*?)\n---\n?", re.S)


class _Dumper(yaml.SafeDumper):
    pass


def _str_repr(dumper: yaml.SafeDumper, data: str):
    style = "|" if "\n" in data else None
    return dumper.represent_scalar("tag:yaml.org,2002:str", data, style=style)


_Dumper.add_representer(str, _str_repr)
_Dumper.add_representer(tuple, lambda d, v: d.represent_list(list(v)))


def dump_yaml(data: Any) -> str:
    return yaml.dump(data, Dumper=_Dumper, sort_keys=False, allow_unicode=True, width=10_000)


class SafeLoader(yaml.SafeLoader):
    """yaml.SafeLoader, except that a scalar shaped like a timestamp but naming no real day or time
    (`2026-11-31`, `2026-11-02T25:00:00Z`) loads as its text. yaml.safe_load raises ValueError there,
    which is not a YAMLError, so one typo in a hand-edited file would stop config/ from loading at all.
    Readers of the value (rules.as_datetime) then reject the text as unreadable."""


def _timestamp(loader: SafeLoader, node: yaml.ScalarNode) -> Any:
    try:
        return yaml.SafeLoader.construct_yaml_timestamp(loader, node)
    except (ValueError, OverflowError):
        return loader.construct_scalar(node)


SafeLoader.add_constructor("tag:yaml.org,2002:timestamp", _timestamp)


def load_yaml(text: str) -> Any:
    """yaml.safe_load with SafeLoader: an impossible date or time is kept as its text.

    Anything else PyYAML fails on raises yaml.YAMLError, so callers need catch only that. PyYAML itself
    raises a plain ValueError for an explicit tag it cannot construct (`!!int 12x`, `!!float abc`) and a
    RecursionError for very deep nesting; those are re-raised as a YAMLError chained from the original."""
    try:
        return yaml.load(text, Loader=SafeLoader)
    except yaml.YAMLError:
        raise
    except Exception as e:
        raise yaml.YAMLError(f"YAML could not be loaded ({type(e).__name__}: {e})") from e


def split(text: str) -> tuple[dict[str, Any], str, int]:
    """Return (front_matter, body, body_offset). Front-matter is {} when absent. Front-matter that
    cannot be loaded raises yaml.YAMLError (see load_yaml)."""
    m = _FM.match(text)
    if not m:
        return {}, text, 0
    data = load_yaml(m.group(1)) or {}
    if not isinstance(data, dict):
        return {}, text, 0
    return data, text[m.end():], m.end()


def join(front_matter: dict[str, Any], body: str) -> str:
    return f"---\n{dump_yaml(front_matter)}---\n\n{body.lstrip(chr(10))}"
