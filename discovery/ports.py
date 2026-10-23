"""Interfaces to the outside world, with in-memory fakes for tests and dry runs.

Real implementations: discovery/gitops.py (Remote), discovery/slackio.py (SlackIO), discovery/tasks.py (CloudTasksQueue).
"""
from __future__ import annotations

import fnmatch
import itertools
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol


# ---------- repo files ----------

class UnsafePathError(PermissionError):
    """A repo path that is a symlink, passes through one, or leads outside the repo root.

    Anyone who can push can commit a link such as `transcripts/x.md -> /proc/self/environ`, which would
    otherwise be parsed, sent to the model providers and served by the MCP server, so RepoFS never
    reads or writes through a link, even one pointing back inside the repo."""


class RepoFS:
    """Working-tree file access relative to the repo root. No git here; see gitops.Remote.

    Every read, write, delete and listing stays inside the root and never goes through a symlink:
    `list` and `exists` skip such paths, `read`, `read_or` and `read_bytes` raise UnsafePathError,
    and `write` and `delete` replace or remove a link at the leaf itself (never its target) and raise
    when a folder on the way is a link. `path` is only a lexical join, for callers that stat a path
    they got from `list`.
    """

    def __init__(self, root: str | Path):
        self.root = Path(root)

    def path(self, rel: str) -> Path:
        return self.root / rel

    def _checked(self, rel: str, *, leaf_link_ok: bool = False) -> Path:
        """The file for `rel` once it is known to stay inside the root: no `..`, no part below the root
        a symlink (the last part may be one when `leaf_link_ok`, for write and delete, which act on the
        link itself), and the path resolves inside the root. Raises UnsafePathError otherwise. A path
        that does not exist yet passes."""
        p = self.root / rel
        try:
            parts = p.relative_to(self.root).parts
        except ValueError:
            raise UnsafePathError(f"{rel} is outside the repo") from None
        if ".." in parts:
            raise UnsafePathError(f"{rel} leaves its folder through '..'")
        cur = self.root
        for i, part in enumerate(parts):
            cur = cur / part
            if cur.is_symlink() and not (leaf_link_ok and i == len(parts) - 1):
                raise UnsafePathError(f"{rel} goes through a symlink ({'/'.join(parts[:i + 1])}); "
                                      "repo files are never read or written through links")
        root = self.root.resolve()
        real = (p.parent if leaf_link_ok else p).resolve()
        if real != root and root not in real.parents:
            raise UnsafePathError(f"{rel} resolves outside the repo")
        return p

    def is_safe(self, rel: str) -> bool:
        """True when `rel` stays inside the repo and goes through no symlink (it need not exist)."""
        try:
            self._checked(rel)
        except UnsafePathError:
            return False
        return True

    def exists(self, rel: str) -> bool:
        """True for a file or folder inside the repo; False for a symlink, or a path through one or outside."""
        return self.is_safe(rel) and self.path(rel).exists()

    def read(self, rel: str) -> str:
        return self._checked(rel).read_text(encoding="utf-8")

    def read_bytes(self, rel: str) -> bytes:
        return self._checked(rel).read_bytes()

    def read_or(self, rel: str, default: str = "") -> str:
        p = self._checked(rel)
        return p.read_text(encoding="utf-8") if p.exists() else default

    def write(self, rel: str, text: str) -> None:
        """Write a regular file. A symlink at `rel` is replaced by the file; its target is never touched."""
        p = self._checked(rel, leaf_link_ok=True)
        if p.is_symlink():
            p.unlink()
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")

    def delete(self, rel: str) -> None:
        """Remove a file. A symlink at `rel` is removed itself; its target is never touched."""
        p = self._checked(rel, leaf_link_ok=True)
        if p.is_symlink() or p.exists():
            p.unlink()

    def list(self, pattern: str) -> list[str]:
        """Repo-relative paths of regular files matching a glob like 'digests/*.md', sorted. Symlinks,
        and files reached through a symlinked folder, are left out."""
        out = []
        for p in self.root.glob(pattern):
            rel = str(p.relative_to(self.root))
            if self.is_safe(rel) and p.is_file():
                out.append(rel)
        return sorted(out)


class Remote(Protocol):
    """Git remote operations (implemented in discovery/gitops.py)."""

    def sync(self) -> None:
        """Fetch and hard-reset the working tree to origin's default branch."""

    def create_file_only(self, rel: str, content: str, message: str) -> bool:
        """Create a file via the GitHub Contents API without a sha. False if it already exists."""

    def commit_and_push(self, message: str, paths: list[str], regenerate: Any = None) -> str | None:
        """Commit `paths` (adds, edits, deletions) and push. On a rejected push, re-sync,
        call regenerate() to rebuild generated files, re-apply, and retry. Returns the commit sha or None if nothing changed."""

    def head_sha(self) -> str: ...

    def blob_sha(self, rel: str) -> str | None: ...

    def last_commit_message(self, rel: str) -> str | None:
        """Full message of the newest commit on the branch that touched `rel` (its deletion included),
        or None when there is none or it cannot be told."""


class NullRemote:
    """Local runs and tests: nothing is pushed."""

    def __init__(self):
        self.commits: list[dict] = []
        self.created: list[str] = []

    def sync(self) -> None:
        pass

    def create_file_only(self, rel: str, content: str, message: str) -> bool:
        self.created.append(rel)
        return True

    def commit_and_push(self, message: str, paths: list[str], regenerate: Any = None) -> str | None:
        self.commits.append({"message": message, "paths": list(paths)})
        return f"local{len(self.commits)}"

    def head_sha(self) -> str:
        return "local"

    def blob_sha(self, rel: str) -> str | None:
        return None

    def last_commit_message(self, rel: str) -> str | None:
        return next((c["message"] for c in reversed(self.commits) if rel in c["paths"]), None)


# ---------- Slack ----------

class SlackIO(Protocol):
    def post(self, channel: str, blocks: list[dict], text: str, *, thread_ts: str | None = None,
             metadata: dict | None = None) -> str:
        """Post a message; returns its ts. metadata = {"event_type": str, "event_payload": dict}."""

    def update(self, channel: str, ts: str, blocks: list[dict], text: str) -> None: ...

    def delete(self, channel: str, ts: str) -> None: ...

    def find_by_metadata(self, channel: str, event_type: str, key: str, *, limit: int = 1000) -> str | None:
        """ts of the most recent bot message whose metadata event_type matches and event_payload['key'] == key."""

    def replies(self, channel: str, ts: str) -> list[dict]:
        """Thread replies (excluding the parent), each {"ts", "blocks", "text", "bot": bool}."""

    def message(self, channel: str, ts: str) -> dict | None:
        """The top-level message at `ts` as it is now, {"ts", "blocks", "text", "bot"}, or None when it is gone."""

    def open_view(self, trigger_id: str, view: dict) -> str: ...

    def update_view(self, view_id: str, view: dict) -> None: ...

    def respond(self, response_url: str, payload: dict) -> None: ...

    def ephemeral(self, channel: str, user: str, text: str) -> None: ...


@dataclass
class FakeSlack:
    messages: list[dict] = field(default_factory=list)   # {"channel","ts","thread_ts","blocks","text","metadata","deleted"}
    views: list[dict] = field(default_factory=list)
    responses: list[dict] = field(default_factory=list)
    ephemerals: list[dict] = field(default_factory=list)
    _seq: Any = field(default_factory=lambda: itertools.count(1))

    def post(self, channel, blocks, text, *, thread_ts=None, metadata=None):
        ts = f"1700000000.{next(self._seq):06d}"
        self.messages.append({"channel": channel, "ts": ts, "thread_ts": thread_ts, "blocks": blocks,
                              "text": text, "metadata": metadata, "deleted": False})
        return ts

    def _get(self, channel, ts):
        return next(m for m in self.messages if m["channel"] == channel and m["ts"] == ts)

    def update(self, channel, ts, blocks, text):
        m = self._get(channel, ts)
        m["blocks"], m["text"] = blocks, text

    def delete(self, channel, ts):
        self._get(channel, ts)["deleted"] = True

    def find_by_metadata(self, channel, event_type, key, *, limit=1000):
        for m in reversed(self.messages):
            md = m.get("metadata") or {}
            if (m["channel"] == channel and not m["deleted"] and md.get("event_type") == event_type
                    and (md.get("event_payload") or {}).get("key") == key):
                return m["ts"]
        return None

    def replies(self, channel, ts):
        return [{"ts": m["ts"], "blocks": m["blocks"], "text": m["text"], "bot": True}
                for m in self.messages if m["channel"] == channel and m["thread_ts"] == ts and not m["deleted"]]

    def message(self, channel, ts):
        for m in self.messages:
            if m["channel"] == channel and m["ts"] == ts and not m["deleted"]:
                return {"ts": m["ts"], "blocks": m["blocks"], "text": m["text"]}
        return None

    def open_view(self, trigger_id, view):
        vid = f"V{len(self.views) + 1}"
        self.views.append({"id": vid, "trigger_id": trigger_id, "view": view})
        return vid

    def update_view(self, view_id, view):
        next(v for v in self.views if v["id"] == view_id)["view"] = view

    def respond(self, response_url, payload):
        self.responses.append({"url": response_url, "payload": payload})

    def ephemeral(self, channel, user, text):
        self.ephemerals.append({"channel": channel, "user": user, "text": text})

    def live(self, channel: str | None = None) -> list[dict]:
        return [m for m in self.messages if not m["deleted"] and (channel is None or m["channel"] == channel)]


# ---------- task queue ----------

TASK_KINDS = ("process_call", "backfill", "apply_belief_change", "backcheck", "reindex", "rollup", "sweep", "ask")


class TaskQueue(Protocol):
    def enqueue(self, kind: str, payload: dict, *, name: str | None = None, delay_seconds: int = 0) -> bool:
        """Enqueue a task. `name` dedupes briefly (Cloud Tasks); returns False if the name already exists."""


@dataclass
class RecordingQueue:
    tasks: list[dict] = field(default_factory=list)

    def enqueue(self, kind, payload, *, name=None, delay_seconds=0):
        assert kind in TASK_KINDS, kind
        if name and any(t["name"] == name for t in self.tasks):
            return False
        self.tasks.append({"kind": kind, "payload": payload, "name": name, "delay_seconds": delay_seconds})
        return True

    def of(self, kind: str) -> list[dict]:
        return [t for t in self.tasks if t["kind"] == kind]


def glob_match(path: str, pattern: str) -> bool:
    return fnmatch.fnmatch(path, pattern)
