"""Tests for nua/gitops.py. Every git operation runs against temporary repos, never this one."""
from __future__ import annotations

import base64
import json
import os
import subprocess
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from nua.gitops import GENERATED_PATHS, GitError, GitHubAppAuth, GitRemote, clone

SLUG = "discovery-ai/nua-discovery"


@pytest.fixture(autouse=True)
def isolated_git(tmp_path, monkeypatch):
    """Keep the developer's global git config (signing, hooks, identity) out of these tests."""
    empty = tmp_path / "empty.gitconfig"
    empty.write_text("", encoding="utf-8")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(empty))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    for var in ("GIT_AUTHOR_NAME", "GIT_AUTHOR_EMAIL", "GIT_COMMITTER_NAME", "GIT_COMMITTER_EMAIL", "GIT_DIR"):
        monkeypatch.delenv(var, raising=False)


def git(cwd: Path, *args: str) -> str:
    out = subprocess.run(["git", "-c", "user.name=Test", "-c", "user.email=test@example.com", *args],
                         cwd=cwd, capture_output=True, text=True, check=True)
    return out.stdout.strip()


@pytest.fixture
def bare(tmp_path) -> Path:
    """A bare 'origin' on main with one seed commit: README.md and calls.csv."""
    origin = tmp_path / "origin.git"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(origin)], check=True)
    seed = tmp_path / "seed"
    subprocess.run(["git", "init", "-q", "-b", "main", str(seed)], check=True)
    (seed / "README.md").write_text("seed\n", encoding="utf-8")
    (seed / "calls.csv").write_text("call_id\n", encoding="utf-8")
    git(seed, "add", "-A")
    git(seed, "commit", "-q", "-m", "seed")
    git(seed, "push", "-q", str(origin), "main")
    return origin


def remote_for(root: Path, origin: Path) -> GitRemote:
    r = GitRemote(root, repo_slug=SLUG, token_fn=lambda: "unused-for-local", remote_url=str(origin))
    r._sleep = lambda s: None
    return r


def origin_show(origin: Path, rel: str) -> str | None:
    res = subprocess.run(["git", "--git-dir", str(origin), "show", f"main:{rel}"], capture_output=True, text=True)
    return res.stdout if res.returncode == 0 else None


def origin_head(origin: Path) -> str:
    return subprocess.run(["git", "--git-dir", str(origin), "rev-parse", "main"],
                          capture_output=True, text=True, check=True).stdout.strip()


def rebuild_calls_csv(root: Path) -> list[str]:
    """A tiny stand-in for indexes.regenerate: calls.csv lists every digest stem."""
    stems = sorted(p.stem for p in (root / "digests").glob("*.md")) if (root / "digests").exists() else []
    (root / "calls.csv").write_text("call_id\n" + "".join(f"{s}\n" for s in stems), encoding="utf-8")
    return ["calls.csv"]


# ---------- clone / commit_and_push ----------

def test_clone_from_local_remote(bare, tmp_path):
    dest = tmp_path / "work"
    clone(SLUG, dest, "tok", remote_url=str(bare))
    assert (dest / "README.md").read_text(encoding="utf-8") == "seed\n"
    r = remote_for(dest, bare)
    assert r.head_sha() == origin_head(bare)


def _push_symlink(origin: Path, seed: Path, rel: str, target: str) -> None:
    """Commit a symlink `rel` -> `target` on origin's main, from a scratch clone at `seed`."""
    if not seed.exists():
        subprocess.run(["git", "clone", "-q", str(origin), str(seed)], check=True)
    git(seed, "pull", "-q", str(origin), "main")
    (seed / rel).parent.mkdir(parents=True, exist_ok=True)
    (seed / rel).symlink_to(target)
    git(seed, "add", "-A")
    git(seed, "commit", "-q", "-m", f"link {rel}")
    git(seed, "push", "-q", str(origin), "HEAD:main")


def test_clone_writes_symlinks_as_plain_files_now_and_after_sync(bare, tmp_path):
    seed = tmp_path / "linker"
    _push_symlink(bare, seed, "transcripts/evil.md", "/etc/passwd")
    dest = tmp_path / "work"
    clone(SLUG, dest, "tok", remote_url=str(bare))
    assert git(dest, "config", "--get", "core.symlinks") == "false"
    first = dest / "transcripts" / "evil.md"
    assert not first.is_symlink() and first.read_text(encoding="utf-8") == "/etc/passwd"

    _push_symlink(bare, seed, "digests/later.md", "../../outside.md")
    remote_for(dest, bare).sync()
    later = dest / "digests" / "later.md"
    assert not later.is_symlink() and later.read_text(encoding="utf-8") == "../../outside.md"


def test_commit_and_push_adds_edits_and_deletes(bare, tmp_path):
    dest = tmp_path / "work"
    clone(SLUG, dest, "tok", remote_url=str(bare))
    r = remote_for(dest, bare)
    (dest / "digests").mkdir()
    (dest / "digests" / "2026-10-01_wade-pruitt.md").write_text("---\ncall_id: x\n---\n", encoding="utf-8")
    (dest / "README.md").write_text("edited\n", encoding="utf-8")
    (dest / "calls.csv").unlink()
    sha = r.commit_and_push("process_call: wade-pruitt",
                            ["digests/2026-10-01_wade-pruitt.md", "README.md", "calls.csv", "never/existed.md"])
    assert sha and sha == origin_head(bare) == r.head_sha()
    assert origin_show(bare, "digests/2026-10-01_wade-pruitt.md") == "---\ncall_id: x\n---\n"
    assert origin_show(bare, "README.md") == "edited\n"
    assert origin_show(bare, "calls.csv") is None
    msg = subprocess.run(["git", "--git-dir", str(bare), "log", "-1", "--format=%s|%an"],
                         capture_output=True, text=True, check=True).stdout.strip()
    assert msg == f"process_call: wade-pruitt|{GitRemote.author_name}"


def test_commit_and_push_returns_none_when_nothing_changed(bare, tmp_path):
    dest = tmp_path / "work"
    clone(SLUG, dest, "tok", remote_url=str(bare))
    r = remote_for(dest, bare)
    before = origin_head(bare)
    assert r.commit_and_push("noop", ["README.md"]) is None
    assert origin_head(bare) == before


def test_commit_only_includes_given_paths(bare, tmp_path):
    dest = tmp_path / "work"
    clone(SLUG, dest, "tok", remote_url=str(bare))
    r = remote_for(dest, bare)
    (dest / "README.md").write_text("mine\n", encoding="utf-8")
    (dest / "stray.txt").write_text("not mine\n", encoding="utf-8")
    r.commit_and_push("readme", ["README.md"])
    assert origin_show(bare, "README.md") == "mine\n"
    assert origin_show(bare, "stray.txt") is None


def test_concurrent_push_retries_with_regenerate(bare, tmp_path):
    a_dir, b_dir = tmp_path / "a", tmp_path / "b"
    clone(SLUG, a_dir, "tok", remote_url=str(bare))
    clone(SLUG, b_dir, "tok", remote_url=str(bare))
    a, b = remote_for(a_dir, bare), remote_for(b_dir, bare)

    # B does its work first (digest plus regenerated index) but has not pushed yet.
    (b_dir / "digests").mkdir()
    (b_dir / "digests" / "call-b.md").write_text("digest b\n", encoding="utf-8")
    rebuild_calls_csv(b_dir)

    # Meanwhile A pushes its own digest and index.
    (a_dir / "digests").mkdir()
    (a_dir / "digests" / "call-a.md").write_text("digest a\n", encoding="utf-8")
    rebuild_calls_csv(a_dir)
    a_sha = a.commit_and_push("a", ["digests/call-a.md", "calls.csv"])
    assert a_sha

    seen_at_regenerate = []

    def regenerate():
        # The stale generated file must not be rewritten before regenerate runs:
        # at this point calls.csv is exactly what A pushed.
        seen_at_regenerate.append((b_dir / "calls.csv").read_text(encoding="utf-8"))
        return rebuild_calls_csv(b_dir)

    slept = []
    b._sleep = slept.append
    b_sha = b.commit_and_push("b", ["digests/call-b.md", "calls.csv"], regenerate=regenerate)
    assert b_sha == origin_head(bare)
    assert slept == []              # a lost race retries at once
    assert seen_at_regenerate == ["call_id\ncall-a\n"]
    assert origin_show(bare, "digests/call-a.md") == "digest a\n"
    assert origin_show(bare, "digests/call-b.md") == "digest b\n"
    assert origin_show(bare, "calls.csv") == "call_id\ncall-a\ncall-b\n"
    parent = subprocess.run(["git", "--git-dir", str(bare), "rev-parse", "main^"],
                            capture_output=True, text=True, check=True).stdout.strip()
    assert parent == a_sha


def test_retry_rewrites_everything_without_regenerate(bare, tmp_path):
    a_dir, b_dir = tmp_path / "a", tmp_path / "b"
    clone(SLUG, a_dir, "tok", remote_url=str(bare))
    clone(SLUG, b_dir, "tok", remote_url=str(bare))
    a, b = remote_for(a_dir, bare), remote_for(b_dir, bare)
    (a_dir / "decisions.md").write_text("D-001\n", encoding="utf-8")
    a.commit_and_push("a", ["decisions.md"])
    (b_dir / "calls.csv").write_text("call_id\nfrom-b\n", encoding="utf-8")
    (b_dir / "README.md").unlink()
    b.commit_and_push("b", ["calls.csv", "README.md"])
    assert origin_show(bare, "decisions.md") == "D-001\n"
    assert origin_show(bare, "calls.csv") == "call_id\nfrom-b\n"
    assert origin_show(bare, "README.md") is None


def test_regenerate_can_add_new_paths(bare, tmp_path):
    a_dir, b_dir = tmp_path / "a", tmp_path / "b"
    clone(SLUG, a_dir, "tok", remote_url=str(bare))
    clone(SLUG, b_dir, "tok", remote_url=str(bare))
    a, b = remote_for(a_dir, bare), remote_for(b_dir, bare)
    (a_dir / "x.md").write_text("x\n", encoding="utf-8")
    a.commit_and_push("a", ["x.md"])

    def regenerate():
        (b_dir / "findings.md").write_text("fresh\n", encoding="utf-8")
        return ["findings.md"]

    (b_dir / "y.md").write_text("y\n", encoding="utf-8")
    b.commit_and_push("b", ["y.md"], regenerate=regenerate)
    assert origin_show(bare, "findings.md") == "fresh\n"
    assert origin_show(bare, "x.md") == "x\n" and origin_show(bare, "y.md") == "y\n"


def test_push_gives_up_after_five_attempts(bare, tmp_path):
    hook = bare / "hooks" / "pre-receive"
    hook.write_text("#!/bin/sh\necho 'locked for maintenance' >&2\nexit 1\n", encoding="utf-8")
    hook.chmod(0o755)
    dest = tmp_path / "work"
    clone(SLUG, dest, "tok", remote_url=str(bare))
    r = remote_for(dest, bare)
    calls, slept = [], []
    r._sleep = slept.append
    (dest / "README.md").write_text("changed\n", encoding="utf-8")
    with pytest.raises(GitError, match="5 attempts"):
        r.commit_and_push("x", ["README.md"], regenerate=lambda: calls.append(1))
    assert len(calls) == 4          # one regenerate per retry, none before the first try
    assert len(slept) == 4          # a hook refusal is not a lost race, so it backs off between tries
    assert origin_show(bare, "README.md") == "seed\n"


def test_shallow_clone_over_file_url_syncs_and_retries(bare, tmp_path):
    url = bare.as_uri()             # file:// honours --depth, unlike a plain local path
    seed = tmp_path / "seed"
    for i in range(3):
        (seed / f"n{i}.md").write_text(f"{i}\n", encoding="utf-8")
        git(seed, "add", "-A")
        git(seed, "commit", "-q", "-m", f"n{i}")
    git(seed, "push", "-q", str(bare), "main")
    a_dir, b_dir = tmp_path / "a", tmp_path / "b"
    clone(SLUG, a_dir, "tok", remote_url=url)
    clone(SLUG, b_dir, "tok", remote_url=url)
    assert git(b_dir, "rev-parse", "--is-shallow-repository") == "true"
    a, b = remote_for(a_dir, bare), remote_for(b_dir, bare)
    a.remote_url = b.remote_url = url
    (a_dir / "from-a.md").write_text("a\n", encoding="utf-8")
    a.commit_and_push("a", ["from-a.md"])
    (b_dir / "from-b.md").write_text("b\n", encoding="utf-8")
    sha = b.commit_and_push("b", ["from-b.md"], regenerate=lambda: None)
    assert sha == origin_head(bare)
    assert origin_show(bare, "from-a.md") == "a\n" and origin_show(bare, "from-b.md") == "b\n"
    b.sync()
    assert (b_dir / "from-a.md").exists()


# ---------- sync / head_sha / blob_sha ----------

def test_sync_resets_and_cleans_to_origin(bare, tmp_path):
    a_dir, b_dir = tmp_path / "a", tmp_path / "b"
    clone(SLUG, a_dir, "tok", remote_url=str(bare))
    clone(SLUG, b_dir, "tok", remote_url=str(bare))
    a, b = remote_for(a_dir, bare), remote_for(b_dir, bare)
    (a_dir / "new.md").write_text("from a\n", encoding="utf-8")
    a.commit_and_push("a", ["new.md"])
    (b_dir / "README.md").write_text("local junk\n", encoding="utf-8")
    (b_dir / "untracked").mkdir()
    (b_dir / "untracked" / "f.txt").write_text("x", encoding="utf-8")
    b.sync()
    assert (b_dir / "README.md").read_text(encoding="utf-8") == "seed\n"
    assert (b_dir / "new.md").read_text(encoding="utf-8") == "from a\n"
    assert not (b_dir / "untracked").exists()
    assert b.head_sha() == origin_head(bare)


def _stalled_remote(tmp_path, monkeypatch) -> tuple[str, Path]:
    """An `ext::` remote that accepts the connection and then sends nothing (a stalled GitHub). Its
    helper writes its pid to the returned file, so a test can check it was stopped."""
    for key, value in {"GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "protocol.ext.allow",
                       "GIT_CONFIG_VALUE_0": "always"}.items():
        monkeypatch.setenv(key, value)
    pid_file = tmp_path / "stalled.pid"
    script = tmp_path / "stall.sh"
    script.write_text(f"#!/bin/sh\necho $$ > '{pid_file}'\nexec sleep 30\n", encoding="utf-8")
    script.chmod(0o755)
    return f"ext::{script}", pid_file


def _gone(pid: int, within: float = 5.0) -> bool:
    deadline = time.monotonic() + within
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        time.sleep(0.05)
    return False


def test_stalled_fetch_times_out_and_releases_the_lock(bare, tmp_path, monkeypatch):
    """A fetch that never answers must fail as GitError within the network timeout, not hang the processor
    past its budget or hold the MCP server's sync lock forever. Git's transfer helper is stopped too."""
    dest = tmp_path / "work"
    clone(SLUG, dest, "tok", remote_url=str(bare))
    url, pid_file = _stalled_remote(tmp_path, monkeypatch)
    r = remote_for(dest, bare)
    r.remote_url = url
    r.network_timeout = 3                           # long enough for git to start the helper on a loaded machine
    started = time.monotonic()
    with pytest.raises(GitError, match="git fetch timed out after 3 seconds"):
        r.sync()
    assert time.monotonic() - started < 12
    assert r._lock.acquire(blocking=False)          # the RLock was released
    r._lock.release()
    assert _gone(int(pid_file.read_text().strip()))
    assert not list((dest / ".git").rglob("*.lock"))


def test_network_git_commands_carry_a_timeout(bare, tmp_path, monkeypatch):
    """fetch, push and clone get a timeout; local commands (checkout, reset, add, commit) run without one.
    A timeout's GitError never carries the token from the auth options."""
    import nua.gitops as gitops
    dest = tmp_path / "work"
    clone(SLUG, dest, "tok", remote_url=str(bare))
    real = gitops._run_git
    seen: list[tuple[str, float | None]] = []

    def spy(args, **kw):
        seen.append((args[0], kw.get("timeout")))
        return real(args, **kw)

    monkeypatch.setattr(gitops, "_run_git", spy)
    r = remote_for(dest, bare)
    (dest / "x.md").write_text("x\n", encoding="utf-8")
    r.commit_and_push("x", ["x.md"])
    r.sync()
    clone(SLUG, tmp_path / "again", "tok", remote_url=str(bare))
    timeouts = dict(seen)
    assert timeouts["fetch"] == timeouts["push"] == gitops.NETWORK_TIMEOUT_SECONDS
    assert timeouts["clone"] == gitops.CLONE_TIMEOUT_SECONDS
    assert timeouts["add"] is None and timeouts["commit"] is None and timeouts["reset"] is None

    url, _ = _stalled_remote(tmp_path, monkeypatch)
    gh = GitRemote(dest, repo_slug=SLUG, token_fn=lambda: "ghs_secret")
    with pytest.raises(GitError, match="git fetch timed out") as err:
        real(["fetch", "--quiet", url, "main"], cwd=dest, pre=gh._auth_args(), timeout=1)
    assert "ghs_secret" not in str(err.value) and "Authorization" not in str(err.value)


def test_https_remotes_abort_a_stalled_transfer():
    gh = GitRemote(Path("."), repo_slug=SLUG, token_fn=lambda: "ghs_abc")
    args = gh._auth_args()
    assert "http.lowSpeedLimit=1000" in args and "http.lowSpeedTime=60" in args


def test_sync_discards_local_edits_that_conflict_with_origin(bare, tmp_path):
    a_dir, b_dir = tmp_path / "a", tmp_path / "b"
    clone(SLUG, a_dir, "tok", remote_url=str(bare))
    clone(SLUG, b_dir, "tok", remote_url=str(bare))
    a, b = remote_for(a_dir, bare), remote_for(b_dir, bare)
    (a_dir / "README.md").write_text("from a\n", encoding="utf-8")
    a.commit_and_push("a", ["README.md"])
    (b_dir / "README.md").write_text("half-written by a failed task\n", encoding="utf-8")
    git(b_dir, "add", "README.md")
    b.sync()
    assert (b_dir / "README.md").read_text(encoding="utf-8") == "from a\n"
    assert git(b_dir, "status", "--porcelain") == ""


def test_refuses_to_reset_or_commit_in_an_unmanaged_checkout(bare, tmp_path):
    plain = tmp_path / "plain"
    subprocess.run(["git", "clone", "-q", str(bare), str(plain)], check=True)
    (plain / "README.md").write_text("precious uncommitted work\n", encoding="utf-8")
    r = remote_for(plain, bare)
    with pytest.raises(GitError, match="gitops.clone"):
        r.sync()
    with pytest.raises(GitError, match="gitops.clone"):
        r.commit_and_push("x", ["README.md"])
    assert (plain / "README.md").read_text(encoding="utf-8") == "precious uncommitted work\n"
    assert origin_show(bare, "README.md") == "seed\n"


def test_blob_sha_matches_git_hash_object(bare, tmp_path):
    dest = tmp_path / "work"
    clone(SLUG, dest, "tok", remote_url=str(bare))
    r = remote_for(dest, bare)
    expected = subprocess.run(["git", "hash-object", str(dest / "README.md")],
                              capture_output=True, text=True, check=True).stdout.strip()
    assert r.blob_sha("README.md") == expected
    assert r.blob_sha("requests/missing.yml") is None


def test_last_commit_message_reads_the_clone_for_a_local_remote(bare, tmp_path):
    dest = tmp_path / "work"
    clone(SLUG, dest, "tok", remote_url=str(bare))
    r = remote_for(dest, bare)
    r._http = httpx.Client(transport=httpx.MockTransport(lambda req: pytest.fail("no API call for a local remote")))
    rel = "requests/2026-10-06_sam_keep.yml"
    (dest / "requests").mkdir()
    (dest / rel).write_text("action: keep\n", encoding="utf-8")
    r.commit_and_push("request: keep", [rel])
    (dest / rel).unlink()
    message = f"Not applied: B-042 does not exist [{rel}@abc1234]"
    r.commit_and_push(message, [rel])
    assert r.last_commit_message(rel) == message                 # the deletion is the newest commit on it
    assert r.last_commit_message("README.md") == "seed"
    assert r.last_commit_message("requests/never.yml") is None
    api_down = GitRemote(dest, repo_slug=SLUG, token_fn=lambda: "t",           # a GitHub remote whose API fails
                         http=httpx.Client(transport=httpx.MockTransport(lambda req: httpx.Response(502))))
    assert api_down.last_commit_message(rel) == message
    assert GitRemote(tmp_path / "not-a-clone", repo_slug=SLUG, token_fn=lambda: "t",
                     remote_url=str(bare)).last_commit_message(rel) is None


def test_generated_paths_match_indexes_module():
    indexes = pytest.importorskip("nua.indexes")
    assert sorted(GENERATED_PATHS) == sorted(indexes.GENERATED)


# ---------- create_file_only (GitHub Contents API) ----------

def api_remote(tmp_path, handler) -> GitRemote:
    http = httpx.Client(transport=httpx.MockTransport(handler))
    r = GitRemote(tmp_path, repo_slug=SLUG, token_fn=lambda: "ghs_installtoken", http=http)
    r._sleep = lambda s: None
    return r


def test_create_file_only_created(tmp_path):
    seen = []

    def handler(request: httpx.Request):
        seen.append(request)
        return httpx.Response(201, json={"content": {"sha": "abc"}, "commit": {"sha": "def"}})

    r = api_remote(tmp_path, handler)
    assert r.create_file_only("transcripts/granola_not_1.md", "hello\n", "ingest granola not_1") is True
    [req] = seen
    assert req.method == "PUT"
    assert str(req.url) == f"https://api.github.com/repos/{SLUG}/contents/transcripts/granola_not_1.md"
    assert req.headers["Authorization"] == "Bearer ghs_installtoken"
    body = json.loads(req.content)
    assert "sha" not in body
    assert body["message"] == "ingest granola not_1"
    assert body["branch"] == "main"
    assert base64.b64decode(body["content"]).decode("utf-8") == "hello\n"


def _commit_json(date: str) -> dict:
    return {"sha": date, "commit": {"committer": {"date": date}, "author": {"date": date}}}


def test_added_date_reads_the_oldest_commit_on_the_last_page(tmp_path):
    from zoneinfo import ZoneInfo
    seen = []
    last = "https://api.github.com/repositories/1/commits?path=transcripts%2Fa.md&sha=main&per_page=100&page=3"

    def handler(request: httpx.Request):
        seen.append(request)
        if "page=3" in str(request.url):
            return httpx.Response(200, json=[_commit_json("2026-09-19T01:30:00Z"), _commit_json("2026-09-18T03:00:00Z")])
        return httpx.Response(200, json=[_commit_json("2026-09-25T10:00:00Z")],
                              headers={"Link": f'<{last.replace("page=3", "page=2")}>; rel="next", <{last}>; rel="last"'})

    r = api_remote(tmp_path, handler)
    assert r.added_date("transcripts/a.md") == "2026-09-18"
    assert r.added_date("transcripts/a.md", ZoneInfo("America/Los_Angeles")) == "2026-09-17"
    first = seen[0]
    assert first.url.path == f"/repos/{SLUG}/commits"
    assert dict(first.url.params) == {"path": "transcripts/a.md", "sha": "main", "per_page": "100"}
    assert first.headers["Authorization"] == "Bearer ghs_installtoken"
    assert str(seen[1].url) == last


def test_added_date_single_page_missing_file_errors_and_local_remotes(tmp_path):
    pages = {"transcripts/a.md": httpx.Response(200, json=[_commit_json("2026-09-20T10:00:00Z"),
                                                           _commit_json("2026-09-18T10:00:00Z")]),
             "transcripts/none.md": httpx.Response(200, json=[]),
             "transcripts/err.md": httpx.Response(500, text="boom")}

    def handler(request: httpx.Request):
        if request.url.params["path"] == "transcripts/down.md":
            raise httpx.ConnectError("down")
        return pages[request.url.params["path"]]

    r = api_remote(tmp_path, handler)
    assert r.added_date("transcripts/a.md") == "2026-09-18"
    assert r.added_date("transcripts/none.md") is None
    assert r.added_date("transcripts/err.md") is None
    assert r.added_date("transcripts/down.md") is None
    local = GitRemote(tmp_path, repo_slug=SLUG, token_fn=lambda: pytest.fail("no token for a local remote"),
                      remote_url=str(tmp_path / "origin"))
    assert local.added_date("transcripts/a.md") is None


def test_last_commit_message_asks_the_commits_api_for_the_newest_commit(tmp_path):
    seen = []
    rel = "requests/2026-10-06_sam_keep.yml"
    message = f"Not applied: B-042 does not exist [{rel}@abc1234]\n\nbody line"
    pages = {rel: httpx.Response(200, json=[{"sha": "d1", "commit": {"message": message}}]),
             "requests/none.yml": httpx.Response(200, json=[]),
             "requests/err.yml": httpx.Response(500, text="boom"),
             "requests/odd.yml": httpx.Response(200, json=[{"sha": "d2", "commit": {}}])}

    def handler(request: httpx.Request):
        seen.append(request)
        if request.url.params["path"] == "requests/down.yml":
            raise httpx.ConnectError("down")
        return pages[request.url.params["path"]]

    r = api_remote(tmp_path, handler)                  # tmp_path is no clone, so there is no git log fallback
    assert r.last_commit_message(rel) == message
    first = seen[0]
    assert first.url.path == f"/repos/{SLUG}/commits"
    assert dict(first.url.params) == {"path": rel, "sha": "main", "per_page": "1"}
    assert first.headers["Authorization"] == "Bearer ghs_installtoken"
    for gone in ("requests/none.yml", "requests/err.yml", "requests/odd.yml", "requests/down.yml"):
        assert r.last_commit_message(gone) is None

    def no_token():
        raise GitError("installation token request returned 401")
    r.token_fn = no_token
    assert r.last_commit_message(rel) is None


def test_create_file_only_existing_returns_false(tmp_path):
    def handler(request: httpx.Request):
        return httpx.Response(422, json={"message": "Invalid request.\n\n\"sha\" wasn't supplied.",
                                         "status": "422"})

    r = api_remote(tmp_path, handler)
    assert r.create_file_only("transcripts/x.md", "x", "m") is False


def test_create_file_only_422_checks_existence_when_message_is_unclear(tmp_path):
    def handler(request: httpx.Request):
        if request.method == "PUT":
            return httpx.Response(422, json={"message": "Validation Failed"})
        return httpx.Response(200, json={"sha": "abc", "path": "transcripts/x.md"})

    assert api_remote(tmp_path, handler).create_file_only("transcripts/x.md", "x", "m") is False

    def handler_missing(request: httpx.Request):
        if request.method == "PUT":
            return httpx.Response(422, json={"message": "Validation Failed"})
        return httpx.Response(404, json={"message": "Not Found"})

    with pytest.raises(GitError, match="422"):
        api_remote(tmp_path, handler_missing).create_file_only("transcripts/x.md", "x", "m")


def test_create_file_only_retries_conflicts_and_server_errors(tmp_path):
    replies = iter([httpx.Response(409, json={"message": "is at abc but expected def"}),
                    httpx.Response(502, text="bad gateway"),
                    httpx.Response(201, json={})])
    count = []

    def handler(request):
        count.append(1)
        return next(replies)

    assert api_remote(tmp_path, handler).create_file_only("transcripts/x.md", "x", "m") is True
    assert len(count) == 3


def test_create_file_only_gives_up(tmp_path):
    r = api_remote(tmp_path, lambda req: httpx.Response(503, text="unavailable"))
    with pytest.raises(GitError):
        r.create_file_only("transcripts/x.md", "x", "m")


def test_create_file_only_does_not_retry_auth_errors(tmp_path):
    count = []

    def handler(request):
        count.append(1)
        return httpx.Response(401, json={"message": "Bad credentials"})

    with pytest.raises(GitError, match="401"):
        api_remote(tmp_path, handler).create_file_only("transcripts/x.md", "x", "m")
    assert len(count) == 1


def test_create_file_only_escapes_path(tmp_path):
    seen = []

    def handler(request):
        seen.append(request.url.raw_path.decode())
        return httpx.Response(201, json={})

    api_remote(tmp_path, handler).create_file_only("prep/2026-10-01 wade pruitt#2.md", "x", "m")
    assert seen == [f"/repos/{SLUG}/contents/prep/2026-10-01%20wade%20pruitt%232.md"]


# ---------- GitHubAppAuth ----------

@pytest.fixture(scope="module")
def rsa_key():
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                            serialization.NoEncryption()).decode()
    return pem, key.public_key()


def test_app_auth_fetches_and_caches_installation_token(rsa_key):
    pem, public = rsa_key
    seen = []

    def handler(request: httpx.Request):
        seen.append(request)
        expires = (datetime.now(timezone.utc) + timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
        return httpx.Response(201, json={"token": f"ghs_{len(seen)}", "expires_at": expires})

    auth = GitHubAppAuth("12345", pem, "678", http=httpx.Client(transport=httpx.MockTransport(handler)))
    assert auth.token() == "ghs_1"
    assert auth.token() == "ghs_1"
    [req] = seen
    assert req.method == "POST"
    assert str(req.url) == "https://api.github.com/app/installations/678/access_tokens"
    scheme, app_jwt = req.headers["Authorization"].split(" ", 1)
    assert scheme == "Bearer"
    claims = jwt.decode(app_jwt, public, algorithms=["RS256"])
    assert claims["iss"] == "12345"
    assert claims["exp"] - claims["iat"] <= 600
    assert claims["iat"] <= time.time()


def test_app_auth_refreshes_near_expiry(rsa_key):
    pem, _ = rsa_key
    count = []

    def handler(request):
        count.append(1)
        expires = (datetime.now(timezone.utc) + timedelta(minutes=2)).strftime("%Y-%m-%dT%H:%M:%SZ")
        return httpx.Response(201, json={"token": f"ghs_{len(count)}", "expires_at": expires})

    auth = GitHubAppAuth("1", pem, "2", http=httpx.Client(transport=httpx.MockTransport(handler)))
    assert auth.token() == "ghs_1"
    assert auth.token() == "ghs_2"       # less than 5 minutes left, so it refreshes


def test_app_auth_accepts_escaped_newlines_in_key(rsa_key):
    pem, _ = rsa_key
    escaped = pem.replace("\n", "\\n")

    def handler(request):
        expires = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
        return httpx.Response(201, json={"token": "ghs_x", "expires_at": expires})

    auth = GitHubAppAuth("1", escaped, "2", http=httpx.Client(transport=httpx.MockTransport(handler)))
    assert auth.token() == "ghs_x"


def test_app_auth_raises_on_failure(rsa_key):
    pem, _ = rsa_key
    auth = GitHubAppAuth("1", pem, "2", http=httpx.Client(
        transport=httpx.MockTransport(lambda r: httpx.Response(401, json={"message": "bad jwt"}))))
    with pytest.raises(GitError, match="401"):
        auth.token()


def test_https_auth_header_is_not_used_for_local_remotes(tmp_path):
    local = GitRemote(tmp_path, repo_slug=SLUG, token_fn=lambda: pytest.fail("token not needed"),
                      remote_url=str(tmp_path / "origin.git"))
    assert local._auth_args() == []
    gh = GitRemote(tmp_path, repo_slug=SLUG, token_fn=lambda: "ghs_abc")
    args = gh._auth_args()
    assert gh._url() == f"https://github.com/{SLUG}.git"
    assert args[0] == "-c"
    header = args[1].split("=", 1)[1]
    assert header == "Authorization: Basic " + base64.b64encode(b"x-access-token:ghs_abc").decode()
