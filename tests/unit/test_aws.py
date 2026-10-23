"""The AWS path with fake clients, offline: nua/tasks.py SqsQueue, nua/settings.py (QUEUE_BACKEND and
load_aws_secrets) and service/worker.py. No boto3 call reaches the network."""
from __future__ import annotations

import json
from collections import deque

import pytest
from botocore.exceptions import ClientError

from nua import settings
from nua.ports import TASK_KINDS
from nua.tasks import (NAME_HOLD_SECONDS, SQS_MAX_DELAY_SECONDS, EcsScaler, SqsQueue, decode_message,
                       encode_message, retry_delay, task_name)
from service.worker import Worker

QUEUE_URL = "https://sqs.us-west-2.amazonaws.com/123456789012/nua-tasks"


# ---------- fakes ----------

class FakeSqs:
    """An in-memory queue: send appends, receive hands out the first visible message once (or `None`)."""

    def __init__(self, depth: dict | None = None):
        self.sent: list[dict] = []
        self.inbox: deque[dict] = deque()
        self.deleted: list[str] = []
        self.visibility: list[tuple[str, int]] = []
        self.depth = depth or {}
        self.depth_calls = 0
        self.fail_send: Exception | None = None
        self._n = 0

    def send_message(self, **kwargs):
        if self.fail_send:
            raise self.fail_send
        self.sent.append(kwargs)
        self._n += 1
        self.inbox.append({"Body": kwargs["MessageBody"], "ReceiptHandle": f"r{self._n}",
                           "Attributes": {"ApproximateReceiveCount": "1"}})
        return {"MessageId": str(self._n)}

    def receive_message(self, **kwargs):
        assert kwargs["QueueUrl"] == QUEUE_URL and kwargs["MaxNumberOfMessages"] == 1
        return {"Messages": [self.inbox.popleft()]} if self.inbox else {}

    def delete_message(self, *, QueueUrl, ReceiptHandle):
        self.deleted.append(ReceiptHandle)

    def change_message_visibility(self, *, QueueUrl, ReceiptHandle, VisibilityTimeout):
        self.visibility.append((ReceiptHandle, VisibilityTimeout))

    def get_queue_attributes(self, *, QueueUrl, AttributeNames):
        self.depth_calls += 1
        depth = self.depth(self.depth_calls) if callable(self.depth) else self.depth
        return {"Attributes": {name: str(depth.get(name, 0)) for name in AttributeNames}}


def conditional_failed() -> ClientError:
    return ClientError({"Error": {"Code": "ConditionalCheckFailedException", "Message": "taken"}}, "PutItem")


class FakeDynamo:
    """put_item honours `attribute_not_exists(id) OR expires_at < :now`."""

    def __init__(self):
        self.items: dict[str, int] = {}
        self.deleted: list[str] = []

    def put_item(self, *, TableName, Item, ConditionExpression, ExpressionAttributeValues):
        assert ConditionExpression == "attribute_not_exists(id) OR expires_at < :now"
        key, now = Item["id"]["S"], int(ExpressionAttributeValues[":now"]["N"])
        if key in self.items and self.items[key] >= now:
            raise conditional_failed()
        self.items[key] = int(Item["expires_at"]["N"])

    def delete_item(self, *, TableName, Key):
        self.deleted.append(Key["id"]["S"])
        self.items.pop(Key["id"]["S"], None)


class FakeEcs:
    def __init__(self, fail: bool = False):
        self.calls: list[int] = []
        self.fail = fail

    def update_service(self, *, cluster, service, desiredCount):
        assert (cluster, service) == ("nua-cluster", "nua-worker")
        if self.fail:
            raise RuntimeError("ecs down")
        self.calls.append(desiredCount)


class Clock:
    def __init__(self, t: float = 1_800_000_000.0):
        self.t = t

    def __call__(self) -> float:
        return self.t


def make_queue(*, table: str | None = "nua-task-names", wake=None, clock=None):
    sqs, dynamo = FakeSqs(), FakeDynamo()
    q = SqsQueue(queue_url=QUEUE_URL, names_table=table, wake=wake, client=sqs, dynamodb=dynamo,
                 clock=clock or Clock())
    return q, sqs, dynamo


# ---------- SqsQueue ----------

def test_message_carries_kind_payload_and_name_and_delay():
    q, sqs, _ = make_queue()
    name = task_name("process_call", "transcripts/a.md")
    assert q.enqueue("process_call", {"transcript": "transcripts/a.md"}, name=name, delay_seconds=900) is True
    [msg] = sqs.sent
    assert msg["QueueUrl"] == QUEUE_URL and msg["DelaySeconds"] == 900
    assert json.loads(msg["MessageBody"]) == {"kind": "process_call", "payload": {"transcript": "transcripts/a.md"},
                                              "name": name}
    q.enqueue("sweep", {"granola_only": True})
    assert "DelaySeconds" not in sqs.sent[1]
    assert decode_message(sqs.sent[1]["MessageBody"]) == ("sweep", {"granola_only": True}, None)


def test_delays_past_15_minutes_ride_in_not_before():
    clock = Clock()
    q, sqs, _ = make_queue(clock=clock)
    q.enqueue("backcheck", {}, delay_seconds=3600)
    [msg] = sqs.sent
    assert msg["DelaySeconds"] == SQS_MAX_DELAY_SECONDS
    assert json.loads(msg["MessageBody"])["not_before"] == clock.t + 3600


def test_a_name_blocks_a_duplicate_for_an_hour_past_its_delay():
    clock = Clock()
    q, sqs, dynamo = make_queue(clock=clock)
    name = task_name("ask", "Ev123")
    assert q.enqueue("ask", {"q": 1}, name=name, delay_seconds=300) is True
    assert q.enqueue("ask", {"q": 1}, name=name) is False
    assert len(sqs.sent) == 1
    assert dynamo.items[name] == clock.t + 300 + NAME_HOLD_SECONDS
    clock.t += 300 + NAME_HOLD_SECONDS + 1          # expired, though DynamoDB's TTL may not have deleted it
    assert q.enqueue("ask", {"q": 1}, name=name) is True
    assert q.enqueue("ask", {"q": 1}, name=task_name("ask", "Ev124")) is True


def test_a_failed_send_releases_the_name():
    q, sqs, dynamo = make_queue()
    sqs.fail_send = RuntimeError("sqs down")
    with pytest.raises(RuntimeError):
        q.enqueue("reindex", {}, name="reindex-x")
    assert dynamo.deleted == ["reindex-x"] and "reindex-x" not in dynamo.items
    sqs.fail_send = None
    assert q.enqueue("reindex", {}, name="reindex-x") is True


def test_other_dynamodb_errors_raise():
    q, sqs, dynamo = make_queue()

    def broken(**_):
        raise ClientError({"Error": {"Code": "ProvisionedThroughputExceededException", "Message": "x"}}, "PutItem")
    dynamo.put_item = broken
    with pytest.raises(ClientError):
        q.enqueue("reindex", {}, name="n")
    assert sqs.sent == []


def test_without_a_table_names_are_not_checked():
    q, sqs, _ = make_queue(table=None)
    assert q.enqueue("reindex", {}, name="n") and q.enqueue("reindex", {}, name="n")
    assert len(sqs.sent) == 2


def test_every_send_wakes_the_worker_and_a_failed_wake_is_only_logged():
    ecs = FakeEcs()
    q, _, _ = make_queue(wake=EcsScaler(cluster="nua-cluster", service="nua-worker", client=ecs).wake)
    q.enqueue("sweep", {})
    q.enqueue("rollup", {})
    assert ecs.calls == [1, 1]
    q, sqs, _ = make_queue(wake=EcsScaler(cluster="nua-cluster", service="nua-worker", client=FakeEcs(fail=True)).wake)
    assert q.enqueue("sweep", {}) is True and len(sqs.sent) == 1


def test_a_duplicate_does_not_wake():
    ecs = FakeEcs()
    q, _, _ = make_queue(wake=EcsScaler(cluster="nua-cluster", service="nua-worker", client=ecs).wake)
    q.enqueue("sweep", {}, name="s")
    q.enqueue("sweep", {}, name="s")
    assert ecs.calls == [1]


def test_unknown_kind_is_refused_before_anything_is_sent():
    q, sqs, dynamo = make_queue()
    with pytest.raises(ValueError):
        q.enqueue("not_a_kind", {}, name="n")
    assert sqs.sent == [] and dynamo.items == {}


def test_backoff_matches_the_cloud_tasks_queue():
    assert [retry_delay(a) for a in range(7)] == [60, 120, 240, 480, 960, 1800, 1800]


def test_decode_refuses_anything_but_a_task():
    for body in ("not json", "[]", '{"payload": {}}', '{"kind": "sweep", "payload": [1]}'):
        with pytest.raises(ValueError):
            decode_message(body)
    assert decode_message(encode_message("rollup", {"bootstrap": True}, not_before=12.2)) == (
        "rollup", {"bootstrap": True}, 13.0)


def test_schedule_bodies_in_terraform_decode_as_tasks():
    """EventBridge Scheduler sends jsonencode({kind, payload}) from infra/aws/terraform/locals.tf."""
    from conftest import ROOT

    text = (ROOT / "infra" / "aws" / "terraform" / "locals.tf").read_text(encoding="utf-8")
    kinds = set(__import__("re").findall(r'kind = "(\w+)"', text))
    assert kinds == {"sweep", "rollup"} and kinds <= set(TASK_KINDS)


# ---------- settings: queue backend ----------

@pytest.fixture
def clean_env(monkeypatch):
    for name in ("QUEUE_BACKEND", "SQS_QUEUE_URL", "TASK_NAMES_TABLE", "WORKER_CLUSTER", "WORKER_SERVICE",
                 "GCP_PROJECT", "TASKS_LOCATION", "PROCESSOR_URL", "TASKS_SERVICE_ACCOUNT", settings.AWS_SECRETS_VAR):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-west-2")
    settings.reset()
    yield monkeypatch
    settings.reset()


def test_cloud_tasks_stays_the_default(clean_env):
    assert settings.queue_backend() == "cloudtasks"
    with pytest.raises(settings.SettingsError, match="GCP_PROJECT"):
        settings.ack_ctx()
    clean_env.setenv("QUEUE_BACKEND", "rabbit")
    with pytest.raises(settings.SettingsError, match="QUEUE_BACKEND"):
        settings.queue_backend()


def test_sqs_backend_needs_only_the_queue_url(clean_env):
    clean_env.setenv("QUEUE_BACKEND", "sqs")
    with pytest.raises(settings.SettingsError, match="SQS_QUEUE_URL"):
        settings.ack_ctx()
    settings.reset()
    clean_env.setenv("SQS_QUEUE_URL", QUEUE_URL)
    clean_env.setenv("TASK_NAMES_TABLE", "nua-task-names")
    clean_env.setenv("WORKER_CLUSTER", "nua-cluster")
    clean_env.setenv("WORKER_SERVICE", "nua-worker")
    ack = settings.ack_ctx()                          # no GCP variable needed
    queue = ack.queue._get()                          # built on first use; boto3 clients make no call here
    assert isinstance(queue, SqsQueue)
    assert queue.queue_url == QUEUE_URL and queue.names_table == "nua-task-names"
    assert queue.wake.__self__.service == "nua-worker"


def test_build_ctx_reports_the_sqs_variable_with_the_repo_ones(clean_env):
    clean_env.setenv("QUEUE_BACKEND", "sqs")
    for name in ("REPO_SLUG", "GITHUB_APP_ID", "GITHUB_APP_INSTALLATION_ID", "GITHUB_APP_PRIVATE_KEY"):
        clean_env.delenv(name, raising=False)
    with pytest.raises(settings.SettingsError) as e:
        settings.build_ctx("processor")
    assert "SQS_QUEUE_URL" in str(e.value) and "REPO_SLUG" in str(e.value) and "GCP_PROJECT" not in str(e.value)


# ---------- settings: load_aws_secrets ----------

class FakeSecrets:
    def __init__(self, values: dict[str, str]):
        self.values = values
        self.calls: list[list[str]] = []

    def batch_get_secret_value(self, *, SecretIdList):
        assert len(SecretIdList) <= 20
        self.calls.append(list(SecretIdList))
        found = [{"Name": s, "ARN": f"arn:aws:secretsmanager:us-west-2:1:secret:{s}-AbCdEf", "SecretString": self.values[s]}
                 for s in SecretIdList if s in self.values]
        errors = [{"SecretId": s, "ErrorCode": "ResourceNotFoundException", "Message": "no"}
                  for s in SecretIdList if s not in self.values]
        return {"SecretValues": found, "Errors": errors}


def test_secrets_load_into_the_environment(clean_env):
    clean_env.setenv(settings.AWS_SECRETS_VAR, "SLACK_BOT_TOKEN=nua/slack-bot-token, GRANOLA_API_KEY=nua/granola-api-key")
    clean_env.delenv("SLACK_BOT_TOKEN", raising=False)
    clean_env.setenv("GRANOLA_API_KEY", "already-set")            # a set variable wins
    client = FakeSecrets({"nua/slack-bot-token": "xoxb-1", "nua/granola-api-key": "never-used"})
    assert settings.load_aws_secrets(client=client) == ["SLACK_BOT_TOKEN"]
    import os
    assert os.environ["SLACK_BOT_TOKEN"] == "xoxb-1" and os.environ["GRANOLA_API_KEY"] == "already-set"
    assert client.calls == [["nua/slack-bot-token"]]


def test_secrets_are_fetched_20_at_a_time(clean_env):
    names = {f"VAR_{i}": f"nua/s{i}" for i in range(25)}
    for var in names:
        clean_env.delenv(var, raising=False)
    clean_env.setenv(settings.AWS_SECRETS_VAR, ",".join(f"{k}={v}" for k, v in names.items()))
    client = FakeSecrets({v: f"value-{v}" for v in names.values()})
    assert len(settings.load_aws_secrets(client=client)) == 25
    assert [len(c) for c in client.calls] == [20, 5]


def test_a_missing_secret_fails_the_start_without_printing_values(clean_env):
    clean_env.setenv(settings.AWS_SECRETS_VAR, "A_ONE=nua/one,A_TWO=nua/two")
    clean_env.delenv("A_ONE", raising=False)
    clean_env.delenv("A_TWO", raising=False)
    with pytest.raises(settings.SettingsError) as e:
        settings.load_aws_secrets(client=FakeSecrets({"nua/one": "s3cr3t-value"}))
    assert "nua/two (ResourceNotFoundException)" in str(e.value)
    assert "s3cr3t-value" not in str(e.value) and "nua/one" not in str(e.value)


def test_no_secret_list_is_a_no_op(clean_env):
    class Boom:
        def batch_get_secret_value(self, **_):
            raise AssertionError("must not be called")
    assert settings.load_aws_secrets(client=Boom()) == []
    clean_env.setenv(settings.AWS_SECRETS_VAR, "BROKEN")
    with pytest.raises(settings.SettingsError, match="ENV_VAR=secret-name"):
        settings.load_aws_secrets(client=Boom())


# ---------- worker ----------

def message(kind: str, payload: dict, *, receive_count: int = 1, receipt: str = "r1", **extra) -> dict:
    return {"Body": encode_message(kind, payload, **extra), "ReceiptHandle": receipt,
            "Attributes": {"ApproximateReceiveCount": str(receive_count)}}


def make_worker(handler, *, sqs=None, scaler=None, idle=60, now=None):
    ticks = iter(range(0, 10_000, 30))              # each clock read is 30 seconds later
    return Worker(sqs=sqs or FakeSqs(), queue_url=QUEUE_URL, handler=handler, scaler=scaler, idle_seconds=idle,
                  clock=lambda: next(ticks), now=now or Clock())


def test_a_finished_task_is_deleted_with_its_attempt_number():
    calls = []
    w = make_worker(lambda k, p, a, m: calls.append((k, p, a, m)) or {"ok": True})
    assert w.handle(message("process_call", {"transcript": "t.md"}, receive_count=3)) == "done"
    assert calls == [("process_call", {"transcript": "t.md"}, 2, 5)]
    assert w.sqs.deleted == ["r1"] and w.sqs.visibility == []


def test_a_failure_waits_the_backoff_and_is_not_deleted():
    def boom(kind, payload, attempt, most):
        raise RuntimeError("model down")
    w = make_worker(boom)
    assert w.handle(message("process_call", {}, receive_count=2, receipt="rx")) == "retry"
    assert w.sqs.visibility == [("rx", 120)] and w.sqs.deleted == []


def test_the_last_attempt_ends_the_task():
    """The runner reports a final failure and returns; the message must go, or SQS would retry it."""
    seen = []

    def final(kind, payload, attempt, most):
        seen.append(attempt >= most - 1)
        return {"ok": False, "final": True}
    w = make_worker(final)
    assert w.handle(message("reindex", {}, receive_count=5)) == "done"
    assert seen == [True] and w.sqs.deleted == ["r1"]


def test_a_body_that_is_not_a_task_is_dropped():
    w = make_worker(lambda *a: pytest.fail("must not run"))
    assert w.handle({"Body": "hello", "ReceiptHandle": "r9"}) == "dropped"
    assert w.sqs.deleted == ["r9"]


def test_a_task_not_due_yet_goes_back_with_the_rest_of_its_delay():
    now = Clock()
    w = make_worker(lambda *a: pytest.fail("must not run yet"), now=now)
    msg = message("backcheck", {"after": "x"}, name="backcheck-1", not_before=now.t + 2000)
    assert w.handle(msg) == "deferred"
    [sent] = w.sqs.sent
    assert sent["DelaySeconds"] == SQS_MAX_DELAY_SECONDS
    assert json.loads(sent["MessageBody"]) == {"kind": "backcheck", "payload": {"after": "x"}, "name": "backcheck-1",
                                               "not_before": now.t + 2000}
    assert w.sqs.deleted == ["r1"]
    now.t += 2001
    ran = []
    w.handler = lambda k, p, a, m: ran.append(k) or {"ok": True}
    assert w.handle(w.sqs.inbox.popleft()) == "done" and ran == ["backcheck"]


def test_idle_worker_scales_itself_to_zero_and_exits():
    ecs = FakeEcs()
    sqs = FakeSqs()
    sqs.inbox.append(message("sweep", {}))
    ran = []
    w = make_worker(lambda k, p, a, m: ran.append(k) or {"ok": True}, sqs=sqs,
                    scaler=EcsScaler(cluster="nua-cluster", service="nua-worker", client=ecs))
    assert w.run() == "idle"
    assert ran == ["sweep"] and ecs.calls == [0]


def test_work_arriving_while_scaling_in_takes_the_worker_back():
    ecs = FakeEcs()
    # first check: empty (scale in); second check, right after: a message arrived
    sqs = FakeSqs(depth=lambda n: {"ApproximateNumberOfMessages": 1} if n == 2 else {})
    w = make_worker(lambda k, p, a, m: {"ok": True}, sqs=sqs,
                    scaler=EcsScaler(cluster="nua-cluster", service="nua-worker", client=ecs))
    assert w.run() == "idle"
    assert ecs.calls == [0, 1, 0]


def test_delayed_work_keeps_the_worker_up():
    sqs = FakeSqs(depth=lambda n: {"ApproximateNumberOfMessagesDelayed": 1} if n <= 3 else {})
    w = make_worker(lambda *a: {"ok": True}, sqs=sqs)
    assert w.run() == "idle"
    assert sqs.depth_calls == 4


def test_stop_ends_the_loop():
    w = make_worker(lambda *a: {"ok": True})
    w.stop()
    assert w.run() == "stopped"


def test_queue_and_worker_together():
    """What the receiver enqueues is what the worker runs, retries included."""
    sqs = FakeSqs()
    q = SqsQueue(queue_url=QUEUE_URL, client=sqs)
    q.enqueue("apply_belief_change", {"request_file": "requests/a.yml"})
    attempts = []

    def flaky(kind, payload, attempt, most):
        attempts.append(attempt)
        if attempt == 0:
            raise RuntimeError("github down")
        return {"ok": True}
    w = make_worker(flaky, sqs=sqs)
    first = sqs.inbox.popleft()
    assert w.handle(first) == "retry"
    first["Attributes"]["ApproximateReceiveCount"] = "2"     # SQS redelivers after the visibility timeout
    assert w.handle(first) == "done"
    assert attempts == [0, 1] and sqs.deleted == [first["ReceiptHandle"]]
