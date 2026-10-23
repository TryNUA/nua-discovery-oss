"""Task queues: Cloud Tasks on GCP, SQS on AWS, an inline queue for local runs.

All three implement ports.TaskQueue. Every Cloud Tasks task goes through `CloudTasksQueue.enqueue`, the one
shared helper that sets `dispatch_deadline` to 1800s (it is a per-task field, not a queue setting),
`schedule_time` for delays and an OIDC token so only the Cloud Tasks service account can call the processor.

`SqsQueue` maps the same interface onto SQS (docs/self-hosting-aws.md): the message body carries the
kind and payload, `DelaySeconds` the delay (SQS allows 15 minutes; longer delays ride in `not_before`
and the worker puts the message back until then), and names are claimed in a DynamoDB table for an
hour, as Cloud Tasks blocks a name. The deadline is the queue's visibility timeout plus the runner's
budget; retries and backoff are the worker's (service/worker.py).

Task names only block duplicates for about an hour, so correctness rests on each task's own idempotency check.
"""
from __future__ import annotations

import hashlib
import json
import logging
import math
import re
import time
from collections import deque
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from nua.ports import TASK_KINDS

log = logging.getLogger("nua.tasks")

DISPATCH_DEADLINE_SECONDS = 1800
MAX_NAME_LENGTH = 100
_UNSAFE = re.compile(r"[^A-Za-z0-9_-]+")

SQS_MAX_DELAY_SECONDS = 900          # SQS DelaySeconds limit
NAME_HOLD_SECONDS = 3600             # how long a task name blocks a duplicate (Cloud Tasks: about an hour)
MIN_BACKOFF_SECONDS = 60             # the Cloud Tasks queue's retry_config (infra/terraform/tasks.tf)
MAX_BACKOFF_SECONDS = 1800


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


# ---------- SQS (AWS) ----------

def retry_delay(attempt: int) -> int:
    """Seconds before retry number `attempt + 1`, as the Cloud Tasks queue spaces them: 60s doubling
    to at most 1800s (60, 120, 240, 480, 960)."""
    return int(min(MAX_BACKOFF_SECONDS, MIN_BACKOFF_SECONDS * 2 ** max(0, attempt)))


def encode_message(kind: str, payload: dict, *, name: str | None = None, not_before: float | None = None) -> str:
    """The SQS message body for one task. EventBridge Scheduler sends the same shape (infra/aws/terraform)."""
    body: dict[str, Any] = {"kind": kind, "payload": payload}
    if name:
        body["name"] = name
    if not_before:
        body["not_before"] = math.ceil(not_before)
    return json.dumps(body)


def decode_message(body: str) -> tuple[str, dict, float | None]:
    """(kind, payload, not_before) from an SQS message body. Raises ValueError on anything else."""
    data = json.loads(body)
    if not isinstance(data, dict) or not isinstance(data.get("kind"), str):
        raise ValueError("message has no kind")
    payload = data.get("payload") or {}
    if not isinstance(payload, dict):
        raise ValueError("message payload is not an object")
    not_before = data.get("not_before")
    return data["kind"], payload, float(not_before) if not_before else None


class EcsScaler:
    """Sets the desired count of the worker's ECS service: 1 to wake it for new work, 0 when it goes idle.
    `client` defaults to `boto3.client("ecs")`."""

    def __init__(self, *, cluster: str, service: str, client=None):
        self.cluster = cluster
        self.service = service
        if client is None:
            import boto3  # lazy: only the AWS deployment needs it
            client = boto3.client("ecs")
        self.client = client

    def set(self, desired: int) -> None:
        self.client.update_service(cluster=self.cluster, service=self.service, desiredCount=desired)

    def wake(self) -> None:
        self.set(1)


class SqsQueue:
    """ports.TaskQueue on Amazon SQS, for the AWS deployment.

    - Delay: `DelaySeconds`, up to SQS's 15 minutes. A longer delay sends the message with 15 minutes and
      a `not_before` time; the worker sends it back until then (service/worker.py).
    - Names: with `names_table`, a name is claimed with a conditional put in DynamoDB and blocks the same
      name for the delay plus an hour, like a Cloud Tasks task name; a second enqueue returns False. A
      failed send releases the claim. Without a table names are not checked (every enqueue returns True).
    - Deadline: SQS has none per message. The queue's visibility timeout (35 minutes) keeps a message
      from a second worker while the runner's 25-minute budget runs.
    - `wake()` is called after every send (best effort, errors are logged): it sets the worker's ECS
      service to one task, so work starts at once instead of when the queue-depth alarm notices.

    `client` and `dynamodb` default to boto3 clients; tests pass fakes with `send_message`, `put_item`
    and `delete_item`.
    """

    def __init__(self, *, queue_url: str, names_table: str | None = None, wake: Callable[[], None] | None = None,
                 client=None, dynamodb=None, clock: Callable[[], float] = time.time):
        self.queue_url = queue_url
        self.names_table = names_table
        self.wake = wake
        self.clock = clock
        if client is None or (names_table and dynamodb is None):
            import boto3  # lazy: only the AWS deployment needs it
            client = client or boto3.client("sqs")
            if names_table and dynamodb is None:
                dynamodb = boto3.client("dynamodb")
        self.client = client
        self.dynamodb = dynamodb

    def build_message(self, kind: str, payload: dict, *, name: str | None = None, delay_seconds: int = 0) -> dict:
        """The `send_message` arguments for one enqueue. Separate from `enqueue` so it can be inspected."""
        _check_kind(kind)
        delay = max(0, int(delay_seconds or 0))
        not_before = self.clock() + delay if delay > SQS_MAX_DELAY_SECONDS else None
        message: dict[str, Any] = {"QueueUrl": self.queue_url,
                                   "MessageBody": encode_message(kind, payload, name=name, not_before=not_before)}
        if delay:
            message["DelaySeconds"] = min(delay, SQS_MAX_DELAY_SECONDS)
        return message

    def enqueue(self, kind: str, payload: dict, *, name: str | None = None, delay_seconds: int = 0) -> bool:
        """Send the task. Returns False when a task with this `name` was enqueued within the last hour."""
        message = self.build_message(kind, payload, name=name, delay_seconds=delay_seconds)
        claimed = bool(name and self.names_table)
        if claimed and not self._claim(name, delay_seconds):
            return False
        try:
            self.client.send_message(**message)
        except Exception:
            if claimed:
                self._release(name)
            raise
        if self.wake is not None:
            try:
                self.wake()
            except Exception:
                log.warning("could not wake the worker; the queue-depth alarm will", exc_info=True)
        return True

    def _claim(self, name: str, delay_seconds: int) -> bool:
        now = int(self.clock())
        expires = now + max(0, int(delay_seconds or 0)) + NAME_HOLD_SECONDS
        try:
            self.dynamodb.put_item(
                TableName=self.names_table,
                Item={"id": {"S": name}, "expires_at": {"N": str(expires)}},
                # DynamoDB's TTL deletes expired items only eventually, so an expired claim is taken over here.
                ConditionExpression="attribute_not_exists(id) OR expires_at < :now",
                ExpressionAttributeValues={":now": {"N": str(now)}},
            )
        except Exception as e:
            if _error_code(e) == "ConditionalCheckFailedException":
                return False
            raise
        return True

    def _release(self, name: str) -> None:
        try:
            self.dynamodb.delete_item(TableName=self.names_table, Key={"id": {"S": name}})
        except Exception:
            log.warning("could not release task name %s; it blocks a re-enqueue for an hour", name, exc_info=True)


def _error_code(error: Exception) -> str | None:
    """The AWS error code of a botocore ClientError, else None."""
    response = getattr(error, "response", None)
    if isinstance(response, dict):
        return (response.get("Error") or {}).get("Code")
    return None


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
