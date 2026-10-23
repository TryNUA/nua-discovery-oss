"""MCP server: the teammates' read and request layer over the repo.

See SPEC "MCP server". Read tools answer from the repo files (digests, beliefs.md, generated
indexes); write tools only commit new files (`requests/`, `prep/`) through the create-only helper
and enqueue their own work, because `/github` ignores the app's pushes. The server never edits a
file in its own working tree.

- Sign-in: FastMCP's GitHub or Google OAuth provider (env `MCP_AUTH=github|google`), limited to the
  people in `config/people.yml` by `allowed_identity`. `create_app` refuses to start with `MCP_AUTH`
  unset; only `MCP_AUTH=none` turns sign-in off. Without auth (tests, local runs, a private service)
  read tools are open and write tools take the person from env `MCP_DEV_USER`. The provider keeps its sign-in
  state (client registrations, token mappings, refresh tokens) in files under FASTMCP_HOME, so that
  folder must outlive the instance: on Cloud Run it is a Cloud Storage volume (infra/terraform/mcp.tf).
- Repo: `ctx.remote.sync()` runs before a tool call when the last sync is older than 60 seconds and
  no other tool call is in flight (a pull rewrites the working tree those calls are reading; an `ask`
  reads for minutes). A skipped pull serves the current tree and is retried on the next call. A
  failed pull serves the current tree and is retried 60 seconds after it gave up; gitops' network
  timeout bounds how long a stalled GitHub holds the sync lock (every call waits on it).
- Time: `ask` gets a time limit (ASK_DEADLINE_SECONDS, env `MCP_ASK_DEADLINE_SECONDS`), less the
  time the call spent on the pull before it. The tightest client is claude.ai, which gives up on a tool
  call after 240 seconds (Cloud Run allows 300), and ask still checks citations and encodes its result
  after the limit, so the limit keeps about 40 seconds clear of 240. That way an ask that runs out of
  time still reaches the user with its coverage.
- Size: every result is capped at 150,000 characters (claude.ai's cap, which has no override); transcripts
  are paged. Every tool declares that size in `_meta["anthropic/maxResultSizeChars"]`, which Claude Code
  reads to raise its own 25,000-token default.

`ctx_factory` is called once per tool call, so each call gets its own cost tracker. It must be
cheap: reuse one clone (see `create_app`).
"""
from __future__ import annotations

import csv
import hashlib
import importlib
import io
import json
import logging
import os
import re
import threading
import time
from contextlib import contextmanager
from typing import Any, Callable, Iterator, Mapping

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError

from nua import config as config_mod
from nua import frontmatter
from nua.beliefs import EMPTY_BELIEFS, BeliefsDoc, format_history, format_scope, parse_beliefs, parse_decisions
from nua.callid import slug_name
from nua.config import Config
from nua.context import Ctx
from nua.digest import apply_overrides, digest_path, is_stub, read_digest
from nua.ports import RepoFS
from nua.prep import parse_prep, validate_prep
from nua.types import SCOPE_KEYS, VERDICTS, Claim, Digest, Entry

SERVER_NAME = "nua-discovery"
MAX_RESULT_CHARS = 150_000        # claude.ai caps a tool result at about this size (no override there)
TRANSCRIPT_PAGE_CHARS = 60_000
SYNC_MAX_AGE_SECONDS = 60
ASK_DEADLINE_SECONDS = 200.0      # claude.ai gives up on a tool call at 240s (Cloud Run's 300s is looser);
                                  # the rest covers the citation check and encoding after the limit
ASK_MIN_SECONDS = 10.0            # what ask still gets when the pull before it ate the whole limit
PAGE_TEXT_MARKER = "\n--- transcript text ---\n"

REQUEST_ACTIONS = ("add", "watch", "promote", "reword", "split", "merge", "retire", "keep", "dismiss_verdict")
CALLS_COLUMNS = ["call_id", "transcript", "status", "origin", "date", "date_source", "contact", "firm", "seat",
                 "institution_type", "role_in_thesis", "recorded_by", "source", "speaker_labels", "claims",
                 "prompt_version"]

_SEP = " · "
_ISO_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")
_ENTRY_ID = re.compile(r"\s*([BbWw])-?0*(\d{1,4})\s*")
_EM_DASH = re.compile(r"[ \t]*\u2014[ \t]*")
_HEADING_LINE = re.compile(r"^(#{1,6})\s")
_ITEM_LINE = re.compile(r"^(?:[-*+]|\d+[.)])\s")

_READ = {"readOnlyHint": True, "openWorldHint": False}
_WRITE = {"readOnlyHint": False, "destructiveHint": False, "idempotentHint": False, "openWorldHint": False}
# Claude Code refuses a tool result over 25,000 tokens unless the tool declares a larger size here.
_META = {"anthropic/maxResultSizeChars": MAX_RESULT_CHARS}

_clock: Callable[[], float] = time.monotonic   # replaced in tests
log = logging.getLogger("nua.mcp")

INSTRUCTIONS = """\
nua-discovery: every customer discovery call the team has recorded, digested at ingest. The team is listed in \
config/people.yml and what the discovery is about in config/domain.yml. The GitHub repo is the source of truth; \
this server reads it.

Hard rules:
- Quotes are verbatim. Copy them exactly from a digest or transcript, ASR garble included. Clean-up goes in your \
own words, never inside the quote.
- No claim without a quote. Cite every claim as call_id plus quote.
- For anything that spans calls, use `ask`. It reads every digest. Report its coverage line ("read 70/70 digests") \
with the answer.
- Never answer a cross-call question from a sample of calls fetched one by one.
- Recorder notes (the Granola or Circleback AI overview above a transcript) are context only, not evidence. Quote \
the transcript body only.
- Mind speaker labels. named: real names per turn. me_them: "Me" is the recorder's mic, "Them" is everyone else. \
diarized: Speaker A, Speaker B. single_stream: everything is "Me". A claim (a finding) carries one to three \
quotes; a quote with attribution: inferred crosses speaker turns. Say so when who said it is uncertain.
- Skip stub digests (status internal, parse_failed or failed). They hold no claims.
- Beliefs change only through `request_change`, applied by the processor and announced in Slack. Never edit \
beliefs.md. Never resolve a conflict yourself: surface it and let the team decide.
- The same firm shows up under speech-recognition variants (for example "Guidewire" and "guide wire"); \
config/entities.yml maps them.

How to work:
- Current picture: get_beliefs, get_findings, get_open_questions.
- One call: list_calls to find it, get_call for the digest, get_call with transcript_page to read the transcript.
- Before a call: get_prep_context, write at most 5 questions tagged with the belief they test, save with save_prep. \
The prep's contact is the person's full name; ask the user if you only have a first name. Add attendee_email when \
known. Processing the call links the prep by that email or that full name.
- Style for anything the team reads: tight and direct, no em dashes, lead with what changed.
"""


# ---------- identity and auth ----------

def allowed_identity(config: Config, email: str | None, github_login: str | None) -> str | None:
    """Person key from config/people.yml for a signed-in account, or None when it is not a teammate.

    Matches the email or the GitHub login, case-insensitively. Blank values never match, and
    neither do people whose email or login is not filled in.
    """
    email_key = (email or "").strip().lower()
    login_key = (github_login or "").strip().lower()
    for p in config.people:
        if email_key and p.email and p.email.strip().lower() == email_key:
            return p.key
        if login_key and p.github_login and p.github_login.strip().lower() == login_key:
            return p.key
    return None


def auth_from_env(env: Mapping[str, str] | None = None):
    """FastMCP OAuth provider chosen by `MCP_AUTH` (github | google), or None for an explicit "none".

    Fails closed: an unset or empty `MCP_AUTH` raises ValueError, so a revision deployed without it
    refuses to start instead of serving the corpus to anyone (nua-mcp is public). Only "none" turns
    sign-in off, for local runs and a private service (.env.example and infra/terraform set it).

    Reads `MCP_OAUTH_CLIENT_ID`, `MCP_OAUTH_CLIENT_SECRET`, `MCP_BASE_URL` (the service's public
    URL) and optional `MCP_JWT_SIGNING_KEY`. Raises ValueError for an unknown provider or a missing
    variable.

    The provider stores sign-in state under FastMCP's home (env `FASTMCP_HOME`, read when fastmcp is
    imported), in a folder derived from the JWT signing key. Both must stay the same across restarts
    and deploys or every connector has to sign in and register again; without `FASTMCP_HOME` set this
    logs a warning, since the default is the container's own disk.

    GitHub sign-in asks only for `read:user`. The provider checks that scope against the token's
    X-OAuth-Scopes header, so a token granted earlier under the broader `user` scope no longer passes:
    that person signs in again, and if GitHub keeps handing back the old grant, revokes the OAuth app
    under GitHub Settings > Applications > Authorized OAuth Apps first.
    """
    env = os.environ if env is None else env
    kind = (env.get("MCP_AUTH") or "").strip().lower()
    if kind == "none":
        return None
    if not kind:
        raise ValueError("MCP_AUTH is not set, so the MCP server will not start: set it to github or google "
                         "(sign-in limited to config/people.yml), or to none for a local run or a private service")
    if kind not in ("github", "google"):
        raise ValueError(f"MCP_AUTH must be github, google or none, got {kind!r}")
    needed = ("MCP_OAUTH_CLIENT_ID", "MCP_OAUTH_CLIENT_SECRET", "MCP_BASE_URL")
    missing = [k for k in needed if not (env.get(k) or "").strip()]
    if missing:
        raise ValueError(f"MCP_AUTH={kind} needs {', '.join(missing)}")
    kwargs: dict[str, Any] = {
        "client_id": env["MCP_OAUTH_CLIENT_ID"].strip(),
        "client_secret": env["MCP_OAUTH_CLIENT_SECRET"].strip(),
        "base_url": env["MCP_BASE_URL"].strip(),
    }
    if (env.get("MCP_JWT_SIGNING_KEY") or "").strip():
        kwargs["jwt_signing_key"] = env["MCP_JWT_SIGNING_KEY"].strip()
    if not (env.get("FASTMCP_HOME") or "").strip():
        log.warning("MCP sign-in state is kept on local disk (FASTMCP_HOME is not set): a restart signs "
                    "everyone out. Point FASTMCP_HOME at durable shared storage (infra/terraform/mcp.tf mounts one).")
    if kind == "github":
        from fastmcp.server.auth.providers import github
        # read:user is enough for GET /user and its public login, the only GitHub claim used
        # (_identity). FastMCP's default, "user", would also grant write access to the profile.
        return github.GitHubProvider(**kwargs, required_scopes=["read:user"])
    from fastmcp.server.auth.providers import google
    return google.GoogleProvider(**kwargs, required_scopes=["openid", "email"])


def _access_token():
    """The signed-in caller's access token, or None (always None over the in-memory transport)."""
    from fastmcp.server.dependencies import get_access_token
    return get_access_token()


def _email_verified(claims: Mapping[str, Any]) -> bool:
    """True only when the provider vouches for the email (Google). GitHub claims carry no such flag."""
    value = claims.get("email_verified")
    if isinstance(value, str):
        return value.strip().lower() == "true"
    return value is True


# ---------- per-server state ----------

class _DigestCache:
    """Parsed digests, reused while a file's mtime and size are unchanged. Malformed files are skipped."""

    def __init__(self):
        self._lock = threading.Lock()
        self._files: dict[str, tuple[tuple[int, int], Digest | None]] = {}

    def load(self, repo: RepoFS) -> list[Digest]:
        """Every digest and stub under digests/, sorted by call_id."""
        out: list[Digest] = []
        seen: set[str] = set()
        for rel in repo.list("digests/*.md"):
            path = repo.path(rel)
            key = str(path)
            try:
                st = path.stat()
            except FileNotFoundError:      # removed since the listing (a pull elsewhere): skip it
                continue
            seen.add(key)
            stamp = (st.st_mtime_ns, st.st_size)
            with self._lock:
                hit = self._files.get(key)
            if hit is not None and hit[0] == stamp:
                d = hit[1]
            else:
                try:
                    d = read_digest(repo, rel)
                except Exception as e:     # a malformed digest must not take every tool down
                    log.warning("skipping malformed digest %s: %s", rel, e)
                    d = None
                with self._lock:
                    self._files[key] = (stamp, d)
            if d is not None:
                out.append(d)
        prefix = str(repo.path("digests"))
        with self._lock:
            for key in [k for k in self._files if k.startswith(prefix) and k not in seen]:
                del self._files[key]
        return sorted(out, key=lambda d: d.call_id)


class _State:
    """What the tools share: the ctx factory, sign-in mode, the last sync time, the number of tool
    calls in flight and the digest cache."""

    def __init__(self, ctx_factory: Callable[[], Ctx], auth_enabled: bool):
        self.ctx_factory = ctx_factory
        self.auth_enabled = auth_enabled
        self.last_sync: float | None = None  # last pull attempt: success, or when a failed one gave up
        self.sync_lock = threading.Lock()   # guards last_sync and active; held for a whole pull
        self.active = 0                     # tool calls between entering and leaving `call`
        self.cache = _DigestCache()

    @contextmanager
    def call(self, *, write: bool = False) -> Iterator[tuple[Ctx, str | None]]:
        """For one tool call: a fresh ctx on a repo synced within the last 60 seconds (unless other
        calls are in flight, see `_sync`), and the caller's person key.

        The call counts as in flight from entry to exit, so no pull rewrites the tree under it. A call
        arriving during a pull waits for the pull to finish. With sign-in on, every call needs a
        teammate account. With it off, read calls need no identity and write calls take `MCP_DEV_USER`.
        """
        with self.sync_lock:
            self.active += 1
        try:
            ctx = self.ctx_factory()
            who = self._identity(ctx.config, write)
            if self._sync(ctx):
                ctx.config = config_mod.load(ctx.repo.root)
            yield ctx, who
        finally:
            with self.sync_lock:
                self.active -= 1

    def digests(self, ctx: Ctx) -> list[Digest]:
        return self.cache.load(ctx.repo)

    def _sync(self, ctx: Ctx) -> bool:
        """Pull the repo when the last sync attempt is older than SYNC_MAX_AGE_SECONDS and this is the
        only call in flight. True when it pulled. A failed pull counts as an attempt too.

        A pull resets and cleans the working tree, which would pull files out from under another call
        (an `ask` reads digests and transcripts for minutes). So with others in flight the pull is
        skipped and the next call tries again; a blocking read-write lock would instead stall every
        caller behind a waiting pull for as long as the longest ask.
        """
        with self.sync_lock:
            now = _clock()
            if self.last_sync is not None and now - self.last_sync < SYNC_MAX_AGE_SECONDS:
                return False
            if self.active > 1:
                log.info("repo sync deferred: %d other tool calls in flight", self.active - 1)
                return False
            try:
                ctx.remote.sync()
            except Exception as e:       # serve the last synced tree rather than fail every read
                log.warning("repo sync failed, serving the last synced tree: %s", e)
                # Back off from when it gave up: during a GitHub outage every call would otherwise
                # re-run the fetch (up to gitops' network timeout) while holding the lock.
                self.last_sync = _clock()
                return False
            self.last_sync = now
            return True

    def _identity(self, config: Config, write: bool) -> str | None:
        if self.auth_enabled:
            token = _access_token()
            if token is None:
                raise ToolError("Sign-in required.")
            claims = dict(getattr(token, "claims", None) or {})
            email = claims.get("email") if _email_verified(claims) else None
            who = allowed_identity(config, email, claims.get("login"))
            if who is None:
                raise ToolError("This account is not one of the teammates in config/people.yml.")
            return who
        if not write:
            return None
        dev = (os.environ.get("MCP_DEV_USER") or "").strip()
        person = config.person(dev) if dev else None
        if person is None:
            raise ToolError("Sign-in is off on this server, so write tools need MCP_DEV_USER set to a person "
                            "key from config/people.yml.")
        return person.key


# ---------- small helpers ----------

def _module(name: str):
    """Import a pipeline module at call time (sys.modules first, so tests can stub it)."""
    try:
        return importlib.import_module(name)
    except ImportError as e:
        raise ToolError(f"{name} is not available on this server: {e}") from None


def _cap_text(text: str) -> str:
    """`text` cut to MAX_RESULT_CHARS at a line break, with a notice saying so."""
    limit = MAX_RESULT_CHARS
    if len(text) <= limit:
        return text
    notice = f"\n\n[Truncated at {limit:,} characters. Narrow the request (since, seat, filters) or page it.]\n"
    cut = text[:max(0, limit - len(notice))]
    newline = cut.rfind("\n")
    if newline > len(cut) // 2:
        cut = cut[:newline]
    return cut + notice


def _dumps(payload: dict) -> str:
    return json.dumps(payload, ensure_ascii=False)


def _json_capped(payload: dict, list_key: str) -> str:
    """JSON for `payload`, dropping trailing items of `payload[list_key]` until it fits MAX_RESULT_CHARS."""
    text = _dumps(payload)
    if len(text) <= MAX_RESULT_CHARS:
        return text
    items = list(payload.get(list_key) or [])

    def trial(n: int) -> str:
        note = f"showing {n} of {len(items)}; narrow the request to see the rest"
        return _dumps({**payload, list_key: items[:n], "truncated": note})

    lo, hi = 0, len(items)
    while lo < hi:                        # largest n whose JSON fits
        mid = (lo + hi + 1) // 2
        if len(trial(mid)) <= MAX_RESULT_CHARS:
            lo = mid
        else:
            hi = mid - 1
    return trial(lo)


def _check_date(value: str | None, name: str) -> str | None:
    if value is None or str(value).strip() == "":
        return None
    value = str(value).strip()
    if not _ISO_DATE.fullmatch(value):
        raise ToolError(f"{name} must be a YYYY-MM-DD date, got {value!r}")
    return value


def _no_em_dash(text: str | None) -> str | None:
    """Em dashes rewritten as commas (the house style). Never used on quotes."""
    if text is None or "\u2014" not in text:
        return text
    return _EM_DASH.sub(", ", text).strip(" ,")


def _tokens(name: str | None) -> set[str]:
    slug = slug_name(name)
    return set() if slug == "unknown" else set(slug.split("-"))


def _names_match(query: set[str], name: str | None) -> bool:
    """Every word of the query appears in `name` ("jane" matches "Jane Doe")."""
    return bool(query) and query <= _tokens(name)


def _canon_tokens(config: Config, name: str | None) -> set[str]:
    return _tokens(config.canonical(name.strip()) if name and name.strip() else None)


def _with_overrides(digests: list[Digest], config: Config) -> list[Digest]:
    """Digests with config/overrides.yml applied (the reindex does the same to the files)."""
    ov = config.overrides or {}
    return [apply_overrides(d, ov) if d.call_id in ov else d for d in digests]


def _quote_block(quote: str, indent: str = "") -> str:
    lines = (quote or "").split("\n")
    return "\n".join(f"{indent}> {line}" if line else f"{indent}>" for line in lines)


def _plural(n: int, word: str) -> str:
    return f"{n} {word}{'' if n == 1 else 's'}"


def _norm_entry_id(value: str) -> str:
    m = _ENTRY_ID.fullmatch(value or "")
    if not m:
        raise ToolError(f"{value!r} is not a belief or Watching id such as B-003 or W-001.")
    return f"{m.group(1).upper()}-{int(m.group(2)):03d}"


def _read_beliefs(repo: RepoFS) -> BeliefsDoc:
    try:
        return parse_beliefs(repo.read_or("beliefs.md", EMPTY_BELIEFS))
    except ValueError as e:
        raise ToolError(f"beliefs.md could not be read: {e}") from None


def _statuses(ctx: Ctx, entries: list[Entry], digests: list[Digest]) -> dict[str, Any]:
    """entry id -> indexes.EntryStatus, the same computation that writes beliefs-status.md."""
    if not entries:
        return {}
    indexes = _module("nua.indexes")
    overrides = indexes.load_verdict_overrides(ctx.repo)
    live = _with_overrides(digests, ctx.config)
    return {e.id: indexes.entry_status(e, live, overrides, ctx.config) for e in entries}


def _evidence_phrase(items: list[dict], *, joiner: str = " ({})") -> str:
    """Distinct calls and seats in `items`: `2 calls (2 seats)` by default, `2 calls, 2 seats` with
    joiner=", {}", and `0 calls` when empty."""
    calls = {i.get("call_id") for i in items}
    seats = {i.get("seat") for i in items if i.get("seat")}
    if not calls:
        return "0 calls"
    return _plural(len(calls), "call") + joiner.format(_plural(len(seats), "seat"))


def _status_line(entry: Entry, st: Any) -> str:
    parts = [f"supports {_evidence_phrase(st.supports)}", f"contradicts {_evidence_phrase(st.contradicts)}",
             f"scope mismatch {_evidence_phrase(st.scope_mismatch)}", f"checked {st.checked}/{st.total} calls"]
    if entry.section == "beliefs" and st.under_pressure:
        parts.append("UNDER PRESSURE")
    return _SEP.join(parts)


def _evidence_seats(st: Any) -> set[str]:
    return {str(i.get("seat")).lower() for i in (st.supports + st.contradicts + st.scope_mismatch) if i.get("seat")}


def _section_title(section: str) -> str:
    return {"beliefs": "Beliefs", "watching": "Watching", "retired": "Retired"}.get(section, section)


# ---------- list_calls ----------

def _digest_row(d: Digest) -> dict[str, str]:
    values = {c: getattr(d, c, None) for c in CALLS_COLUMNS}
    values["claims"] = len(d.claims)
    return {k: "" if v is None else str(v) for k, v in values.items()}


def _calls_rows(ctx: Ctx, digests: list[Digest]) -> tuple[list[dict[str, str]], str]:
    """Rows of calls.csv, or rows built from the digests when calls.csv is not generated yet."""
    text = ctx.repo.read_or("calls.csv", "")
    if text.strip():
        return [dict(r) for r in csv.DictReader(io.StringIO(text))], "calls.csv"
    rows = [_digest_row(d) for d in _with_overrides(digests, ctx.config)]
    return rows, "digests (calls.csv not generated yet)"


def _list_calls(ctx: Ctx, digests: list[Digest], *, person: str | None, firm: str | None, seat: str | None,
                since: str | None, until: str | None, status: str | None) -> str:
    since, until = _check_date(since, "since"), _check_date(until, "until")
    cfg = ctx.config
    rows, source = _calls_rows(ctx, digests)
    person_q = _canon_tokens(cfg, person)
    teammate = cfg.person(person) if person else None
    firm_q = _canon_tokens(cfg, firm)

    def keep(r: dict[str, str]) -> bool:
        date = r.get("date") or ""
        if status and (r.get("status") or "") != status.strip():
            return False
        if seat and (r.get("seat") or "").strip().lower() != seat.strip().lower():
            return False
        if since and (not date or date < since):
            return False
        if until and (not date or date > until):
            return False
        if firm and not _names_match(firm_q, cfg.canonical(r.get("firm") or None)):
            return False
        if person:
            by_contact = _names_match(person_q, cfg.canonical(r.get("contact") or None))
            by_recorder = teammate is not None and (r.get("recorded_by") or "") == teammate.key
            if not (by_contact or by_recorder):
                return False
        return True

    matched = [r for r in rows if keep(r)]
    matched.sort(key=lambda r: (r.get("date") or "", r.get("call_id") or ""), reverse=True)
    payload = {"source": source, "total_calls": len(rows), "matched": len(matched),
               "note": "Stubs (status internal, parse_failed, failed) hold no claims.", "rows": matched}
    return _json_capped(payload, "rows")


# ---------- get_call ----------

def _page_bounds(text: str) -> list[tuple[int, int]]:
    """[start, end) offsets of each page, ending after a line break where one falls in the second half."""
    size = TRANSCRIPT_PAGE_CHARS
    bounds, start = [], 0
    while start < len(text):
        end = min(start + size, len(text))
        if end < len(text):
            newline = text.rfind("\n", start, end)
            if newline >= start + size // 2:
                end = newline + 1
        bounds.append((start, end))
        start = end
    return bounds or [(0, 0)]


def _transcript_rel(fm: dict) -> str | None:
    """The digest's transcript path when it stays inside transcripts/, else None."""
    rel = str(fm.get("transcript") or "")
    parts = rel.split("/")
    if parts[0] != "transcripts" or len(parts) < 2 or "\\" in rel or any(p in ("", ".", "..") for p in parts):
        return None
    return rel


def _safe_call_id(call_id: str) -> bool:
    """True when `call_id` can only name a file directly inside digests/.

    Checks safety, not shape: a stub's call id is the dropped export's own file stem, spaces,
    parentheses and dots included ("Alex _ Jane (Example Mutual) intro call"). No path separator, no control
    character (NUL included) and no leading dot means `digests/<call_id>.md` is one file name in that
    folder ("..", "." and hidden files are refused), so the existence check that follows decides
    membership.
    """
    return (bool(call_id) and not call_id.startswith(".")
            and not any(ch in "/\\" or ord(ch) < 32 or ord(ch) == 127 for ch in call_id))


def _get_call(ctx: Ctx, call_id: str, transcript_page: int | None) -> str:
    call_id = (call_id or "").strip()
    if not _safe_call_id(call_id):
        raise ToolError(f"{call_id!r} is not a call id. list_calls shows every call id.")
    rel = digest_path(call_id)
    if not ctx.repo.exists(rel):
        raise ToolError(f"No digest {call_id}. list_calls shows every call id.")
    text = ctx.repo.read(rel)
    fm, _, _ = frontmatter.split(text)
    transcript = _transcript_rel(fm)
    raw, not_utf8 = None, None
    if transcript and ctx.repo.exists(transcript):
        try:
            raw = ctx.repo.read(transcript)    # strict: no replacement characters that could be quoted
        except UnicodeDecodeError:
            not_utf8 = (f"Transcript {transcript} is not valid UTF-8, so it cannot be shown or quoted. "
                        f"Re-drop it saved as UTF-8.")

    if transcript_page is None:
        if not_utf8:
            tail = f"\n\n---\n{not_utf8}\n"
        elif raw is None:
            tail = f"\n\n---\nTranscript {fm.get('transcript')} is not in the repo.\n"
        else:
            pages = len(_page_bounds(raw))
            tail = (f"\n\n---\nTranscript: {transcript}, {_plural(pages, 'page')}. Read it with get_call("
                    f"call_id, transcript_page=1..{pages}).\n")
        return _cap_text(text + tail)

    if not_utf8:
        raise ToolError(not_utf8)
    if raw is None:
        raise ToolError(f"Transcript {fm.get('transcript')} for {call_id} is not in the repo.")
    bounds = _page_bounds(raw)
    if not isinstance(transcript_page, int) or not 1 <= transcript_page <= len(bounds):
        raise ToolError(f"transcript_page must be 1 to {len(bounds)} for {call_id}.")
    start, end = bounds[transcript_page - 1]
    lines = [f"Transcript {transcript} for {call_id}, page {transcript_page} of {len(bounds)} "
             f"(characters {start:,} to {end:,} of {len(raw):,}).",
             f"Speaker labels: {fm.get('speaker_labels') or 'unknown'}.",
             "Anything above the transcript turns is the recorder's AI overview: context only, not evidence. "
             "Quote the transcript body only."]
    body_page = _body_page(transcript, raw, bounds)
    if body_page is not None:
        lines.append(f"The transcript body starts on page {body_page}.")
    return _cap_text("\n".join(lines) + PAGE_TEXT_MARKER + raw[start:end])


def _body_page(rel: str, raw: str, bounds: list[tuple[int, int]]) -> int | None:
    """Page holding the first character of the transcript body, or None when the file does not parse."""
    from nua.parse import parse
    try:
        body_start = parse(rel, raw).body_start
    except Exception:
        return None
    return next((n for n, (a, b) in enumerate(bounds, start=1) if a <= body_start < b), None)


# ---------- beliefs ----------

def _beliefs_view(ctx: Ctx, digests: list[Digest]) -> str:
    doc = _read_beliefs(ctx.repo)
    if not doc.entries:
        return ("No beliefs or Watching items yet. beliefs.md fills up when teammates approve roll-up "
                "proposals or change requests. get_findings shows what the calls say so far.\n")
    statuses = _statuses(ctx, doc.current(), digests)
    lines = ["beliefs.md with a status line for each current entry. Evidence and quotes: get_belief(id).", ""]
    for section in ("beliefs", "watching", "retired"):
        lines += [f"## {_section_title(section)}", ""]
        for e in (x for x in doc.entries if x.section == section):
            lines.append(f"### {e.id} {e.statement}")
            if e.id in statuses:
                lines.append(f"- status: {_status_line(e, statuses[e.id])}")
            lines.append(f"- scope: {format_scope(e.scope) or 'none'}")
            lines.append("- history:")
            lines += [f"  - {format_history(h)}" for h in e.history]
            lines.append("")
    return _cap_text("\n".join(lines).rstrip() + "\n")


def _evidence_lines(items: list[dict]) -> list[str]:
    out: list[str] = []
    for i in sorted(items, key=lambda x: (str(x.get("date") or ""), str(x.get("call_id") or ""))):
        meta = [str(i.get(k)) for k in ("call_id", "seat", "date") if i.get(k)]
        if i.get("decided_by"):
            meta.append(f"decided by {i['decided_by']}")
        out.append(f"- {_SEP.join(meta)}")
        if i.get("quote"):
            out.append(_quote_block(str(i["quote"]), indent="  "))
    return out


def _belief_view(ctx: Ctx, digests: list[Digest], entry_id: str) -> str:
    eid = _norm_entry_id(entry_id)
    doc = _read_beliefs(ctx.repo)
    e = doc.get(eid)
    if e is None:
        raise ToolError(f"No entry {eid} in beliefs.md. get_beliefs lists every entry.")
    st = _statuses(ctx, [e], digests)[e.id]
    decisions = [d for d in parse_decisions(ctx.repo.read_or("decisions.md", "")) if e.id in d.rests_on]
    lines = [f"### {e.id} {e.statement}", f"Section: {_section_title(e.section)}",
             f"Scope: {format_scope(e.scope) or 'none'}", f"Status: {_status_line(e, st)}"]
    if decisions:
        lines.append("Decisions resting on it: " + "; ".join(f"{d.id} {d.title}" for d in decisions))
    lines += ["", "## History", ""] + [f"- {format_history(h)}" for h in e.history]
    for label, items in (("Contradicts", st.contradicts), ("Scope mismatch", st.scope_mismatch),
                         ("Supports", st.supports)):
        lines += ["", f"## {label} ({_evidence_phrase(items, joiner=', {}')})", ""]
        lines += _evidence_lines(items) or ["None."]
    return _cap_text("\n".join(lines).rstrip() + "\n")


# ---------- findings ----------

def _ungrouped_findings(ctx: Ctx, digests: list[Digest], since: str | None) -> list[tuple[Digest, Claim]]:
    """Claims no current belief check touches and no theme holds yet, newest call first: the
    "Ungrouped" part of findings.md, by the same rule (nua.indexes.group_findings)."""
    indexes = _module("nua.indexes")
    live = _with_overrides(digests, ctx.config)
    pairs = indexes.finding_claims(live, _read_beliefs(ctx.repo), indexes.load_verdict_overrides(ctx.repo))
    _, ungrouped = indexes.group_findings(live, pairs, indexes.load_themes(ctx.repo))
    out = [(d, c) for d, c in ungrouped
           if not is_stub(d) and not (since and (not d.date or d.date < since))]
    out.sort(key=lambda p: p[1].offset or (0, 0))
    out.sort(key=lambda p: (p[0].date or "", p[0].call_id), reverse=True)
    return out


def _finding_lines(pairs: list[tuple[Digest, Claim]]) -> list[str]:
    lines: list[str] = []
    for d, c in pairs:
        meta = _SEP.join(x for x in (d.call_id, d.seat, c.speaker) if x)
        lines.append(f"- {meta}: {c.text}")
        lines.extend(_quote_block(q, indent="  ") for q in c.quote_texts)
    return lines


def _findings_view(ctx: Ctx, digests: list[Digest], since: str | None) -> str:
    since = _check_date(since, "since")
    if since is None:
        text = ctx.repo.read_or("findings.md", "")
        if text.strip():
            return _cap_text(text)
    pairs = _ungrouped_findings(ctx, digests, since)
    if since is None:
        head = [f"findings.md has not been generated yet. {_plural(len(pairs), 'finding')} from the digests, "
                "newest first:", ""]
    else:
        head = [f"# Ungrouped findings from calls on or after {since}", "",
                f"{_plural(len(pairs), 'claim')} that touch no current belief and sit in no theme yet, "
                "newest first.", ""]
    return _cap_text("\n".join(head + _finding_lines(pairs)).rstrip() + "\n")


# ---------- open questions ----------

def _blocks(text: str) -> list[tuple[str, int, str]]:
    """Markdown as (kind, heading level, text) blocks: headings, list items with their continuation, paragraphs."""
    blocks: list[tuple[str, int, str]] = []
    cur: list | None = None

    def flush():
        nonlocal cur
        if cur is not None:
            blocks.append((cur[0], 0, cur[1]))
        cur = None

    for line in text.splitlines():
        m = _HEADING_LINE.match(line)
        if m:
            flush()
            blocks.append(("heading", len(m.group(1)), line))
        elif not line.strip():
            flush()
        elif _ITEM_LINE.match(line):
            flush()
            cur = ["item", line]
        elif cur is not None and (line[0] in " \t" or cur[0] == "para"):
            cur[1] += "\n" + line
        else:
            flush()
            cur = ["para", line]
    flush()
    return blocks


def _filter_markdown(text: str, term: str) -> str:
    """Headings plus the list items and paragraphs that mention `term` (case-insensitive).

    Everything under a heading that mentions the term is kept. A heading is shown only when
    something kept sits under it before the next heading of the same or a higher level.
    """
    needle = term.strip().lower()
    kept: list[tuple[str, int, str]] = []
    inside: int | None = None             # level of a heading that mentions the term: keep all under it
    for kind, level, body in _blocks(text):
        if kind == "heading":
            if inside is not None and level <= inside:
                inside = None
            if inside is None and needle in body.lower():
                inside = level
            kept.append((kind, level, body))
        elif inside is not None or needle in body.lower():
            kept.append((kind, level, body))
    selected = []
    for n, (kind, level, body) in enumerate(kept):
        if kind == "heading":
            has_content = False
            for later in kept[n + 1:]:
                if later[0] == "heading":
                    if later[1] <= level:
                        break
                    continue
                has_content = True
                break
            if not has_content:
                continue
        selected.append((kind, body))
    lines: list[str] = []
    for kind, body in selected:
        if kind != "item" and lines and lines[-1] != "":
            lines.append("")
        lines.append(body)
        if kind != "item":
            lines.append("")
    return "\n".join(lines).strip() + "\n" if lines else ""


def _nest(markdown: str) -> str:
    """Markdown embedded under a `##` section: the `#` title dropped, other headings one level down."""
    out = []
    for line in markdown.splitlines():
        m = _HEADING_LINE.match(line)
        if m and len(m.group(1)) == 1:
            continue
        out.append("#" + line if m and len(m.group(1)) < 6 else line)
    return "\n".join(out).strip() + "\n" if out else ""


def _open_questions_view(ctx: Ctx, seat: str | None) -> str:
    text = ctx.repo.read_or("open-questions.md", "")
    if not text.strip():
        return "open-questions.md has not been generated yet. It appears after the first reindex.\n"
    if not seat or not seat.strip():
        return _cap_text(text)
    filtered = _filter_markdown(text, seat)
    if not filtered.strip():
        return f"No open questions mention the seat {seat.strip()!r}.\n"
    return _cap_text(f"Open questions that mention {seat.strip()!r}, filtered from open-questions.md:\n\n{filtered}")


# ---------- prep context ----------

PREP_TEMPLATE = """\
---
planned_date: YYYY-MM-DD
contact: {contact}   # full name; ask the user if you only have a first name
firm: {firm}
attendee_email: name@example.com   # if known, else delete the line
seat: {seat}
caller: {caller}
---

## Context
Prior calls, mentions, open follow-ups (we owe / they offered)

## Questions
1. ... (B-003)
2. ...
"""


def _latest(digests: list[Digest]) -> Digest | None:
    return max(digests, key=lambda d: (d.date or "", d.call_id)) if digests else None


def _first_name_only(name: str | None) -> bool:
    """True for a one-word contact such as "Jane". Digests carry the full name, and process_call matches a
    prep by attendee email, then by the contact name as a whole, so "Jane" never matches "Jane Doe"."""
    return len((name or "").split()) < 2


def _prep_context(ctx: Ctx, digests: list[Digest], who: str | None, contact: str, firm: str | None,
                  seat: str | None) -> str:
    cfg = ctx.config
    person_q = _canon_tokens(cfg, contact)
    if not person_q:
        raise ToolError("contact is required: the person the call is with.")
    calls = [d for d in _with_overrides(digests, cfg) if not is_stub(d)]
    prior = sorted((d for d in calls if _names_match(person_q, cfg.canonical(d.contact))),
                   key=lambda d: (d.date or "", d.call_id), reverse=True)
    latest = _latest(prior)
    name = (latest.contact if latest and latest.contact else contact.strip())   # "Jane" -> "Jane Doe"
    firm = firm or (latest.firm if latest else None)
    firm_q = _canon_tokens(cfg, firm)
    prior_ids = {d.call_id for d in prior}
    same_firm = sorted((d for d in calls if d.call_id not in prior_ids and firm_q
                        and _names_match(firm_q, cfg.canonical(d.firm))),
                       key=lambda d: (d.date or "", d.call_id), reverse=True)
    seat = seat or (latest.seat if latest else None) or (_latest(same_firm).seat if same_firm else None)
    seen = prior_ids | {d.call_id for d in same_firm}
    mentions = [d for d in calls if d.call_id not in seen and (
        any(_names_match(person_q, cfg.canonical(p)) for p in (d.mentions or {}).get("people") or [])
        or (firm_q and any(_names_match(firm_q, cfg.canonical(f)) for f in (d.mentions or {}).get("firms") or [])))]

    title = _SEP.join(x for x in (name, firm, seat) if x)
    lines = [f"# Prep context: {title}", "",
             f"Context for writing prep questions. Write at most 5, tag each with the belief or Watching id it "
             f"tests, and save them with save_prep as prep/<planned-date>_{slug_name(name)}.md.", ""]

    lines += [f"## Prior calls with {name} ({len(prior)})", ""]
    if not prior:
        lines += ["No prior calls with this contact.", ""]
        if _first_name_only(name):
            lines += [f"Ask the user for {name}'s full name (and email, if known) and put it in the prep's contact "
                      f"line. Processing the call links a prep by attendee email or by the full contact name, so "
                      f"a prep saved as contact: {name} will not link to the call.", ""]
    for d in prior:
        lines += _prior_call_lines(d)

    if firm:
        lines += [f"## Other calls at {firm} ({len(same_firm)})", ""]
        for d in same_firm:
            lines.append(f"### {_SEP.join(x for x in (d.call_id, d.contact, d.seat) if x)}")
            lines += [f"- {s}" for s in d.summary] + [""]
        if not same_firm:
            lines += ["None.", ""]

    lines += [f"## Mentions in other calls ({len(mentions)})", ""]
    lines += [f"- {_SEP.join(x for x in (d.call_id, d.contact, d.firm, d.seat) if x)}" for d in mentions] or ["None."]
    lines.append("")

    follow = [(d, f) for d in prior + same_firm for f in d.follow_ups]
    lines += ["## Open follow-ups", ""]
    for d, f in follow:
        label = "we owe" if f.direction == "we_owe" else "they offered"
        lines.append(f"- {d.call_id}, {label}: {f.what}" + (f" ({f.who})" if f.who else ""))
        if f.quote:
            lines.append(_quote_block(f.quote, indent="  "))
    if not follow:
        lines.append("None recorded.")
    lines.append("")

    lines += _belief_lines(ctx, digests, seat)
    lines += [f"## Open questions for {seat or 'this seat'}", ""]
    oq = ctx.repo.read_or("open-questions.md", "")
    filtered = _nest(_filter_markdown(oq, seat)) if (seat and oq.strip()) else ""
    lines += [filtered.rstrip() if filtered.strip() else "None recorded.", ""]

    email = next((a.email for d in prior for a in d.attendees
                  if a.email and _names_match(person_q, a.name)), None)
    template = PREP_TEMPLATE.format(contact=name, firm=firm or "", seat=seat or "", caller=who or "")
    if email:
        template = template.replace("name@example.com   # if known, else delete the line", email)
    lines += ["## Prep file template", "", "```markdown", template.rstrip(), "```", ""]
    return _cap_text("\n".join(lines).rstrip() + "\n")


def _prior_call_lines(d: Digest) -> list[str]:
    lines = [f"### {_SEP.join(x for x in (d.call_id, d.date, d.firm, d.seat) if x)}", ""]
    if d.summary:
        lines += ["Summary:"] + [f"- {s}" for s in d.summary] + [""]
    if d.claims:
        lines.append("Claims:")
        for c in d.claims:
            lines.append(f"- {c.text}" + (f" ({c.speaker})" if c.speaker else ""))
            lines.extend(_quote_block(q, indent="  ") for q in c.quote_texts)
        lines.append("")
    open_q = [q for q in d.question_coverage if q.status in ("dodged", "never_asked")]
    if open_q:
        lines += ["Prep questions left open:"]
        lines += [f"- {q.status.replace('_', ' ')}: {q.question}" for q in open_q] + [""]
    if d.ask_next:
        lines += ["Suggested next questions:"] + [f"- {q}" for q in d.ask_next] + [""]
    return lines


def _belief_lines(ctx: Ctx, digests: list[Digest], seat: str | None) -> list[str]:
    doc = _read_beliefs(ctx.repo)
    current = doc.current()
    statuses = _statuses(ctx, current, digests)
    seat_key = (seat or "").strip().lower()

    def for_seat(e: Entry) -> bool:
        return not seat_key or not e.scope.seat or e.scope.seat.strip().lower() == seat_key

    def heard(e: Entry) -> str:
        if not seat_key:
            return ""
        return f" {seat} heard: {'yes' if seat_key in _evidence_seats(statuses[e.id]) else 'not yet'}."

    lines = ["## Beliefs under pressure", ""]
    pressured = [e for e in current if e.section == "beliefs" and statuses[e.id].under_pressure and for_seat(e)]
    for e in pressured:
        seats = sorted({str(i.get("seat")) for i in statuses[e.id].contradicts if i.get("seat")})
        lines.append(f"- {e.id} {e.statement}. Contradicted by: {', '.join(seats) or 'unknown seats'}.{heard(e)}")
    if not pressured:
        lines.append("None.")
    lines += ["", f"## Watching items{' for ' + seat if seat else ''}", ""]
    watching = [e for e in current if e.section == "watching" and for_seat(e)]
    for e in watching:
        scope = format_scope(e.scope)
        lines.append(f"- {e.id} {e.statement}" + (f" (scope {scope})" if scope else "")
                     + f". Evidence: {_status_line(e, statuses[e.id])}.{heard(e)}")
    if not watching:
        lines.append("None.")
    lines.append("")
    if seat_key and current:
        missing = [e.id for e in current if seat_key not in _evidence_seats(statuses[e.id])]
        lines += [f"Not yet heard from {seat}: {', '.join(missing) if missing else 'none'}.", ""]
    return lines


# ---------- request_change ----------

def _clean_scope(value: Any, where: str, errors: list[str]) -> dict[str, str]:
    if value in (None, {}):
        return {}
    if not isinstance(value, dict):
        errors.append(f"{where} must be an object with keys {', '.join(SCOPE_KEYS)}")
        return {}
    unknown = sorted(set(value) - set(SCOPE_KEYS))
    if unknown:
        errors.append(f"{where} keys must be {', '.join(SCOPE_KEYS)}; got {', '.join(unknown)}")
        return {}
    return {k: _no_em_dash(str(v).strip()) for k, v in value.items() if v is not None and str(v).strip()}


def _find_quote(ctx: Ctx, d: Digest, quote: str) -> str | None:
    """The raw transcript text for `quote` in call `d`, or the claim quote it sits inside, or None."""
    from nua.verify import QuoteIndex, contains
    rel = d.transcript
    if rel and ctx.repo.exists(rel):
        try:
            from nua.parse import parse
            m = QuoteIndex(parse(rel, ctx.repo.read(rel))).find(quote)
            if m is not None:
                return m.raw_text
        except Exception:
            pass
    if any(contains(q, quote) for c in d.claims for q in c.quote_texts):
        return quote.strip()
    return None


def _clean_sources(ctx: Ctx, digests: list[Digest], given: list[Any], errors: list[str]) -> list[dict[str, str]]:
    by_id = {d.call_id: d for d in digests}
    out: list[dict[str, str]] = []
    for s in given:
        if not isinstance(s, dict) or not str(s.get("call_id") or "").strip() or not str(s.get("quote") or "").strip():
            errors.append("each source needs call_id and quote")
            continue
        call_id, quote = str(s["call_id"]).strip(), str(s["quote"])
        d = by_id.get(call_id)
        if d is None or is_stub(d):
            errors.append(f"no digest for call {call_id} (stubs hold no claims)")
            continue
        found = _find_quote(ctx, d, quote)
        if found is None:
            errors.append(f"quote not found in call {call_id}. Copy it verbatim from the transcript body, "
                          f"not the recorder notes: {quote[:80]!r}")
            continue
        out.append({"call_id": call_id, "quote": found})
    return out


def _already_backed(doc: BeliefsDoc, sources: list[dict[str, str]]) -> list[str]:
    """Errors for sources already in the history of a current entry (SPEC "Applying a change").

    The processor makes the final call by span overlap; this catches the plain cases early.
    """
    from nua.verify import contains
    errors = []
    for s in sources:
        for entry_id, call_id, old in doc.current_sources():
            if call_id == s["call_id"] and (contains(old, s["quote"]) or contains(s["quote"], old)):
                errors.append(f"source from {call_id} already backs {entry_id}; use get_belief({entry_id})")
                break
    return errors


def _build_request(ctx: Ctx, digests: list[Digest], who: str, *, action: str, target: str | None,
                   targets: list[str] | None, statement: str | None, statements: list[dict] | None,
                   scope: dict | None, source: dict | None, sources: list[dict] | None,
                   reason: str | None, verdict: str | None = None) -> tuple[dict, list[str]]:
    """The request file content (SPEC "Change requests") and the reasons it is invalid, if any."""
    errors: list[str] = []
    action = (action or "").strip().lower()
    if action not in REQUEST_ACTIONS:
        return {}, [f"action must be one of {', '.join(REQUEST_ACTIONS)}; got {action!r}"]
    doc = _read_beliefs(ctx.repo)

    def current_entry(value: str, label: str) -> str | None:
        try:
            eid = _norm_entry_id(value)
        except ToolError as e:
            errors.append(f"{label}: {e}")
            return None
        e = doc.get(eid)
        if e is None:
            errors.append(f"unknown target {eid}: get_beliefs lists every entry")
        elif not e.is_current:
            errors.append(f"{eid} is retired")
        return eid

    tgt = None
    if action in ("add", "watch", "merge"):
        if target:
            errors.append(f"target is not used by {action}" + ("; use targets" if action == "merge" else ""))
    elif not (target or "").strip():
        errors.append(f"target is required for {action}")
    else:
        tgt = current_entry(target, "target")
        if tgt and action == "promote" and not tgt.startswith("W-"):
            errors.append("promote takes a Watching item (W-###)")
        if tgt and action == "keep" and not tgt.startswith("B-"):
            errors.append("keep takes a belief (B-###)")

    tgts: list[str] = []
    if action == "merge":
        tgts = [t for t in (current_entry(v, "targets") for v in (targets or [])) if t]
        if len(set(tgts)) < 2:
            errors.append("merge needs at least two distinct targets")
    elif targets:
        errors.append("targets is only used by merge")

    stmt = _no_em_dash((statement or "").strip()) or None
    if stmt and action in ("split", "retire", "keep", "dismiss_verdict"):
        errors.append(f"statement is not used by {action}" + ("; use statements" if action == "split" else ""))

    parts: list[dict] = []
    if action == "split":
        for n, s in enumerate(statements or [], start=1):
            text = _no_em_dash(str((s or {}).get("statement") or "").strip()) if isinstance(s, dict) else ""
            if not text:
                errors.append(f"statements[{n}] needs a statement")
                continue
            part: dict[str, Any] = {"statement": text}
            part_scope = _clean_scope(s.get("scope"), f"statements[{n}].scope", errors)
            if part_scope:
                part["scope"] = part_scope
            parts.append(part)
        if len(parts) < 2 and not any(e.startswith("statements[") for e in errors):
            errors.append("split needs at least two statements")
    elif statements:
        errors.append("statements is only used by split")

    clean_scope = _clean_scope(scope, "scope", errors)
    if clean_scope and action in ("split", "retire", "keep", "dismiss_verdict"):
        errors.append(f"scope is not used by {action}")

    given = ([source] if source else []) + list(sources or [])
    srcs = _clean_sources(ctx, digests, given, errors)
    if action in ("add", "watch") and not (stmt or given):
        errors.append(f"{action} needs a statement or a source (call_id plus the verbatim quote), ideally both")
    if action == "reword" and not (stmt or clean_scope):
        errors.append("reword needs a new statement or scope")
    if action == "dismiss_verdict" and not given:
        errors.append("source is required for dismiss_verdict: the call_id and quote of the verdict")
    if action == "dismiss_verdict" and len(given) > 1:
        errors.append("dismiss_verdict takes one source: the quote of the verdict being dismissed")
    if action in ("add", "watch"):
        errors += _already_backed(doc, srcs)
    wrong = (verdict or "").strip().lower() or None
    if wrong and action != "dismiss_verdict":
        errors.append("verdict is only used by dismiss_verdict")
    elif wrong and wrong not in VERDICTS:
        errors.append(f"verdict must be one of {', '.join(VERDICTS)}; got {verdict!r}")

    req: dict[str, Any] = {"action": action, "target": tgt, "targets": tgts, "statements": parts,
                           "statement": stmt, "scope": clean_scope}
    if len(srcs) > 1:
        req["sources"] = srcs
    else:
        req["source"] = srcs[0] if srcs else None
    if wrong and action == "dismiss_verdict":
        req["verdict"] = wrong
    req["requested_by"] = who
    req["reason"] = _no_em_dash((reason or "").strip()) or None
    return req, errors


def _request_slug(req: dict) -> str:
    if req.get("statement"):
        words = req["statement"].split()[:6]
        slug = slug_name(" ".join(words))[:48].strip("-")
        if slug != "unknown":
            return slug
    ids = [req["target"]] if req.get("target") else list(req.get("targets") or [])
    return slug_name(" ".join([req["action"].replace("_", " ")] + ids))


def _create_unique(ctx: Ctx, stem: str, ext: str, content: str, message: str) -> str:
    """Create `<stem><ext>`, or `<stem>-2<ext>` and so on when the name is taken. Returns the path."""
    for n in range(1, 21):
        rel = f"{stem}{ext}" if n == 1 else f"{stem}-{n}{ext}"
        if ctx.repo.exists(rel):
            continue
        if ctx.remote.create_file_only(rel, content, message):
            return rel
    raise ToolError(f"Could not find a free file name for {stem}{ext}.")


def _enqueue_apply(ctx: Ctx, rel: str, content: str) -> bool:
    """Queue apply_belief_change for a request file just created. False when there is no queue, the
    enqueue failed, or the queue refused the task name.

    Request file names repeat: once the processor has applied and deleted a file, the next request of
    the same kind on the same target that day gets the same name. Cloud Tasks refuses a reused task
    name for about an hour after the first task ran, so the name carries a nonce (the time and the
    file's content hash), as /github's does (the push's commit).
    """
    if ctx.queue is None:
        return False
    from nua.tasks import task_name
    payload = {"request_file": rel, "via": "agent"}     # the payload nua.apply.apply_task and the sweep use
    nonce = f"{ctx.now().isoformat()}:{hashlib.sha256(content.encode('utf-8')).hexdigest()[:16]}"
    try:
        queued = ctx.queue.enqueue("apply_belief_change", payload,
                                   name=task_name("apply_belief_change", rel, nonce=nonce))
    except Exception as e:
        log.warning("enqueue failed for %s: %s", rel, e)
        return False
    if not queued:
        log.warning("queue refused apply_belief_change for %s; the nightly sweep picks it up", rel)
    return bool(queued)


def _processor_check(req: dict) -> list[str]:
    """Run the processor's own request check (nua.apply.request_from_dict) so a bad file is never committed."""
    try:
        apply = importlib.import_module("nua.apply")
    except ImportError:
        return []
    try:
        apply.request_from_dict(req)
    except ValueError as e:
        return [str(e)]
    return []


def _request_change(ctx: Ctx, digests: list[Digest], who: str, **fields: Any) -> str:
    req, errors = _build_request(ctx, digests, who, **fields)
    if not errors:
        errors = _processor_check(req)
    if errors:
        raise ToolError("Change request not committed:\n- " + "\n- ".join(errors))
    now = ctx.now()
    # The processor keys a request on `requests/<file>@<blob sha>`. File names repeat within a day (the
    # processor deletes each file once applied), so an identical request later that day would carry the
    # same bytes, the same ref, and be skipped as already recorded. The time makes every request's content new.
    req["requested_at"] = now.isoformat()
    day = now.astimezone(ctx.tz).date().isoformat()
    content = frontmatter.dump_yaml(req)
    rel = _create_unique(ctx, f"requests/{day}_{who}_{_request_slug(req)}", ".yml", content,
                         f"Change request: {req['action']} by {who} via MCP")
    queued = _enqueue_apply(ctx, rel, content)
    nxt = ("The processor applies it and posts the outcome in #discovery-calls. beliefs.md changes only then."
           if queued else "Committed but not queued. The nightly sweep picks it up.")
    return _dumps({"request_file": rel, "requested_by": who, "queued": queued, "next": nxt})


# ---------- save_prep ----------

def _save_prep(ctx: Ctx, who: str, markdown: str) -> str:
    text = _no_em_dash((markdown or "").replace("\r\n", "\n")) or ""
    if text and not text.endswith("\n"):
        text += "\n"
    errors = validate_prep(text)
    if errors:
        raise ToolError("Prep file not saved. Fix these and call save_prep again:\n- " + "\n- ".join(errors)
                        + "\n\nTemplate:\n" + PREP_TEMPLATE.format(contact="Jane Doe", firm="Example Mutual",
                                                                    seat="underwriter", caller=who))
    pf = parse_prep("prep/new.md", text)
    rel = f"prep/{pf.planned_date}_{slug_name(pf.contact)}.md"
    if ctx.repo.exists(rel) or not ctx.remote.create_file_only(rel, text, f"Prep for {pf.contact} by {who} via MCP"):
        raise ToolError(f"{rel} already exists. The connector only creates files: change it on GitHub or a clone.")
    window = ctx.config.pipeline.t("prep_window_days")
    out: dict[str, Any] = {
        "prep_file": rel, "questions": len(pf.questions),
        "next": (f"Saved. Processing the call links this prep to its digest when an attendee's email matches "
                 f"attendee_email or the digest's contact matches contact, within {window} days of planned_date."),
    }
    if _first_name_only(pf.contact) and not pf.attendee_email:
        out["warning"] = (f"contact is only a first name ({pf.contact}) and there is no attendee_email. Digests carry "
                          f"the full name, so this prep links only if the call's contact is exactly {pf.contact}. "
                          f"Ask the user for the full name or email and save_prep again with it (a new file), "
                          f"or fix {rel} on GitHub.")
    return _dumps(out)


# ---------- server ----------

def _ask_deadline() -> float:
    """`ask`'s time limit: env MCP_ASK_DEADLINE_SECONDS when it is a positive number, else ASK_DEADLINE_SECONDS."""
    raw = (os.environ.get("MCP_ASK_DEADLINE_SECONDS") or "").strip()
    try:
        value = float(raw) if raw else ASK_DEADLINE_SECONDS
    except ValueError:
        log.warning("MCP_ASK_DEADLINE_SECONDS=%r is not a number; using %s", raw, ASK_DEADLINE_SECONDS)
        return ASK_DEADLINE_SECONDS
    return value if value > 0 else ASK_DEADLINE_SECONDS


def build_server(ctx_factory: Callable[[], Ctx], *, auth=None) -> FastMCP:
    """The MCP server with the read tools (phase 2) and write tools (phase 3) from SPEC "MCP server".

    `ctx_factory` returns a Ctx for one tool call (see the module docstring). `auth` is a FastMCP
    auth provider (see `auth_from_env`); None leaves sign-in off, as in tests and local runs.
    """
    state = _State(ctx_factory, auth_enabled=auth is not None)
    server = FastMCP(SERVER_NAME, instructions=INSTRUCTIONS, auth=auth)

    @server.tool(output_schema=None, annotations=_READ, meta=_META)
    def ask(question: str, since: str | None = None, seat: str | None = None) -> str:
        """Answer a question from EVERY call: maps over all digests, then writes one answer with verbatim
        citations and a coverage line such as "read 70/70 digests". Use it for anything spanning calls and
        report the coverage. Citations with verified false did not match the call: do not repeat them as fact.
        since: YYYY-MM-DD, only calls on or after it. seat: one seat from config/seats.yml. Takes 1 to 3
        minutes; digests it had no time to read count as unread in the coverage."""
        started = _clock()
        with state.call() as (ctx, _):
            if not (question or "").strip():
                raise ToolError("question is required.")
            since = _check_date(since, "since")
            waited = int(_clock() - started)      # whole seconds spent waiting for or running a pull
            limit = _ask_deadline()
            deadline = max(min(ASK_MIN_SECONDS, limit), limit - waited)
            result = _module("nua.ask").ask(ctx, question.strip(), since=since, seat=seat or None,
                                            deadline_seconds=deadline)
            payload = {"answer": result.answer, "coverage": result.coverage, "read": result.read,
                       "total": result.total, "citations": list(result.citations)}
            return _json_capped(payload, "citations")

    @server.tool(output_schema=None, annotations=_READ, meta=_META)
    def list_calls(person: str | None = None, firm: str | None = None, seat: str | None = None,
                   since: str | None = None, until: str | None = None, status: str | None = None) -> str:
        """Calls from calls.csv, newest first, as JSON rows (call_id, date, contact, firm, seat, status, ...).
        person: the contact's name (partial is fine) or a teammate key (matches recorded_by). firm: firm name,
        ASR variants included. seat: exact seat. since/until: YYYY-MM-DD, inclusive. status: digested |
        internal | parse_failed | failed."""
        with state.call() as (ctx, _):
            return _list_calls(ctx, state.digests(ctx), person=person, firm=firm, seat=seat, since=since,
                               until=until, status=status)

    @server.tool(output_schema=None, annotations=_READ, meta=_META)
    def get_call(call_id: str, transcript_page: int | None = None) -> str:
        """One call's digest (YAML front-matter plus readable markdown: summary, claims with verbatim quotes,
        belief checks, follow-ups). With transcript_page (1-based), one page of the raw transcript instead;
        the digest says how many pages there are."""
        with state.call() as (ctx, _):
            return _get_call(ctx, call_id, transcript_page)

    @server.tool(output_schema=None, annotations=_READ, meta=_META)
    def get_beliefs() -> str:
        """beliefs.md (Beliefs, Watching, Retired) with a status line per current entry: calls and seats
        supporting, contradicting or scope-mismatching it, coverage, and UNDER PRESSURE when it applies."""
        with state.call() as (ctx, _):
            return _beliefs_view(ctx, state.digests(ctx))

    @server.tool(output_schema=None, annotations=_READ, meta=_META)
    def get_belief(id: str) -> str:
        """One belief or Watching item (B-### or W-###) with its history, the decisions resting on it, and
        its full evidence for and against, each with call, seat, date and verbatim quote."""
        with state.call() as (ctx, _):
            return _belief_view(ctx, state.digests(ctx), id)

    @server.tool(output_schema=None, annotations=_READ, meta=_META)
    def get_findings(since: str | None = None) -> str:
        """findings.md: claims that touch no belief, grouped by theme. With since (YYYY-MM-DD), only the
        ungrouped findings from calls on or after that date, with quotes."""
        with state.call() as (ctx, _):
            return _findings_view(ctx, state.digests(ctx), since)

    @server.tool(output_schema=None, annotations=_READ, meta=_META)
    def get_open_questions(seat: str | None = None) -> str:
        """open-questions.md: prep questions dodged or never asked, beliefs under pressure with the seats
        not yet heard from, and the hand-kept list. With seat, only the parts that mention that seat."""
        with state.call() as (ctx, _):
            return _open_questions_view(ctx, seat)

    @server.tool(output_schema=None, annotations=_READ, meta=_META)
    def get_prep_context(contact: str, firm: str | None = None, seat: str | None = None) -> str:
        """Everything for prepping a call with `contact`: prior calls with them and their firm, mentions in
        other calls, open follow-ups, beliefs under pressure and Watching items for the seat, open questions
        for the seat, and the prep template. Write at most 5 questions from it, then call save_prep."""
        with state.call() as (ctx, who):
            return _prep_context(ctx, state.digests(ctx), who, contact or "", firm or None, seat or None)

    @server.tool(output_schema=None, annotations=_WRITE, meta=_META)
    def request_change(action: str, target: str | None = None, targets: list[str] | None = None,
                       statement: str | None = None, statements: list[dict] | None = None,
                       scope: dict[str, str | None] | None = None, source: dict[str, str] | None = None,
                       sources: list[dict[str, str]] | None = None, reason: str | None = None,
                       verdict: str | None = None) -> str:
        """Ask the processor to change beliefs.md. Commits requests/<date>_<you>_<slug>.yml and queues it; the
        outcome is posted in Slack. action: add | watch | promote | reword | split | merge | retire | keep |
        dismiss_verdict. target: B-/W- id (not for add, watch, merge). targets: ids to merge. statement: new
        wording (add, watch, reword, merge). statements: split parts [{statement, scope}]. scope: {line, party,
        segment, seat}. source: {call_id, quote} with the quote copied verbatim from the transcript
        body; add and watch need a statement or a source (give both), dismiss_verdict needs the verdict's
        source; sources for several. reason: why (retire, keep, dismiss_verdict). verdict: dismiss_verdict
        only, the verdict judged wrong (supports | contradicts | scope_mismatch); left out, every verdict on
        that quote is dismissed. requested_by is your signed-in account."""
        with state.call(write=True) as (ctx, who):
            return _request_change(ctx, state.digests(ctx), who, action=action, target=target, targets=targets,
                                   statement=statement, statements=statements, scope=scope, source=source,
                                   sources=sources, reason=reason, verdict=verdict)

    @server.tool(output_schema=None, annotations=_WRITE, meta=_META)
    def save_prep(markdown: str) -> str:
        """Check a prep file against the template and commit it as prep/<planned_date>_<contact>.md. Needs
        front-matter with planned_date (YYYY-MM-DD) and contact, the person's full name (firm, attendee_email,
        seat, caller optional), and a ## Questions list. get_prep_context shows the template. The result
        carries a warning when the prep may not link to the call."""
        with state.call(write=True) as (ctx, who):
            return _save_prep(ctx, who, markdown)

    return server


def create_app():
    """ASGI app for the Cloud Run `mcp` service (`uvicorn --factory service.mcp:create_app`, path /mcp).

    Each tool call gets `nua.settings.build_ctx("mcp")`: the shared clone, remote and queue, with
    freshly read config and its own cost tracker. Sign-in comes from `auth_from_env`, which raises
    (so the app never starts) when `MCP_AUTH` is unset: only an explicit `none` serves without sign-in.

    The MCP transport is stateless HTTP (no session is kept between requests), but sign-in is not:
    the OAuth provider's registrations, pending authorizations and token mappings are files under
    FASTMCP_HOME. An instance serves sign-in correctly only when that folder is durable and shared,
    which on Cloud Run is the Cloud Storage volume infra/terraform/mcp.tf mounts (one instance at most, so
    two never write it at once).
    """
    settings = importlib.import_module("nua.settings")
    server = build_server(lambda: settings.build_ctx("mcp"), auth=auth_from_env())
    return server.http_app(path="/mcp", stateless_http=True)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(create_app(), host="0.0.0.0", port=int(os.environ.get("PORT", "8080")))
