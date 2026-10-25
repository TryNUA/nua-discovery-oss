"""`nua setup`: a local setup page for a new team, served on 127.0.0.1 only.

    setup [--port N] [--no-browser]

The page (built from setup-ui/ into nua/setup/static/) edits config/ through nua.setup.configio, writes
keys to `.env` and non-secret deploy inputs to terraform.tfvars, registers the GitHub App through the
manifest flow, links to the Slack create page, and shows a live checklist and the bootstrap command. It
never runs Terraform or the bootstrap itself.

Security: the server binds 127.0.0.1 on a free port. Every /api call needs the random per-run token
(in the URL the command opens; the page sends it as `X-Nua-Token`), and a Host or Origin that is not
this loopback address is refused, so another site cannot reach it by DNS rebinding. Secret values go
in (PUT /api/env) and never come back out: the API only says whether each key is set.

Small local choices that live nowhere else (cloud, recorder, repo, a pasted receiver URL) are kept in
`.nua-setup.json` at the repo root, which is gitignored.
"""
from __future__ import annotations

import argparse
import hmac
import json
import mimetypes
import re
import secrets
import shutil
import socket
import subprocess
import threading
import time
import webbrowser
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Callable
from zoneinfo import available_timezones

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response

from nua.setup import configio, github_app, receiver, slack
from nua.setup import people as teammates

STATIC = Path(__file__).resolve().parent / "static"
STATE_FILE = ".nua-setup.json"
GCP_TFVARS = Path("infra") / "terraform" / "terraform.tfvars"
AWS_TFVARS = Path("infra") / "aws" / "terraform" / "terraform.tfvars"
AWS_TF_DIR = Path("infra") / "aws" / "terraform"
CLOUDS = ("gcp", "aws")
RECORDERS = ("granola", "circleback", "both")
CHECK_TIMEOUT = 8.0
RECEIVER_CACHE_SECONDS = 60.0

MODEL_KEYS = ("GEMINI_API_KEY", "ANTHROPIC_API_KEY", "OPENAI_API_KEY")
SLACK_KEYS = ("SLACK_BOT_TOKEN", "SLACK_SIGNING_SECRET")
GITHUB_KEYS = ("GITHUB_APP_PRIVATE_KEY", "GITHUB_WEBHOOK_SECRET", "GITHUB_APP_ID", "GITHUB_APP_SLUG",
               "GITHUB_APP_INSTALLATION_ID", "REPO_SLUG")
GRANOLA_KEYS = ("GRANOLA_API_KEY", "GRANOLA_WEBHOOK_SECRET")
# Keys the page may write. GitHub ones come from the manifest flow only.
WRITABLE_FIXED = MODEL_KEYS + SLACK_KEYS + GRANOLA_KEYS
CIRCLEBACK_PREFIXES = ("CIRCLEBACK_API_KEY_", "CIRCLEBACK_SIGNING_SECRET_")

# Non-secret tfvars each cloud's page edits, with their kind. Others in the file are left alone.
TFVARS_FIELDS: dict[str, dict[str, str]] = {
    "gcp": {"project_id": "str", "region": "str", "repo_slug": "str", "repo_branch": "str",
            "billing_account": "str", "monthly_budget_usd": "num", "time_zone": "str",
            "circleback_people": "list", "schedules_on": "bool", "slack_test_mode": "bool",
            "github_app_id": "str", "github_app_installation_id": "str", "github_app_slug": "str"},
    "aws": {"region": "str", "repo_slug": "str", "repo_branch": "str", "architecture": "str",
            "budget_emails": "list", "monthly_budget_usd": "num", "time_zone": "str",
            "circleback_people": "list", "schedules_on": "bool", "slack_test_mode": "bool",
            "receiver_keep_warm": "bool",
            "github_app_id": "str", "github_app_installation_id": "str", "github_app_slug": "str"},
}
# Written only by the GitHub App flow (or the bootstrap), shown read-only.
READ_ONLY_FIELDS = ("github_app_id", "github_app_installation_id", "github_app_slug")
# What a value equal to the shipped example means: not filled in yet.
EXAMPLE_VALUES = {"project_id": {"my-discovery-project"}, "repo_slug": {"owner/nua-discovery"},
                  "github_app_id": {"123456"}, "github_app_installation_id": {"78901234"},
                  "github_app_slug": {"nua-discovery"}}

_PROJECT = re.compile(r"^[a-z][a-z0-9-]{4,28}[a-z0-9]$")
_REGION = re.compile(r"^[a-z]{2,}(-[a-z]+)+-?[0-9]+$")
_REPO = re.compile(r"^[\w.-]+/[\w.-]+$")
_BILLING = re.compile(r"^[0-9A-F]{6}-[0-9A-F]{6}-[0-9A-F]{6}$")
_BRANCH = re.compile(r"^[\w./-]{1,100}$")
_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_ENV_KEY = re.compile(r"^[A-Z][A-Z0-9_]*$")
# An AWS CLI profile name as it may appear in ~/.aws/config; also safe to put in a shell command.
_PROFILE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.+=,@-]{0,63}$")


# ---------- local files ----------

def read_env(path: Path) -> dict[str, str]:
    """`KEY=value` lines of a dotenv file (first uncommented one wins). Values stay on the server."""
    out: dict[str, str] = {}
    if not path.is_file():
        return out
    for line in path.read_text(encoding="utf-8").splitlines():
        m = re.match(r"^([A-Za-z_][A-Za-z0-9_]*)=(.*)$", line)
        if m and m.group(1) not in out:
            value = m.group(2).strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                value = value[1:-1]
            out[m.group(1)] = value
    return out


def read_tfvars(path: Path) -> dict[str, Any]:
    """The simple `key = value` lines of a tfvars file: strings, numbers, bools and one-line lists."""
    out: dict[str, Any] = {}
    if not path.is_file():
        return out
    for line in path.read_text(encoding="utf-8").splitlines():
        m = re.match(r'^[ \t]*([a-z_][a-z0-9_]*)[ \t]*=[ \t]*(.+?)[ \t]*$', line)
        if not m or m.group(1) in out:
            continue
        key, raw = m.group(1), m.group(2)
        if raw.startswith('"'):
            s = re.match(r'^"((?:[^"\\]|\\.)*)"', raw)
            out[key] = json.loads(f'"{s.group(1)}"') if s else ""
        elif raw.startswith("["):
            out[key] = re.findall(r'"((?:[^"\\]|\\.)*)"', raw.split("]")[0])
        else:
            raw = raw.split("#")[0].strip()
            if raw in ("true", "false"):
                out[key] = raw == "true"
            else:
                try:
                    out[key] = float(raw) if "." in raw else int(raw)
                except ValueError:
                    out[key] = raw
    return out


def person_var(prefix: str, person: str) -> str:
    """("CIRCLEBACK_API_KEY_", "alex") -> CIRCLEBACK_API_KEY_ALEX, as nua/settings.py and push-secrets.sh name it."""
    return prefix + re.sub(r"[^A-Z0-9]", "_", person.upper())


def _is_set(value: str | None) -> bool:
    return bool(value and value.strip())


# Copied into tfvars by other steps (time zone from step 1, Circleback people from step 2), so they never
# count as the user having started the Cloud step.
MIRRORED_TFVARS = frozenset({"time_zone", "circleback_people"})


def _example_or_empty(key: str, value: Any) -> bool:
    if value is None or (isinstance(value, str) and not value.strip()):
        return True
    return isinstance(value, str) and value in EXAMPLE_VALUES.get(key, set())


class Setup:
    """Everything the API does, against one repo root. Separate from the routes so tests can fake the
    slow parts: `run` (subprocess), `http_get` (receiver health), `flow` and `find_install` (GitHub)."""

    def __init__(self, root: Path, *, run: Callable[[list[str], float], tuple[int, str]] | None = None,
                 which: Callable[[str], str | None] = shutil.which,
                 http_get: Callable[[str, float], int] | None = None,
                 flow: Callable[..., dict] | None = None,
                 find_install: Callable[..., int | None] | None = None,
                 is_org: Callable[[str], bool] | None = None,
                 install_timeout: float = github_app.INSTALL_TIMEOUT_SECONDS,
                 poll_seconds: float = github_app.POLL_SECONDS):
        self.root = Path(root)
        self.run = run or _run
        self.which = which
        self.http_get = http_get or _http_get
        self.flow = flow or github_app.run_manifest_flow
        self.find_install = find_install or github_app.find_installation_id
        self.is_org = is_org or _is_org
        self.install_timeout = install_timeout
        self.poll_seconds = poll_seconds
        self.lock = threading.Lock()
        self.github: dict[str, Any] = {"status": "idle"}
        self._receiver_cache: tuple[float, str, dict] | None = None
        self.last_health: bool | None = None

    # ----- local state -----

    def state(self) -> dict:
        path = self.root / STATE_FILE
        try:
            data = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}
        except (OSError, ValueError):
            data = {}
        data = data if isinstance(data, dict) else {}
        cloud = data.get("cloud") if data.get("cloud") in CLOUDS else None
        if cloud is None:
            cloud = "aws" if (self.root / AWS_TFVARS).is_file() and not (self.root / GCP_TFVARS).is_file() else "gcp"
        recorder = data.get("recorder") if data.get("recorder") in RECORDERS else None
        return {"cloud": cloud, "recorder": recorder, "repo": data.get("repo") or "",
                "receiver_url": data.get("receiver_url") or "",
                "aws_profile": data.get("aws_profile") if _PROFILE.match(str(data.get("aws_profile") or "")) else ""}

    def save_state(self, **changes: Any) -> dict:
        data = self.state()
        data.update({k: v for k, v in changes.items() if v is not None})
        path = self.root / STATE_FILE
        path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
        return data

    def tfvars_path(self, cloud: str | None = None) -> Path:
        return self.root / (AWS_TFVARS if (cloud or self.state()["cloud"]) == "aws" else GCP_TFVARS)

    def env(self) -> dict[str, str]:
        return read_env(self.root / ".env")

    # ----- config -----

    def config(self) -> dict:
        """load_setup, plus the personal mail domains the page never suggests as internal."""
        return {**configio.load_setup(self.root), "personal_domains": list(teammates.PERSONAL_DOMAINS)}

    def save_config(self, body: dict) -> dict:
        data = {k: body[k] for k in ("domain", "people", "seats", "pipeline") if k in body}
        if not data:
            raise ApiError(422, ["send at least one of domain, people, seats, pipeline"])
        if "pipeline" in data:
            if not isinstance(data["pipeline"], dict):
                raise ApiError(422, ["config/pipeline.yml: expected a mapping"])
            # the page edits only these; ingest_cutoff has its own step
            data["pipeline"] = {k: v for k, v in data["pipeline"].items()
                                if k in ("timezone", "internal_email_domains", "channels", "ingest_cutoff")}
        try:
            changed, problems = configio.save_setup_partial(self.root, data)
        except configio.SetupError as e:
            raise ApiError(422, e.problems)
        notes = self._mirror_timezone(self.config()["pipeline"]["timezone"]) if "pipeline" in data else []
        # missing values never block a save; malformed ones are left out and listed by field
        return {"changed": [self._rel(p) for p in changed], "notes": notes, "problems": problems,
                "setup": self.config()}

    def _mirror_timezone(self, zone: str) -> list[str]:
        """Keep time_zone in every tfvars that exists equal to config/pipeline.yml, as the bootstraps expect."""
        notes = []
        for rel in (GCP_TFVARS, AWS_TFVARS):
            path = self.root / rel
            if path.is_file() and not path.is_symlink() and read_tfvars(path).get("time_zone") != zone:
                github_app.write_tfvars(path, {"time_zone": zone})
                notes.append(f"{rel.as_posix()}: time_zone = \"{zone}\"")
        return notes

    # ----- .env -----

    def needed_keys(self) -> dict[str, list[str]]:
        """Keys by step, for the recorder choice and the people in circleback_people."""
        st = self.state()
        recorder = st["recorder"] or "granola"
        people = self._circleback_people()
        rec: list[str] = []
        if recorder in ("granola", "both"):
            rec += list(GRANOLA_KEYS)
        if recorder in ("circleback", "both"):
            for p in people:
                rec += [person_var(CIRCLEBACK_PREFIXES[0], p), person_var(CIRCLEBACK_PREFIXES[1], p)]
        # a key for every provider the model roles in config/pipeline.yml name
        models = [configio.PROVIDER_KEYS[p] for p in configio.providers_used(configio.load_models(self.root))]
        return {"models": models, "slack": list(SLACK_KEYS),
                "github": ["GITHUB_APP_PRIVATE_KEY", "GITHUB_WEBHOOK_SECRET"], "recorder": rec}

    def _circleback_people(self) -> list[str]:
        people = list(self.config()["people"])
        chosen = read_tfvars(self.tfvars_path()).get("circleback_people")
        if isinstance(chosen, list) and chosen:
            return [p for p in chosen if p in people] or list(chosen)
        return []

    def env_status(self) -> dict:
        env = self.env()
        people = list(self.config()["people"])
        keys = list(MODEL_KEYS + SLACK_KEYS + GITHUB_KEYS + GRANOLA_KEYS)
        for p in people:
            keys += [person_var(CIRCLEBACK_PREFIXES[0], p), person_var(CIRCLEBACK_PREFIXES[1], p)]
        return {"path": ".env", "exists": (self.root / ".env").is_file(),
                "keys": {k: _is_set(env.get(k)) for k in keys}, "needed": self.needed_keys()}

    def write_env(self, body: dict) -> dict:
        values = body.get("values")
        if not isinstance(values, dict):
            raise ApiError(422, ["expected {\"values\": {KEY: value}}"])
        people = list(self.config()["people"])
        allowed = set(WRITABLE_FIXED)
        for p in people:
            allowed |= {person_var(CIRCLEBACK_PREFIXES[0], p), person_var(CIRCLEBACK_PREFIXES[1], p)}
        problems, clean = [], {}

        def bad(key: Any, message: str) -> None:
            problems.append({"field": f"env.{key}", "message": message})

        for k, v in values.items():
            if not isinstance(k, str) or not _ENV_KEY.match(k) or k not in allowed:
                bad(k, f"{k}: not a key this page writes")
                continue
            if not isinstance(v, str):
                bad(k, "expected text")
                continue
            v = v.strip()
            if not v:
                continue                    # an empty box means "leave as is"
            if "\n" in v or "\r" in v:
                bad(k, "must be one line")
                continue
            if k == "SLACK_BOT_TOKEN" and not v.startswith("xoxb-"):
                bad(k, "the Bot User OAuth Token starts with xoxb-")
                continue
            clean[k] = v
        # every valid key is written even when another one is refused
        if clean:
            github_app.write_env(self.root / ".env", clean)
        return {**self.env_status(), "written": sorted(clean), "problems": problems}

    # ----- deploy settings -----

    def deploy(self) -> dict:
        st = self.state()
        cloud = st["cloud"]
        path = self.tfvars_path(cloud)
        example = path.parent / "terraform.tfvars.example"
        source = path if path.is_file() else example
        values = read_tfvars(source)
        fields = {k: values.get(k) for k in TFVARS_FIELDS[cloud]}
        for k in fields:
            if _example_or_empty(k, fields[k]) and k in EXAMPLE_VALUES:
                fields[k] = ""
        return {"cloud": cloud, "recorder": st["recorder"], "repo": st["repo"] or fields.get("repo_slug") or "",
                "tfvars_path": self._rel(path), "exists": path.is_file(), "fields": fields,
                "read_only": list(READ_ONLY_FIELDS), "timezone": self.config()["pipeline"]["timezone"],
                "people": list(self.config()["people"]), "receiver_url_override": st["receiver_url"],
                "aws_profile": st["aws_profile"],
                "aws_ready": (self.root / AWS_TF_DIR).is_dir()}

    def save_deploy(self, body: dict) -> dict:
        cloud = body.get("cloud", self.state()["cloud"])
        if cloud not in CLOUDS:
            raise ApiError(422, ["cloud must be gcp or aws"])
        if cloud == "aws" and not (self.root / AWS_TF_DIR).is_dir():
            raise ApiError(422, ["infra/aws/terraform is missing from this checkout; pull the latest code or pick GCP"])
        fields = body.get("fields") or {}
        if not isinstance(fields, dict):
            raise ApiError(422, ["fields must be a mapping"])
        # each valid piece is saved; a malformed one is left out and listed by field
        problems: list[dict] = []

        def bad(field: str, message: str) -> None:
            problems.append({"field": field, "message": message})

        recorder = body.get("recorder")
        if recorder is not None and recorder not in RECORDERS:
            bad("recorder", "recorder must be granola, circleback or both")
            recorder = None
        repo = body.get("repo")
        if repo is not None and repo and not _REPO.match(repo.strip()):
            bad("repo", "repo must be owner/name, e.g. acme/discovery")
            repo = None
        profile = body.get("aws_profile")
        if profile is not None:
            profile = str(profile).strip()
            if profile and not _PROFILE.match(profile):
                bad("aws_profile", "a profile name: letters, digits and _ . + = , @ - (as in ~/.aws/config)")
                profile = None
        override = body.get("receiver_url")
        save_override = "receiver_url" in body
        if override:
            try:
                override = receiver.normalize(override)
            except ValueError as e:
                bad("receiver_url", str(e))
                save_override = False
        allowed = TFVARS_FIELDS[cloud]
        people = list(self.config()["people"])
        clean = {}
        for k, v in fields.items():
            if k not in allowed or k in READ_ONLY_FIELDS or k == "time_zone":
                continue
            problem, value = _check_field(cloud, k, allowed[k], v, people)
            if problem:
                bad(f"fields.{k}", problem.split(": ", 1)[-1])
            else:
                clean[k] = value
        if recorder == "granola":
            clean["circleback_people"] = []
        if repo:
            clean["repo_slug"] = repo.strip()
        self.save_state(cloud=cloud, recorder=recorder, repo=(repo or "").strip() or None,
                        receiver_url=(override or "") if save_override else None, aws_profile=profile)
        clean["time_zone"] = self.config()["pipeline"]["timezone"]
        target = self.tfvars_path(cloud)
        if not target.is_file():
            # a new tfvars for this cloud starts with what the other cloud's file already says
            other = read_tfvars(self.tfvars_path("aws" if cloud == "gcp" else "gcp"))
            for k in ("repo_slug", "repo_branch", "circleback_people", "github_app_id", "github_app_installation_id",
                      "github_app_slug"):
                if k not in clean and not _example_or_empty(k, other.get(k)) and other.get(k) != []:
                    clean[k] = other[k]
        github_app.write_tfvars(target, clean)
        self._receiver_cache = None
        return {**self.deploy(), "problems": problems}

    # ----- receiver URL -----

    def receiver(self) -> dict:
        """{url, source, how_to}. Cached for a minute: gcloud and terraform are slow."""
        st = self.state()
        key = json.dumps([st, self._mtime(self.tfvars_path())])
        now = time.monotonic()
        if self._receiver_cache and self._receiver_cache[1] == key and now - self._receiver_cache[0] < RECEIVER_CACHE_SECONDS:
            return self._receiver_cache[2]
        out = self._find_receiver(st)
        out["detail"] = self._receiver_detail(st, out)
        self._receiver_cache = (now, key, out)
        return out

    def _aws_env(self) -> list[str]:
        """`env AWS_PROFILE=<profile>` before a command, when a profile is set (terraform reads its S3 state)."""
        profile = self.state()["aws_profile"]
        return ["env", f"AWS_PROFILE={profile}"] if profile else []

    def _receiver_detail(self, st: dict, found: dict) -> str:
        """Where the URL came from, in a few words for the page."""
        if found["source"] == "pasted":
            return "pasted in step 3"
        if found["source"] == "terraform":
            return "from the Terraform output receiver_url"
        if found["source"] == "project":
            values = read_tfvars(self.tfvars_path("gcp"))
            return (f"from project {values.get('project_id')} (its project number) and region "
                    f"{values.get('region') or receiver.DEFAULT_REGION}")
        return ""

    def _find_receiver(self, st: dict) -> dict:
        if st["receiver_url"]:
            return {"url": st["receiver_url"], "source": "pasted", "how_to": ""}
        if st["cloud"] == "aws":
            tf = self.root / AWS_TF_DIR
            if (tf / ".terraform").is_dir() and self.which("terraform"):
                code, out = self.run(self._aws_env() + ["terraform", f"-chdir={tf}", "output", "-raw", "receiver_url"], 20)
                if code == 0 and out.startswith("https://") and "\n" not in out:
                    return {"url": out, "source": "terraform", "how_to": ""}
            return {"url": None, "source": None,
                    "how_to": "On AWS the receiver URL exists after the first deploy. Run the deploy command in "
                              "step 6; it prints the URL. Or paste it in step 3."}
        tf = self.root / receiver.TF_DIR
        if (tf / ".terraform").is_dir() and self.which("terraform"):
            code, out = self.run(["terraform", f"-chdir={tf}", "output", "-raw", "receiver_url"], 20)
            if code == 0 and out.startswith("https://") and "\n" not in out:
                return {"url": out, "source": "terraform", "how_to": ""}
        values = read_tfvars(self.tfvars_path("gcp"))
        project = values.get("project_id")
        if project and not _example_or_empty("project_id", project) and self.which("gcloud"):
            code, number = self.run(["gcloud", "projects", "describe", str(project),
                                     "--format=value(projectNumber)"], 15)
            if code == 0 and number.isdigit():
                return {"url": receiver.url_for(number, values.get("region") or receiver.DEFAULT_REGION),
                        "source": "project", "how_to": ""}
            return {"url": None, "source": None,
                    "how_to": f"gcloud cannot see project {project} yet. Log in to gcloud, or run the first "
                              "bootstrap (step 6) to create it, then reload. Or paste the receiver URL in step 3."}
        return {"url": None, "source": None,
                "how_to": "Set your GCP project and region in step 3 (and log in to gcloud), or paste the "
                          "receiver URL there."}

    # ----- Slack -----

    def slack(self) -> dict:
        url = self.receiver()
        bot = self.config()["domain"].get("bot_name") or "Nua Discovery"
        out = {"receiver_url": url["url"], "how_to": url["how_to"], "bot_name": bot,
               "channels": self.config()["pipeline"]["channels"]}
        if url["url"]:
            manifest = slack.render_manifest(url["url"], bot)
            out.update(create_url=slack.create_app_url(manifest),
                       create_url_no_events=slack.create_app_url(slack.render_manifest(url["url"], bot, events=False)),
                       manifest_yaml=slack.manifest_yaml(url["url"], bot))
        return out

    # ----- GitHub App -----

    def github_status(self) -> dict:
        with self.lock:
            out = {k: v for k, v in self.github.items() if k in
                   ("status", "url", "html_url", "install_url", "installation_id", "error", "slug", "repo")}
        env = self.env()
        tf = read_tfvars(self.tfvars_path())
        out["saved"] = {"app": _is_set(env.get("GITHUB_APP_PRIVATE_KEY")) and _is_set(env.get("GITHUB_WEBHOOK_SECRET")),
                        "slug": tf.get("github_app_slug") if not _example_or_empty("github_app_slug", tf.get("github_app_slug")) else "",
                        "installation_id": tf.get("github_app_installation_id")
                        if not _example_or_empty("github_app_installation_id", tf.get("github_app_installation_id")) else ""}
        if out["saved"]["slug"] and "install_url" not in out:
            out["install_url"] = github_app.install_url(out["saved"]["slug"])
        out["default_name"] = github_app.default_app_name(self.config()["domain"]["company"])
        return out

    def github_start(self, body: dict) -> dict:
        repo = (body.get("repo") or self.state()["repo"] or "").strip()
        if not _REPO.match(repo):
            raise ApiError(422, ["repo must be owner/name, e.g. acme/discovery"])
        owner = (body.get("owner") or "").strip() or repo.split("/")[0]
        name = (body.get("name") or "").strip() or github_app.default_app_name(self.config()["domain"]["company"])
        if len(name) > github_app.MAX_APP_NAME:
            raise ApiError(422, [f"the app name can be at most {github_app.MAX_APP_NAME} characters (a GitHub limit)"])
        url = self.receiver()["url"]
        if not url:
            raise ApiError(409, ["no receiver URL yet. " + self.receiver()["how_to"]])
        with self.lock:
            if self.github.get("status") in ("starting", "waiting", "installing"):
                raise ApiError(409, ["a GitHub App setup is already running"])
            self.github = {"status": "starting", "repo": repo}
        self.save_state(repo=repo)
        tfvars = self.tfvars_path()
        threading.Thread(target=self._github_flow, args=(repo, owner, name, url, tfvars), daemon=True).start()
        return self.github_status()

    def _set_github(self, **kw: Any) -> None:
        with self.lock:
            self.github.update(kw)

    def _github_flow(self, repo: str, owner: str, name: str, url: str, tfvars: Path) -> None:
        try:
            org = owner if self.is_org(owner) else None
            manifest = github_app.build_manifest(name, url, f"{github_app.GITHUB}/{repo}")
            result = self.flow(org, manifest, open_browser=False,
                               on_ready=lambda u: self._set_github(status="waiting", url=u))
            github_app.save_app(self.root, repo, result, tfvars)
            self._set_github(status="installing", html_url=result.get("html_url"), slug=result["slug"],
                             install_url=github_app.install_url(result["slug"]), url=None)
            deadline = time.monotonic() + self.install_timeout
            account = repo.split("/")[0]
            while True:
                installation = self.find_install(result["id"], result["pem"], account=account)
                if installation is not None:
                    github_app.save_installation(self.root, installation, tfvars)
                    self._set_github(status="done", installation_id=str(installation))
                    return
                if time.monotonic() > deadline:
                    self._set_github(status="error", error="No installation seen yet. Install the app, then press "
                                     "Check again, or set github_app_installation_id in tfvars by hand.")
                    return
                time.sleep(self.poll_seconds)
        except Exception as e:      # shown on the page; never includes a secret
            self._set_github(status="error", error=f"{type(e).__name__}: {str(e)[:300]}")

    def github_check_install(self) -> dict:
        """Look for the installation once more, with the key saved in .env."""
        env = self.env()
        app_id, pem = env.get("GITHUB_APP_ID"), env.get("GITHUB_APP_PRIVATE_KEY", "").replace("\\n", "\n")
        repo = env.get("REPO_SLUG") or self.state()["repo"]
        if not app_id or not pem.strip():
            raise ApiError(409, ["create the app first"])
        try:
            installation = self.find_install(app_id, pem, account=repo.split("/")[0] if repo else None)
        except Exception as e:
            raise ApiError(502, [f"{type(e).__name__}: {str(e)[:300]}"])
        if installation is None:
            raise ApiError(404, ["not installed yet: open the install link, pick your repo, Install"])
        github_app.save_installation(self.root, installation, self.tfvars_path())
        self._set_github(status="done", installation_id=str(installation))
        return self.github_status()

    # ----- model roles -----

    def models(self) -> dict:
        """The roles, which preset they match, the providers they need and which keys are set (never values)."""
        roles = configio.load_models(self.root)
        env = self.env()
        keys = {p: _is_set(env.get(k)) for p, k in configio.PROVIDER_KEYS.items()}
        plain = {r: roles[r] for r in configio.MODEL_ROLES}
        if plain == configio.GEMINI_ONLY:
            preset = "gemini"
        elif plain in (configio.cross_provider(True), configio.cross_provider(False)):
            preset = "cross"
        else:
            preset = "custom"
        return {"roles": plain, "prompt_version": roles["prompt_version"], "preset": preset,
                "providers": configio.providers_used(roles), "keys": keys}

    def set_models(self, body: dict) -> dict:
        preset = body.get("preset")
        env = self.env()
        if preset == "gemini":
            models = configio.GEMINI_ONLY
        elif preset == "cross":
            if not (_is_set(env.get("ANTHROPIC_API_KEY")) and _is_set(env.get("OPENAI_API_KEY"))):
                raise ApiError(409, ["the cross-provider setup needs both an Anthropic and an OpenAI key in .env"])
            models = configio.cross_provider(_is_set(env.get("GEMINI_API_KEY")))
        else:
            raise ApiError(422, ["preset must be gemini or cross"])
        try:
            changed = configio.set_models(self.root, models)
        except configio.SetupError as e:
            raise ApiError(422, e.problems)
        return {**self.models(), "changed": [self._rel(p) for p in changed]}

    # ----- go live -----

    def golive(self) -> dict:
        url = self.receiver()["url"]
        tf = read_tfvars(self.tfvars_path())
        st = self.state()
        out = {"ingest_cutoff": self.config()["pipeline"]["ingest_cutoff"], "schedules_on": bool(tf.get("schedules_on")),
               "timezone": self.config()["pipeline"]["timezone"], "recorder": st["recorder"], "receiver_url": url,
               "circleback_people": self._circleback_people(), "cloud": st["cloud"]}
        if url:
            out["granola_url"] = f"{url}/ingest/granola"
            out["granola_curl"] = ("curl -X POST https://public-api.granola.ai/v1/webhook-endpoints \\\n"
                                   "  -H \"Authorization: Bearer $GRANOLA_API_KEY\" -H \"Content-Type: application/json\" \\\n"
                                   f"  -d '{{\"url\": \"{url}/ingest/granola\", \"scopes\": [\"workspace\"], "
                                   "\"events\": [\"note.generated\", \"note.access_granted\"]}'")
            out["circleback_urls"] = {p: f"{url}/ingest/circleback/{p}" for p in out["circleback_people"]}
        return out

    def save_golive(self, body: dict) -> dict:
        notes, problems = [], []
        if "ingest_cutoff" in body:
            changed, problems = configio.save_setup_partial(
                self.root, {"pipeline": {"ingest_cutoff": body["ingest_cutoff"] or None}})
            notes += [self._rel(p) for p in changed]
        if "schedules_on" in body:
            if not isinstance(body["schedules_on"], bool):
                problems.append({"field": "schedules_on", "message": "schedules_on must be true or false"})
            else:
                path = self.tfvars_path()
                if body["schedules_on"] != bool(read_tfvars(path).get("schedules_on")):
                    github_app.write_tfvars(path, {"schedules_on": body["schedules_on"]})
                    notes.append(self._rel(path))
        return {**self.golive(), "changed": notes, "problems": problems}

    # ----- checks -----

    def deploy_command(self) -> dict:
        st = self.state()
        tf = read_tfvars(self.tfvars_path())
        if st["cloud"] == "aws":
            if (self.root / "infra" / "aws" / "bootstrap.sh").is_file():
                region = tf.get("region") or "us-west-2"
                prefix = f"AWS_PROFILE={st['aws_profile']} " if st["aws_profile"] else ""
                return {"command": f"{prefix}bash infra/aws/bootstrap.sh --region {region}",
                        "doc": "docs/self-hosting-aws.md" if (self.root / "docs" / "self-hosting-aws.md").is_file() else ""}
            return {"command": "",
                    "doc": "docs/self-hosting-aws.md" if (self.root / "docs" / "self-hosting-aws.md").is_file() else "",
                    "note": "infra/aws/bootstrap.sh is not in this checkout. Pull the latest code for the one-command "
                            "AWS setup."}
        project = tf.get("project_id")
        project = "" if _example_or_empty("project_id", project) else project
        region = tf.get("region") or "us-west1"
        return {"command": f"bash infra/bootstrap.sh --project {project or '<your-project-id>'} --region {region}",
                "command_create": (f"bash infra/bootstrap.sh --project {project or '<your-project-id>'} --region {region} "
                                   f"--create-project --billing-account {tf.get('billing_account') or '<XXXXXX-XXXXXX-XXXXXX>'}"),
                "doc": "docs/self-hosting.md"}

    def checks(self) -> dict:
        st = self.state()
        cloud = st["cloud"]
        tools = ("gcloud", "terraform", "uv", "curl") if cloud == "gcp" else ("aws", "terraform", "uv", "zip", "curl")
        install = {"gcloud": "https://cloud.google.com/sdk/docs/install",
                   "terraform": "https://developer.hashicorp.com/terraform/install",
                   "uv": "https://docs.astral.sh/uv/",
                   "zip": "your package manager", "curl": "your package manager",
                   "aws": "https://docs.aws.amazon.com/cli/latest/userguide/getting-started-install.html"}
        out: list[dict] = []
        for t in tools:
            ok = bool(self.which(t))
            out.append(_check(f"tool:{t}", f"{t} installed", ok, "" if ok else f"Install {t}: {install[t]}"))

        def login_checks() -> list[dict]:
            if cloud == "gcp":
                if not self.which("gcloud"):
                    return [_check("login:gcloud", "gcloud logged in", False, "Install gcloud first"),
                            _check("login:adc", "Application default credentials", False, "Install gcloud first")]
                code, acct = self.run(["gcloud", "auth", "list", "--filter=status:ACTIVE", "--format=value(account)"],
                                      CHECK_TIMEOUT)
                ok = code == 0 and bool(acct.strip())
                a = _check("login:gcloud", "gcloud logged in", ok, "" if ok else "Run: gcloud auth login",
                           acct.strip().splitlines()[0] if ok else "")
                code, _ = self.run(["gcloud", "auth", "application-default", "print-access-token"], CHECK_TIMEOUT)
                b = _check("login:adc", "Application default credentials (for Terraform)", code == 0,
                           "" if code == 0 else "Run: gcloud auth application-default login")
                return [a, b]
            if not self.which("aws"):
                return [_check("login:aws", "AWS CLI logged in", False, "Install the AWS CLI first")]
            profile = ["--profile", st["aws_profile"]] if st["aws_profile"] else []
            code, ident = self.run(["aws", "sts", "get-caller-identity", "--output", "json", *profile], CHECK_TIMEOUT)
            detail = ""
            if code == 0:
                try:
                    detail = f"account {json.loads(ident).get('Account', '')}"
                except ValueError:
                    pass
            if st["aws_profile"]:
                fix = (f"Run: aws sso login --profile {st['aws_profile']} (SSO), or check the profile name in step 3")
                detail = f"{detail}, profile {st['aws_profile']}".lstrip(", ") if detail else f"profile {st['aws_profile']}"
            else:
                fix = "Sign in (step 3 has the steps), or set the AWS profile in step 3"
            return [_check("login:aws", "AWS CLI logged in", code == 0, "" if code == 0 else fix, detail)]

        def health() -> list[dict]:
            url = self.receiver()["url"]
            if not url:
                return [_check("receiver", "Receiver /healthz answers", False, "Deploy first (the command above)")]
            code = self.http_get(f"{url}/healthz", 5.0)
            self.last_health = code == 200
            return [_check("receiver", "Receiver /healthz answers", code == 200,
                           "" if code == 200 else f"Got {code or 'no answer'}. Run the deploy command, then check again.",
                           url)]

        with ThreadPoolExecutor(max_workers=2) as pool:
            logins, healthy = pool.submit(login_checks), pool.submit(health)
            out += logins.result()
            setup = self.config()
            problems = configio.validate_setup(setup)
            out.append(_check("config", "Config valid (config/*.yml)", not problems,
                              "" if not problems else "Fix in step 1: " + problems[0], ""))
            env = self.env()
            needed = self.needed_keys()
            missing = [k for group in needed.values() for k in group
                       if not _is_set(env.get(k)) and k != "GRANOLA_WEBHOOK_SECRET"
                       and not k.startswith(CIRCLEBACK_PREFIXES[1])]
            if not st["recorder"]:
                out.append(_check("env", ".env keys present", False, "Pick a recorder in step 2"))
            else:
                out.append(_check("env", ".env keys present", not missing,
                                  "" if not missing else "Missing: " + ", ".join(missing)))
            tf_path = self.tfvars_path()
            tf = read_tfvars(tf_path)
            need_tf = ["project_id", "region"] if cloud == "gcp" else ["region"]
            need_tf += ["repo_slug", "github_app_id", "github_app_installation_id", "github_app_slug"]
            tf_missing = [k for k in need_tf if _example_or_empty(k, tf.get(k))]
            out.append(_check("tfvars", "terraform.tfvars complete", tf_path.is_file() and not tf_missing,
                              "" if tf_path.is_file() and not tf_missing else
                              ("Missing: " + ", ".join(tf_missing) if tf_path.is_file() else
                               f"No {self._rel(tf_path)} yet: save step 3")))
            out += healthy.result()
            secrets_needed = [k for k in needed["recorder"] if k == "GRANOLA_WEBHOOK_SECRET"
                              or k.startswith(CIRCLEBACK_PREFIXES[1])]
            unsigned = [k for k in secrets_needed if not _is_set(env.get(k))]
            if not st["recorder"]:
                signing_fix = "Pick a recorder in step 2"
            elif unsigned:
                signing_fix = ("After the first deploy: create the webhook (step 7), paste its secret, run the deploy "
                               f"command again. Missing: {', '.join(unsigned)}")
            else:
                signing_fix = ""
            out.append(_check("signing", "Recorder webhook signing secrets in .env", not signing_fix, signing_fix))
            cutoff = setup["pipeline"]["ingest_cutoff"]
            out.append(_check("cutoff", "ingest_cutoff set", bool(cutoff), "" if cutoff else "Set it in step 7",
                              cutoff or ""))
            on = bool(tf.get("schedules_on"))
            out.append(_check("schedules", "schedules_on", on, "" if on else
                              "Turn it on in step 7, then run the deploy command once more"))
        return {"cloud": cloud, "checks": out, "deploy": self.deploy_command()}

    # ----- progress -----

    def _cloud_touched(self, tf: dict) -> bool:
        """Whether any Cloud-step value differs from the shipped example, so a tfvars file created from
        the example by another step's save does not show the step as started."""
        path = self.tfvars_path()
        if not path.is_file():
            return False
        example_path = path.parent / "terraform.tfvars.example"
        example = read_tfvars(example_path) if example_path.is_file() else {}
        return any(v != example.get(k) for k, v in tf.items() if k not in MIRRORED_TFVARS)

    def status(self) -> dict:
        """Which steps look done, from files only (no subprocess)."""
        setup = self.config()
        env = self.env()
        tf = read_tfvars(self.tfvars_path())
        st = self.state()
        d = setup["domain"]
        needed = self.needed_keys()
        rec_ok = bool(st["recorder"]) and all(_is_set(env.get(k)) for k in needed["recorder"]
                                               if k != "GRANOLA_WEBHOOK_SECRET" and not k.startswith(CIRCLEBACK_PREFIXES[1]))
        cloud_need = ["project_id", "region", "repo_slug"] if st["cloud"] == "gcp" else ["region", "repo_slug"]
        # some but not all of a step's parts: the stepper shows these as in progress
        started = {
            "team": bool(d.get("company") or setup["people"] or any(_is_set(env.get(k)) for k in MODEL_KEYS)),
            "recorder": bool(st["recorder"]),
            "cloud": self._cloud_touched(tf),
            "github": _is_set(env.get("GITHUB_APP_PRIVATE_KEY")),
            "slack": any(_is_set(env.get(k)) for k in SLACK_KEYS),
            "deploy": False,
            "golive": bool(setup["pipeline"]["ingest_cutoff"]) or bool(tf.get("schedules_on")),
        }
        steps = {
            # step 1: what you research, who you are, and the one model key every run needs
            "team": not configio.validate_setup({"domain": d, "people": setup["people"], "pipeline": setup["pipeline"]})
                    and bool(setup["people"]) and all(_is_set(env.get(k)) for k in needed["models"]),
            "recorder": rec_ok,
            "cloud": self.tfvars_path().is_file() and not any(_example_or_empty(k, tf.get(k)) for k in cloud_need),
            "github": _is_set(env.get("GITHUB_APP_PRIVATE_KEY"))
                      and not _example_or_empty("github_app_installation_id", tf.get("github_app_installation_id")),
            "slack": all(_is_set(env.get(k)) for k in SLACK_KEYS),
            "deploy": bool(self.last_health),
            "golive": bool(setup["pipeline"]["ingest_cutoff"]) and bool(tf.get("schedules_on")),
        }
        return {"steps": steps, "started": {k: v and not steps[k] for k, v in started.items()},
                "cloud": st["cloud"], "recorder": st["recorder"], "root": str(self.root)}

    # ----- helpers -----

    def _rel(self, path: Path) -> str:
        try:
            return Path(path).relative_to(self.root).as_posix()
        except ValueError:
            return str(path)

    @staticmethod
    def _mtime(path: Path) -> float:
        try:
            return path.stat().st_mtime
        except OSError:
            return 0.0


def _check(id_: str, label: str, ok: bool, fix: str, detail: str = "") -> dict:
    return {"id": id_, "label": label, "ok": bool(ok), "fix": fix, "detail": detail}


def _check_field(cloud: str, key: str, kind: str, value: Any, people: list[str]) -> tuple[str | None, Any]:
    """(problem, clean value) for one tfvars field from the page."""
    if kind == "bool":
        return (None, value) if isinstance(value, bool) else (f"{key}: expected true or false", None)
    if kind == "num":
        try:
            n = float(value)
        except (TypeError, ValueError):
            return f"{key}: expected a number", None
        if n < 0 or n > 100000:
            return f"{key}: expected a number from 0 to 100000", None
        return None, int(n) if n == int(n) else n
    if kind == "list":
        if isinstance(value, str):
            value = [v for v in re.split(r"[,\s]+", value) if v]
        if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
            return f"{key}: expected a list", None
        value = [v.strip() for v in value if v.strip()]
        if key == "circleback_people":
            bad = [v for v in value if v not in people]
            if bad:
                return f"circleback_people: {', '.join(bad)} not in config/people.yml", None
        if key == "budget_emails":
            bad = [v for v in value if not _EMAIL.match(v)]
            if bad:
                return f"budget_emails: {', '.join(bad)} is not an email address", None
        return None, value
    value = "" if value is None else str(value).strip()
    if "\n" in value or '"' in value or "\\" in value:
        return f"{key}: plain text only", None
    if key == "project_id" and not _PROJECT.match(value):
        return "project_id: 6 to 30 lowercase letters, digits or hyphens, starting with a letter", None
    if key == "region" and not _REGION.match(value):
        return f"region: {value!r} does not look like a region such as {'us-west1' if cloud == 'gcp' else 'us-west-2'}", None
    if key == "repo_slug" and value and not _REPO.match(value):
        return "repo_slug: expected owner/name", None
    if key == "repo_branch" and value and not _BRANCH.match(value):
        return "repo_branch: not a branch name", None
    if key == "billing_account" and value and not _BILLING.match(value.upper()):
        return "billing_account: looks like XXXXXX-XXXXXX-XXXXXX (gcloud billing accounts list)", None
    if key == "architecture" and value not in ("x86_64", "arm64"):
        return "architecture: x86_64 or arm64", None
    return None, value.upper() if key == "billing_account" else value


def _run(cmd: list[str], timeout: float) -> tuple[int, str]:
    """(exit code, stdout) of a quick read-only command; (127, "") when it is missing or too slow."""
    if shutil.which(cmd[0]) is None:
        return 127, ""
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.TimeoutExpired):
        return 124, ""
    return proc.returncode, proc.stdout.strip()


def _http_get(url: str, timeout: float) -> int:
    try:
        return httpx.get(url, timeout=timeout, follow_redirects=False).status_code
    except httpx.HTTPError:
        return 0


def _is_org(owner: str) -> bool:
    with httpx.Client(timeout=15.0) as client:
        return github_app._is_org(owner, client)


class ApiError(Exception):
    def __init__(self, status: int, problems: list[str]):
        super().__init__("; ".join(problems))
        self.status, self.problems = status, problems


# ---------- the app ----------

def create_app(setup: Setup, token: str, port: int, static_dir: Path = STATIC) -> FastAPI:
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    allowed_hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}
    allowed_origins = {f"http://{h}" for h in allowed_hosts}

    @app.middleware("http")
    async def guard(request: Request, call_next):
        host = request.headers.get("host", "")
        if host not in allowed_hosts:
            return JSONResponse({"problems": ["wrong host"]}, status_code=403)
        origin = request.headers.get("origin")
        if origin is not None and origin not in allowed_origins:
            return JSONResponse({"problems": ["wrong origin"]}, status_code=403)
        if request.url.path.startswith("/api/"):
            sent = request.headers.get("x-nua-token", "")
            if not hmac.compare_digest(sent.encode(), token.encode()):
                return JSONResponse({"problems": ["missing or wrong token: open the link `nua setup` printed"]},
                                    status_code=401)
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["X-Content-Type-Options"] = "nosniff"
        return response

    @app.exception_handler(ApiError)
    async def api_error(_: Request, e: ApiError):
        return JSONResponse({"problems": e.problems}, status_code=e.status)

    async def body(request: Request) -> dict:
        try:
            data = await request.json()
        except ValueError:
            raise ApiError(400, ["expected a JSON body"])
        if not isinstance(data, dict):
            raise ApiError(400, ["expected a JSON object"])
        return data

    @app.get("/api/status")
    def status():
        return setup.status()

    @app.get("/api/config")
    def get_config():
        return setup.config()

    @app.put("/api/config")
    async def put_config(request: Request):
        return setup.save_config(await body(request))

    @app.get("/api/timezones")
    def timezones():
        return sorted(z for z in available_timezones() if "/" in z and not z.startswith(("Etc/", "SystemV/"))) + ["UTC"]

    @app.get("/api/env")
    def get_env():
        return setup.env_status()

    @app.put("/api/env")
    async def put_env(request: Request):
        return setup.write_env(await body(request))

    @app.get("/api/deploy")
    def get_deploy():
        return setup.deploy()

    @app.put("/api/deploy")
    async def put_deploy(request: Request):
        return setup.save_deploy(await body(request))

    @app.get("/api/receiver")
    def get_receiver():
        return setup.receiver()

    @app.get("/api/slack")
    def get_slack():
        return setup.slack()

    @app.get("/api/github")
    def get_github():
        return setup.github_status()

    @app.post("/api/github/start")
    async def github_start(request: Request):
        return setup.github_start(await body(request))

    @app.post("/api/github/check-install")
    def github_check():
        return setup.github_check_install()

    @app.get("/api/checks")
    def get_checks():
        return setup.checks()

    @app.get("/api/models")
    def get_models():
        return setup.models()

    @app.post("/api/models")
    async def post_models(request: Request):
        return setup.set_models(await body(request))

    @app.get("/api/golive")
    def get_golive():
        return setup.golive()

    @app.put("/api/golive")
    async def put_golive(request: Request):
        return setup.save_golive(await body(request))

    @app.api_route("/api/{rest:path}", methods=["GET", "POST", "PUT", "DELETE"])
    def api_404(rest: str):
        raise ApiError(404, [f"no such endpoint /api/{rest}"])

    # The page is read once here, so a server left running keeps serving the page it shipped with: a
    # rebuild on disk would otherwise send new page code to old API code, and the page breaks.
    files = _snapshot(static_dir)

    @app.get("/{path:path}")
    def static(path: str):
        key = path.strip("/")
        if key in files:
            target = key
        elif f"{key}/index.html".lstrip("/") in files:
            target = f"{key}/index.html".lstrip("/")
        else:
            if "index.html" not in files:
                return Response("The setup page is not built: see setup-ui/README.md.", status_code=500,
                                media_type="text/plain")
            if key and "." in key.rsplit("/", 1)[-1]:
                return Response("not found", status_code=404, media_type="text/plain")
            target = "index.html"
        return Response(files[target], media_type=mimetypes.guess_type(target)[0] or "application/octet-stream")

    return app


def _snapshot(static_dir: Path) -> dict[str, bytes]:
    """Every file under `static_dir` by its relative posix path; nothing outside it can be named."""
    base = static_dir.resolve()
    if not base.is_dir():
        return {}
    return {p.relative_to(base).as_posix(): p.read_bytes() for p in base.rglob("*") if p.is_file()}


# ---------- `nua setup` ----------

def add_arguments(p: argparse.ArgumentParser) -> None:
    p.add_argument("--port", type=int, default=0, help="local port (default: any free one)")
    p.add_argument("--no-browser", action="store_true", help="print the link instead of opening it")


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def command(args: argparse.Namespace, root: Path) -> int:
    import uvicorn

    port = args.port or free_port()
    token = secrets.token_urlsafe(24)
    app = create_app(Setup(root), token, port)
    url = f"http://127.0.0.1:{port}/?token={token}"
    if not (STATIC / "index.html").is_file():
        print("warning: nua/setup/static/ has no build; see setup-ui/README.md", flush=True)
    print(f"Setup page for {root}:\n  {url}\nIt writes config/, .env and terraform.tfvars in this folder. "
          "Ctrl-C to stop.", flush=True)
    if not args.no_browser:
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")
    return 0
