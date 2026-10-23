"""Build a Ctx for each role from environment variables.

On Cloud Run every secret reaches the process as an environment variable (secret env refs, see
infra/terraform/cloud_run.tf), so this module only reads the environment. `.env` at the repo root is loaded only by
`python -m nua` (nua/__main__.py) and by the `local` role here; variables already set win. The
receiver, processor and mcp roles never read it: to run a service yourself, hand it to uvicorn with
`uv run uvicorn --env-file .env service.main:app` (see `.env.example`).

Roles:
- `local`: this working tree, no Slack, no git remote, tasks run inline (`tasks.InlineQueue`).
- `processor`: a managed shallow clone at `NUA_REPO_DIR`, synced before every task, GitHub App remote,
  Slack, Cloud Tasks.
- `receiver`: scales to zero, so it takes nothing on at startup. Routes that read repo files (recorder
  ingest, /github) call `build_ctx("receiver")`, which clones on first use and pulls again when the
  clone is older than RECEIVER_SYNC_SECONDS. Routes that only enqueue or answer Slack (/slack/interact,
  /sweep, /rollup) use `ack_ctx()`: the Slack client and the queue, never the repo (Slack gives the
  receiver 3 seconds, cold start included).
- `mcp`: the same clone; the MCP server syncs it itself when it is older than 60 seconds.

Environment variables (all names are listed in `.env.example`):
- repo: REPO_SLUG (owner/name), REPO_BRANCH (main), NUA_REPO_DIR (/tmp/nua-repo), NUA_REPO_ROOT (local)
- GitHub App: GITHUB_APP_ID, GITHUB_APP_INSTALLATION_ID, GITHUB_APP_PRIVATE_KEY, GITHUB_APP_SLUG,
  GITHUB_WEBHOOK_SECRET
- Slack: SLACK_BOT_TOKEN, SLACK_SIGNING_SECRET, NUA_SLACK_TEST=1 to send everything to the test channel
- Cloud Tasks: GCP_PROJECT, TASKS_LOCATION, TASKS_QUEUE (discovery), PROCESSOR_URL, TASKS_SERVICE_ACCOUNT
- Scheduler: RECEIVER_URL (OIDC audience), SCHEDULER_SERVICE_ACCOUNT; NUA_DEV=1 skips the OIDC check
- AWS instead of Cloud Tasks (docs/self-hosting-aws.md): QUEUE_BACKEND=sqs, SQS_QUEUE_URL, TASK_NAMES_TABLE
  (DynamoDB table for task names), WORKER_CLUSTER and WORKER_SERVICE (the ECS service an enqueue wakes);
  NUA_AWS_SECRETS lists the Secrets Manager secrets the Lambda receiver loads at startup (load_aws_secrets)
- recorders: GRANOLA_API_KEY, GRANOLA_WEBHOOK_SECRET, CIRCLEBACK_API_KEY_<PERSON>,
  CIRCLEBACK_SIGNING_SECRET_<PERSON> (person key in upper case, e.g. CIRCLEBACK_API_KEY_ALEX)
- models: ANTHROPIC_API_KEY, OPENAI_API_KEY, GEMINI_API_KEY (read by nua/providers.py)
"""
from __future__ import annotations

import logging
import os
import re
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from nua import config as config_mod
from nua.context import Ctx
from nua.llm import CostTracker, LLMFactory
from nua.ports import NullRemote, RepoFS, SlackIO, TaskQueue

if TYPE_CHECKING:
    from nua.config import Config
    from nua.ingest.circleback import CirclebackClient
    from nua.ingest.granola import GranolaClient

log = logging.getLogger("nua.settings")

ROLES = ("receiver", "processor", "mcp", "local")
DEFAULT_REPO_DIR = "/tmp/nua-repo"
DEFAULT_QUEUE = "nua"
RECEIVER_SYNC_SECONDS = 60

_REPO_VARS = ("REPO_SLUG", "GITHUB_APP_ID", "GITHUB_APP_INSTALLATION_ID", "GITHUB_APP_PRIVATE_KEY")
_QUEUE_VARS = ("GCP_PROJECT", "TASKS_LOCATION", "PROCESSOR_URL", "TASKS_SERVICE_ACCOUNT")
_SQS_VARS = ("SQS_QUEUE_URL",)
QUEUE_BACKENDS = ("cloudtasks", "sqs")


class SettingsError(RuntimeError):
    """A required environment variable is missing or a setting is malformed."""


# ---------- environment ----------

def env(name: str, default: str | None = None) -> str | None:
    """An environment variable, with empty values and push-secrets.sh's "placeholder" treated as unset,
    so a recorder or provider the team does not use is never called with a stand-in key."""
    value = os.environ.get(name)
    return value if value not in (None, "", "placeholder") else default


def require(*names: str) -> dict[str, str]:
    """The named variables; raises SettingsError listing every missing one."""
    missing = [n for n in names if env(n) is None]
    if missing:
        raise SettingsError(f"missing environment variables: {', '.join(missing)}")
    return {n: env(n) for n in names}


def queue_backend() -> str:
    """QUEUE_BACKEND: `cloudtasks` (the default, GCP) or `sqs` (AWS)."""
    backend = (env("QUEUE_BACKEND", "cloudtasks") or "cloudtasks").lower()
    if backend not in QUEUE_BACKENDS:
        raise SettingsError(f"QUEUE_BACKEND is {backend!r}; expected one of {', '.join(QUEUE_BACKENDS)}")
    return backend


def _queue_vars() -> tuple[str, ...]:
    """The variables the configured queue backend needs."""
    return _SQS_VARS if queue_backend() == "sqs" else _QUEUE_VARS


def is_dev() -> bool:
    """True when NUA_DEV=1 (local runs of the services; skips the Scheduler OIDC check)."""
    return env("NUA_DEV") == "1"


def person_var(prefix: str, person: str) -> str:
    """Per-person variable name: ("CIRCLEBACK_API_KEY", "alex") -> "CIRCLEBACK_API_KEY_ALEX"."""
    return f"{prefix}_{re.sub(r'[^A-Z0-9]', '_', person.upper())}"


AWS_SECRETS_VAR = "NUA_AWS_SECRETS"
_SECRETS_BATCH = 20                    # BatchGetSecretValue takes at most 20 ids


def load_aws_secrets(*, client=None) -> list[str]:
    """Fill environment variables from AWS Secrets Manager, once at startup (the Lambda receiver).

    NUA_AWS_SECRETS is `ENV_VAR=secret-name,...` (infra/aws/terraform/receiver.tf). Each secret's
    current value becomes os.environ[ENV_VAR], unless that variable is already set. Cloud Run and ECS
    put secrets in the environment themselves and set no NUA_AWS_SECRETS, so this is a no-op there.
    Raises SettingsError naming (never printing) every secret that could not be read, so a receiver
    with a missing secret fails at startup instead of on a webhook. Returns the variables it set.
    """
    spec = os.environ.get(AWS_SECRETS_VAR, "").strip()
    if not spec:
        return []
    wanted: dict[str, list[str]] = {}
    for item in (part.strip() for part in spec.split(",")):
        if not item:
            continue
        var, sep, secret = (s.strip() for s in item.partition("="))
        if not sep or not var or not secret:
            raise SettingsError(f"{AWS_SECRETS_VAR}: expected ENV_VAR=secret-name, got {item!r}")
        if os.environ.get(var):
            continue                   # already set (a local run, or a test) wins
        wanted.setdefault(secret, []).append(var)
    if not wanted:
        return []
    if client is None:
        import boto3  # lazy: only the AWS deployment needs it
        client = boto3.client("secretsmanager")
    ids = list(wanted)
    values: dict[str, str] = {}
    errors: dict[str, str] = {}
    for i in range(0, len(ids), _SECRETS_BATCH):
        response = client.batch_get_secret_value(SecretIdList=ids[i:i + _SECRETS_BATCH])
        for item in response.get("SecretValues") or []:
            for key in (item.get("Name"), item.get("ARN")):
                if key and item.get("SecretString") is not None:
                    values[key] = item["SecretString"]
        for e in response.get("Errors") or []:
            errors[str(e.get("SecretId"))] = str(e.get("ErrorCode") or "error")
    failed = [f"{s} ({errors.get(s, 'no value')})" for s in ids if s not in values]
    if failed:
        raise SettingsError("could not read secrets (run infra/aws/push-secrets.sh): " + ", ".join(failed))
    set_vars: list[str] = []
    for secret, names in wanted.items():
        for var in names:
            os.environ[var] = values[secret]
            set_vars.append(var)
    return set_vars


def repo_branch() -> str:
    """The branch the pipeline reads and writes (REPO_BRANCH, default main)."""
    return env("REPO_BRANCH", "main")


def github_bot_login() -> str | None:
    """The login GitHub shows for the app's own pushes ("<slug>[bot]"), or None when GITHUB_APP_SLUG is unset."""
    slug = env("GITHUB_APP_SLUG")
    return f"{slug}[bot]" if slug else None


# ---------- recorder clients ----------

def granola_client(*, http=None) -> "GranolaClient | None":
    """A Granola client on the workspace key, or None when GRANOLA_API_KEY is unset."""
    key = env("GRANOLA_API_KEY")
    if not key:
        return None
    from nua.ingest.granola import GranolaClient
    return GranolaClient(key, http=http)


def circleback_client(person: str, *, http=None) -> "CirclebackClient | None":
    """A Circleback client on one teammate's key, or None when CIRCLEBACK_API_KEY_<PERSON> is unset."""
    key = env(person_var("CIRCLEBACK_API_KEY", person))
    if not key:
        return None
    from nua.ingest.circleback import CirclebackClient
    return CirclebackClient(key, http=http)


def circleback_clients(config: "Config", *, http=None) -> dict[str, "CirclebackClient"]:
    """person key -> client, for every teammate in config/people.yml whose key is set."""
    out: dict[str, CirclebackClient] = {}
    for p in config.people:
        client = circleback_client(p.key, http=http)
        if client is not None:
            out[p.key] = client
    return out


# ---------- shared runtime (one per process) ----------

@dataclass
class _Clients:
    """What every role shares that needs no repo: the Slack client and the task queue."""
    slack: SlackIO | None
    queue: TaskQueue


@dataclass
class _Repo:
    """The managed clone and its git remote."""
    root: Path
    remote: object
    synced_at: float = 0.0


_clients: _Clients | None = None
_repo: _Repo | None = None
_clients_lock = threading.Lock()
_repo_lock = threading.Lock()          # held while cloning or pulling, so two requests never run git at once


def reset() -> None:
    """Forget the cached clone, clients and queue (tests, or after a settings change)."""
    global _clients, _repo
    with _clients_lock, _repo_lock:
        _clients = None
        _repo = None


def _slack() -> SlackIO | None:
    token = env("SLACK_BOT_TOKEN")
    if not token:
        return None
    from nua.slackio import SlackWebIO
    return SlackWebIO(token)


def _queue() -> TaskQueue:
    if queue_backend() == "sqs":
        return _sqs_queue()
    values = require(*_QUEUE_VARS)
    from nua.tasks import CloudTasksQueue
    return CloudTasksQueue(project=values["GCP_PROJECT"], location=values["TASKS_LOCATION"],
                           queue=env("TASKS_QUEUE", DEFAULT_QUEUE), processor_url=values["PROCESSOR_URL"],
                           service_account=values["TASKS_SERVICE_ACCOUNT"])


def _sqs_queue() -> TaskQueue:
    from nua.tasks import EcsScaler, SqsQueue

    url = require(*_SQS_VARS)["SQS_QUEUE_URL"]
    wake = None
    cluster, service = env("WORKER_CLUSTER"), env("WORKER_SERVICE")
    if cluster and service:
        wake = EcsScaler(cluster=cluster, service=service).wake
    return SqsQueue(queue_url=url, names_table=env("TASK_NAMES_TABLE"), wake=wake)


class _LazyQueue:
    """The Cloud Tasks queue, built on its first use. google.cloud.tasks_v2 pulls in grpc (about a second
    on a cold start), so a Slack click that only opens a form never pays for it."""

    def __init__(self, build):
        self._build = build
        self._queue: TaskQueue | None = None
        self._lock = threading.Lock()

    def _get(self) -> TaskQueue:
        with self._lock:
            if self._queue is None:
                self._queue = self._build()
            return self._queue

    def enqueue(self, kind: str, payload: dict, *, name: str | None = None, delay_seconds: int = 0) -> bool:
        return self._get().enqueue(kind, payload, name=name, delay_seconds=delay_seconds)

    def __getattr__(self, name: str):
        if name.startswith("_"):
            raise AttributeError(name)
        return getattr(self._get(), name)


def _get_clients() -> _Clients:
    """The Slack client and the queue (built on first enqueue), once per process. Never touches the
    repo or GitHub."""
    global _clients
    with _clients_lock:
        if _clients is None:
            require(*_queue_vars())        # fail at once on a missing setting, not at the first enqueue
            _clients = _Clients(slack=_slack(), queue=_LazyQueue(_queue))
        return _clients


def _get_repo() -> _Repo:
    """Clone the repo once per process (or reuse a managed clone already on disk)."""
    global _repo
    with _repo_lock:
        if _repo is not None:
            return _repo
        values = require(*_REPO_VARS)
        from nua import gitops

        auth = gitops.GitHubAppAuth(values["GITHUB_APP_ID"], values["GITHUB_APP_PRIVATE_KEY"],
                                    values["GITHUB_APP_INSTALLATION_ID"])
        root = Path(env("NUA_REPO_DIR", DEFAULT_REPO_DIR))
        if not (root / ".git").exists():
            root.parent.mkdir(parents=True, exist_ok=True)
            gitops.clone(values["REPO_SLUG"], root, auth.token())
        remote = gitops.GitRemote(root, repo_slug=values["REPO_SLUG"], token_fn=auth.token, branch=repo_branch())
        remote.sync()
        _repo = _Repo(root=root, remote=remote, synced_at=time.monotonic())
        return _repo


def _sync(repo: _Repo, *, max_age: float | None = None) -> None:
    """Pull the clone: always (`max_age` None), or only when the last pull is older than `max_age`
    seconds. The receiver passes RECEIVER_SYNC_SECONDS: with CPU allocated only during requests a
    background thread cannot keep the clone fresh, so a request that reads repo files pulls first.
    A failed on-demand pull is logged and the request reads the last good tree (the next one retries)."""
    with _repo_lock:
        if max_age is not None and time.monotonic() - repo.synced_at < max_age:
            return
        try:
            repo.remote.sync()
        except Exception:
            if max_age is None:
                raise
            log.exception("repo sync failed; reading the last good tree")
            return
        repo.synced_at = time.monotonic()


# ---------- the receiver's repo-free path ----------

def ack_ctx():
    """What /slack/interact, /sweep and /rollup need: the Slack client and the queue. No clone, no
    GitHub, no model SDK, so a cold-started receiver still answers Slack in 3 seconds."""
    from nua.interact import AckCtx

    clients = _get_clients()
    return AckCtx(slack=clients.slack, queue=clients.queue)


def _load_dotenv(root: Path) -> None:
    path = root / ".env"
    if not path.exists():
        return
    try:
        from dotenv import load_dotenv
    except ImportError:             # python-dotenv is a declared dependency; tolerate its absence
        return
    load_dotenv(path, override=False)


def _test_channels(cfg: "Config") -> None:
    """NUA_SLACK_TEST=1: every channel becomes the test channel (staging and go-live checks)."""
    if env("NUA_SLACK_TEST") == "1":
        test = cfg.pipeline.channels.get("test", "#discovery-test")
        cfg.pipeline.channels = {name: test for name in cfg.pipeline.channels}


# ---------- entry point ----------

def build_ctx(role: str) -> Ctx:
    """A fresh Ctx for one request or task.

    Config is re-read from the repo every time and each Ctx gets its own cost tracker (the per-task
    cap from config/pipeline.yml). The clone, git remote, Slack client and queue are shared per process.
    Raises ValueError for an unknown role and SettingsError when a required variable is missing.

    The receiver calls this only on routes that read repo files; it clones on first use and pulls
    when the clone is older than RECEIVER_SYNC_SECONDS. The processor's pull runs before the task's 25-minute budget starts; gitops' network timeout
    bounds it, so a stalled GitHub raises GitError here (service/main.py then retries, or alerts
    #discovery-ops on the last attempt) instead of running into Cloud Run's request timeout.
    """
    if role not in ROLES:
        raise ValueError(f"unknown role {role!r}; expected one of {', '.join(ROLES)}")
    if role == "local":
        return _local_ctx()

    require(*_REPO_VARS, *_queue_vars())    # report every missing name at once
    clients = _get_clients()
    repo = _get_repo()
    if role == "processor":
        _sync(repo)                   # every task starts from the tip of the branch
    elif role == "receiver":
        _sync(repo, max_age=RECEIVER_SYNC_SECONDS)
    cfg = config_mod.load(repo.root)
    _test_channels(cfg)
    return Ctx(config=cfg, repo=RepoFS(repo.root), llms=LLMFactory(CostTracker(cfg.pipeline.cost_cap_usd_per_task)),
               remote=repo.remote, slack=clients.slack, queue=clients.queue)


def _local_ctx() -> Ctx:
    root = Path(env("NUA_REPO_ROOT") or os.getcwd())
    _load_dotenv(root)
    cfg = config_mod.load(root)
    _test_channels(cfg)
    ctx = Ctx(config=cfg, repo=RepoFS(root), llms=LLMFactory(CostTracker(cfg.pipeline.cost_cap_usd_per_task)),
              remote=NullRemote(), slack=None)
    from nua import runner
    from nua.tasks import InlineQueue

    ctx.queue = InlineQueue(lambda kind, payload: runner.run_task(ctx, kind, payload, max_attempts=1, budget=False))
    return ctx
