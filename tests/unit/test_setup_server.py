"""nua/setup/server.py (`nua setup`): the local setup API, on a temporary copy of the repo's config. Offline:
subprocess, HTTP and GitHub calls are faked."""
from __future__ import annotations

import shutil
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from conftest import ROOT
from nua import __main__ as cli
from nua.setup import configio, github_app, server

PORT = 48123
TOKEN = "test-token-123"
SECRET = "sk-very-secret-value-42"


@pytest.fixture
def root(tmp_path: Path) -> Path:
    shutil.copytree(ROOT / "config", tmp_path / "config")
    for name in configio.EXAMPLE_FILES:
        shutil.copy(ROOT / "examples" / "blank" / name, tmp_path / "config" / name)
    shutil.copy(ROOT / "tests" / "config" / "people.yml", tmp_path / "config" / "people.yml")
    shutil.copy(ROOT / ".env.example", tmp_path / ".env.example")
    for rel in ("infra/terraform/terraform.tfvars.example", "infra/aws/terraform/terraform.tfvars.example"):
        src = ROOT / rel
        if src.is_file():
            (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy(src, tmp_path / rel)
    return tmp_path


class Fakes:
    def __init__(self):
        self.commands: list[list[str]] = []
        self.answers: dict[str, tuple[int, str]] = {}
        self.health = 200
        self.tools = {"gcloud", "terraform", "uv", "aws"}

    def run(self, cmd, timeout):
        self.commands.append(cmd)
        assert timeout <= 20
        for prefix, answer in self.answers.items():
            if " ".join(cmd).startswith(prefix):
                return answer
        return 1, ""

    def which(self, name):
        return f"/usr/bin/{name}" if name in self.tools else None

    def http_get(self, url, timeout):
        return self.health


def make(root: Path, fakes: Fakes | None = None, **kw) -> tuple[TestClient, server.Setup, Fakes]:
    fakes = fakes or Fakes()
    setup = server.Setup(root, run=fakes.run, which=fakes.which, http_get=fakes.http_get, **kw)
    client = TestClient(server.create_app(setup, TOKEN, PORT), base_url=f"http://127.0.0.1:{PORT}")
    client.headers["X-Nua-Token"] = TOKEN
    return client, setup, fakes


# ---------- guard ----------

def test_api_needs_the_token(root):
    client, _, _ = make(root)
    assert client.get("/api/status").status_code == 200
    assert client.get("/api/status", headers={"X-Nua-Token": "wrong"}).status_code == 401
    del client.headers["X-Nua-Token"]
    assert client.get("/api/config").status_code == 401
    assert client.put("/api/env", json={"values": {"GEMINI_API_KEY": "x"}}).status_code == 401
    assert not (root / ".env").exists()


def test_wrong_host_or_origin_is_refused(root):
    client, _, _ = make(root)
    assert client.get("/api/status", headers={"Host": f"evil.example:{PORT}"}).status_code == 403
    assert client.get("/api/status", headers={"Host": "127.0.0.1:1"}).status_code == 403
    assert client.get("/api/status", headers={"Origin": "http://evil.example"}).status_code == 403
    assert client.get("/api/status", headers={"Origin": f"http://localhost:{PORT}"}).status_code == 200
    assert client.get("/", headers={"Host": "evil.example"}).status_code == 403


def test_static_page_is_served_without_escaping_the_folder(root, tmp_path):
    static = tmp_path / "static"
    static.mkdir()
    (static / "index.html").write_text("<html>setup</html>", encoding="utf-8")
    (tmp_path / "outside.txt").write_text("nope", encoding="utf-8")
    setup = server.Setup(root)
    client = TestClient(server.create_app(setup, TOKEN, PORT, static_dir=static), base_url=f"http://127.0.0.1:{PORT}")
    assert "setup" in client.get("/").text
    assert client.get("/").headers["cache-control"] == "no-store"
    assert "nope" not in client.get("/..%2Foutside.txt").text
    assert client.get("/missing.js").status_code == 404


# ---------- config ----------

def test_config_round_trip_keeps_comments(root):
    client, _, _ = make(root)
    data = client.get("/api/config").json()
    assert data["examples"][0] == "blank"
    domain = dict(data["domain"], company="Acme", market="dental", exploring="billing")
    r = client.put("/api/config", json={"domain": domain})
    assert r.status_code == 200, r.text
    assert r.json()["changed"] == ["config/domain.yml"]
    text = (root / "config" / "domain.yml").read_text(encoding="utf-8")
    assert "company: Acme" in text and "# Your company" in text
    assert client.get("/api/config").json()["domain"]["company"] == "Acme"
    # same data again writes nothing
    assert client.put("/api/config", json={"domain": domain}).json()["changed"] == []


def test_config_saves_what_is_valid_and_lists_malformed_fields(root):
    client, _, _ = make(root)
    before = configio.load_setup(root)["people"]["alex"]
    r = client.put("/api/config", json={"people": {"alex": {"name": "Alex R", "email": "not-an-email"}},
                                        "pipeline": {"timezone": "Mars/Base"},
                                        "domain": {"company": "Acme"}})
    assert r.status_code == 200, r.text
    fields = {p["field"] for p in r.json()["problems"]}
    assert fields == {"people.alex.email", "pipeline.timezone"}
    saved = configio.load_setup(root)
    assert saved["people"]["alex"]["name"] == "Alex R"                 # the valid part saved
    assert saved["people"]["alex"]["email"] == before["email"]          # the malformed one left as it was
    assert saved["pipeline"]["timezone"] == "America/Los_Angeles"
    assert saved["domain"]["company"] == "Acme"
    assert client.put("/api/config", json={}).status_code == 422


def test_missing_required_values_never_block_a_save(root):
    client, _, _ = make(root)
    r = client.put("/api/config", json={"domain": {"company": "", "market": "dental", "exploring": ""},
                                        "people": {}})
    assert r.status_code == 200 and r.json()["problems"] == []
    assert configio.load_setup(root)["domain"]["market"] == "dental"
    assert client.get("/api/status").json()["steps"]["team"] is False


def test_timezone_change_mirrors_into_existing_tfvars(root):
    client, _, _ = make(root)
    tfvars = root / server.GCP_TFVARS
    shutil.copy(tfvars.parent / "terraform.tfvars.example", tfvars)
    pipeline = client.get("/api/config").json()["pipeline"]
    r = client.put("/api/config", json={"pipeline": dict(pipeline, timezone="Europe/Berlin")})
    assert r.status_code == 200, r.text
    assert 'time_zone = "Europe/Berlin"' in tfvars.read_text(encoding="utf-8")
    r = client.put("/api/config", json={"pipeline": dict(pipeline, timezone="Mars/Base")})
    assert [p["field"] for p in r.json()["problems"]] == ["pipeline.timezone"]
    assert 'time_zone = "Europe/Berlin"' in tfvars.read_text(encoding="utf-8")


def test_seats_are_an_optional_seed_list(root):
    client, _, _ = make(root)
    assert client.put("/api/config", json={"seats": ["head of operations", "other"]}).status_code == 200
    assert configio.load_setup(root)["seats"] == ["head of operations", "other"]
    assert client.put("/api/config", json={"seats": []}).status_code == 200
    assert configio.load_setup(root)["seats"] == []
    r = client.put("/api/config", json={"seats": ["a", "A", ""]})
    assert [p["field"] for p in r.json()["problems"]] == ["seats"]
    assert configio.load_setup(root)["seats"] == ["a"]


def test_team_step_needs_domain_people_and_the_gemini_key_but_no_internal_domain(root):
    client, _, _ = make(root)
    assert client.get("/api/status").json()["steps"]["team"] is False
    data = client.get("/api/config").json()
    assert "gmail.com" in data["personal_domains"]
    domain = dict(data["domain"], company="Acme", market="dental", exploring="billing")
    pipeline = dict(data["pipeline"], internal_email_domains=[])
    assert client.put("/api/config", json={"domain": domain, "pipeline": pipeline}).status_code == 200
    assert client.get("/api/status").json()["steps"]["team"] is False
    client.put("/api/env", json={"values": {"GEMINI_API_KEY": "g"}})
    assert client.get("/api/status").json()["steps"]["team"] is True


def test_example_api_is_gone(root):
    client, _, _ = make(root)
    assert client.get("/api/examples/freight-brokerage").status_code == 404
    assert client.post("/api/examples/freight-brokerage/apply").status_code == 404


# ---------- .env ----------

def test_env_is_write_only(root):
    client, _, _ = make(root)
    r = client.put("/api/env", json={"values": {"GEMINI_API_KEY": SECRET, "SLACK_BOT_TOKEN": "xoxb-" + SECRET}})
    assert r.status_code == 200, r.text
    assert SECRET not in r.text
    assert r.json()["keys"]["GEMINI_API_KEY"] is True
    assert r.json()["written"] == ["GEMINI_API_KEY", "SLACK_BOT_TOKEN"]
    env = (root / ".env").read_text(encoding="utf-8")
    assert f"GEMINI_API_KEY={SECRET}" in env
    assert (root / ".env").stat().st_mode & 0o077 == 0
    for path in ("status", "config", "env", "deploy", "receiver", "slack", "github", "checks", "golive"):
        assert SECRET not in client.get(f"/api/{path}").text, path


def test_env_refuses_unknown_keys_bad_values_and_ignores_empty_boxes(root):
    client, _, _ = make(root)
    client.put("/api/env", json={"values": {"GEMINI_API_KEY": SECRET}})
    for key, value in (("PATH", "/tmp"), ("GITHUB_APP_PRIVATE_KEY", "x"), ("GEMINI_API_KEY", "a\nb"),
                       ("SLACK_BOT_TOKEN", "not-a-bot-token"), ("CIRCLEBACK_API_KEY_NOBODY", "x")):
        r = client.put("/api/env", json={"values": {key: value, "OPENAI_API_KEY": "sk-ok"}})
        assert r.status_code == 200
        assert [p["field"] for p in r.json()["problems"]] == [f"env.{key}"], key
        assert r.json()["written"] == ["OPENAI_API_KEY"]              # the valid key still saved
    env = (root / ".env").read_text(encoding="utf-8")
    assert "PATH=/tmp" not in env and "not-a-bot-token" not in env and f"GEMINI_API_KEY={SECRET}" in env
    r = client.put("/api/env", json={"values": {"GEMINI_API_KEY": "  ", "CIRCLEBACK_API_KEY_ALEX": "cb_1"}})
    assert r.status_code == 200 and r.json()["problems"] == []
    env = (root / ".env").read_text(encoding="utf-8")
    assert f"GEMINI_API_KEY={SECRET}" in env and "CIRCLEBACK_API_KEY_ALEX=cb_1" in env


def test_needed_keys_follow_the_recorder(root):
    client, _, _ = make(root)
    r = client.put("/api/deploy", json={"recorder": "circleback", "fields": {"circleback_people": ["alex"]}})
    assert r.status_code == 200, r.text
    needed = client.get("/api/env").json()["needed"]["recorder"]
    assert needed == ["CIRCLEBACK_API_KEY_ALEX", "CIRCLEBACK_SIGNING_SECRET_ALEX"]
    client.put("/api/deploy", json={"recorder": "granola"})
    assert client.get("/api/env").json()["needed"]["recorder"] == ["GRANOLA_API_KEY", "GRANOLA_WEBHOOK_SECRET"]
    assert 'circleback_people = []' in (root / server.GCP_TFVARS).read_text(encoding="utf-8")


# ---------- deploy settings ----------

def test_deploy_settings_write_tfvars_and_hide_example_values(root):
    client, _, _ = make(root)
    d = client.get("/api/deploy").json()
    assert d["cloud"] == "gcp" and d["fields"]["project_id"] == "" and d["fields"]["github_app_id"] == ""
    r = client.put("/api/deploy", json={"cloud": "gcp", "fields": {
        "project_id": "acme-discovery", "region": "europe-west4", "monthly_budget_usd": "40",
        "billing_account": "01ab23-45cd67-89ef01", "repo_slug": "acme/discovery", "schedules_on": True,
        "github_app_id": "999"}})
    assert r.status_code == 200, r.text
    text = (root / server.GCP_TFVARS).read_text(encoding="utf-8")
    assert 'project_id                 = "acme-discovery"' in text
    assert "monthly_budget_usd = 40" in text
    assert 'billing_account    = "01AB23-45CD67-89EF01"' in text
    assert "schedules_on = true" in text
    assert 'github_app_id              = "123456"' in text            # read-only from the page
    assert 'time_zone = "America/Los_Angeles"' in text               # always follows config/pipeline.yml


@pytest.mark.parametrize("fields", [{"project_id": "Bad_Project"}, {"region": "west"}, {"repo_slug": "nope"},
                                    {"billing_account": "123"}, {"monthly_budget_usd": "lots"},
                                    {"circleback_people": ["nobody"]}, {"schedules_on": "yes"},
                                    {"repo_branch": 'main"\nx = 1'}])
def test_deploy_settings_are_validated(root, fields):
    client, _, _ = make(root)
    r = client.put("/api/deploy", json={"cloud": "gcp", "fields": {**fields, "monthly_budget_usd": 40}
                                        if "monthly_budget_usd" not in fields else fields})
    assert r.status_code == 200
    (key,) = fields
    assert [p["field"] for p in r.json()["problems"]] == [f"fields.{key}"]
    text = (root / server.GCP_TFVARS).read_text(encoding="utf-8")
    assert server.read_tfvars(root / server.GCP_TFVARS).get(key) != fields[key]    # the bad value is not written
    if key != "monthly_budget_usd":
        assert "monthly_budget_usd = 40" in text                       # the rest is


@pytest.mark.skipif(not (ROOT / server.AWS_TF_DIR).is_dir(), reason="no infra/aws in this checkout")
def test_switching_to_aws_starts_its_tfvars_from_the_gcp_one(root):
    client, _, _ = make(root)
    client.put("/api/deploy", json={"cloud": "gcp", "recorder": "circleback",
                                    "fields": {"repo_slug": "acme/discovery", "circleback_people": ["sam"]}})
    r = client.put("/api/deploy", json={"cloud": "aws", "fields": {"region": "us-west-2",
                                                                   "budget_emails": "a@acme.dev, b@acme.dev"}})
    assert r.status_code == 200, r.text
    text = (root / server.AWS_TFVARS).read_text(encoding="utf-8")
    assert 'repo_slug   = "acme/discovery"' in text and 'circleback_people = ["sam"]' in text
    assert 'budget_emails      = ["a@acme.dev", "b@acme.dev"]' in text
    assert client.get("/api/deploy").json()["cloud"] == "aws"
    assert "bootstrap.sh is not in this checkout" in client.get("/api/checks").json()["deploy"]["note"]
    (root / "infra" / "aws" / "bootstrap.sh").write_text("#!/bin/sh\n", encoding="utf-8")
    assert client.get("/api/checks").json()["deploy"]["command"] == "bash infra/aws/bootstrap.sh --region us-west-2"


def test_aws_is_refused_when_the_checkout_has_no_aws_infra(root, monkeypatch):
    monkeypatch.setattr(server, "AWS_TF_DIR", Path("infra/not-there"))
    client, _, _ = make(root)
    assert client.put("/api/deploy", json={"cloud": "aws"}).status_code == 422


# ---------- receiver, Slack ----------

def test_receiver_from_project_number_and_pasted_url(root):
    fakes = Fakes()
    fakes.answers["gcloud projects describe acme-discovery"] = (0, "123456789")
    client, _, _ = make(root, fakes)
    assert client.get("/api/receiver").json()["url"] is None
    client.put("/api/deploy", json={"fields": {"project_id": "acme-discovery", "region": "us-west1"}})
    assert client.get("/api/receiver").json()["url"] == "https://nua-receiver-123456789.us-west1.run.app"
    client.put("/api/deploy", json={"receiver_url": "nua.example.dev/"})
    assert client.get("/api/receiver").json() == {"url": "https://nua.example.dev", "source": "pasted", "how_to": "",
                                                  "detail": "pasted in step 3"}
    r = client.put("/api/deploy", json={"receiver_url": "http://insecure.dev"})
    assert [p["field"] for p in r.json()["problems"]] == ["receiver_url"]
    assert client.get("/api/receiver").json()["url"] == "https://nua.example.dev"


def test_slack_link_needs_a_receiver(root):
    client, _, _ = make(root)
    assert "create_url" not in client.get("/api/slack").json()
    client.put("/api/deploy", json={"receiver_url": "https://r.example.dev"})
    s = client.get("/api/slack").json()
    assert s["create_url"].startswith("https://api.slack.com/apps?new_app=1&manifest_json=")
    assert "r.example.dev%2Fslack%2Fevents" in s["create_url"]
    assert "slack%2Fevents" not in s["create_url_no_events"]


# ---------- GitHub App ----------

def test_github_flow_writes_env_and_tfvars_and_waits_for_the_install(root):
    calls = {"find": 0}

    def flow(owner, manifest, *, open_browser, on_ready, **_):
        assert open_browser is False and owner is None
        assert manifest["hook_attributes"]["url"] == "https://r.example.dev/github"
        on_ready("http://127.0.0.1:5555/")
        return {"id": 77, "slug": "acme-bot", "pem": "-----BEGIN KEY-----\nabc\n-----END KEY-----\n",
                "webhook_secret": SECRET, "html_url": "https://github.com/apps/acme-bot"}

    def find(app_id, pem, account=None):
        calls["find"] += 1
        return None if calls["find"] < 2 else 4242

    client, setup, _ = make(root, flow=flow, find_install=find, is_org=lambda owner: False, poll_seconds=0.01)
    assert client.post("/api/github/start", json={"repo": "acme/discovery"}).status_code == 409   # no receiver yet
    client.put("/api/deploy", json={"receiver_url": "https://r.example.dev"})
    assert client.post("/api/github/start", json={"repo": "not a repo"}).status_code == 422
    assert client.post("/api/github/start", json={"repo": "acme/discovery"}).status_code == 200
    for _ in range(200):
        status = client.get("/api/github").json()
        if status["status"] in ("done", "error"):
            break
        time.sleep(0.01)
    assert status["status"] == "done", status
    assert status["installation_id"] == "4242"
    assert status["install_url"] == "https://github.com/apps/acme-bot/installations/new"
    assert SECRET not in client.get("/api/github").text and "BEGIN KEY" not in client.get("/api/github").text
    env = (root / ".env").read_text(encoding="utf-8")
    assert f"GITHUB_WEBHOOK_SECRET={SECRET}" in env and "GITHUB_APP_INSTALLATION_ID=4242" in env
    assert "REPO_SLUG=acme/discovery" in env
    tf = server.read_tfvars(root / server.GCP_TFVARS)
    assert (tf["github_app_id"], tf["github_app_slug"], tf["github_app_installation_id"]) == ("77", "acme-bot", "4242")
    assert client.get("/api/status").json()["steps"]["github"] is True


def test_github_flow_error_is_reported(root):
    def flow(*a, **k):
        raise TimeoutError("no redirect from GitHub within 900s")

    client, _, _ = make(root, flow=flow, is_org=lambda o: False)
    client.put("/api/deploy", json={"receiver_url": "https://r.example.dev"})
    client.post("/api/github/start", json={"repo": "acme/discovery"})
    for _ in range(200):
        status = client.get("/api/github").json()
        if status["status"] == "error":
            break
        time.sleep(0.01)
    assert "TimeoutError" in status["error"]
    assert not (root / ".env").exists()


# ---------- checks, go live ----------

def test_checks_with_faked_tools_and_logins(root):
    fakes = Fakes()
    fakes.tools = {"gcloud", "uv"}
    fakes.answers["gcloud auth list"] = (0, "alex@acme.dev")
    fakes.answers["gcloud auth application-default"] = (1, "")
    client, _, _ = make(root, fakes)
    out = client.get("/api/checks").json()
    checks = {c["id"]: c for c in out["checks"]}
    assert checks["tool:gcloud"]["ok"] and not checks["tool:terraform"]["ok"]
    assert "developer.hashicorp.com" in checks["tool:terraform"]["fix"]
    assert checks["login:gcloud"]["ok"] and checks["login:gcloud"]["detail"] == "alex@acme.dev"
    assert not checks["login:adc"]["ok"] and "application-default login" in checks["login:adc"]["fix"]
    assert not checks["config"]["ok"]                                   # blank domain
    assert not checks["receiver"]["ok"] and not checks["cutoff"]["ok"] and not checks["schedules"]["ok"]
    assert all(c["fix"] for c in out["checks"] if not c["ok"])
    assert out["deploy"]["command"] == "bash infra/bootstrap.sh --project <your-project-id> --region us-west1"
    assert all(cmd[0] == "gcloud" for cmd in fakes.commands)


def test_checks_go_green_when_everything_is_in_place(root):
    fakes = Fakes()
    fakes.answers["gcloud auth list"] = (0, "alex@acme.dev")
    fakes.answers["gcloud auth application-default"] = (0, "token")
    client, setup, _ = make(root, fakes)
    configio.apply_example(root, "freight-brokerage")
    client.put("/api/config", json={"pipeline": {"ingest_cutoff": "2026-11-02T09:00:00-08:00"}})
    client.put("/api/deploy", json={"recorder": "granola", "receiver_url": "https://r.example.dev", "fields": {
        "project_id": "acme-discovery", "region": "us-west1", "repo_slug": "acme/discovery", "schedules_on": True}})
    github_app.write_tfvars(root / server.GCP_TFVARS, {"github_app_id": "77", "github_app_slug": "acme-bot",
                                                       "github_app_installation_id": "4242"})
    client.put("/api/env", json={"values": {"GEMINI_API_KEY": "g", "SLACK_BOT_TOKEN": "xoxb-1",
                                            "SLACK_SIGNING_SECRET": "s", "GRANOLA_API_KEY": "k",
                                            "GRANOLA_WEBHOOK_SECRET": "w"}})
    github_app.write_env(root / ".env", {"GITHUB_APP_PRIVATE_KEY": "pem", "GITHUB_WEBHOOK_SECRET": "h"})
    out = client.get("/api/checks").json()
    assert [c["id"] for c in out["checks"] if not c["ok"]] == []
    assert out["deploy"]["command"] == "bash infra/bootstrap.sh --project acme-discovery --region us-west1"
    steps = client.get("/api/status").json()["steps"]
    assert all(steps.values()), steps
    assert list(steps) == ["team", "recorder", "cloud", "github", "slack", "deploy", "golive"]


def test_golive_saves_cutoff_and_schedules(root):
    client, _, _ = make(root)
    client.put("/api/deploy", json={"recorder": "both", "receiver_url": "https://r.example.dev",
                                    "fields": {"circleback_people": ["alex"]}})
    g = client.get("/api/golive").json()
    assert g["granola_url"] == "https://r.example.dev/ingest/granola"
    assert g["circleback_urls"] == {"alex": "https://r.example.dev/ingest/circleback/alex"}
    assert "$GRANOLA_API_KEY" in g["granola_curl"]
    r = client.put("/api/golive", json={"ingest_cutoff": "2026-11-02T09:00:00-05:00", "schedules_on": True})
    assert r.status_code == 200, r.text
    assert configio.load_setup(root)["pipeline"]["ingest_cutoff"] == "2026-11-02T09:00:00-05:00"
    assert server.read_tfvars(root / server.GCP_TFVARS)["schedules_on"] is True
    r = client.put("/api/golive", json={"ingest_cutoff": "soon", "schedules_on": False})
    assert [p["field"] for p in r.json()["problems"]] == ["pipeline.ingest_cutoff"]
    assert server.read_tfvars(root / server.GCP_TFVARS)["schedules_on"] is False    # the valid part saved
    assert configio.load_setup(root)["pipeline"]["ingest_cutoff"] == "2026-11-02T09:00:00-05:00"
    r = client.put("/api/golive", json={"schedules_on": "on"})
    assert [p["field"] for p in r.json()["problems"]] == ["schedules_on"]
    client.put("/api/golive", json={"ingest_cutoff": None})
    assert configio.load_setup(root)["pipeline"]["ingest_cutoff"] is None


def test_read_tfvars_and_env_parsers(tmp_path):
    tf = tmp_path / "t.tfvars"
    tf.write_text('a = "x" # note\nb = 25\nc = true\nd = ["p", "q"]\n# e = "no"\na = "second"\n', encoding="utf-8")
    assert server.read_tfvars(tf) == {"a": "x", "b": 25, "c": True, "d": ["p", "q"]}
    env = tmp_path / ".env"
    env.write_text('# X=1\nA=one\nB="two"\nA=again\nC=\n', encoding="utf-8")
    assert server.read_env(env) == {"A": "one", "B": "two", "C": ""}


def test_cli_wires_setup():
    args = cli.build_parser().parse_args(["setup", "--port", "9999", "--no-browser"])
    assert args.command == "setup" and args.port == 9999 and args.no_browser
    assert "setup" in cli.COMMANDS


def test_status_marks_started_steps_and_receiver_says_where_its_url_came_from(root):
    fakes = Fakes()
    fakes.answers["gcloud projects describe acme-discovery"] = (0, "123456789")
    client, _, _ = make(root, fakes)
    status = client.get("/api/status").json()
    assert status["started"]["slack"] is False
    client.put("/api/env", json={"values": {"SLACK_BOT_TOKEN": "xoxb-1"}})
    status = client.get("/api/status").json()
    assert status["started"]["slack"] is True and status["steps"]["slack"] is False
    client.put("/api/env", json={"values": {"SLACK_SIGNING_SECRET": "s"}})
    status = client.get("/api/status").json()
    assert status["steps"]["slack"] is True and status["started"]["slack"] is False
    client.put("/api/deploy", json={"fields": {"project_id": "acme-discovery", "region": "us-west1"}})
    r = client.get("/api/receiver").json()
    assert r["source"] == "project" and "acme-discovery" in r["detail"] and "us-west1" in r["detail"]
    client.put("/api/deploy", json={"receiver_url": "https://r.example.dev"})
    assert client.get("/api/receiver").json()["detail"] == "pasted in step 3"


def test_cloud_is_not_started_by_a_tfvars_copied_from_the_example(root):
    tfvars = root / "infra/terraform/terraform.tfvars"
    tfvars.write_text((root / "infra/terraform/terraform.tfvars.example").read_text()
                      + '\ntime_zone = "Europe/London"\ncircleback_people = ["alex"]\n')
    client, _, _ = make(root)
    assert client.get("/api/status").json()["started"]["cloud"] is False
    client.put("/api/deploy", json={"fields": {"billing_account": "ABCDEF-123456-ABCDEF"}})
    assert client.get("/api/status").json()["started"]["cloud"] is True     # touched, project still missing
    client.put("/api/deploy", json={"fields": {"project_id": "acme-discovery"}})
    status = client.get("/api/status").json()
    assert status["steps"]["cloud"] is True and status["started"]["cloud"] is False


def test_github_default_name_comes_from_the_company(root):
    client, _, _ = make(root)
    domain = dict(client.get("/api/config").json()["domain"], company="Keel Labs", market="m", exploring="e")
    client.put("/api/config", json={"domain": domain})
    assert client.get("/api/github").json()["default_name"] == "keel-labs-nua-discovery"
    client.put("/api/deploy", json={"receiver_url": "https://r.example.dev"})
    r = client.post("/api/github/start", json={"repo": "keel/disc", "name": "x" * 35})
    assert r.status_code == 422 and "34" in r.json()["problems"][0]


def test_channels_without_a_staging_channel_save(root):
    client, _, _ = make(root)
    r = client.put("/api/config", json={"pipeline": {"channels": {"calls": "#c", "ops": "#o", "test": ""}}})
    assert r.status_code == 200, r.text
    assert r.json()["setup"]["pipeline"]["channels"]["test"] == ""


@pytest.mark.skipif(not (ROOT / server.AWS_TF_DIR).is_dir(), reason="no infra/aws in this checkout")
def test_aws_profile_is_saved_validated_and_used(root):
    fakes = Fakes()
    fakes.answers["aws sts get-caller-identity"] = (0, '{"Account": "123456789012"}')
    client, setup, _ = make(root, fakes)
    (root / "infra" / "aws" / "bootstrap.sh").write_text("#!/bin/sh\n", encoding="utf-8")
    r = client.put("/api/deploy", json={"cloud": "aws", "aws_profile": "bad profile!"})
    assert [p["field"] for p in r.json()["problems"]] == ["aws_profile"]
    assert setup.state()["aws_profile"] == ""
    r = client.put("/api/deploy", json={"cloud": "aws", "aws_profile": "keel-admin", "fields": {"region": "us-west-2"}})
    assert r.json()["problems"] == [] and r.json()["aws_profile"] == "keel-admin"
    assert setup.state()["aws_profile"] == "keel-admin"
    out = client.get("/api/checks").json()
    assert out["deploy"]["command"] == "AWS_PROFILE=keel-admin bash infra/aws/bootstrap.sh --region us-west-2"
    sts = [c for c in fakes.commands if c[:3] == ["aws", "sts", "get-caller-identity"]]
    assert sts and sts[-1][-2:] == ["--profile", "keel-admin"]
    login = next(c for c in out["checks"] if c["id"] == "login:aws")
    assert login["ok"] and "keel-admin" in login["detail"]
    client.put("/api/deploy", json={"aws_profile": ""})
    assert client.get("/api/checks").json()["deploy"]["command"].startswith("bash infra/aws/bootstrap.sh")
