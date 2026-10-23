"""Task queues: Cloud Tasks in production, an inline queue for local runs.

Both implement ports.TaskQueue. Every Cloud Tasks task goes through `CloudTasksQueue.enqueue`, the one
shared helper that sets `dispatch_deadline` to 1800s (it is a per-task field, not a queue setting),
`schedule_time` for delays and an OIDC token so only the Cloud Tasks service account can call the processor.

Task names only block duplicates for about an hour, so correctness rests on each task's own idempotency check.
"""
from __future__ import annotations

import hashlib
import json
import re
import time
from collections import deque
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from discovery.ports import TASK_KINDS

DISPATCH_DEADLINE_SECONDS = 1800
MAX_NAME_LENGTH = 100
_UNSAFE = re.compile(r"[^A-Za-z0-9_-]+")


def _slug(text: str, limit: int) -> str:
    """Readable part of a task name: allowed characters only, runs collapsed, trimmed."""
    return _UNSAFE.sub("-", text).strip("-")[:limit].strip("-")


def task_name(kind: str, key: str, *, nonce: str | None = None) -> str:
    """Deterministic Cloud Tasks task id for (kind, key), e.g. `process_call-3f9a...-transcripts-granola_x-md`.

    Only [A-Za-z0-9_-], at most 100 characters. The hash covers the whole key (and the nonce), so long
    keys that share a prefix still get different names. Pass a `nonce` (for example a Slack message ts)
    to get a fresh name for a deliberate re-run.
    """
    kind_part = _slug(kind, 24) or "task"
    material = key if nonce is None else f"{key}\x00{nonce}"
    digest = hashlib.sha256(material.encode("utf-8")).hexdigest()[:24]
    name = f"{kind_part}-{digest}"
    readable = _slug(key, MAX_NAME_LENGTH - len(name) - 1)
    return f"{name}-{readable}" if readable else name


def _check_kind(kind: str) -> None:
    if kind not in TASK_KINDS:
        raise ValueError(f"unknown task kind {kind!r}; expected one of {', '.join(TASK_KINDS)}")


class CloudTasksQueue:
    """ports.TaskQueue on Google Cloud Tasks. Each task is an HTTP POST to `<processor_url>/task/<kind>`
    with the payload as a JSON body.

    `processor_url` is the processor service's base URL (it is also the OIDC audience Cloud Run checks).
    `client` defaults to `google.cloud.tasks_v2.CloudTasksClient()`; tests pass a fake with `create_task`.
    """

    def __init__(self, *, project: str, location: str, queue: str, processor_url: str, service_account: str,
                 client=None):
        self.project = project
        self.location = location
        self.queue = queue
        self.processor_url = processor_url.rstrip("/")
        self.service_account = service_account
        if client is None:
            from google.cloud import tasks_v2  # lazy: pulls in grpc
            client = tasks_v2.CloudTasksClient()
        self.client = client

    @property
    def parent(self) -> str:
        return f"projects/{self.project}/locations/{self.location}/queues/{self.queue}"

    def build_task(self, kind: str, payload: dict, *, name: str | None = None, delay_seconds: int = 0):
        """The `tasks_v2.Task` for one enqueue. Separate from `enqueue` so it can be inspected."""
        from google.cloud import tasks_v2

        _check_kind(kind)
        fields: dict[str, Any] = {
            "http_request": tasks_v2.HttpRequest(
                http_method=tasks_v2.HttpMethod.POST,
                url=f"{self.processor_url}/task/{kind}",
                headers={"Content-Type": "application/json"},
                body=json.dumps(payload).encode("utf-8"),
                oidc_token=tasks_v2.OidcToken(service_account_email=self.service_account,
                                              audience=self.processor_url),
            ),
            "dispatch_deadline": timedelta(seconds=DISPATCH_DEADLINE_SECONDS),
        }
        if name:
            fields["name"] = f"{self.parent}/tasks/{name}"
        if delay_seconds and delay_seconds > 0:
            fields["schedule_time"] = datetime.fromtimestamp(time.time() + delay_seconds, tz=timezone.utc)
        return tasks_v2.Task(**fields)

    def enqueue(self, kind: str, payload: dict, *, name: str | None = None, delay_seconds: int = 0) -> bool:
        """Create the task. Returns False when a task with this `name` already exists (or recently ran)."""
        from google.api_core import exceptions as gexc

        task = self.build_task(kind, payload, name=name, delay_seconds=delay_seconds)
        try:
            self.client.create_task(parent=self.parent, task=task)
        except gexc.AlreadyExists:
            return False
        return True


class InlineQueue:
    """ports.TaskQueue for local runs: runs `handler(kind, payload)` right away, one task at a time.

    A task enqueued from inside a running task waits until that task finishes, like the real queue with
    `maxConcurrentDispatches: 1`. Delays are ignored. Names dedupe for the life of the queue.
    """

    def __init__(self, handler: Callable[[str, dict], None]):
        self.handler = handler
        self.names: set[str] = set()
        self.ran: list[dict] = []
        self._pending: deque[tuple[str, dict]] = deque()
        self._running = False

    def enqueue(self, kind: str, payload: dict, *, name: str | None = None, delay_seconds: int = 0) -> bool:
        """Queue the task and, unless a task is already running, run everything queued."""
        _check_kind(kind)
        if name:
            if name in self.names:
                return False
            self.names.add(name)
        self._pending.append((kind, payload))
        if not self._running:
            self._drain()
        return True

    def _drain(self) -> None:
        self._running = True
        try:
            while self._pending:
                kind, payload = self._pending.popleft()
                self.ran.append({"kind": kind, "payload": payload})
                self.handler(kind, payload)
        finally:
            self._running = False
