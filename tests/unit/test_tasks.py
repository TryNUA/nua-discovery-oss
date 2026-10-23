"""Tests for discovery/tasks.py: task names, the Cloud Tasks queue (fake client) and the inline queue."""
from __future__ import annotations

import json
import re
import time
from datetime import timedelta

import pytest
from google.api_core import exceptions as gexc

from discovery.tasks import CloudTasksQueue, InlineQueue, task_name

NAME_RE = re.compile(r"^[A-Za-z0-9_-]+$")


# ---------- task_name ----------

@pytest.mark.parametrize("kind,key", [
    ("process_call", "transcripts/granola_not_1d3tmLijexWr3F.md"),
    ("apply_belief_change", "requests/2026-10-06_sam_footage-overwriting.yml@" + "a" * 40),
    ("backfill", "x" * 1000),
    ("sweep", "2026-10-01"),
    ("rollup", "rollup:2026-W40"),
    ("process_call", "transcripts/Café résumé (copy) #2.md"),
    ("reindex", ""),
])
def test_task_name_charset_and_length(kind, key):
    name = task_name(kind, key)
    assert NAME_RE.match(name), name
    assert len(name) <= 100
    assert name.startswith(kind + "-")


def test_task_name_is_deterministic_and_hashes_the_whole_key():
    a = task_name("process_call", "transcripts/a.md")
    assert a == task_name("process_call", "transcripts/a.md")
    assert a != task_name("process_call", "transcripts/b.md")
    assert a != task_name("backfill", "transcripts/a.md")
    # keys that only differ past any readable truncation still get different names
    long_a = "transcripts/" + "x" * 200 + "a.md"
    long_b = "transcripts/" + "x" * 200 + "b.md"
    assert task_name("process_call", long_a) != task_name("process_call", long_b)
    # keys that sanitize to the same readable text still differ
    assert task_name("process_call", "a/b") != task_name("process_call", "a_b")


def test_task_name_nonce_makes_it_unique_and_stays_valid():
    base = task_name("process_call", "transcripts/a.md")
    one = task_name("process_call", "transcripts/a.md", nonce="rerun 1700000000.000100")
    two = task_name("process_call", "transcripts/a.md", nonce="rerun 1700000000.000200")
    assert len({base, one, two}) == 3
    for n in (one, two):
        assert NAME_RE.match(n) and len(n) <= 100


def test_task_name_sanitizes_odd_kind():
    name = task_name("weird kind!", "k")
    assert NAME_RE.match(name) and len(name) <= 100


# ---------- CloudTasksQueue ----------

class FakeTasksClient:
    def __init__(self, fail_with: Exception | None = None):
        self.created: list[dict] = []
        self.fail_with = fail_with

    def create_task(self, *, parent, task):
        if self.fail_with:
            raise self.fail_with
        self.created.append({"parent": parent, "task": task})
        return task


def make_queue(client):
    return CloudTasksQueue(project="discovery-prod", location="us-west1", queue="discovery",
                           processor_url="https://processor-abc-uw.a.run.app/",
                           service_account="tasks@discovery-prod.iam.gserviceaccount.com", client=client)


def test_cloud_tasks_queue_builds_task_with_deadline_and_oidc():
    client = FakeTasksClient()
    q = make_queue(client)
    assert q.enqueue("process_call", {"transcript": "transcripts/a.md"}) is True
    [call] = client.created
    assert call["parent"] == "projects/discovery-prod/locations/us-west1/queues/discovery"
    task = call["task"]
    assert task.dispatch_deadline == timedelta(seconds=1800)
    req = task.http_request
    assert req.url == "https://processor-abc-uw.a.run.app/task/process_call"
    assert req.http_method.name == "POST"
    assert req.headers["Content-Type"] == "application/json"
    assert json.loads(req.body) == {"transcript": "transcripts/a.md"}
    assert req.oidc_token.service_account_email == "tasks@discovery-prod.iam.gserviceaccount.com"
    assert req.oidc_token.audience == "https://processor-abc-uw.a.run.app"
    assert not task.name
    # no delay: no schedule_time set
    assert not type(task).pb(task).HasField("schedule_time")


def test_cloud_tasks_queue_sets_every_task_deadline_and_name_and_delay():
    client = FakeTasksClient()
    q = make_queue(client)
    name = task_name("backcheck", "after:1700000000.000100")
    before = time.time()
    assert q.enqueue("backcheck", {}, name=name, delay_seconds=300) is True
    q.enqueue("sweep", {"granola_only": True})
    first, second = (c["task"] for c in client.created)
    assert first.dispatch_deadline == timedelta(seconds=1800)
    assert second.dispatch_deadline == timedelta(seconds=1800)
    assert first.name == f"projects/discovery-prod/locations/us-west1/queues/discovery/tasks/{name}"
    scheduled = first.schedule_time.timestamp()
    assert before + 299 <= scheduled <= time.time() + 301
    assert json.loads(second.http_request.body) == {"granola_only": True}


def test_cloud_tasks_queue_existing_name_returns_false():
    q = make_queue(FakeTasksClient(fail_with=gexc.AlreadyExists("task exists")))
    assert q.enqueue("process_call", {"transcript": "t"}, name="process_call-abc") is False


def test_cloud_tasks_queue_other_errors_raise():
    q = make_queue(FakeTasksClient(fail_with=gexc.ServiceUnavailable("down")))
    with pytest.raises(gexc.ServiceUnavailable):
        q.enqueue("process_call", {"transcript": "t"})


def test_cloud_tasks_queue_rejects_unknown_kind():
    client = FakeTasksClient()
    with pytest.raises(ValueError):
        make_queue(client).enqueue("not_a_kind", {})
    assert client.created == []


# ---------- InlineQueue ----------

def test_inline_queue_runs_handler_immediately():
    ran = []
    q = InlineQueue(lambda kind, payload: ran.append((kind, payload)))
    assert q.enqueue("reindex", {}) is True
    assert ran == [("reindex", {})]


def test_inline_queue_runs_nested_tasks_after_the_current_one():
    order = []
    q = None

    def handler(kind, payload):
        order.append(f"start {kind}")
        if kind == "backfill":
            for p in payload["paths"]:
                q.enqueue("process_call", {"transcript": p})
        order.append(f"end {kind}")

    q = InlineQueue(handler)
    q.enqueue("backfill", {"paths": ["a", "b"]})
    assert order == ["start backfill", "end backfill",
                     "start process_call", "end process_call",
                     "start process_call", "end process_call"]


def test_inline_queue_dedupes_names_and_rejects_unknown_kind():
    ran = []
    q = InlineQueue(lambda kind, payload: ran.append(kind))
    assert q.enqueue("sweep", {}, name="sweep-1") is True
    assert q.enqueue("sweep", {}, name="sweep-1") is False
    assert ran == ["sweep"]
    with pytest.raises(ValueError):
        q.enqueue("nope", {})


def test_inline_queue_recovers_after_handler_error():
    calls = []

    def handler(kind, payload):
        calls.append(kind)
        if payload.get("boom"):
            raise RuntimeError("boom")

    q = InlineQueue(handler)
    with pytest.raises(RuntimeError):
        q.enqueue("reindex", {"boom": True})
    assert q.enqueue("sweep", {}) is True
    assert calls == ["reindex", "sweep"]
