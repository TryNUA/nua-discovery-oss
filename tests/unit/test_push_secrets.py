"""infra/push-secrets.sh against a fake gcloud that keeps secrets in a temp dir. Offline."""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "infra" / "push-secrets.sh"
CORE = ["granola-api-key", "granola-webhook-secret", "github-app-private-key", "github-webhook-secret",
        "slack-bot-token", "slack-signing-secret", "anthropic-api-key", "openai-api-key", "gemini-api-key"]

FAKE_GCLOUD = r"""#!/usr/bin/env bash
# Secrets live in $STORE/<name>/ (exists = created); versions are files 1, 2, ...
echo "$*" >> "$STORE/calls.log"
args=" $* "
name=""
for a in "$@"; do case "$a" in --secret=*) name="${a#--secret=}";; esac; done
if [[ "$1 $2" == "secrets describe" ]]; then [ -d "$STORE/$3" ]; exit; fi
if [[ "$1 $2 $3" == "secrets versions access" ]]; then
  last=$(ls "$STORE/$name" 2>/dev/null | sort -n | tail -1); [ -n "$last" ] || exit 1
  cat "$STORE/$name/$last"; exit 0
fi
if [[ "$1 $2 $3" == "secrets versions add" ]]; then
  n=$(( $(ls "$STORE/$4" | wc -l) + 1 )); cat > "$STORE/$4/$n"; exit 0
fi
exit 3
"""


@pytest.fixture
def run(tmp_path: Path):
    store = tmp_path / "store"
    store.mkdir()
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    gcloud = bin_dir / "gcloud"
    gcloud.write_text(FAKE_GCLOUD)
    gcloud.chmod(0o755)
    env_file = tmp_path / ".env"

    def _run(env_text: str, *args: str, create=CORE, **extra) -> tuple[int, str, Path]:
        for name in create:
            (store / name).mkdir(exist_ok=True)
        env_file.write_text(env_text)
        env = {"PATH": f"{bin_dir}{os.pathsep}/usr/bin{os.pathsep}/bin", "STORE": str(store),
               "PROJECT_ID": "proj", "ENV_FILE": str(env_file), **extra}
        p = subprocess.run(["bash", str(SCRIPT), *args], env=env, capture_output=True, text=True)
        return p.returncode, p.stdout + p.stderr, store

    return _run


def versions(store: Path, name: str) -> list[str]:
    return [(store / name / v).read_text() for v in sorted(os.listdir(store / name), key=int)]


def status(out: str, name: str) -> str:
    line = next(l for l in out.splitlines() if l.strip().startswith(name + " "))
    return line.split()[-1]


ENV = ("SLACK_BOT_TOKEN=xoxb-real\nGEMINI_API_KEY=gem-secret\n"
       "GITHUB_APP_PRIVATE_KEY=-----BEGIN RSA PRIVATE KEY-----\\nabc\\n-----END RSA PRIVATE KEY-----\n")


def test_pushes_values_and_placeholders_without_printing_values(run):
    code, out, store = run(ENV)
    assert code == 0, out
    assert versions(store, "slack-bot-token") == ["xoxb-real"]
    assert versions(store, "gemini-api-key") == ["gem-secret"]
    assert versions(store, "github-app-private-key") == [
        "-----BEGIN RSA PRIVATE KEY-----\\nabc\\n-----END RSA PRIVATE KEY-----"]
    assert versions(store, "openai-api-key") == ["placeholder"]
    assert status(out, "slack-bot-token") == "set" and status(out, "openai-api-key") == "placeholder"
    for secret_value in ("xoxb-real", "gem-secret", "BEGIN RSA"):
        assert secret_value not in out
    calls = (store / "calls.log").read_text()
    assert "xoxb-real" not in calls and "gem-secret" not in calls       # never on a command line


def test_second_run_adds_nothing_and_keeps_existing_values(run):
    run(ENV)
    code, out, store = run("SLACK_BOT_TOKEN=xoxb-real\n")              # gemini no longer in .env
    assert code == 0, out
    assert status(out, "slack-bot-token") == "unchanged"
    assert status(out, "gemini-api-key") == "kept"
    assert status(out, "openai-api-key") == "kept"                     # placeholder already there
    assert versions(store, "slack-bot-token") == ["xoxb-real"]
    assert versions(store, "gemini-api-key") == ["gem-secret"]


def test_a_changed_value_becomes_a_new_version(run):
    run(ENV)
    _, out, store = run("SLACK_BOT_TOKEN=xoxb-rotated\n")
    assert status(out, "slack-bot-token") == "set"
    assert versions(store, "slack-bot-token") == ["xoxb-real", "xoxb-rotated"]


def test_dry_run_adds_nothing(run):
    code, out, store = run(ENV, "--dry-run")
    assert code == 0, out
    assert "dry run" in out
    assert status(out, "slack-bot-token") == "set" and status(out, "openai-api-key") == "placeholder"
    assert all(not os.listdir(store / n) for n in CORE)


def test_example_values_count_as_unset(run):
    example = (ROOT / ".env.example").read_text()
    code, out, store = run(example)
    assert code == 0, out
    assert all(versions(store, n) == ["placeholder"] for n in CORE)


def test_missing_secret_containers_fail_with_a_hint(run):
    code, out, _ = run(ENV, create=CORE[:-1])
    assert code == 1
    assert status(out, "gemini-api-key") == "missing"
    assert "stage 1 terraform apply" in out


def test_optional_circleback_and_mcp_secrets(run):
    extra = ["circleback-key-alex", "circleback-signing-alex", "mcp-oauth-client-secret", "mcp-jwt-signing-key"]
    code, out, store = run("CIRCLEBACK_API_KEY_ALEX=cb\n", create=CORE + extra,
                           CIRCLEBACK_PEOPLE="alex", DEPLOY_MCP="1")
    assert code == 0, out
    assert versions(store, "circleback-key-alex") == ["cb"]
    assert status(out, "mcp-jwt-signing-key") == "placeholder"


def test_project_id_comes_from_tfvars_by_default():
    text = SCRIPT.read_text()
    assert "terraform.tfvars" in text and "project_id" in text


def test_pairs_match_the_terraform_secret_map():
    """Every secret the services mount by default (locals.tf secret_env) is pushed, and nothing else."""
    locals_tf = (ROOT / "infra" / "terraform" / "locals.tf").read_text()
    script = SCRIPT.read_text()
    for name in CORE:
        assert f'"{name}"' in locals_tf
        assert f" {name}\n" in script or f" {name}\"" in script
