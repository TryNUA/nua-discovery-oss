"""The AWS processor: an ECS Fargate task that runs queued tasks from SQS, one at a time.

On GCP Cloud Tasks pushes each task to the processor over HTTP. On AWS a task can run 30 minutes, past
Lambda's 15, so this worker pulls instead (docs/self-hosting-aws.md):

    python -m service.worker

Loop: long-poll SQS for one message, run it through the same `_run` the HTTP processor uses
(service/main.py, then nua.runner), and
- delete the message when the task ends: success, or a final failure the runner already reported to
  #discovery-ops (it returns {"ok": False, "final": True} on the last attempt, like the 200 on GCP);
- on a failure before the last attempt, leave it on the queue and set its visibility timeout to the
  Cloud Tasks backoff (60s doubling to 1800s), so SQS redelivers it then. The attempt number is SQS's
  ApproximateReceiveCount minus one; the DLQ (maxReceiveCount 5) only catches messages whose worker died
  every time, since the fifth attempt always ends the task;
- a message with a `not_before` in the future (a delay past SQS's 15 minutes) is sent back with the rest
  of the delay, and a body that is not a task is logged and dropped.

Scale to zero: the ECS service runs 0 or 1 task. An enqueue wakes it (nua.tasks.SqsQueue sets the
desired count to 1), and a queue-depth alarm does the same as a backstop. After WORKER_IDLE_SECONDS with
no message, and with nothing visible, in flight or delayed on the queue, the worker sets the desired count
to 0, checks the queue once more (and takes the work back if something arrived meanwhile), then exits.

SIGTERM (a deploy or a scale-in) stops the loop after the current task. ECS kills the task 120 seconds
later; a task still running then is redelivered when its visibility timeout ends.
"""
from __future__ import annotations

import json
import logging
import signal
import time
from typing import Any, Callable

from nua import settings
from nua.tasks import SQS_MAX_DELAY_SECONDS, EcsScaler, decode_message, encode_message, retry_delay

log = logging.getLogger("nua.worker")

WAIT_SECONDS = 20                     # SQS long poll, the most it allows
DEFAULT_IDLE_SECONDS = 120
DEFAULT_MAX_ATTEMPTS = 5
_DEPTH = ("ApproximateNumberOfMessages", "ApproximateNumberOfMessagesNotVisible",
          "ApproximateNumberOfMessagesDelayed")

# (kind, payload, attempt, max_attempts) -> result dict; raises to ask for a retry.
Handler = Callable[[str, dict, int, int], dict]


class Worker:
    """One SQS consumer. `sqs` is a boto3 SQS client (tests pass a fake); `scaler` an EcsScaler or None."""

    def __init__(self, *, sqs, queue_url: str, handler: Handler, scaler: EcsScaler | None = None,
                 idle_seconds: float = DEFAULT_IDLE_SECONDS, max_attempts: int = DEFAULT_MAX_ATTEMPTS,
                 clock: Callable[[], float] = time.monotonic, now: Callable[[], float] = time.time):
        self.sqs = sqs
        self.queue_url = queue_url
        self.handler = handler
        self.scaler = scaler
        self.idle_seconds = idle_seconds
        self.max_attempts = max_attempts
        self.clock = clock
        self.now = now
        self.stopping = False

    def stop(self, *_: Any) -> None:
        log.info("stop requested; finishing the current task")
        self.stopping = True

    # ---------- loop ----------

    def run(self) -> str:
        """Work until stopped or idle. Returns why it ended: "stopped" or "idle"."""
        idle_since = self.clock()
        while not self.stopping:
            message = self.receive()
            if message is not None:
                self.handle(message)
                idle_since = self.clock()
                continue
            if self.clock() - idle_since < self.idle_seconds:
                continue
            if not self.queue_empty():
                idle_since = self.clock()          # delayed or in-flight work: stay up for it
                continue
            if self.scale_to_zero():
                return "idle"
            idle_since = self.clock()
        return "stopped"

    def receive(self, wait: int = WAIT_SECONDS) -> dict | None:
        response = self.sqs.receive_message(QueueUrl=self.queue_url, MaxNumberOfMessages=1, WaitTimeSeconds=wait,
                                            MessageSystemAttributeNames=["ApproximateReceiveCount"])
        messages = response.get("Messages") or []
        return messages[0] if messages else None

    def queue_empty(self) -> bool:
        attrs = self.sqs.get_queue_attributes(QueueUrl=self.queue_url, AttributeNames=list(_DEPTH))
        values = attrs.get("Attributes") or {}
        return all(int(values.get(name, 0) or 0) == 0 for name in _DEPTH)

    def scale_to_zero(self) -> bool:
        """Set the service to 0 tasks, then look once more. True: exit now. False: work arrived, keep going."""
        if self.scaler is None:
            log.info("idle for %ss; exiting", self.idle_seconds)
            return True
        try:
            self.scaler.set(0)
        except Exception:
            log.exception("could not set the worker service to 0 tasks; exiting anyway (the scale-in alarm will)")
            return True
        if self.queue_empty():
            log.info("idle for %ss; service set to 0 tasks, exiting", self.idle_seconds)
            return True
        log.info("work arrived while scaling in; back to 1 task")
        try:
            self.scaler.set(1)
        except Exception:
            log.exception("could not set the worker service back to 1 task")
        return False

    # ---------- one message ----------

    def handle(self, message: dict) -> str:
        """Run one message. Returns what happened: done, retry, deferred or dropped."""
        receipt = message["ReceiptHandle"]
        try:
            kind, payload, not_before = decode_message(message.get("Body") or "")
        except ValueError as e:
            log.error("dropping a message that is not a task (%s): %.200s", e, message.get("Body"))
            self._delete(receipt)
            return "dropped"
        if not_before and not_before > self.now():
            self._defer(kind, payload, not_before, receipt, message.get("Body") or "")
            return "deferred"
        attempt = max(0, int((message.get("Attributes") or {}).get("ApproximateReceiveCount", 1)) - 1)
        try:
            result = self.handler(kind, payload, attempt, self.max_attempts)
        except Exception as e:
            delay = retry_delay(attempt)
            log.warning("task %s failed on attempt %d/%d, retrying in %ss: %s", kind, attempt + 1,
                        self.max_attempts, delay, e)
            try:
                self.sqs.change_message_visibility(QueueUrl=self.queue_url, ReceiptHandle=receipt,
                                                   VisibilityTimeout=delay)
            except Exception:
                log.exception("could not set the retry delay; SQS redelivers after the visibility timeout")
            return "retry"
        log.info("task %s ended: ok=%s", kind, (result or {}).get("ok"))
        self._delete(receipt)
        return "done"

    def _defer(self, kind: str, payload: dict, not_before: float, receipt: str, body: str) -> None:
        """Send a task back with the rest of its delay. It keeps its `not_before`, so it can hop again."""
        remaining = max(1, int(not_before - self.now()))
        name = (json.loads(body) or {}).get("name")
        self.sqs.send_message(QueueUrl=self.queue_url, DelaySeconds=min(remaining, SQS_MAX_DELAY_SECONDS),
                              MessageBody=encode_message(kind, payload, name=name, not_before=not_before))
        self._delete(receipt)

    def _delete(self, receipt: str) -> None:
        self.sqs.delete_message(QueueUrl=self.queue_url, ReceiptHandle=receipt)


def _int_env(name: str, default: int) -> int:
    try:
        return int(settings.env(name) or default)
    except ValueError:
        return default


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
    settings.load_aws_secrets()              # a no-op on ECS, which injects the secrets itself
    import boto3

    from service.main import _run

    queue_url = settings.require("SQS_QUEUE_URL")["SQS_QUEUE_URL"]
    cluster, service = settings.env("WORKER_CLUSTER"), settings.env("WORKER_SERVICE")
    scaler = EcsScaler(cluster=cluster, service=service) if cluster and service else None
    ctx_factory = lambda: settings.build_ctx("processor")  # noqa: E731
    worker = Worker(sqs=boto3.client("sqs"), queue_url=queue_url, scaler=scaler,
                    handler=lambda kind, payload, attempt, most: _run(ctx_factory, kind, payload, attempt, most),
                    idle_seconds=_int_env("WORKER_IDLE_SECONDS", DEFAULT_IDLE_SECONDS),
                    max_attempts=_int_env("NUA_TASK_MAX_ATTEMPTS", DEFAULT_MAX_ATTEMPTS))
    signal.signal(signal.SIGTERM, worker.stop)
    signal.signal(signal.SIGINT, worker.stop)
    log.info("worker started on %s", queue_url)
    why = worker.run()
    log.info("worker exiting: %s", why)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
