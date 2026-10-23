"""The GitHub repo as the store of truth: app auth, clone, sync, commit and push, create-only writes.

`GitRemote` implements ports.Remote on a local clone plus the GitHub Contents API.

Safety: `sync()` hard-resets and cleans the working tree, and `commit_and_push` commits. Both refuse to
run in a checkout that `clone()` did not create (it marks its clones with `git config nua.managed true`),
so pointing a GitRemote at someone's working copy can never wipe or commit their work.

The installation token is passed to git as a one-off `http.extraHeader`, never written into the URL or
`.git/config`.
"""
from __future__ import annotations

import base64
import os
import signal
import subprocess
import threading
import time
from datetime import datetime, timezone, tzinfo
from pathlib import Path
from typing import Any, Callable
from urllib.parse import quote

import httpx

GITHUB_API = "https://api.github.com"
MANAGED_KEY = "nua.managed"
PUSH_ATTEMPTS = 5
CREATE_ATTEMPTS = 4
# Network git commands (fetch, push) are killed after this; clone gets longer, it moves the whole tree.
# They run under locks (GitRemote._lock, the MCP server's sync lock) and inside the processor's task
# budget, so a stalled GitHub must fail fast rather than hang.
NETWORK_TIMEOUT_SECONDS = 180
CLONE_TIMEOUT_SECONDS = 300
LOW_SPEED_LIMIT = 1000            # bytes a second; https transfers slower than this ...
LOW_SPEED_SECONDS = 60            # ... for this long are aborted by git itself

# Files the processor regenerates from the digests (keep in step with nua.indexes.GENERATED).
# On a retried push they are rebuilt by regenerate(), never restored from the stale copy.
GENERATED_PATHS = ("calls.csv", "claims.csv", "findings.md", "beliefs-status.md", "open-questions.md")


class GitError(Exception):
    """A git command or GitHub API call failed."""


def _api_headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28"}


def _run_git(args: list[str], *, cwd: Path | None = None, pre: list[str] | None = None,
             check: bool = True, timeout: float | None = None) -> subprocess.CompletedProcess:
    """Run `git [pre...] args...`. `pre` holds `-c` options that may carry a token, so they never
    appear in error messages. Never prompts for credentials.

    `timeout` (seconds) is for network commands (fetch, push, clone): past it git is killed and
    GitError is raised, even with check=False, so a stalled GitHub cannot hold a caller's lock or
    outlast a task's budget.
    """
    env = dict(os.environ, GIT_TERMINAL_PROMPT="0")
    cmd = ["git", *(pre or []), *args]
    if timeout is None:
        res = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, env=env)
    else:
        res = _run_with_timeout(cmd, cwd, env, timeout, args[0])
    if check and res.returncode != 0:
        raise GitError(f"git {args[0]} failed ({res.returncode}): {(res.stderr or res.stdout).strip()[:1000]}")
    return res


def _run_with_timeout(cmd: list[str], cwd: Path | None, env: dict, timeout: float,
                      verb: str) -> subprocess.CompletedProcess:
    """Run `cmd` in its own process group; past `timeout`, stop the whole group and raise GitError.

    The group matters: git runs the transfer in helpers (git-remote-https, fetch-pack) that outlive a
    killed parent. SIGTERM goes first so git removes its lock files (a SIGKILLed fetch can leave
    `*.lock` behind and fail every later fetch), then SIGKILL for anything still running.
    """
    proc = subprocess.Popen(cmd, cwd=cwd, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                            start_new_session=True)
    try:
        out, err = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        _stop_group(proc)
        raise GitError(f"git {verb} timed out after {timeout:g} seconds") from None
    return subprocess.CompletedProcess(cmd, proc.returncode, out, err)


def _stop_group(proc: subprocess.Popen, grace: float = 5.0) -> None:
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(proc.pid, sig)
        except (ProcessLookupError, PermissionError):
            pass
        try:
            proc.communicate(timeout=grace)      # drain the pipes and reap
            return
        except subprocess.TimeoutExpired:
            continue
    for stream in (proc.stdout, proc.stderr):    # a process outside the group still holds a pipe
        if stream is not None:
            stream.close()
    proc.kill()
    proc.wait()


def _https_url(repo_slug: str) -> str:
    return f"https://github.com/{repo_slug}.git"


def _auth_header_args(url: str, token_fn: Callable[[], str]) -> list[str]:
    """`-c` options for an https remote: the auth header, and an abort when the transfer runs slower than
    LOW_SPEED_LIMIT bytes a second for LOW_SPEED_SECONDS. Nothing for a local path or file:// remote."""
    if not url.startswith("https://"):
        return []
    basic = base64.b64encode(f"x-access-token:{token_fn()}".encode("utf-8")).decode("ascii")
    return ["-c", f"http.extraHeader=Authorization: Basic {basic}",
            "-c", f"http.lowSpeedLimit={LOW_SPEED_LIMIT}", "-c", f"http.lowSpeedTime={LOW_SPEED_SECONDS}"]


# ---------- GitHub App ----------

class GitHubAppAuth:
    """Installation access tokens for the GitHub App, cached until 5 minutes before they expire.

    `private_key_pem` may come from an env var with literal `\\n` sequences; they are turned into newlines.
    """

    REFRESH_MARGIN_SECONDS = 300

    def __init__(self, app_id: str, private_key_pem: str, installation_id: str, *, http: httpx.Client | None = None):
        self.app_id = str(app_id)
        if "\\n" in private_key_pem and "\n" not in private_key_pem.strip():
            private_key_pem = private_key_pem.replace("\\n", "\n")
        self.private_key_pem = private_key_pem
        self.installation_id = str(installation_id)
        self.http = http or httpx.Client(timeout=10.0)
        self._token: str | None = None
        self._expires_at = 0.0
        self._lock = threading.Lock()
        self._clock: Callable[[], float] = time.time

    def app_jwt(self) -> str:
        """A short-lived RS256 JWT that authenticates as the app itself (valid 9 minutes, 60s clock skew)."""
        import jwt  # PyJWT

        now = int(self._clock())
        claims = {"iat": now - 60, "exp": now + 540, "iss": self.app_id}
        return jwt.encode(claims, self.private_key_pem, algorithm="RS256")

    def token(self) -> str:
        """The current installation token, fetching a new one when none is cached or it is about to expire."""
        with self._lock:
            if self._token and self._clock() < self._expires_at - self.REFRESH_MARGIN_SECONDS:
                return self._token
            url = f"{GITHUB_API}/app/installations/{self.installation_id}/access_tokens"
            r = self.http.post(url, headers=_api_headers(self.app_jwt()))
            if r.status_code != 201:
                raise GitError(f"GitHub installation token request returned {r.status_code}: {r.text[:300]}")
            data = r.json()
            self._token = data["token"]
            self._expires_at = datetime.fromisoformat(data["expires_at"].replace("Z", "+00:00")).timestamp()
            return self._token


# ---------- clone ----------

def clone(repo_slug: str, dest: Path, token: str, *, depth: int = 1, remote_url: str | None = None) -> None:
    """Clone the repo into `dest` (shallow by default) and mark it as managed by gitops.

    `origin` is set to the plain https URL (or `remote_url`); the token is only used for this command.
    `remote_url` points at another location, such as a local bare repo in tests.

    `core.symlinks=false` is stored in the clone's config, so this checkout and every later sync write a
    committed symlink as a plain text file holding the link target. The pipeline never follows a link
    out of the repo (ports.RepoFS also refuses them).
    """
    url = remote_url or _https_url(repo_slug)
    args = ["clone", "--quiet", "-c", "core.symlinks=false"]
    if depth and depth > 0:
        args.append(f"--depth={depth}")
    args += [url, str(dest)]
    _run_git(args, pre=_auth_header_args(url, lambda: token), timeout=CLONE_TIMEOUT_SECONDS)
    _run_git(["config", MANAGED_KEY, "true"], cwd=Path(dest))


# ---------- remote ----------

class GitRemote:
    """ports.Remote on a local clone at `root` (made by `clone()`) and the GitHub Contents API.

    `token_fn` returns a current installation token (usually `GitHubAppAuth.token`). `remote_url` replaces
    the GitHub URL for fetch and push (tests use a local bare repo); the Contents API always targets `repo_slug`.
    """

    author_name = "nua-discovery[bot]"
    author_email = "nua-discovery@users.noreply.github.com"

    def __init__(self, root: Path, *, repo_slug: str, token_fn: Callable[[], str], branch: str = "main",
                 http: httpx.Client | None = None, remote_url: str | None = None):
        self.root = Path(root)
        self.repo_slug = repo_slug
        self.token_fn = token_fn
        self.branch = branch
        self.remote_url = remote_url
        self._http = http
        self.generated: tuple[str, ...] = GENERATED_PATHS
        self._sleep: Callable[[float], None] = time.sleep
        self._lock = threading.RLock()     # one sync or commit at a time (the MCP server syncs from threads)
        self.network_timeout: float = NETWORK_TIMEOUT_SECONDS   # per fetch or push

    # ---------- plumbing ----------

    @property
    def http(self) -> httpx.Client:
        if self._http is None:
            self._http = httpx.Client(timeout=10.0)
        return self._http

    def _url(self) -> str:
        return self.remote_url or _https_url(self.repo_slug)

    def _auth_args(self) -> list[str]:
        return _auth_header_args(self._url(), self.token_fn)

    def _git(self, *args: str, check: bool = True, auth: bool = False) -> subprocess.CompletedProcess:
        """A git command in the clone. `auth` marks a network command (fetch, push): it gets the remote's
        auth options and the network timeout."""
        pre = self._auth_args() if auth else []
        return _run_git(list(args), cwd=self.root, pre=pre, check=check,
                        timeout=self.network_timeout if auth else None)

    def _require_managed(self) -> None:
        res = self._git("config", "--get", MANAGED_KEY, check=False)
        if res.stdout.strip() != "true":
            raise GitError(f"{self.root} was not created by gitops.clone; refusing to reset or commit there")

    # ---------- ports.Remote ----------

    def sync(self) -> None:
        """Fetch the branch and hard-reset the working tree to it, removing untracked files."""
        with self._lock:
            self._require_managed()
            tracking = f"refs/remotes/origin/{self.branch}"
            self._git("fetch", "--quiet", self._url(), f"+refs/heads/{self.branch}:{tracking}", auth=True)
            self._git("checkout", "--quiet", "--force", "-B", self.branch, tracking)
            self._git("reset", "--quiet", "--hard", tracking)
            self._git("clean", "-fdq")

    def head_sha(self) -> str:
        """Sha of the checked-out commit."""
        return self._git("rev-parse", "HEAD").stdout.strip()

    def blob_sha(self, rel: str) -> str | None:
        """Git blob sha of `rel` at HEAD (the same sha GitHub reports), or None when HEAD has no such file."""
        res = self._git("rev-parse", "--verify", "--quiet", f"HEAD:{rel}", check=False)
        if res.returncode != 0:
            return None
        return res.stdout.strip() or None

    def create_file_only(self, rel: str, content: str, message: str) -> bool:
        """Create `rel` on the branch through the Contents API with no sha, so it can never overwrite.

        Returns False when the file already exists. Conflicts (409), server errors and network errors are
        retried a few times within a few seconds; anything else raises GitError. The local clone sees the
        new file after the next sync().
        """
        url = f"{GITHUB_API}/repos/{self.repo_slug}/contents/{quote(rel)}"
        body = {"message": message, "branch": self.branch,
                "content": base64.b64encode(content.encode("utf-8")).decode("ascii")}
        last = ""
        for attempt in range(CREATE_ATTEMPTS):
            if attempt:
                self._sleep(0.5 * 2 ** (attempt - 1))
            try:
                r = self.http.put(url, json=body, headers=_api_headers(self.token_fn()))
            except httpx.TransportError as e:
                last = f"network error: {e}"
                continue
            if r.status_code in (200, 201):
                return True
            if r.status_code == 422:
                if '"sha"' in _error_message(r) or self._exists_on_branch(rel):
                    return False
                raise GitError(f"create {rel} returned 422: {r.text[:300]}")
            if r.status_code == 409 or r.status_code >= 500:
                last = f"{r.status_code}: {r.text[:300]}"
                continue
            raise GitError(f"create {rel} returned {r.status_code}: {r.text[:300]}")
        raise GitError(f"create {rel} failed after {CREATE_ATTEMPTS} attempts ({last})")

    def added_date(self, rel: str, tz: tzinfo | None = None) -> str | None:
        """The day `rel` was first committed on the branch, 'YYYY-MM-DD' in `tz` (UTC when None), or None
        when it cannot be told (no GitHub remote, no such file on the branch, an API error).

        Clones are shallow, so this asks the GitHub commits API (newest first, 100 a page) and reads the
        oldest commit on the last page. Optional: nothing in the pipeline needs it yet.
        """
        if self.remote_url and not self.remote_url.startswith("https://"):
            return None
        url = f"{GITHUB_API}/repos/{self.repo_slug}/commits"
        params = {"path": rel, "sha": self.branch, "per_page": 100}
        try:
            r = self.http.get(url, params=params, headers=_api_headers(self.token_fn()))
            last = r.links.get("last", {}).get("url") if r.status_code == 200 else None
            if last:
                r = self.http.get(last, headers=_api_headers(self.token_fn()))
            if r.status_code != 200:
                return None
            commits = r.json()
        except (httpx.HTTPError, ValueError):
            return None
        if not isinstance(commits, list) or not commits or not isinstance(commits[-1], dict):
            return None
        info = commits[-1].get("commit") or {}
        stamp = ((info.get("committer") or {}).get("date") or (info.get("author") or {}).get("date") or "")
        try:
            when = datetime.fromisoformat(str(stamp).replace("Z", "+00:00"))
        except ValueError:
            return None
        if when.tzinfo is None:
            when = when.replace(tzinfo=timezone.utc)
        return when.astimezone(tz or timezone.utc).date().isoformat()

    def last_commit_message(self, rel: str) -> str | None:
        """Full message of the newest commit on the branch that touched `rel` (its deletion included), or
        None when there is none or it cannot be told. nua/apply.py reads a lost outcome from it.

        A GitHub remote asks the commits API (newest first, one result). On an API error, and for a local
        remote (tests), it reads `git log` in the clone instead. The clone is shallow, so that only sees
        commits since it was made: an older commit is not found (None), and a file already in the oldest
        local commit reads as added there.
        """
        message = self._api_last_message(rel) if self._on_github() else None
        return message if message is not None else self._local_last_message(rel)

    def _on_github(self) -> bool:
        return not self.remote_url or self.remote_url.startswith("https://")

    def _api_last_message(self, rel: str) -> str | None:
        url = f"{GITHUB_API}/repos/{self.repo_slug}/commits"
        params = {"path": rel, "sha": self.branch, "per_page": 1}
        try:
            r = self.http.get(url, params=params, headers=_api_headers(self.token_fn()))
            commits = r.json() if r.status_code == 200 else None
        except (httpx.HTTPError, ValueError, GitError):
            return None
        if not isinstance(commits, list) or not commits or not isinstance(commits[0], dict):
            return None
        message = (commits[0].get("commit") or {}).get("message")
        return message if isinstance(message, str) else None

    def _local_last_message(self, rel: str) -> str | None:
        if not (self.root / ".git").exists():     # never read an enclosing repo's history
            return None
        try:
            res = self._git("log", "-1", "--format=%B", "--", rel, check=False)
        except OSError:
            return None
        text = res.stdout.strip() if res.returncode == 0 else ""
        return text or None

    def _exists_on_branch(self, rel: str) -> bool:
        url = f"{GITHUB_API}/repos/{self.repo_slug}/contents/{quote(rel)}"
        r = self.http.get(url, params={"ref": self.branch}, headers=_api_headers(self.token_fn()))
        return r.status_code == 200

    def commit_and_push(self, message: str, paths: list[str], regenerate: Any = None) -> str | None:
        """Commit `paths` (adds, edits and deletions) and push. Returns the new commit sha, or None when
        nothing in `paths` changed.

        On a rejected push: sync(), restore the remembered content of every non-generated path (deleting
        the ones that were deleted), call `regenerate()` to rebuild the generated files from the synced
        tree, commit and push again, up to 5 attempts. If `regenerate` returns a list of paths, they are
        committed too. Without `regenerate`, generated paths are restored like any other.
        """
        with self._lock:
            self._require_managed()
            paths = list(dict.fromkeys(paths))
            remembered = {p: self._read_bytes(p) for p in paths}
            stage = list(paths)
            last = ""
            for attempt in range(1, PUSH_ATTEMPTS + 1):
                if attempt > 1:
                    self.sync()
                    for p, data in remembered.items():
                        if regenerate is not None and p in self.generated:
                            continue
                        self._restore(p, data)
                    if regenerate is not None:
                        extra = regenerate()
                        if isinstance(extra, (list, tuple, set)):
                            stage = list(dict.fromkeys([*paths, *extra]))
                sha = self._commit(message, stage)
                if sha is None:
                    return None
                res = self._git("push", self._url(), f"HEAD:refs/heads/{self.branch}", check=False, auth=True)
                if res.returncode == 0:
                    return sha
                last = f"{res.stdout}\n{res.stderr}".strip()
                if attempt < PUSH_ATTEMPTS and not _lost_race(last):
                    self._sleep(min(2 ** attempt, 10))    # network or server trouble: back off a little
            raise GitError(f"push to {self.branch} failed after {PUSH_ATTEMPTS} attempts: {last[:1000]}")

    # ---------- helpers ----------

    def _read_bytes(self, rel: str) -> bytes | None:
        p = self.root / rel
        return p.read_bytes() if p.is_file() else None

    def _restore(self, rel: str, data: bytes | None) -> None:
        p = self.root / rel
        if data is None:
            if p.exists():
                p.unlink()
            return
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)

    def _commit(self, message: str, paths: list[str]) -> str | None:
        """Stage exactly `paths` and commit them. None when nothing staged differs from HEAD."""
        present = [p for p in paths if (self.root / p).exists()]
        missing = [p for p in paths if not (self.root / p).exists()]
        if present:
            self._git("add", "--", *present)
        if missing:
            self._git("rm", "--cached", "--quiet", "--ignore-unmatch", "--", *missing)
        if self._git("diff", "--cached", "--quiet", check=False).returncode == 0:
            return None
        identity = ["-c", f"user.name={self.author_name}", "-c", f"user.email={self.author_email}",
                    "-c", "commit.gpgsign=false"]
        _run_git(["commit", "--quiet", "--no-verify", "-m", message], cwd=self.root, pre=identity)
        return self.head_sha()


def _error_message(r: httpx.Response) -> str:
    """The `message` field of a GitHub error response, or the raw text when it is not JSON."""
    try:
        data = r.json()
    except ValueError:
        return r.text
    return str(data.get("message", "")) if isinstance(data, dict) else r.text


def _lost_race(output: str) -> bool:
    """True when a failed push lost a race (the branch moved on), as opposed to auth, hook or network trouble.

    Matches `! [rejected] ... (fetch first)` and `(non-fast-forward)`, not `! [remote rejected]`."""
    s = output.lower()
    return any(k in s for k in ("[rejected]", "non-fast-forward", "fetch first", "stale info", "cannot lock ref"))
