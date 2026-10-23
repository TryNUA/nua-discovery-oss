"""infra/bootstrap.sh against fake gcloud, terraform, uv and curl on PATH.

Each test copies infra/ and .env.example into a temporary root, so tfvars and .env writes stay
there. The fakes log every call; FAKE_FAIL (patterns joined by |) makes matching gcloud calls fail,
FAKE_PLAN_EXIT sets what `terraform plan -detailed-exitcode` returns (0 no changes, 2 changes).
"""
from __future__ import annotations

import os
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

from conftest import ROOT

BASH = shutil.which("bash")
pytestmark = pytest.mark.skipif(BASH is None, reason="needs bash")
EM_DASH = chr(0x2014)

GCLOUD = r'''#!/usr/bin/env bash
printf '%s\n' "gcloud $*" >> "$FAKE_LOG"
IFS='|' read -ra pats <<< "${FAKE_FAIL:-}"
for p in "${pats[@]}"; do
  if [ -n "$p" ] && [[ "$*" == *"$p"* ]]; then exit 1; fi
done
case "$*" in
  *"value(projectNumber)"*) echo 123 ;;
  *"value(billingEnabled)"*) echo "${FAKE_BILLING:-True}" ;;
  *"services list"*) printf 'cloudresourcemanager.googleapis.com\nserviceusage.googleapis.com\n' ;;
  *"versions access"*) echo old-value ;;
  *"versions add"*) cat > /dev/null ;;
esac
exit 0
'''
TERRAFORM = r'''#!/usr/bin/env bash
printf '%s\n' "terraform $*" >> "$FAKE_LOG"
case "$*" in *" plan "*) echo "Plan: 3 to add, 0 to change, 0 to destroy."; exit "${FAKE_PLAN_EXIT:-0}" ;; esac
exit 0
'''
QUIET = '#!/usr/bin/env bash\nprintf \'%s\\n\' "$(basename "$0") $*" >> "$FAKE_LOG"\nexit 0\n'

TFVARS_DONE = '''project_id                 = "acme-disc"
region                     = "us-west1"
repo_slug                  = "acme/disc"
github_app_id              = "99"
github_app_installation_id = "314"
github_app_slug            = "disc-bot"
image = "{image}"
schedules_on = false
circleback_people = []
deploy_mcp = false
'''
ENV_DONE = ("GEMINI_API_KEY=gem-secret-1\nSLACK_BOT_TOKEN=slack-secret-2\nSLACK_SIGNING_SECRET=sign-secret-3\n"
            "GITHUB_APP_PRIVATE_KEY=pem-secret-4\\n\nGITHUB_WEBHOOK_SECRET=hook-secret-5\n")
SECRETS = ["gem-secret-1", "slack-secret-2", "sign-secret-3", "pem-secret-4", "hook-secret-5"]


@pytest.fixture
def boot(tmp_path: Path):
    root = tmp_path / "repo"
    shutil.copytree(ROOT / "infra", root / "infra",
                    ignore=shutil.ignore_patterns(".terraform", "terraform.tfvars", "*.tfstate*", "*.tfplan"))
    shutil.copy(ROOT / ".env.example", root / ".env.example")
    (root / "config").mkdir()
    (root / "config" / "pipeline.yml").write_text("timezone: Europe/London  # team zone\n", encoding="utf-8")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name, body in (("gcloud", GCLOUD), ("terraform", TERRAFORM), ("uv", QUIET), ("curl", QUIET)):
        (bin_dir / name).write_text(body, encoding="utf-8")
        (bin_dir / name).chmod(0o755 | stat.S_IXUSR)
    log = tmp_path / "calls.log"

    def run(*args: str, **extra: str) -> tuple[int, str, list[str]]:
        log.unlink(missing_ok=True)
        env = {"PATH": f"{bin_dir}{os.pathsep}/usr/bin{os.pathsep}/bin", "TMPDIR": str(tmp_path),
               "HOME": str(tmp_path), "FAKE_LOG": str(log), "IMAGE_TAG": "t1", **extra}
        proc = subprocess.run([BASH, str(root / "infra" / "bootstrap.sh"), *args], env=env, capture_output=True,
                              text=True, timeout=60, stdin=subprocess.DEVNULL)
        calls = log.read_text(encoding="utf-8").splitlines() if log.exists() else []
        return proc.returncode, proc.stdout + proc.stderr, calls

    run.root = root
    return run


def _tf(root: Path) -> Path:
    return root / "infra" / "terraform"


def _done_state(root: Path, image: str = "") -> None:
    (_tf(root) / "terraform.tfvars").write_text(TFVARS_DONE.format(image=image), encoding="utf-8")
    (root / ".env").write_text(ENV_DONE, encoding="utf-8")


def test_parses_and_has_no_em_dashes():
    path = ROOT / "infra" / "bootstrap.sh"
    assert subprocess.run([BASH, "-n", str(path)]).returncode == 0
    assert EM_DASH not in path.read_text(encoding="utf-8")
    assert os.access(path, os.X_OK)


def test_dry_run_prints_every_step_and_runs_nothing(boot):
    code, out, calls = boot("--dry-run", "--project", "acme-disc", "--region", "europe-west1")
    assert code == 0, out
    assert calls == []
    assert not (_tf(boot.root) / "terraform.tfvars").exists()
    for text in ("gcloud storage buckets create gs://acme-disc-tfstate", "init -input=false",
                 "-backend-config=bucket=acme-disc-tfstate", "plan -input=false", "apply -input=false",
                 "push-secrets.sh", "deploy.sh --build-only", "https://nua-receiver-<project number>.europe-west1.run.app",
                 "nua github-app", "nua slack-manifest", "Dry run: nothing was run or changed."):
        assert text in out, text


def test_stops_with_instructions_when_inputs_are_missing(boot):
    code, out, calls = boot("--project", "acme-disc", "--yes")
    assert code == 1
    assert "github_app_id is not set" in out and "nua github-app" in out
    assert "GEMINI_API_KEY is empty" in out and "nua slack-manifest" in out
    assert "https://nua-receiver-123.us-west1.run.app" in out
    assert not any(c.startswith("terraform") and (" plan" in c or " apply" in c) for c in calls)
    tfvars = (_tf(boot.root) / "terraform.tfvars").read_text(encoding="utf-8")
    assert 'project_id                 = "acme-disc"' in tfvars          # copied from the example and set
    assert 'time_zone = "Europe/London"' in tfvars and "warning" not in out    # follows config/pipeline.yml


def test_time_zone_mismatch_warns_and_follows_the_pipeline(boot):
    _done_state(boot.root, image="x")
    tfvars = _tf(boot.root) / "terraform.tfvars"
    tfvars.write_text(tfvars.read_text() + 'time_zone = "Asia/Tokyo"\n')
    code, out, _ = boot()
    assert code == 0, out
    assert "Asia/Tokyo" in out and "warning" in out
    assert 'time_zone = "Europe/London"' in tfvars.read_text() and "Asia/Tokyo" not in tfvars.read_text()


def test_fresh_project_runs_both_stages_and_writes_the_image(boot):
    _done_state(boot.root)
    code, out, calls = boot("--yes", FAKE_PLAN_EXIT="2",
                            FAKE_FAIL="storage buckets describe|run services describe")
    assert code == 0, out
    assert any("gcloud storage buckets create gs://acme-disc-tfstate" in c for c in calls)
    assert sum(1 for c in calls if c.startswith("terraform") and " apply " in c) == 2
    assert sum(1 for c in calls if c.startswith("gcloud builds submit")) == 1
    builds = next(i for i, c in enumerate(calls) if c.startswith("gcloud builds submit"))
    applies = [i for i, c in enumerate(calls) if c.startswith("terraform") and " apply " in c]
    assert applies[0] < builds < applies[1]
    tfvars = (_tf(boot.root) / "terraform.tfvars").read_text(encoding="utf-8")
    assert 'image = "us-west1-docker.pkg.dev/acme-disc/nua/nua:t1"' in tfvars
    assert not any("secrets-refreshed" in c for c in calls)        # new services read the secrets at start
    for secret in SECRETS:
        assert secret not in out and not any(secret in c for c in calls)


def test_rerun_skips_what_is_done_and_restarts_on_secret_changes(boot):
    _done_state(boot.root, image="us-west1-docker.pkg.dev/acme-disc/nua/nua:t0")
    state = _tf(boot.root) / ".terraform"
    state.mkdir()
    (state / "terraform.tfstate").write_text('{"backend": {"config": {"bucket": "acme-disc-tfstate"}}}')
    code, out, calls = boot()
    assert code == 0, out
    assert not any("buckets create" in c or "services enable" in c or "projects create" in c for c in calls)
    assert not any(c.startswith("terraform") and (" init" in c or " apply " in c) for c in calls)
    assert not any("builds submit" in c for c in calls)
    restarts = [c for c in calls if "secrets-refreshed=" in c]
    assert [c.split()[4] for c in restarts] == ["nua-receiver", "nua-processor"]
    assert "stage 2: no changes" in out


def test_asks_before_applying_and_refuses_without_a_terminal(boot):
    _done_state(boot.root, image="x")
    code, out, calls = boot(FAKE_PLAN_EXIT="2")
    assert code == 1 and "pass --yes" in out
    assert not any(" apply " in c for c in calls)


def test_login_and_billing_checks_explain_the_fix(boot):
    code, out, _ = boot("--project", "nope", FAKE_FAIL="projects describe nope")
    assert code == 1 and "--create-project" in out
    code, out, calls = boot("--project", "nope", "--create-project", FAKE_FAIL="projects describe nope")
    assert "gcloud projects create nope" in calls
    code, out, _ = boot("--project", "acme-disc", FAKE_FAIL="application-default")
    assert code == 1 and "gcloud auth application-default login" in out
    code, out, _ = boot("--project", "acme-disc", FAKE_BILLING="False")
    assert code == 1 and "--billing-account" in out
    code, out, calls = boot("--project", "acme-disc", "--billing-account", "AAA-BBB", FAKE_BILLING="False")
    assert any("billing projects link acme-disc --billing-account=AAA-BBB" in c for c in calls)


def test_project_mismatch_with_tfvars_stops(boot):
    _done_state(boot.root)
    code, out, calls = boot("--project", "other-project")
    assert code == 1 and "acme-disc" in out and not any(c.startswith("terraform") for c in calls)
