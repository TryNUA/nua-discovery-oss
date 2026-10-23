"""nua/setup/slack.py, nua/setup/github_app.py and nua/setup/receiver.py, offline.

GitHub's API is an httpx.MockTransport; the browser is a thread that fetches the local page and
follows the redirect GitHub would send. Nothing reaches the network.
"""
from __future__ import annotations

import argparse
import json
import re
import threading
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

import httpx
import jwt
import pytest
import yaml
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from conftest import ROOT
from nua.setup import github_app, receiver, slack

RECV = "https://nua-receiver-123456789.us-west1.run.app"


@pytest.fixture(scope="module")
def pem() -> str:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                             serialization.NoEncryption()).decode()


# ---------- Slack ----------

def test_slack_manifest_matches_the_manual_one():
    manual = yaml.safe_load((ROOT / "docs" / "slack-app-manifest.yml").read_text(encoding="utf-8"))
    built = slack.render_manifest(RECV, "Nua Discovery")
    host = RECV.removeprefix("https://")
    expected = json.loads(json.dumps(manual).replace("RECEIVER_URL", host))
    assert built == expected


def test_slack_manifest_names_urls_and_no_events():
    m = slack.render_manifest("nua-receiver-1.europe-west1.run.app/", "Acme Bot", "Acme Discovery")
    assert m["display_information"]["name"] == "Acme Discovery"
    assert m["features"]["bot_user"]["display_name"] == "Acme Bot"
    assert m["settings"]["interactivity"]["request_url"] == "https://nua-receiver-1.europe-west1.run.app/slack/interact"
    assert m["settings"]["event_subscriptions"] == {
        "request_url": "https://nua-receiver-1.europe-west1.run.app/slack/events", "bot_events": ["app_mention"]}
    assert "event_subscriptions" not in slack.render_manifest(RECV, "x", events=False)["settings"]
    assert yaml.safe_load(slack.manifest_yaml(RECV, "Acme Bot")) == slack.render_manifest(RECV, "Acme Bot")
    with pytest.raises(ValueError):
        slack.render_manifest("http://insecure.example", "x")


def test_slack_create_link_round_trips():
    m = slack.render_manifest(RECV, "Nua Discovery")
    url = slack.create_app_url(m)
    assert url.startswith("https://api.slack.com/apps?new_app=1&manifest_json=")
    q = parse_qs(urlparse(url).query)
    assert q["new_app"] == ["1"] and json.loads(q["manifest_json"][0]) == m


def test_slack_manifest_command(tmp_repo, capsys):
    args = argparse.Namespace(receiver_url=RECV, app_name=None, no_events=False)
    assert slack.command(args, tmp_repo) == 0
    out = capsys.readouterr().out
    assert f"{RECV}/slack/interact" in out and "manifest_json=" in out


def test_slack_manifest_command_explains_a_missing_url(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(receiver, "receiver_url", lambda root: None)
    args = argparse.Namespace(receiver_url=None, app_name=None, no_events=False)
    assert slack.command(args, tmp_path) == 1
    assert "--receiver-url" in capsys.readouterr().out


# ---------- receiver URL ----------

def test_receiver_url_from_tfvars_and_gcloud(tmp_path, monkeypatch):
    tf = tmp_path / "infra" / "terraform"
    tf.mkdir(parents=True)
    (tf / "terraform.tfvars").write_text('project_id = "my-discovery-project"\nregion = "europe-west1"\n')
    calls = []
    monkeypatch.setattr(receiver, "_run", lambda cmd, cwd=None: calls.append(cmd) or "42")
    assert receiver.receiver_url(tmp_path) is None and calls == []     # the example project is not yours
    (tf / "terraform.tfvars").write_text('project_id = "acme-disc"\n# region = "x"\nregion = "europe-west1"\n')
    assert receiver.receiver_url(tmp_path) == "https://nua-receiver-42.europe-west1.run.app"
    assert calls[-1][:3] == ["gcloud", "projects", "describe"] and "acme-disc" in calls[-1]


# ---------- GitHub App ----------

def test_build_manifest():
    m = github_app.build_manifest("acme-bot", RECV + "/", "https://github.com/acme/disc")
    assert m["hook_attributes"] == {"url": f"{RECV}/github", "active": True}
    assert m["default_permissions"] == {"contents": "write", "metadata": "read"}
    assert m["default_events"] == ["push"] and m["public"] is False
    assert m["url"] == "https://github.com/acme/disc" and m["name"] == "acme-bot"
    assert github_app.new_app_url(None) == "https://github.com/settings/apps/new"
    assert github_app.new_app_url("acme") == "https://github.com/organizations/acme/settings/apps/new"


def _browser(page_checks: dict, *, code: str = "the-code", bad_state: bool = False):
    """A fake browser: fetch the local page, then follow GitHub's redirect back with ?code=."""
    def on_ready(url: str) -> None:
        def go():
            page = httpx.get(url).text
            action = re.search(r'action="([^"]+)"', page).group(1).replace("&amp;", "&")
            value = re.search(r'name="manifest" value="([^"]*)"', page).group(1)
            manifest = json.loads(value.replace("&quot;", '"').replace("&#x27;", "'").replace("&amp;", "&"))
            page_checks.update(action=action, manifest=manifest)
            state = parse_qs(urlparse(action).query)["state"][0]
            redirect = manifest["redirect_url"]
            page_checks["callback"] = httpx.get(f"{redirect}?code={code}&state={'x' if bad_state else state}")
            if bad_state:
                httpx.get(f"{redirect}?code={code}&state={state}")
        page_checks["thread"] = threading.Thread(target=go, daemon=True)
        page_checks["thread"].start()
    return on_ready


def test_manifest_flow_posts_the_form_and_converts_the_code(pem):
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        assert request.url.path == "/app-manifests/the-code/conversions" and request.method == "POST"
        assert "authorization" not in request.headers
        return httpx.Response(201, json={"id": 99, "slug": "acme-bot", "pem": pem, "webhook_secret": "whs",
                                         "html_url": "https://github.com/apps/acme-bot", "client_secret": "cs"})

    checks: dict = {}
    m = github_app.build_manifest("acme-bot", RECV, "https://github.com/acme/disc")
    result = github_app.run_manifest_flow("acme", m, open_browser=False, timeout=20,
                                          http=httpx.Client(transport=httpx.MockTransport(handler)),
                                          on_ready=_browser(checks))
    assert result == {"id": 99, "slug": "acme-bot", "pem": pem, "webhook_secret": "whs",
                      "html_url": "https://github.com/apps/acme-bot"}
    assert checks["action"].startswith("https://github.com/organizations/acme/settings/apps/new?state=")
    assert checks["manifest"]["redirect_url"].startswith("http://127.0.0.1:")
    assert checks["manifest"]["hook_attributes"]["url"] == f"{RECV}/github"
    checks["thread"].join(5)
    assert checks["callback"].status_code == 200 and len(seen) == 1


def test_manifest_flow_refuses_a_wrong_state_and_sets_a_missing_secret(pem):
    patched = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "PATCH":
            assert request.url.path == "/app/hook/config"
            token = request.headers["authorization"].removeprefix("Bearer ")
            assert jwt.decode(token, options={"verify_signature": False})["iss"] == "7"
            patched.update(json.loads(request.content))
            return httpx.Response(200, json={})
        return httpx.Response(201, json={"id": 7, "slug": "s", "pem": pem, "webhook_secret": None,
                                         "html_url": "https://github.com/apps/s"})

    checks: dict = {}
    result = github_app.run_manifest_flow(None, github_app.build_manifest("s", RECV, "https://github.com/a/b"),
                                          open_browser=False, timeout=20,
                                          http=httpx.Client(transport=httpx.MockTransport(handler)),
                                          on_ready=_browser(checks, bad_state=True))
    checks["thread"].join(5)
    assert checks["callback"].status_code == 400
    assert checks["action"].startswith("https://github.com/settings/apps/new?state=")
    assert len(result["webhook_secret"]) == 64 and patched["secret"] == result["webhook_secret"]


def test_manifest_flow_times_out():
    with pytest.raises(TimeoutError):
        github_app.run_manifest_flow(None, {"name": "x"}, open_browser=False, timeout=0.2, on_ready=lambda u: None)


def test_env_values_puts_the_key_on_one_line(pem):
    values = github_app.env_values({"pem": pem.replace("\n", "\r\n"), "webhook_secret": "w"})
    key = values["GITHUB_APP_PRIVATE_KEY"]
    assert "\n" not in key and "\r" not in key and key.startswith("-----BEGIN PRIVATE KEY-----\\n")
    assert key.replace("\\n", "\n") == pem
    assert values["GITHUB_WEBHOOK_SECRET"] == "w"


def test_find_installation_id(pem):
    answers = [[], [{"id": 5, "account": {"login": "someone-else"}}, {"id": 6, "account": {"login": "Acme"}}]]

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/app/installations"
        token = request.headers["authorization"].removeprefix("Bearer ")
        assert jwt.decode(token, options={"verify_signature": False})["iss"] == "99"
        return httpx.Response(200, json=answers.pop(0))

    http = httpx.Client(transport=httpx.MockTransport(handler))
    assert github_app.find_installation_id(99, pem, http=http) is None
    assert github_app.find_installation_id(99, pem, http=http, account="acme") == 6


def test_write_env_updates_appends_and_never_breaks_lines(tmp_path):
    (tmp_path / ".env.example").write_text("# models\nGEMINI_API_KEY=\nGITHUB_WEBHOOK_SECRET=\n")
    env = tmp_path / ".env"
    github_app.write_env(env, {"GITHUB_WEBHOOK_SECRET": "abc", "GITHUB_APP_ID": 12})
    assert env.read_text() == "# models\nGEMINI_API_KEY=\nGITHUB_WEBHOOK_SECRET=abc\n\nGITHUB_APP_ID=12\n"
    assert env.stat().st_mode & 0o777 == 0o600
    github_app.write_env(env, {"GITHUB_WEBHOOK_SECRET": "def"})
    assert env.read_text().count("GITHUB_WEBHOOK_SECRET=") == 1 and "GITHUB_WEBHOOK_SECRET=def" in env.read_text()
    with pytest.raises(ValueError):
        github_app.write_env(env, {"X": "a\nb"})


def test_write_tfvars_keeps_alignment_and_starts_from_the_example(tmp_path):
    (tmp_path / "terraform.tfvars.example").write_text(
        'project_id                 = "my-discovery-project"\ngithub_app_id              = "123456"\n'
        '# github_app_slug = "x"\nimage = ""\n')
    tfvars = tmp_path / "terraform.tfvars"
    github_app.write_tfvars(tfvars, {"github_app_id": "77", "github_app_slug": "acme-bot",
                                     "schedules_on": False, "circleback_people": ["alex"]})
    text = tfvars.read_text()
    assert 'github_app_id              = "77"\n' in text
    assert '# github_app_slug = "x"\n' in text and 'github_app_slug = "acme-bot"' in text
    assert "schedules_on = false" in text and 'circleback_people = ["alex"]' in text
    assert 'project_id                 = "my-discovery-project"' in text


def test_github_app_command_writes_files_and_waits_for_the_install(tmp_path, pem, monkeypatch, capsys):
    (tmp_path / "infra" / "terraform").mkdir(parents=True)
    installs = [[], [{"id": 314, "account": {"login": "acme"}}]]

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/users/acme":
            return httpx.Response(200, json={"type": "Organization"})
        if request.url.path == "/app/installations":
            return httpx.Response(200, json=installs.pop(0))
        raise AssertionError(request.url)

    def fake_flow(owner, manifest, **kw):
        assert owner == "acme" and manifest["hook_attributes"]["url"] == f"{RECV}/github"
        assert manifest["name"] == "disc-bot"
        return {"id": 99, "slug": "disc-bot", "pem": pem, "webhook_secret": "s3cr3t-value",
                "html_url": "https://github.com/apps/disc-bot"}

    monkeypatch.setattr(github_app, "run_manifest_flow", fake_flow)
    args = argparse.Namespace(repo="acme/disc", owner=None, receiver_url=RECV, name=None, no_browser=True, port=0)
    code = github_app.command(args, tmp_path, http=httpx.Client(transport=httpx.MockTransport(handler)),
                              sleep=lambda s: None)
    out = capsys.readouterr().out
    assert code == 0, out
    assert "s3cr3t-value" not in out and "PRIVATE KEY" not in out
    assert "https://github.com/apps/disc-bot/installations/new" in out
    env = (tmp_path / ".env").read_text()
    assert "GITHUB_WEBHOOK_SECRET=s3cr3t-value" in env and "GITHUB_APP_INSTALLATION_ID=314" in env
    tfvars = (tmp_path / "infra" / "terraform" / "terraform.tfvars").read_text()
    assert 'github_app_installation_id = "314"' in tfvars and 'github_app_id = "99"' in tfvars
    assert 'repo_slug = "acme/disc"' in tfvars and "s3cr3t" not in tfvars and "PRIVATE" not in tfvars


def test_cli_wires_both_commands():
    from nua import __main__ as cli
    parser = cli.build_parser()
    assert parser.parse_args(["slack-manifest", "--receiver-url", RECV]).command == "slack-manifest"
    assert parser.parse_args(["github-app", "--repo", "a/b"]).command == "github-app"
    assert {"slack-manifest", "github-app"} <= set(cli.COMMANDS)
    assert Path(github_app.__file__).parent.name == "setup"
