"""Cloud Run apps: the public receiver and the private processor (SPEC "Receiver endpoints").

One image is deployed three times; SERVICE_ROLE picks the app (`receiver`, `processor` or `mcp`):

    uvicorn service.main:app

Run locally with the settings from .env (the services never load .env themselves, and SERVICE_ROLE is
read at import):

    uvicorn --env-file .env service.main:app

The receiver scales to zero, with CPU only while a request is open, so it starts nothing at startup
and runs nothing after a response. Each route takes only what it needs, on demand:
- repo routes (/ingest/*, /github) call `ctx_factory()` (settings.build_ctx("receiver")), which clones
  the repo on first use and pulls when the clone is older than settings.RECEIVER_SYNC_SECONDS; /github
  only once a push has work in it.
- the other routes (/slack/interact, /sweep, /rollup) call `ack_factory()` (settings.ack_ctx): the
  Slack client, the queue and the teammates, never the repo, GitHub or a model SDK, so a Slack click
  is answered within 3 seconds even on a cold start.
Heavy modules (git, recorder clients, google-auth, model SDKs) are imported inside the functions that
use them, so importing this module stays light.

Receiver routes:
- POST /ingest/granola: verify the Standard Webhooks signature, fetch the note, create
  transcripts/granola_<id>.md (create-only), enqueue process_call. `note.edited` creates the next
  version only when the transcript got longer.
- POST /ingest/circleback/<person>: verify the x-signature HMAC, ignore meetings not tagged `discovery`,
  look up the calendar start, create transcripts/circleback_<id>.md, enqueue process_call.
  Both answer 503 on any failure, never a 3xx or 4xx (Granola drops events on most 4xx); a redelivery
  skips the write and still enqueues. These get 200 and are ignored, like the nightly sweep ignores them:
  every item while `ingest_cutoff` is unset or unreadable, items from before it (a Granola note by the
  later of its creation and its calendar start, since notes are often created ahead), and items with no
  transcript text yet (a file then would parse to no turns and could never be repaired; the sweep
  ingests the item once the recorder has its transcript).
- POST /github: verify X-Hub-Signature-256 (401 when wrong), ignore the app's own pushes, enqueue work
  for files added directly in transcripts/ (one backfill task when more than 5) and requests/, and reindex
  when config/overrides.yml changed. Files added in a subfolder of transcripts/ are not processed (the
  pipeline reads transcripts/ flat); #discovery-ops is told to move them up.
- POST /slack/interact: verify the Slack signature (401 when wrong), then discovery.interact (lock the clicked
  block through response_url, enqueue, or open a form pre-filled from the payload), all before the
  response.
- POST /slack/events: verify the Slack signature (401 when wrong); answer Slack's url_verification
  challenge; for an `app_mention` ("@<bot> <question>"), reply "on it" in the thread and queue an
  `ask` task named after the event id (discovery.slack_ask), all before the response. Other events, the bot's
  own messages and Slack's retries of an event already queued are acknowledged and ignored.
- POST /sweep, POST /rollup: verify the Cloud Scheduler OIDC token (skipped only with DISCOVERY_DEV=1), enqueue.

Processor route:
- POST /task/{kind}: run the task with discovery.runner; X-CloudTasks-TaskRetryCount says which attempt this is.
  500 asks Cloud Tasks to retry; 200 ends the task (success, or a final failure already reported).
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import math
from datetime import datetime, timezone
from typing import Any, Callable
from urllib.parse import parse_qs, urlparse

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response
from starlette.concurrency import run_in_threadpool

from discovery import interact, runner, settings
from discovery.context import Ctx
from discovery.rules import as_datetime
from discovery.tasks import task_name

log = logging.getLogger("discovery.service")

GRANOLA_EVENTS = ("note.generated", "note.access_granted", "note.edited")
DISCOVERY_TAG = "discovery"
OVERRIDES_FILE = "config/overrides.yml"
MAX_LOOKBACK_HOURS = 24 * 30
EDITED_SYNCS = 2                  # note.edited: syncs to try when the next version already exists remotely

CtxFactory = Callable[[], Ctx]
AckFactory = Callable[[], "interact.AckCtx"]


class IngestError(Exception):
    """A recorder delivery that could not be taken in. The receiver answers 503 so the recorder retries."""


# ---------- signatures ----------

def verify_github_signature(secret: str | None, body: bytes, header: str | None) -> bool:
    """True when `X-Hub-Signature-256` is "sha256=" plus the hex HMAC-SHA256 of the raw body."""
    if not secret or not header:
        return False
    expected = "sha256=" + hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    # Bytes, not str: compare_digest raises TypeError on a str with non-ASCII characters, and Starlette
    # decodes headers as latin-1, so a forged header byte >= 0x80 must fail the check, not crash the route.
    return hmac.compare_digest(expected.encode("ascii"), header.strip().encode("utf-8", "surrogateescape"))


def verify_scheduler_token(authorization: str | None, audiences: list[str], service_account: str | None) -> bool:
    """Check a Cloud Scheduler OIDC token: signed by Google, `aud` one of `audiences`, and issued to
    `service_account`. Fails closed when no service account is configured."""
    if not authorization or not authorization.lower().startswith("bearer "):
        return False
    if not service_account:
        log.error("SCHEDULER_SERVICE_ACCOUNT is not set; refusing scheduler calls (set DISCOVERY_DEV=1 locally)")
        return False
    token = authorization.split(" ", 1)[1].strip()
    from google.auth.transport import requests as google_requests
    from google.oauth2 import id_token

    try:
        claims = id_token.verify_oauth2_token(token, google_requests.Request(), audience=audiences)
    except Exception as e:           # bad signature, expired, wrong audience, cert fetch failure
        log.warning("scheduler token rejected: %s", e)
        return False
    if claims.get("aud") not in audiences:
        return False
    if claims.get("email") != service_account or claims.get("email_verified") is False:
        return False
    return True


# ---------- receiver ----------

def create_receiver_app(ctx_factory: CtxFactory, *, ack: AckFactory | None = None,
                        granola: Callable[[], Any] | None = None,
                        circleback: Callable[[str], Any] | None = None) -> FastAPI:
    """The public receiver. `ctx_factory` builds a Ctx (with the repo) for the routes that read repo
    files; `ack()` (default settings.ack_ctx) the repo-free AckCtx for Slack and the scheduler routes.
    `granola()` and `circleback(person)` return recorder API clients (default: from environment keys;
    tests pass clients on mock transports)."""
    granola_factory = granola or settings.granola_client
    circleback_factory = circleback or settings.circleback_client
    ack_factory = ack or settings.ack_ctx

    app = FastAPI(title="discovery receiver", docs_url=None, redoc_url=None, openapi_url=None)

    @app.get("/healthz")
    def health() -> dict:
        return {"ok": True}

    @app.post("/ingest/granola")
    async def ingest_granola(request: Request):
        body = await request.body()
        headers = dict(request.headers)
        try:
            result = await run_in_threadpool(_granola, ctx_factory, granola_factory, headers, body)
        except Exception as e:
            log.exception("granola ingest failed")
            return _unavailable(e)
        log.info("granola webhook: %s", result)      # says why a note was ignored, or what was queued
        return result

    @app.post("/ingest/circleback/{person}")
    async def ingest_circleback(person: str, request: Request):
        body = await request.body()
        signature = request.headers.get("x-signature", "")
        try:
            result = await run_in_threadpool(_circleback, ctx_factory, circleback_factory, person, signature, body)
        except Exception as e:
            log.exception("circleback ingest failed for %s", person)
            return _unavailable(e)
        log.info("circleback webhook for %s: %s", person, result)     # says why a meeting was ignored, or what was queued
        return result

    @app.post("/github")
    async def github(request: Request):
        body = await request.body()
        if not verify_github_signature(settings.env("GITHUB_WEBHOOK_SECRET"), body,
                                       request.headers.get("x-hub-signature-256")):
            return JSONResponse({"ok": False, "error": "bad signature"}, status_code=401)
        event = request.headers.get("x-github-event", "")
        if event == "ping":
            return {"ok": True, "pong": True}
        if event != "push":
            return {"ok": True, "ignored": f"event {event}"}
        try:
            return await run_in_threadpool(_github_push, ctx_factory, _github_json(body))
        except Exception as e:           # GitHub never redelivers; the nightly sweep covers misses
            log.exception("github push handling failed")
            return _unavailable(e)

    @app.post("/slack/interact")
    async def slack_interact(request: Request):
        from discovery.slackio import verify_slack_signature

        body = await request.body()
        try:
            signed = verify_slack_signature(settings.env("SLACK_SIGNING_SECRET") or "", request.headers, body)
        except Exception:                # a forged, non-ASCII X-Slack-Signature: a failed check, not a 500
            log.warning("slack signature check raised; answering 401", exc_info=True)
            signed = False
        if not signed:
            return JSONResponse({"ok": False, "error": "bad signature"}, status_code=401)
        form = parse_qs(body.decode("utf-8"))
        payload = json.loads((form.get("payload") or ["{}"])[0])
        result = await run_in_threadpool(_interact, ack_factory, payload)
        if result is None:
            return Response(status_code=200)
        return JSONResponse(result)

    @app.post("/slack/events")
    async def slack_events(request: Request):
        from discovery.slackio import verify_slack_signature

        body = await request.body()
        try:
            signed = verify_slack_signature(settings.env("SLACK_SIGNING_SECRET") or "", request.headers, body)
        except Exception:
            log.warning("slack signature check raised; answering 401", exc_info=True)
            signed = False
        if not signed:
            return JSONResponse({"ok": False, "error": "bad signature"}, status_code=401)
        try:
            data = json.loads(body)
        except ValueError:
            return JSONResponse({"ok": False, "error": "bad json"}, status_code=400)
        if data.get("type") == "url_verification":
            return {"challenge": data.get("challenge")}
        event = data.get("event") or {}
        if data.get("type") != "event_callback" or event.get("type") != "app_mention" or event.get("bot_id"):
            return {"ok": True, "ignored": event.get("type") or data.get("type")}
        try:
            result = await run_in_threadpool(_slack_ask, ack_factory, event, str(data.get("event_id") or ""))
        except Exception:                # Slack retries a non-2xx; the task name makes a retry a no-op
            log.exception("slack ask failed to queue")
            return _unavailable(RuntimeError("ask not queued"))
        log.info("slack mention %s: %s", data.get("event_id"), result)
        return {"ok": True, **result}

    @app.post("/sweep")
    async def sweep(request: Request):
        if not await run_in_threadpool(_scheduler_ok, request.headers.get("authorization"), str(request.url)):
            return JSONResponse({"ok": False, "error": "unauthorized"}, status_code=401)
        body = await _json_body(request)
        return await run_in_threadpool(_enqueue, ack_factory, "sweep",
                                       {"granola_only": bool(body.get("granola_only"))})

    @app.post("/rollup")
    async def rollup(request: Request):
        if not await run_in_threadpool(_scheduler_ok, request.headers.get("authorization"), str(request.url)):
            return JSONResponse({"ok": False, "error": "unauthorized"}, status_code=401)
        body = await _json_body(request)
        return await run_in_threadpool(_enqueue, ack_factory, "rollup", {"bootstrap": bool(body.get("bootstrap"))})

    return app


def _github_json(body: bytes) -> dict:
    """The push payload, sent as JSON or (if the webhook is set to form encoding) as payload=<json>."""
    if body.startswith(b"payload="):
        return json.loads((parse_qs(body.decode("utf-8")).get("payload") or ["{}"])[0])
    return json.loads(body)


def _unavailable(error: Exception) -> JSONResponse:
    """503 so the sender retries. The details stay in the logs, not in a reply to a third party."""
    return JSONResponse({"ok": False, "error": type(error).__name__}, status_code=503)


async def _json_body(request: Request) -> dict:
    raw = await request.body()
    if not raw.strip():
        return {}
    try:
        data = json.loads(raw)
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def _enqueue(ack_factory: AckFactory, kind: str, payload: dict) -> dict:
    ack_factory().queue.enqueue(kind, payload)
    return {"ok": True, "queued": kind, "payload": payload}


def _slack_ask(ack_factory: AckFactory, event: dict, event_id: str) -> dict:
    from discovery.slack_ask import queue_from_event

    return queue_from_event(ack_factory(), event, event_id)


def _interact(ack_factory: AckFactory, payload: dict) -> dict | None:
    return interact.handle_interaction(ack_factory(), payload)


# ---------- ingest ----------

def _granola(ctx_factory: CtxFactory, granola_factory: Callable[[], Any], headers: dict, body: bytes) -> dict:
    from discovery.ingest.granola import has_transcript, note_to_standard, verify_webhook

    secret = settings.env("GRANOLA_WEBHOOK_SECRET")
    if not secret or not verify_webhook(secret, headers, body):
        raise IngestError("Granola signature did not verify")
    event = json.loads(body)
    event_type = event.get("event_type")
    note_id = event.get("note_id") or (event.get("data") or {}).get("note_id")
    if event_type not in GRANOLA_EVENTS:
        return {"ok": True, "ignored": f"event {event_type}"}
    if not note_id:
        raise IngestError("Granola event without a note_id")
    ctx = ctx_factory()
    cutoff = _cutoff(ctx)
    if cutoff is None:
        return {"ok": True, "ignored": "ingest_cutoff not set"}
    client = granola_factory()
    if client is None:
        raise IngestError("GRANOLA_API_KEY is not set")

    note = client.get_note(note_id)
    from discovery.ingest.granola import note_time

    t = note_time(note)
    if t is not None and t < cutoff:       # created and scheduled before the cutoff (see note_time)
        return {"ok": True, "ignored": "call before ingest_cutoff"}
    if not has_transcript(note):
        log.warning("Granola note %s (%s) has no transcript yet; left for the sweep", note_id,
                    (note.get("owner") or {}).get("email") or "unknown owner")
        return {"ok": True, "ignored": "no transcript in note"}
    if event_type == "note.edited":
        return _granola_edited(ctx, note)
    rel, text = note_to_standard(note, config=ctx.config, version=1, ingested_at=ingest_stamp(ctx))
    return _ingest(ctx, rel, text, f"Add Granola note {note_id}")


def _granola_edited(ctx: Ctx, note: dict) -> dict:
    """Someone hit Resume: store the next version if the transcript got longer, else ignore the event.

    The versions are looked up in the local clone, which is pulled only when it is older than
    RECEIVER_SYNC_SECONDS. When the version to write already exists on the remote, another event stored it since the
    last sync: sync and compare again, at most EDITED_SYNCS times, then answer 503 so Granola redelivers.

    A transcript exactly as long as the latest stored version: when no digest points to that version
    yet, its enqueue may have been lost (the enqueue failed after the write, or the create response
    was lost), so it is enqueued again, like any redelivery. When a digest does, the event is a
    notes-only edit of a processed call and is ignored.
    """
    from discovery.ingest.granola import note_to_standard, transcript_length

    note_id = note.get("id")
    first, _ = note_to_standard(note, config=ctx.config, version=1)
    length = transcript_length(note)
    stamp = ingest_stamp(ctx)
    if not _granola_versions(ctx, first):
        ctx.remote.sync()               # the first version may have landed after the last sync
    for attempt in range(EDITED_SYNCS + 1):
        if attempt:
            ctx.remote.sync()
        versions = _granola_versions(ctx, first)
        version = max(versions) + 1 if versions else 1
        latest = versions.get(version - 1)
        if latest:
            stored = _stored_length(ctx, latest)
            if length == stored and not _digested(ctx, latest):
                return _enqueue_call(ctx, latest, ctx.repo.read(latest), created=False)
            if length <= stored:
                return {"ok": True, "ignored": "transcript is not longer than the stored one"}
        rel, text = note_to_standard(note, config=ctx.config, version=version, ingested_at=stamp)
        suffix = f" version {version}" if version > 1 else ""
        if ctx.remote.create_file_only(rel, text, f"Add Granola note {note_id}{suffix}"):
            return _enqueue_call(ctx, rel, text, created=True)
        log.info("note.edited %s: %s already exists on the remote; syncing", note_id, rel)
    raise IngestError(f"Granola note {note_id}: the next version still exists after {EDITED_SYNCS} syncs")


def _granola_versions(ctx: Ctx, first: str) -> dict[int, str]:
    """version -> path for every stored copy of the note whose first version is `first`."""
    from discovery.rules import note_version

    note_id = note_version(first)[0]
    found: dict[int, str] = {}
    for rel in ctx.repo.list(first[:-len(".md")] + "*.md"):
        nid, version = note_version(rel)
        if nid == note_id:
            found[version] = rel
    return found


def _digested(ctx: Ctx, rel: str) -> bool:
    """True when a digest or stub in the local clone points to `rel`, as process_call's skip check reads it.
    A stale clone can miss a new digest; that costs one extra enqueue, which the task name or
    process_call's skip check absorbs."""
    from discovery.digest import digest_for_transcript, load_digests

    return digest_for_transcript(load_digests(ctx.repo), rel) is not None


def _stored_length(ctx: Ctx, rel: str) -> int:
    from discovery.parse import ParseError, parse

    try:
        return sum(len(t.text) for t in parse(rel, ctx.repo.read(rel)).turns)
    except ParseError:
        return 0


def _circleback(ctx_factory: CtxFactory, circleback_factory: Callable[[str], Any], person: str, signature: str,
                body: bytes) -> dict:
    from discovery.ingest.circleback import has_tag, has_transcript, payload_to_standard, verify_signature

    secret = settings.env(settings.person_var("CIRCLEBACK_SIGNING_SECRET", person))
    if not secret or not verify_signature(secret, body, signature):
        raise IngestError(f"Circleback signature for {person!r} did not verify")
    ctx = ctx_factory()
    known = ctx.config.person(person)
    if known is None:
        raise IngestError(f"{person!r} is not in config/people.yml")
    payload = json.loads(body)
    cutoff = _cutoff(ctx)
    if cutoff is None:
        return {"ok": True, "ignored": "ingest_cutoff not set"}
    if _before(payload.get("createdAt"), cutoff):
        return {"ok": True, "ignored": "created before ingest_cutoff"}
    if not has_tag(payload, DISCOVERY_TAG):
        return {"ok": True, "ignored": f"not tagged {DISCOVERY_TAG}"}
    if not has_transcript(payload):
        # The automation was set up without the Transcript outcome, or the meeting is still processing.
        # The sweep fetches the transcript through the API and ingests the meeting then.
        log.warning("Circleback meeting %s from %s has no transcript in the payload; left for the sweep "
                    "(check that %s's automation sends the transcript)", payload.get("id"), known.key, known.key)
        return {"ok": True, "ignored": "no transcript in payload"}
    start = _calendar_start(circleback_factory(known.key), payload)
    rel, text = payload_to_standard(payload, person=known.key, config=ctx.config, start=start,
                                    start_source="calendar", ingested_at=ingest_stamp(ctx))
    return _ingest(ctx, rel, text, f"Add Circleback meeting {payload.get('id')}")


def _calendar_start(client: Any, payload: dict) -> str | None:
    """One calendar lookup for the meeting's scheduled start; None (use createdAt) when it fails."""
    from discovery.ingest.circleback import find_start

    if client is None:
        return None
    hours = 24
    created = as_datetime(payload.get("createdAt"), "UTC", strict=False)
    if created is not None:
        age = (datetime.now(timezone.utc) - created).total_seconds() / 3600
        hours = max(24, min(MAX_LOOKBACK_HOURS, math.ceil(age) + 2))
    try:
        events = client.calendar_events(lookback_hours=hours)
    except Exception as e:
        log.warning("circleback calendar lookup failed: %s", e)
        return None
    normalized = [{**e, "meetingId": str(e.get("meetingId"))} for e in events if isinstance(e, dict)]
    return find_start(normalized, str(payload.get("id")))


def ingest_stamp(ctx: Ctx) -> str:
    """`ingested_at` for a transcript file written now (see rules.ingest_stamp). Files are create-only,
    so a redelivery never changes the stamp of the first delivery."""
    from discovery.rules import ingest_stamp as stamp

    return stamp(ctx.now(), ctx.config.pipeline.timezone)


def _ingest(ctx: Ctx, rel: str, text: str, message: str) -> dict:
    """Create the transcript file (create-only; an existing file is left alone) and enqueue it."""
    return _enqueue_call(ctx, rel, text, created=ctx.remote.create_file_only(rel, text, message))


def _enqueue_call(ctx: Ctx, rel: str, text: str, *, created: bool) -> dict:
    """Enqueue process_call for a transcript the receiver created or found already stored."""
    delay = _dup_delay(ctx, rel, text)
    queued = ctx.queue.enqueue("process_call", {"transcript": rel}, name=task_name("process_call", rel),
                               delay_seconds=delay)
    log.info("ingest %s created=%s queued=%s delay=%ss", rel, created, queued, delay)
    return {"ok": True, "transcript": rel, "created": created, "queued": queued, "delay_seconds": delay}


def _dup_delay(ctx: Ctx, rel: str, text: str) -> int:
    """15 minutes (dup_delay_minutes) when another teammate was invited, so a second recording can land."""
    from discovery.parse import ParseError, parse
    from discovery.rules import cofounder_invited

    try:
        parsed = parse(rel, text)
    except ParseError:
        return 0
    return int(ctx.config.pipeline.t("dup_delay_minutes")) * 60 if cofounder_invited(parsed, ctx.config) else 0


def _cutoff(ctx: Ctx) -> datetime | None:
    """`ingest_cutoff` as an aware datetime, or None when it is unset or unreadable.

    Fails closed like the sweep: until go-live, dropped exports are the only source, and taking in
    recorder items (teammates moving old notes into the Team space, a Circleback automation re-run
    on past meetings) would ingest the backfill again as live calls. Nothing is lost: once the cutoff
    is set, the sweep ingests every item created on or after it.
    """
    value = ctx.config.pipeline.ingest_cutoff
    try:
        cutoff = as_datetime(value, ctx.config.pipeline.timezone)
    except ValueError:
        cutoff = None
    if cutoff is None:
        log.error("ingest_cutoff in config/pipeline.yml is %r, not a datetime; recorder webhooks are ignored", value)
    return cutoff


def _before(created: Any, cutoff: datetime) -> bool:
    """True when a recorder item was created before `cutoff` (unknown creation times are never cut off)."""
    c = as_datetime(created, "UTC", strict=False)
    return c is not None and c < cutoff


# ---------- GitHub ----------

def _github_push(ctx_factory: CtxFactory, push: dict) -> dict:
    if push.get("deleted") or push.get("ref") != f"refs/heads/{settings.repo_branch()}":
        return {"ok": True, "ignored": "not the pipeline branch"}
    if _own_push(push):
        return {"ok": True, "ignored": "pushed by the app"}
    added, changed = _files(push)
    transcripts = [p for p in added if _direct_child(p, "transcripts/")]
    nested = [p for p in added if _nested_child(p, "transcripts/")]
    requests = [p for p in added if _direct_child(p, "requests/") and p.endswith((".yml", ".yaml"))]
    reindex = OVERRIDES_FILE in changed
    if not (transcripts or nested or requests or reindex):
        return {"ok": True, "queued": []}

    ctx = ctx_factory()
    after = str(push.get("after") or "")
    queued: list[str] = []
    if len(transcripts) >= int(ctx.config.pipeline.t("backfill_push_min")):
        ctx.queue.enqueue("backfill", {"paths": transcripts, "batch_id": after}, name=task_name("backfill", after))
        queued.append("backfill")
    else:
        for rel in transcripts:
            ctx.queue.enqueue("process_call", {"transcript": rel}, name=task_name("process_call", rel))
            queued.append(f"process_call {rel}")
    for rel in requests:
        ctx.queue.enqueue("apply_belief_change", {"request_file": rel},
                          name=task_name("apply_belief_change", rel, nonce=after))
        queued.append(f"apply_belief_change {rel}")
    if reindex:
        ctx.queue.enqueue("reindex", {}, name=task_name("reindex", after))
        queued.append("reindex")
    result: dict[str, Any] = {"ok": True, "queued": queued}
    if nested:
        _notify_nested(ctx, nested)
        result["not_processed"] = nested
    return result


def _notify_nested(ctx: Ctx, paths: list[str]) -> None:
    """Tell #discovery-ops that files pushed into a subfolder of transcripts/ are not processed (the web UI
    keeps folder structure when a folder is dragged in). Best effort: the push's other work is queued."""
    log.warning("push added %d file(s) in a subfolder of transcripts/, not processed: %s", len(paths), paths)
    if ctx.slack is None:
        return
    try:
        from discovery.sweep import subfolder_notice

        text = subfolder_notice(paths)[:2900]
        ctx.slack.post(ctx.channel("ops"), [{"type": "section", "text": {"type": "mrkdwn", "text": text}}], text)
    except Exception:
        log.exception("could not post the subfolder notice to #discovery-ops")


def _own_push(push: dict) -> bool:
    """Pushes the GitHub App makes (receiver, processor, MCP) trigger this webhook too; they enqueue
    their own work. Without GITHUB_APP_SLUG, any bot sender counts as the app."""
    sender = push.get("sender") or {}
    login = str(sender.get("login") or "")
    own = settings.github_bot_login()
    if own:
        return login.lower() == own.lower()
    return sender.get("type") == "Bot"


def _files(push: dict) -> tuple[list[str], set[str]]:
    """(files added and still present after the push, in order; every path added, modified or removed)."""
    added: dict[str, None] = {}
    changed: set[str] = set()
    for commit in push.get("commits") or []:
        for rel in commit.get("added") or []:
            added[rel] = None
        for rel in commit.get("removed") or []:
            added.pop(rel, None)
        for key in ("added", "modified", "removed"):
            changed.update(commit.get(key) or [])
    return list(added), changed


def _direct_child(rel: str, folder: str) -> bool:
    if not rel.startswith(folder):
        return False
    name = rel[len(folder):]
    return bool(name) and "/" not in name and not name.startswith(".")


def _nested_child(rel: str, folder: str) -> bool:
    """True for a file in a subfolder of `folder` with no part that starts with a dot (.DS_Store, .git)."""
    if not rel.startswith(folder):
        return False
    parts = rel[len(folder):].split("/")
    return len(parts) > 1 and all(part and not part.startswith(".") for part in parts)


# ---------- scheduler ----------

def _scheduler_ok(authorization: str | None, url: str) -> bool:
    if settings.is_dev():
        return True
    path = urlparse(url).path
    base = settings.env("RECEIVER_URL")
    if base:
        base = base.rstrip("/")
    else:                                # Cloud Run terminates TLS, so the app may see http
        parsed = urlparse(url)
        base = f"https://{parsed.netloc}"
    return verify_scheduler_token(authorization, [base, base + path], settings.env("SCHEDULER_SERVICE_ACCOUNT"))


# ---------- processor ----------

def create_processor_app(ctx_factory: CtxFactory) -> FastAPI:
    """The private processor: POST /task/{kind} runs one task (Cloud Run IAM admits only Cloud Tasks)."""
    app = FastAPI(title="discovery processor", docs_url=None, redoc_url=None, openapi_url=None)

    @app.get("/healthz")
    def health() -> dict:
        return {"ok": True}

    @app.post("/task/{kind}")
    async def task(kind: str, request: Request):
        payload = await _json_body(request)
        attempt = _int(request.headers.get("x-cloudtasks-taskretrycount"), 0)
        max_attempts = _int(settings.env("DISCOVERY_TASK_MAX_ATTEMPTS"), 5)
        try:
            result = await run_in_threadpool(_run, ctx_factory, kind, payload, attempt, max_attempts)
        except Exception as e:
            log.exception("task %s failed (attempt %d of %d)", kind, attempt + 1, max_attempts)
            return JSONResponse({"ok": False, "kind": kind, "error": str(e)[:500]}, status_code=500)
        return JSONResponse(result)

    return app


def _run(ctx_factory: CtxFactory, kind: str, payload: dict, attempt: int, max_attempts: int) -> dict:
    try:
        ctx = ctx_factory()
    except Exception as e:
        if attempt < max_attempts - 1:
            raise
        _alert_without_ctx(kind, payload, e, attempt, max_attempts)
        return {"ok": False, "kind": kind, "error": str(e)[:500], "final": True}
    return runner.run_task(ctx, kind, payload, attempt=attempt, max_attempts=max_attempts)


def _alert_without_ctx(kind: str, payload: dict, error: Exception, attempt: int, max_attempts: int) -> None:
    """Last attempt, and the repo could not even be loaded (GitHub down, bad settings): tell #discovery-ops
    with the bot token alone, best effort."""
    token = settings.env("SLACK_BOT_TOKEN")
    if not token:
        return
    try:
        from discovery.slackio import SlackWebIO

        text = (f"Task failed: {kind}. Attempt {attempt + 1} of {max_attempts}. "
                f"Could not load the repo: {type(error).__name__}: {error}")[:2900].replace(chr(0x2014), "-")
        blocks = [{"type": "section", "block_id": "failure", "text": {"type": "mrkdwn", "text": text}}]
        value = runner.fit_rerun_value(kind, payload)     # None: too big for a button, so post none
        if value is not None:
            blocks.append({"type": "actions", "block_id": "rerun", "elements": [{
                "type": "button", "action_id": "rerun", "text": {"type": "plain_text", "text": "Re-run"},
                "value": value}]})
        SlackWebIO(token).post(_ops_channel_without_ctx(), blocks, text)
    except Exception:
        log.exception("could not alert #discovery-ops")


def _ops_channel_without_ctx() -> str:
    """Where an alert goes when there is no config to read channels from. DISCOVERY_SLACK_TEST=1 sends it to
    the test channel's default, as settings._test_channels does for every other post."""
    if settings.env("DISCOVERY_SLACK_TEST") == "1":
        return "#discovery-test"
    return settings.env("DISCOVERY_OPS_CHANNEL", "#discovery-ops")


def _int(value: str | None, default: int) -> int:
    try:
        return int(value) if value not in (None, "") else default
    except ValueError:
        return default


# ---------- app selection ----------

def _mcp_app():
    """The MCP server's own ASGI app (service/mcp.py: sign-in from env, stateless HTTP at /mcp)."""
    from service import mcp

    return mcp.create_app()


def create_app(role: str | None = None):
    """The ASGI app for SERVICE_ROLE (default receiver). Contexts are built lazily, per request or task."""
    role = role or settings.env("SERVICE_ROLE", "receiver")
    if settings.env("SERVICE_ROLE"):
        logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
    if role == "processor":
        return create_processor_app(lambda: settings.build_ctx("processor"))
    if role == "mcp":
        return _mcp_app()
    if role == "receiver":
        return create_receiver_app(lambda: settings.build_ctx("receiver"), ack=settings.ack_ctx)
    raise ValueError(f"unknown SERVICE_ROLE {role!r}; expected receiver, processor or mcp")


app = create_app()
