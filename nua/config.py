"""Load repo configuration from config/*.yml. Prompts are code, in nua/prompts/."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any

from nua.frontmatter import load_yaml
from nua.ports import RepoFS

DEFAULT_THRESHOLDS: dict[str, Any] = {
    "under_pressure_calls": 2,
    "under_pressure_seats": 2,
    "add_calls": 3,
    "add_seats": 2,
    "split_scope_mismatch": 3,
    "retire_calls": 3,
    "retire_seats": 3,
    "drop_watching_weeks": 6,
    "scarce_seat_max_calls": 2,
    "backfill_push_min": 6,          # a push adding more than 5 transcripts is a backfill batch
    "dup_window_minutes": 10,
    "dup_delay_minutes": 15,
    "prep_window_days": 7,
    "reprocess_stale_limit": 40,
    "short_answer_words": 8,
    "max_beliefs": 40,
    "single_call_findings": 5,
    "backcheck_delay_seconds": 300,
    "brief_repost_hours": 48,
}


DEFAULT_ROLES: dict[str, str] = {
    "customer": "would buy or use what you are building",
    "intermediary": "sits between the buyer and the seller: a broker, reseller or service provider",
    "vendor": "sells data, software or services into the market",
    "expert": "consultant, advisor or former executive speaking from experience",
    "competitor": "builds something close to what you are building",
}


@dataclass
class Domain:
    """What the discovery is about (config/domain.yml). Every prompt reads it, so nothing in the code is
    tied to one market. company, market and exploring are what a team fills in; the rest are optional.
    Missing keys fall back to these neutral defaults, and keys this version no longer reads (team, about,
    interviewees) are ignored."""
    company: str = "our company"
    market: str = "our market"            # who you sell to, a short phrase
    exploring: str = "the problems our customers have and what they would pay to solve"
    scope: dict[str, str] = field(default_factory=lambda: {
        "line": "the product line, workflow or use case the finding is about",
        "party": "the kind of organization the finding is about",
        "segment": "the market segment: company size, region or tier",
    })
    roles: dict[str, str] = field(default_factory=lambda: dict(DEFAULT_ROLES))
    institution_examples: str = "startup, enterprise company, agency, consultancy"
    bot_name: str = "Nua Discovery"


@dataclass
class Person:
    key: str                          # alex | sam | jordan
    name: str
    email: str | None = None
    slack_id: str | None = None
    github_login: str | None = None
    circleback_key_name: str | None = None   # Secret Manager secret name for this person's Circleback key


@dataclass
class PipelineConfig:
    extractors: list[str]             # model specs, first is primary, e.g. "anthropic:claude-sonnet-5"
    arbiter: str
    ask_map: str
    ask_reduce: str
    rollup: str
    prompt_version: str
    # Go-live: an ISO timestamp with offset, or what YAML makes of one (a datetime, or a date for a
    # date-only value, which means midnight in `timezone`). Read it with nua.rules.as_datetime. An
    # impossible date such as `2026-11-31` stays text, which as_datetime rejects as unreadable.
    ingest_cutoff: str | date | datetime | None = None
    reprocess_stale: bool = False
    timezone: str = "America/Los_Angeles"
    internal_email_domains: list[str] = field(default_factory=list)
    cost_cap_usd_per_task: float = 5.0
    channels: dict[str, str] = field(default_factory=lambda: {
        "calls": "#discovery-calls", "ops": "#discovery-ops", "test": "#discovery-test"})
    thresholds: dict[str, Any] = field(default_factory=lambda: dict(DEFAULT_THRESHOLDS))

    def t(self, key: str) -> Any:
        return self.thresholds.get(key, DEFAULT_THRESHOLDS[key])


@dataclass
class Config:
    root: Path
    pipeline: PipelineConfig
    people: list[Person]
    seats: list[str]
    entities: dict[str, list[str]]    # canonical name -> aliases
    overrides: dict[str, dict[str, Any]]   # call_id -> {contact, firm, seat, role_in_thesis, date}
    domain: Domain = field(default_factory=Domain)

    def person(self, key_or_name: str | None) -> Person | None:
        if not key_or_name:
            return None
        k = key_or_name.strip().lower()
        for p in self.people:
            if k in {p.key.lower(), p.name.lower(), (p.email or "").lower(), (p.github_login or "").lower(), (p.slack_id or "").lower()}:
                return p
        return None

    def is_internal_email(self, email: str | None) -> bool:
        if not email or "@" not in email:
            return False
        dom = email.rsplit("@", 1)[1].lower()
        return dom in {d.lower() for d in self.pipeline.internal_email_domains}

    def canonical(self, name: str | None) -> str | None:
        """Map an ASR alias to its canonical entity name (search and structured fields only)."""
        if not name:
            return name
        n = name.strip().lower()
        for canon, aliases in self.entities.items():
            if n == canon.lower() or n in (a.lower() for a in aliases):
                return canon
        return name


def _load_yaml(repo: RepoFS, rel: str, default: Any) -> Any:
    """The file's YAML, or `default` when it is missing or empty. An impossible date such as
    `ingest_cutoff: 2026-11-31` loads as its text (frontmatter.SafeLoader), so the value reads as
    unreadable where it is used instead of stopping every Ctx from being built. A symlink counts as
    missing: config/ is read through RepoFS, which never follows one."""
    if not repo.exists(rel):
        return default
    data = load_yaml(repo.read(rel))
    return default if data is None else data


def load(root: str | Path) -> Config:
    root = Path(root)
    repo = RepoFS(root)
    p = _load_yaml(repo, "config/pipeline.yml", {})
    thresholds = dict(DEFAULT_THRESHOLDS)
    thresholds.update(p.get("thresholds") or {})
    pipeline = PipelineConfig(
        extractors=list(p.get("extractors") or []),
        arbiter=p.get("arbiter", ""),
        ask_map=p.get("ask_map", ""),
        ask_reduce=p.get("ask_reduce", ""),
        rollup=p.get("rollup", p.get("arbiter", "")),
        prompt_version=str(p.get("prompt_version", "v1")),
        ingest_cutoff=p.get("ingest_cutoff"),
        reprocess_stale=bool(p.get("reprocess_stale", False)),
        timezone=p.get("timezone", "America/Los_Angeles"),
        internal_email_domains=list(p.get("internal_email_domains") or []),
        cost_cap_usd_per_task=float(p.get("cost_cap_usd_per_task", 5.0)),
        channels={**PipelineConfig.__dataclass_fields__["channels"].default_factory(), **(p.get("channels") or {})},
        thresholds=thresholds,
    )
    people = [_load_person(k, v) for k, v in (_load_yaml(repo, "config/people.yml", {}) or {}).items()]
    seats = list(_load_yaml(repo, "config/seats.yml", []) or [])
    entities_raw = _load_yaml(repo, "config/entities.yml", {}) or {}
    entities: dict[str, list[str]] = {}
    for canon, v in entities_raw.items():
        aliases = v.get("aliases", []) if isinstance(v, dict) else (v or [])
        entities[canon] = [a["alias"] if isinstance(a, dict) else a for a in aliases]
    overrides = _load_yaml(repo, "config/overrides.yml", {}) or {}
    return Config(root=root, pipeline=pipeline, people=people, seats=seats,
                  entities=entities, overrides=overrides,
                  domain=_load_domain(_load_yaml(repo, "config/domain.yml", {}) or {}))


def _load_person(key: Any, raw: Any) -> Person:
    """One config/people.yml entry: unknown fields ignored, a bare string taken as the name, the name
    defaulting to the key, so a hand-edit slip never stops every task."""
    raw = {"name": raw} if isinstance(raw, str) else (raw if isinstance(raw, dict) else {})
    fields = {f: str(raw[f]) for f in ("email", "slack_id", "github_login", "circleback_key_name") if raw.get(f)}
    return Person(key=str(key), name=str(raw.get("name") or key), **fields)


def _load_domain(raw: dict) -> Domain:
    """config/domain.yml as a Domain: unknown keys ignored, missing or empty ones left at their defaults,
    scope merged key by key, roles replaced as a whole (ids lower-cased)."""
    base = Domain()
    if not isinstance(raw, dict):
        return base
    for name in ("company", "market", "exploring", "institution_examples", "bot_name"):
        value = raw.get(name)
        if isinstance(value, str) and value.strip():
            setattr(base, name, " ".join(value.split()))
    scope = raw.get("scope")
    if isinstance(scope, dict):
        for key in base.scope:
            if isinstance(scope.get(key), str) and scope[key].strip():
                base.scope[key] = " ".join(scope[key].split())
    roles = raw.get("roles")
    if isinstance(roles, dict) and roles:
        base.roles = {str(k).strip().lower(): " ".join(str(v or "").split()) for k, v in roles.items() if str(k).strip()}
    return base
