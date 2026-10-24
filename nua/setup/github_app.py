"""The GitHub App, registered in one click with GitHub's manifest flow.

Flow (https://docs.github.com/en/apps/sharing-github-apps/registering-a-github-app-from-a-manifest):

1. A local page POSTs a form with one field, `manifest` (the manifest as JSON), to
   `https://github.com/settings/apps/new?state=<state>` (or
   `https://github.com/organizations/<org>/settings/apps/new?state=<state>`).
2. The person names the app and clicks Create. GitHub redirects to the manifest's `redirect_url`
   with `?code=<code>&state=<state>`; the code is good for one hour.
3. `POST https://api.github.com/app-manifests/<code>/conversions` (no auth) answers 201 with the
   app's `id`, `slug`, `html_url`, `pem` and `webhook_secret`
   (https://docs.github.com/en/rest/apps/apps#create-a-github-app-from-a-manifest).

`run_manifest_flow` does all three on a localhost server. Then the person installs the app on the
repo; `find_installation_id` sees the installation through `GET /app/installations` with the app's
JWT. `nua github-app` runs the lot and writes the secrets to `.env` and the ids to terraform.tfvars.
No secret is ever printed.
"""
from __future__ import annotations

import argparse
import html
import json
import os
import re
import secrets
import tempfile
import threading
import time
import unicodedata
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Callable
from urllib.parse import parse_qs, quote, urlparse

import httpx

from nua.gitops import GITHUB_API, GitHubAppAuth, _api_headers
from nua.setup import receiver

GITHUB = "https://github.com"
FLOW_TIMEOUT_SECONDS = 900
INSTALL_TIMEOUT_SECONDS = 600
POLL_SECONDS = 3.0
TFVARS = receiver.TF_DIR / "terraform.tfvars"


def build_manifest(name: str, receiver_url: str, repo_url: str) -> dict:
    """The app this pipeline needs: push webhooks to the receiver, contents write, private."""
    return {
        "name": name,
        "url": repo_url,
        "description": "Commits digests and indexes for nua-discovery and sends push webhooks to its receiver.",
        "hook_attributes": {"url": f"{receiver.normalize(receiver_url)}/github", "active": True},
        "public": False,
        "default_permissions": {"contents": "write", "metadata": "read"},
        "default_events": ["push"],
    }


MAX_APP_NAME = 34          # "The name cannot be longer than 34 characters." (GitHub docs, registering a GitHub App)


def default_app_name(company: str | None) -> str:
    """`<company slug>-nua-discovery`, lowercase with hyphens, cut to GitHub's 34-character limit."""
    ascii_name = unicodedata.normalize("NFKD", company or "").encode("ascii", "ignore").decode("ascii")
    slug = re.sub(r"[^a-z0-9]+", "-", ascii_name.lower()).strip("-")
    name = f"{slug}-nua-discovery" if slug else "nua-discovery"
    if len(name) > MAX_APP_NAME:
        name = f"{slug[:MAX_APP_NAME - len('-nua-discovery')].rstrip('-')}-nua-discovery"
    return name


def new_app_url(owner: str | None) -> str:
    """Where the manifest form posts: the signed-in account, or an organization."""
    if owner:
        return f"{GITHUB}/organizations/{quote(owner, safe='')}/settings/apps/new"
    return f"{GITHUB}/settings/apps/new"


def _form_page(action: str, manifest: dict) -> bytes:
    return f"""<!doctype html>
<html><head><meta charset="utf-8"><title>Create the GitHub App</title></head>
<body style="font-family: system-ui, sans-serif; margin: 3rem">
<p>Sending you to GitHub to create the app...</p>
<form id="f" method="post" action="{html.escape(action)}">
<input type="hidden" name="manifest" value="{html.escape(json.dumps(manifest))}">
<button type="submit">Continue to GitHub</button>
</form>
<script>document.getElementById("f").submit()</script>
</body></html>""".encode("utf-8")


DONE_PAGE = b"""<!doctype html><html><head><meta charset="utf-8"><title>Done</title></head>
<body style="font-family: system-ui, sans-serif; margin: 3rem"><p>App created. Go back to the terminal.</p>
</body></html>"""


def run_manifest_flow(owner: str | None, manifest: dict, *, open_browser: bool = True, port: int = 0,
                      http: httpx.Client | None = None, timeout: float = FLOW_TIMEOUT_SECONDS,
                      on_ready: Callable[[str], None] | None = None) -> dict:
    """Register the app from `manifest` and return `{id, slug, pem, webhook_secret, html_url}`.

    `owner` is an organization login, or None for the personal account signed in to the browser.
    Serves a page on 127.0.0.1:`port` (0: any free port) that auto-POSTs the manifest to GitHub, and
    waits up to `timeout` seconds for GitHub's redirect back. `on_ready` gets the local page's URL
    (default: print it); `open_browser` also opens it. If GitHub returns no webhook secret, one is
    generated and set on the app with its JWT.
    """
    state = secrets.token_urlsafe(24)
    got: dict[str, str] = {}
    done = threading.Event()
    holder: dict[str, bytes] = {}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a) -> None:      # keep the code out of the terminal
            pass

        def _send(self, status: int, body: bytes) -> None:
            self.send_response(status)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:
            u = urlparse(self.path)
            if u.path == "/":
                self._send(200, holder["page"])
            elif u.path == "/callback":
                q = parse_qs(u.query)
                if q.get("state", [""])[0] != state or not q.get("code", [""])[0]:
                    self._send(400, b"state mismatch or no code; start again from the terminal")
                    return
                got["code"] = q["code"][0]
                self._send(200, DONE_PAGE)
                done.set()
            else:
                self._send(404, b"not found")

    server = HTTPServer(("127.0.0.1", port), Handler)
    base = f"http://127.0.0.1:{server.server_address[1]}"
    form = dict(manifest, redirect_url=f"{base}/callback")
    holder["page"] = _form_page(f"{new_app_url(owner)}?state={quote(state)}", form)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        (on_ready or (lambda u: print(f"Open {u} to create the app on GitHub.", flush=True)))(base + "/")
        if open_browser:
            webbrowser.open(base + "/")
        if not done.wait(timeout):
            raise TimeoutError(f"no redirect from GitHub within {int(timeout)}s; run the command again")
    finally:
        server.shutdown()
        server.server_close()

    client = http or httpx.Client(timeout=30.0)
    r = client.post(f"{GITHUB_API}/app-manifests/{got['code']}/conversions",
                    headers={"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"})
    if r.status_code != 201:
        raise RuntimeError(f"GitHub manifest conversion returned {r.status_code}: {r.text[:300]}")
    data = r.json()
    result = {k: data.get(k) for k in ("id", "slug", "pem", "webhook_secret", "html_url")}
    if not result["webhook_secret"]:
        result["webhook_secret"] = secrets.token_hex(32)
        jwt = GitHubAppAuth(str(result["id"]), result["pem"], "").app_jwt()
        p = client.patch(f"{GITHUB_API}/app/hook/config", headers=_api_headers(jwt),
                         json={"secret": result["webhook_secret"], "content_type": "json"})
        if p.status_code != 200:
            raise RuntimeError(f"setting the webhook secret returned {p.status_code}: {p.text[:300]}")
    return result


def env_values(result: dict) -> dict:
    """The two secrets for `.env`: the private key on one line with literal `\\n`, and the webhook secret."""
    pem = str(result["pem"]).replace("\r", "")
    lines = [line for line in pem.split("\n") if line.strip()]
    return {"GITHUB_APP_PRIVATE_KEY": "\\n".join(lines) + "\\n",
            "GITHUB_WEBHOOK_SECRET": str(result["webhook_secret"])}


def find_installation_id(app_id: int | str, pem: str, *, http: httpx.Client | None = None,
                         account: str | None = None) -> int | None:
    """The app's installation id, or None while it is not installed. With `account`, only an
    installation on that user or organization counts."""
    jwt = GitHubAppAuth(str(app_id), pem, "").app_jwt()
    client = http or httpx.Client(timeout=15.0)
    r = client.get(f"{GITHUB_API}/app/installations", headers=_api_headers(jwt))
    if r.status_code != 200:
        raise RuntimeError(f"GitHub listing installations returned {r.status_code}: {r.text[:300]}")
    for inst in r.json():
        login = (inst.get("account") or {}).get("login") or (inst.get("account") or {}).get("slug")
        if account is None or (login or "").lower() == account.lower():
            return int(inst["id"])
    return None


# ---------- local files ----------

def _replace_or_append(lines: list[str], keys: dict[str, str], pattern: Callable[[str], re.Pattern]) -> list[str]:
    left = dict(keys)
    out = []
    for line in lines:
        hit = next((k for k in left if pattern(k).match(line)), None)
        if hit is not None:
            out.append(left.pop(hit))
        else:
            out.append(line)
    if left and out and out[-1].strip():
        out.append("")
    out.extend(left.values())
    return out


def _write_private(path: Path, text: str) -> None:
    """Write via a temp file in the same folder, owner-only when the file is new."""
    path.parent.mkdir(parents=True, exist_ok=True)
    mode = path.stat().st_mode & 0o777 if path.exists() else 0o600
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def write_env(path: Path | str, values: dict) -> None:
    """Set `KEY=value` lines in a dotenv file: replace the first uncommented line for each key, append
    the rest. A missing file starts from `.env.example` next to it. Values are written bare and
    never printed; a value with a line break is refused."""
    path = Path(path)
    for k, v in values.items():
        if "\n" in str(v) or "\r" in str(v):
            raise ValueError(f"{k}: a .env value must be one line")
    if path.exists():
        lines = path.read_text(encoding="utf-8").splitlines()
    elif (path.parent / ".env.example").exists():
        lines = (path.parent / ".env.example").read_text(encoding="utf-8").splitlines()
    else:
        lines = []
    keys = {k: f"{k}={v}" for k, v in values.items()}
    out = _replace_or_append(lines, keys, lambda k: re.compile(rf"^{re.escape(k)}="))
    _write_private(path, "\n".join(out) + "\n")


def _hcl(value) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (list, tuple)):
        return "[" + ", ".join(_hcl(v) for v in value) + "]"
    if isinstance(value, (int, float)):
        return str(value)
    return json.dumps(str(value))


def write_tfvars(path: Path | str, values: dict) -> None:
    """Set `key = value` lines in terraform.tfvars, keeping the alignment of a line it replaces. A
    missing file starts from terraform.tfvars.example next to it. Strings are quoted, numbers, bools and lists
    written as HCL. Never pass a secret: tfvars is for non-secret inputs only."""
    path = Path(path)
    if path.exists():
        lines = path.read_text(encoding="utf-8").splitlines()
    elif (path.parent / "terraform.tfvars.example").exists():
        lines = (path.parent / "terraform.tfvars.example").read_text(encoding="utf-8").splitlines()
    else:
        lines = []
    keys = {}
    for k, v in values.items():
        m = next((re.match(rf"^([ \t]*{re.escape(k)}[ \t]*=[ \t]*)", line) for line in lines
                  if re.match(rf"^[ \t]*{re.escape(k)}[ \t]*=", line)), None)
        keys[k] = f"{m.group(1) if m else k + ' = '}{_hcl(v)}"
    out = _replace_or_append(lines, keys, lambda k: re.compile(rf"^[ \t]*{re.escape(k)}[ \t]*="))
    _write_private(path, "\n".join(out) + "\n")


def save_app(root: Path | str, repo: str, result: dict, tfvars_path: Path | str | None = None) -> tuple[Path, Path]:
    """Write a created app: the key, webhook secret, ids and repo to `.env`, the non-secret ids to tfvars
    (default: the GCP one). Returns (env path, tfvars path). Shared by `nua github-app` and `nua setup`."""
    env_path = Path(root) / ".env"
    write_env(env_path, {**env_values(result), "GITHUB_APP_ID": result["id"], "GITHUB_APP_SLUG": result["slug"],
                         "REPO_SLUG": repo})
    tfvars = Path(tfvars_path) if tfvars_path else Path(root) / TFVARS
    write_tfvars(tfvars, {"repo_slug": repo, "github_app_id": str(result["id"]), "github_app_slug": result["slug"]})
    return env_path, tfvars


def save_installation(root: Path | str, installation: int | str, tfvars_path: Path | str | None = None) -> Path:
    """Write the installation id to `.env` and tfvars (default: the GCP one). Returns the tfvars path."""
    write_env(Path(root) / ".env", {"GITHUB_APP_INSTALLATION_ID": installation})
    tfvars = Path(tfvars_path) if tfvars_path else Path(root) / TFVARS
    write_tfvars(tfvars, {"github_app_installation_id": str(installation)})
    return tfvars


def install_url(slug: str) -> str:
    return f"{GITHUB}/apps/{slug}/installations/new"


# ---------- `nua github-app` ----------

def add_arguments(p: argparse.ArgumentParser) -> None:
    p.add_argument("--repo", required=True, help="your private instance repo, owner/name")
    p.add_argument("--owner", help="user or organization that owns the app (default: the repo's owner)")
    p.add_argument("--receiver-url", help="default: terraform output, else project_id and region in "
                                          "terraform.tfvars plus gcloud")
    p.add_argument("--name", help="app name, unique on GitHub (default: <repo name>-bot); editable on GitHub")
    p.add_argument("--no-browser", action="store_true", help="print the local URL instead of opening it")
    p.add_argument("--port", type=int, default=0, help="local port for GitHub's redirect (default: any free one)")


def _is_org(owner: str, http: httpx.Client) -> bool:
    r = http.get(f"{GITHUB_API}/users/{quote(owner, safe='')}", headers={"Accept": "application/vnd.github+json"})
    return r.status_code == 200 and r.json().get("type") == "Organization"


def command(args: argparse.Namespace, root: Path, *, http: httpx.Client | None = None,
            sleep: Callable[[float], None] = time.sleep) -> int:
    if not re.fullmatch(r"[\w.-]+/[\w.-]+", args.repo or ""):
        print("error: --repo must be owner/name", flush=True)
        return 2
    url = args.receiver_url or receiver.receiver_url(root)
    if not url:
        print(f"error: no receiver URL yet. {receiver.HOW_TO}", flush=True)
        return 1
    http = http or httpx.Client(timeout=30.0)
    repo_owner, repo_name = args.repo.split("/")
    owner = args.owner or repo_owner
    org = owner if _is_org(owner, http) else None
    name = args.name or f"{repo_name}-bot"[:34]
    manifest = build_manifest(name, url, f"{GITHUB}/{args.repo}")
    print(f"Creating a GitHub App for {args.repo} with its webhook at {receiver.normalize(url)}/github.")
    if org is None:
        print(f"Sign in to GitHub as {owner} in the browser: the app is created on the signed-in account.")
    result = run_manifest_flow(org, manifest, open_browser=not args.no_browser, port=args.port, http=http)

    env_path, tfvars_path = save_app(root, args.repo, result)
    print(f"Created {result['html_url']}")
    print(f"Wrote GITHUB_APP_PRIVATE_KEY and GITHUB_WEBHOOK_SECRET to {env_path} (values not shown).")
    print(f"Wrote repo_slug, github_app_id and github_app_slug to {tfvars_path}.")
    print()
    print("Now install it on the repo (Only select repositories > your repo):")
    print(f"  {install_url(result['slug'])}")
    print("Waiting for the installation...", flush=True)
    deadline = time.monotonic() + INSTALL_TIMEOUT_SECONDS
    installation = None
    while installation is None:
        installation = find_installation_id(result["id"], result["pem"], http=http, account=repo_owner)
        if installation is not None:
            break
        if time.monotonic() > deadline:
            print("No installation yet. Install the app, then set github_app_installation_id in "
                  f"{tfvars_path} to the number at the end of the installation page URL.")
            return 1
        sleep(POLL_SECONDS)
    save_installation(root, installation)
    print(f"Installed: github_app_installation_id = {installation} written to {tfvars_path}.")
    print("GitHub pings the webhook now; that delivery fails until the receiver is deployed, which is fine.")
    return 0


__all__ = ["default_app_name", "MAX_APP_NAME", "build_manifest", "run_manifest_flow", "env_values", "find_installation_id", "write_env", "save_app",
           "save_installation", "install_url",
           "write_tfvars", "new_app_url"]
