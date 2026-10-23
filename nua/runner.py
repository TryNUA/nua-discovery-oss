"""Run one queued task on the processor (SPEC "Task types", "Degraded modes and failures").

`run_task` dispatches by kind to the module that does the work, enforces the 25-minute budget and
applies the failure rule:
- a failure before the last attempt re-raises, so the HTTP handler answers 500 and Cloud Tasks retries;
- a failure on the last attempt, a cost-cap stop, or a task that can never succeed posts to
  #discovery-ops (with a Re-run button when a re-run could help), writes the `failed` stub or
  `reprocess_failed` mark for `process_call`, and returns {"ok": False} so the handler answers 200.

Task payloads:
- process_call: {"transcript": path, "force"?: bool, "batch"?: batch id}
- backfill: {"paths": [...], "batch_id": head commit}; with only "batch_id" (a Re-run whose button could
  not carry the list), paths come from the batch file, or from the transcripts no digest points to when
  the batch file was never committed
- apply_belief_change: whatever `nua.apply.apply_task` takes: {"request_file": "requests/<file>.yml"} from
  /github, the sweep and the MCP server, or {"ref", "change": {...}, "via": "slack", "slack_item": {...}}
  from Slack (built by nua/interact.py)
- backcheck: {}, or {"after": ref} from nua/apply.py, which turns on the debounce
- reindex: {}
- rollup: {"bootstrap"?: bool, "week"?: ISO week}
- sweep: {"granola_only"?: bool, "last_sweep"?: ISO datetime}

The task modules (process, apply, backcheck, indexes, rollup, sweep) are imported when a task needs them.
"""
from __future__ import annotations

import dataclasses
import importlib
import json
import logging
import re
import threading
import time
from typing import Any, Callable

from nua import settings
from nua.context import Ctx
from nua.llm import CostCapExceeded

log = logging.getLogger("nua.runner")

BUDGET_SECONDS = 25 * 60         # Cloud Run and the task dispatch deadline allow 30 minutes
MAX_BUTTON_VALUE = 2000          # Slack's limit for a button value

_SLACK_METHODS = ("post", "update", "delete", "open_view", "update_view", "respond", "ephemeral")
_EM_DASH = chr(0x2014)


class TaskTimeout(Exception):
    """The task ran past its budget. Side effects it attempts after the deadline raise this too."""


class InvalidTask(Exception):
    """The task can never succeed as queued (unknown kind, missing field). It is not retried."""


# ---------- entry point ----------

def run_task(ctx: Ctx, kind: str, payload: dict, *, attempt: int = 0, max_attempts: int = 5,
             budget: bool = True) -> dict:
    """Run one task and return the JSON body for the processor's response.

    `attempt` is Cloud Tasks' retry count (0 on the first try). Returns {"ok": True, "kind", "result"}
    on success and {"ok": False, "kind", "error", "final": True} once the task has given up. Raises on
    an earlier attempt's failure so Cloud Tasks retries. With `budget` (the default), the work runs in a
    worker thread that is abandoned at BUDGET_SECONDS; after that deadline its repo writes, pushes, Slack
    calls, enqueues and model calls raise TaskTimeout, so a late finish cannot commit or post anything.
    """
    payload = dict(payload or {})
    final = attempt >= max_attempts - 1
    ctx.info(f"task {kind} attempt {attempt + 1}/{max_attempts}")
    try:
        handler = _HANDLERS.get(kind)
        if handler is None:
            raise InvalidTask(f"unknown task kind {kind!r}")
        if budget:
            result = _within_budget(ctx, lambda c: handler(c, payload), BUDGET_SECONDS)
        else:
            result = handler(ctx, payload)
    except InvalidTask as e:
        return _give_up(ctx, kind, payload, e, attempt, max_attempts, rerun=False)
    except CostCapExceeded as e:
        return _give_up(ctx, kind, payload, e, attempt, max_attempts, rerun=True)
    except Exception as e:
        if not final:
            log.warning("task %s failed on attempt %d/%d: %s", kind, attempt + 1, max_attempts, e)
            raise
        return _give_up(ctx, kind, payload, e, attempt, max_attempts, rerun=True)
    ctx.info(f"task {kind} done")
    return {"ok": True, "kind": kind, "result": _jsonable(result)}


# ---------- handlers ----------

def _module(name: str):
    """nua.<name>, imported on first use (tests swap in stubs through sys.modules)."""
    return importlib.import_module(f"nua.{name}")


def _process_call(ctx: Ctx, payload: dict) -> Any:
    transcript = payload.get("transcript") or payload.get("path")
    if not transcript:
        raise InvalidTask("process_call needs a transcript path")
    return _module("process").process_call(ctx, transcript, force=bool(payload.get("force")),
                                           batch=payload.get("batch") or payload.get("batch_id"))


def _backfill(ctx: Ctx, payload: dict) -> dict:
    """Start a backfill batch. Without `paths` (a Re-run whose button could not carry them) the list comes
    from the batch file, or, when that was never committed (a failed push; the next sync deletes it), from
    the transcripts no digest points to: what the sweep would process one by one, now with a batch summary."""
    process = _module("process")
    batch_id = payload.get("batch_id") or payload.get("batch")
    paths = payload.get("paths")
    if paths is None and batch_id:
        paths = process.load_batch(ctx, str(batch_id))
        if not paths:
            paths = _undigested_transcripts(ctx)
            ctx.info(f"backfill {batch_id}: no batch file, {len(paths)} transcripts with no digest")
            if not paths:
                return {"batch_id": batch_id, "paths": 0,
                        "note": "no batch file and every transcript has a digest: nothing to do"}
    if not batch_id or not paths:
        raise InvalidTask("backfill needs a batch_id and paths (or an existing batch file)")
    process.start_backfill(ctx, list(paths), str(batch_id))
    return {"batch_id": batch_id, "paths": len(paths)}


def _undigested_transcripts(ctx: Ctx) -> list[str]:
    """Files directly in transcripts/ (dotfiles skipped) that no digest or stub points to, sorted."""
    from nua.digest import digest_for_transcript, load_digests

    digests = load_digests(ctx.repo)
    return [rel for rel in ctx.repo.list("transcripts/*")
            if not rel.rsplit("/", 1)[-1].startswith(".") and digest_for_transcript(digests, rel) is None]


def _apply_belief_change(ctx: Ctx, payload: dict) -> Any:
    has_file = any(isinstance(payload.get(k), str) and payload.get(k) for k in ("request_file", "request", "path"))
    if not has_file and not payload.get("ref"):
        raise InvalidTask("apply_belief_change needs a request_file, or a ref and a change")
    return _module("apply").apply_task(ctx, payload)


def _backcheck(ctx: Ctx, payload: dict) -> Any:
    return _module("backcheck").backcheck(ctx, payload)


def _reindex(ctx: Ctx, payload: dict) -> dict:
    """Rewrite digests whose metadata config/overrides.yml corrects, regenerate every index, commit.

    A digest file that cannot be read is skipped (it is fixed by hand, never rewritten) and named in
    the result's `unreadable`, so one malformed file never stops the reindex. Corrections in
    config/overrides.yml that apply_overrides skips (digest.override_problems) are named in the result's
    `override_problems` and posted to #discovery-ops once the reindex is committed."""
    from nua import digest as digest_mod

    indexes = _module("indexes")
    changed: list[str] = []
    digests = digest_mod.load_digests(ctx.repo)
    for d in digests:
        fixed = digest_mod.apply_overrides(d, ctx.config.overrides)
        if fixed.to_dict() != d.to_dict():
            changed.append(digest_mod.write_digest(ctx.repo, fixed))
    unreadable = digest_mod.unreadable_paths(digests)
    for rel in unreadable:
        ctx.info(f"reindex: {rel} cannot be read, skipped (fix it by hand)")
    problems = digest_mod.override_problems(ctx.config.overrides)
    for line in problems:
        ctx.info(f"reindex: {line}")
    generated = list(indexes.regenerate(ctx) or [])
    sha = None
    if not ctx.dry_run:
        sha = ctx.remote.commit_and_push("reindex: apply overrides, regenerate indexes", changed + generated,
                                         regenerate=lambda: indexes.regenerate(ctx))
    _post_override_problems(ctx, problems)
    return {"digests_updated": changed, "generated": generated, "commit": sha, "unreadable": unreadable,
            "override_problems": problems}


def _post_override_problems(ctx: Ctx, problems: list[str]) -> None:
    """One #discovery-ops message naming the config/overrides.yml corrections the reindex skipped. The
    reindex is already committed, so a Slack failure is only logged."""
    if not problems or ctx.slack is None or ctx.dry_run:
        return
    head = "Reindex skipped corrections in config/overrides.yml"
    lines = [_one_line(p) for p in problems]
    mrkdwn = f"*{_esc(head)}*\n" + "\n".join(f"- {_esc(line)}" for line in lines)
    try:
        ctx.slack.post(ctx.channel("ops"), [_section("override_problems", mrkdwn)], f"{head}: " + " ".join(lines))
    except Exception:
        log.exception("could not post the reindex's override problems to Slack")


def _rollup(ctx: Ctx, payload: dict) -> Any:
    return _module("rollup").run_rollup(ctx, bootstrap=bool(payload.get("bootstrap")), week=payload.get("week"))


def _sweep(ctx: Ctx, payload: dict) -> Any:
    granola_only = bool(payload.get("granola_only"))
    granola = settings.granola_client()
    circleback = None if granola_only else settings.circleback_clients(ctx.config)
    return _module("sweep").run_sweep(ctx, granola=granola, circleback=circleback, granola_only=granola_only,
                                      last_sweep=payload.get("last_sweep"))


def _ask(ctx: Ctx, payload: dict) -> dict:
    return _module("slack_ask").run(ctx, payload)


_HANDLERS: dict[str, Callable[[Ctx, dict], Any]] = {
    "process_call": _process_call,
    "backfill": _backfill,
    "apply_belief_change": _apply_belief_change,
    "backcheck": _backcheck,
    "reindex": _reindex,
    "rollup": _rollup,
    "sweep": _sweep,
    "ask": _ask,
}


# ---------- giving up ----------

def _give_up(ctx: Ctx, kind: str, payload: dict, error: Exception, attempt: int, max_attempts: int, *,
             rerun: bool) -> dict:
    message = _error_text(error)
    log.error("task %s gave up on attempt %d/%d: %s", kind, attempt + 1, max_attempts, message)
    if kind == "process_call" and payload.get("transcript"):
        try:
            _module("process").mark_final_failure(ctx, payload["transcript"], message)
        except Exception:
            log.exception("mark_final_failure failed for %s", payload["transcript"])
    try:
        post_failure(ctx, kind, payload, message, attempt=attempt, max_attempts=max_attempts, rerun=rerun)
    except Exception:
        log.exception("could not post the failure of %s to Slack", kind)
    return {"ok": False, "kind": kind, "error": message, "final": True}


def post_failure(ctx: Ctx, kind: str, payload: dict, error: str, *, attempt: int, max_attempts: int,
                 rerun: bool = True) -> str | None:
    """Post a failed task to #discovery-ops, with a Re-run button carrying the task. Returns the ts."""
    if ctx.slack is None or ctx.dry_run:
        return None
    subject = _one_line(_subject(payload))       # a dropped file's name may carry an em dash; the button keeps it
    head = f"Task failed: {kind}" + (f" {subject}" if subject else "")
    value = fit_rerun_value(kind, payload) if rerun else None
    note = " No Re-run: the task is too large for a button, so the change must be redone." \
        if rerun and value is None else ""
    detail = _one_line(f"Attempt {attempt + 1} of {max_attempts}. {error}{note}")
    blocks = [_section("failure", f"*{_esc(head)}*\n{_esc(detail)}")]
    if value is not None:
        blocks.append({"type": "actions", "block_id": "rerun", "elements": [{
            "type": "button", "action_id": "rerun", "text": {"type": "plain_text", "text": "Re-run"},
            "value": value}]})
    return ctx.slack.post(ctx.channel("ops"), blocks, f"{head}. {detail}")


# Payload fields a Re-run can never do without: dropping one would re-run a different task.
RERUN_ESSENTIAL = ("change",)
# What apply needs to find the clicked Slack item again (it re-reads a thread reply by ts).
SLACK_ITEM_KEYS = ("channel", "ts", "block_id", "thread_ts")


def fit_rerun_value(kind: str, payload: dict) -> str | None:
    """Button value for Re-run: {"kind", "payload"} as JSON, forced for process_call, under Slack's
    2000-character limit. None when even the trimmed task does not fit.

    Trimmed in this order until it fits: the copy of the clicked message in `slack_item` (its blocks
    and text; channel, ts, block_id and thread_ts stay), then every other list or dict field (a
    backfill's `paths` reload from the batch file, or from the undigested transcripts when it was
    never committed; see `_backfill`), then `slack_item` itself. Scalars (`ref`,
    `transcript`, `batch_id`, ...) and `change` are never dropped.
    """
    p = dict(payload)
    if kind == "process_call":
        p["force"] = True
    trials = [p]
    item = p.get("slack_item")
    if isinstance(item, dict):
        p = {**p, "slack_item": {k: item[k] for k in SLACK_ITEM_KEYS if k in item}}
        trials.append(p)
    p = {k: v for k, v in p.items()
         if k in RERUN_ESSENTIAL or k == "slack_item" or not isinstance(v, (list, dict))}
    trials.append(p)
    trials.append({k: v for k, v in p.items() if k != "slack_item"})
    for trial in trials:
        value = json.dumps({"kind": kind, "payload": trial})
        if len(value) <= MAX_BUTTON_VALUE:
            return value
    return None


def rerun_value(kind: str, payload: dict) -> str:
    """`fit_rerun_value`, or {"kind", "payload": {}} when the task cannot fit. Prefer `fit_rerun_value`
    and post no button on None: an empty payload re-runs as an invalid task."""
    return fit_rerun_value(kind, payload) or json.dumps({"kind": kind, "payload": {}})


def _subject(payload: dict) -> str:
    for key in ("transcript", "request_file", "batch_id", "ref"):
        if isinstance(payload.get(key), str) and payload[key]:
            return payload[key]
    return ""


def _error_text(error: Exception) -> str:
    if isinstance(error, CostCapExceeded):
        text = f"Stopped at the per-task cost cap ({error})"
    elif isinstance(error, (TaskTimeout, InvalidTask)):
        text = str(error)
    else:
        text = f"{type(error).__name__}: {error}"
    return _one_line(text)[:500]


# ---------- budget ----------

class _Guard:
    """Forwards everything to `inner`; the named methods first check the task deadline."""

    def __init__(self, inner: Any, check: Callable[[], None], methods: tuple[str, ...]):
        object.__setattr__(self, "_inner", inner)
        object.__setattr__(self, "_check", check)
        object.__setattr__(self, "_methods", methods)

    def __getattr__(self, name: str) -> Any:
        attr = getattr(self._inner, name)
        if name in self._methods and callable(attr):
            check = self._check

            def guarded(*args, **kwargs):
                check()
                return attr(*args, **kwargs)
            return guarded
        return attr

    def __setattr__(self, name: str, value: Any) -> None:
        setattr(self._inner, name, value)


class _GuardedLLMs(_Guard):
    """The model factory: resolving a model after the deadline raises TaskTimeout."""

    def __call__(self, spec: str, **options: Any):
        """`options` (timeout, delays) pass through to LLMFactory, e.g. the Slack router's short timeout."""
        self._check()
        return self._inner(spec, **options)


def _within_budget(ctx: Ctx, fn: Callable[[Ctx], Any], seconds: float) -> Any:
    deadline = time.monotonic() + seconds
    stopped = f"stopped after the task budget of {_duration(seconds)}"

    def check() -> None:
        if time.monotonic() > deadline:
            raise TaskTimeout(stopped)

    guarded = dataclasses.replace(
        ctx,
        repo=_Guard(ctx.repo, check, ("write", "delete")),
        remote=_Guard(ctx.remote, check, ("sync", "create_file_only", "commit_and_push")),
        slack=_Guard(ctx.slack, check, _SLACK_METHODS) if ctx.slack is not None else None,
        queue=_Guard(ctx.queue, check, ("enqueue",)) if ctx.queue is not None else None,
        llms=_GuardedLLMs(ctx.llms, check, ()),
    )
    box: dict[str, Any] = {}

    def work() -> None:
        try:
            box["result"] = fn(guarded)
        except BaseException as e:           # handed back to the caller's thread
            box["error"] = e

    worker = threading.Thread(target=work, name="nua-task", daemon=True)
    worker.start()
    worker.join(seconds)
    if worker.is_alive():
        raise TaskTimeout(stopped)
    if "error" in box:
        raise box["error"]
    return box.get("result")


def _duration(seconds: float) -> str:
    if seconds >= 60 and seconds % 60 == 0:
        return f"{int(seconds // 60)} minutes"
    return f"{seconds:g} seconds"


# ---------- small helpers ----------

def _jsonable(value: Any) -> Any:
    """A task result as plain JSON data (dataclasses become dicts, anything odd becomes a string)."""
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        value = dataclasses.asdict(value)
    try:
        json.dumps(value)
        return value
    except TypeError:
        return json.loads(json.dumps(value, default=str))


def _one_line(text: str) -> str:
    return re.sub(r"\s+", " ", text.replace(_EM_DASH, "-")).strip()


def _esc(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _section(block_id: str, mrkdwn: str) -> dict:
    return {"type": "section", "block_id": block_id, "text": {"type": "mrkdwn", "text": mrkdwn[:3000]}}
