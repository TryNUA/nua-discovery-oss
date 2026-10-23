"""Offline checks of infra/: the Terraform files, infra/deploy.sh, .gcloudignore and the image.

Nothing here runs terraform or gcloud. The .tf files are read as text, and each resource block is cut
out by matching braces, so a test can check what one resource says. `terraform fmt -check` and
`terraform validate` are run by hand (infra/README.md).
"""
from __future__ import annotations

import os
import re
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

from conftest import ROOT

INFRA = ROOT / "infra"
TF = INFRA / "terraform"
DEPLOY = INFRA / "deploy.sh"
BASH = shutil.which("bash")
EM_DASH = chr(0x2014)


def tf_files() -> dict[str, str]:
    return {p.name: p.read_text(encoding="utf-8") for p in sorted(TF.glob("*.tf"))}


def all_tf() -> str:
    return "\n".join(tf_files().values())


def block(kind: str, name: str, text: str | None = None) -> str:
    """The body of `resource "<kind>" "<name>" { ... }` (or `data`), by brace matching."""
    text = all_tf() if text is None else text
    m = re.search(rf'^(resource|data) "{re.escape(kind)}" "{re.escape(name)}" \{{', text, re.M)
    assert m, f"no {kind}.{name}"
    depth, i = 1, m.end()
    while depth:
        depth += {"{": 1, "}": -1}.get(text[i], 0)
        i += 1
    return text[m.end():i - 1]


def attr(body: str, name: str) -> str:
    m = re.search(rf"^\s*{name}\s*=\s*(.+)$", body, re.M)
    assert m, f"no {name}"
    return m.group(1).strip()


# ---------- layout ----------

def test_files_are_in_place_and_setup_sh_is_gone():
    assert not (INFRA / "setup.sh").exists()
    for name in ("versions.tf", "variables.tf", "apis.tf", "service_accounts.tf", "secrets.tf",
                 "artifact_registry.tf", "tasks.tf", "cloud_run.tf", "scheduler.tf", "budget.tf", "outputs.tf",
                 "terraform.tfvars.example"):
        assert (TF / name).exists(), name
    assert (TF / ".terraform.lock.hcl").exists()
    assert (INFRA / "README.md").exists() and DEPLOY.exists()


def test_no_em_dashes_in_infra():
    for path in [*TF.glob("*.tf"), TF / "terraform.tfvars.example", DEPLOY, INFRA / "README.md",
                 ROOT / ".gcloudignore"]:
        assert EM_DASH not in path.read_text(encoding="utf-8"), path


def test_gitignore_keeps_state_and_inputs_local_but_commits_the_lock_file():
    lines = (ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
    for pattern in (".terraform/", "*.tfstate", "terraform.tfvars"):
        assert pattern in lines, pattern
    assert not any("lock.hcl" in line for line in lines if not line.startswith("#"))


def test_gcs_backend_with_bucket_supplied_at_init():
    versions = tf_files()["versions.tf"]
    assert re.search(r'backend "gcs" \{\s*\}', versions)
    assert 'source  = "hashicorp/google"' in versions
    assert "-backend-config=" in (INFRA / "README.md").read_text(encoding="utf-8")


def test_required_variables_and_defaults():
    variables = tf_files()["variables.tf"]
    for name in ("project_id", "region", "repo_slug", "repo_branch", "github_app_id",
                 "github_app_installation_id", "github_app_slug", "image", "deploy_mcp",
                 "circleback_people", "slack_test_mode"):
        assert f'variable "{name}"' in variables, name
    assert attr(block_var("region"), "default") == '"us-west1"'
    assert attr(block_var("deploy_mcp"), "default") == "false"
    assert attr(block_var("circleback_people"), "default") == "[]"
    assert attr(block_var("slack_test_mode"), "default") == "false"
    assert attr(block_var("image"), "default") == '""'


def block_var(name: str) -> str:
    text = tf_files()["variables.tf"]
    m = re.search(rf'^variable "{name}" \{{', text, re.M)
    assert m, name
    depth, i = 1, m.end()
    while depth:
        depth += {"{": 1, "}": -1}.get(text[i], 0)
        i += 1
    return text[m.end():i - 1]


# ---------- secrets ----------

SECRET_VALUE_PATTERNS = [
    r"sk-ant-[A-Za-z0-9_-]{10,}",          # Anthropic
    r"sk-[A-Za-z0-9]{20,}",                # OpenAI
    r"AIza[0-9A-Za-z_-]{20,}",             # Google API key
    r"xox[abpr]-[0-9A-Za-z-]{10,}",        # Slack
    r"whsec_[A-Za-z0-9]{10,}",             # webhook signing secrets
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----",
    r"gh[pousr]_[A-Za-z0-9]{20,}",
]


def test_no_secret_values_in_terraform():
    text = all_tf() + (TF / "terraform.tfvars.example").read_text(encoding="utf-8")
    for pattern in SECRET_VALUE_PATTERNS:
        assert not re.search(pattern, text), pattern
    # Terraform holds the containers and the grants only: no version resource, no secret_data.
    assert "google_secret_manager_secret_version" not in text
    assert "secret_data" not in text


def test_secret_env_vars_read_the_latest_version_by_reference():
    run = tf_files()["cloud_run.tf"] + tf_files()["mcp.tf"]
    assert run.count("secret_key_ref") == 3
    assert run.count('version = "latest"') == 3


def test_secret_readers_follow_the_mounts():
    """Grants come from the same map the services mount secrets from, so a service reads exactly
    the secrets it uses."""
    locals_tf = tf_files()["locals.tf"]
    assert "secret_readers = merge([" in locals_tf and "for svc, m in local.secret_env" in locals_tf
    reader = block("google_secret_manager_secret_iam_member", "reader")
    assert "for_each = local.secret_readers" in reader
    assert '"roles/secretmanager.secretAccessor"' in reader
    # model keys go to the processor (and mcp), never the receiver
    receiver = re.search(r"receiver = merge\(\s*\{(.*?)\}", locals_tf, re.S).group(1)
    assert "anthropic-api-key" not in receiver and "slack-signing-secret" in receiver


# ---------- MCP and Circleback are conditional ----------

def test_mcp_resources_exist_only_with_deploy_mcp():
    locals_tf = tf_files()["locals.tf"]
    assert "local.mcp_on ? { mcp = " in locals_tf                  # the account
    assert 'local.mcp_on ? ["mcp"] : []' in locals_tf               # enqueuer grants
    assert re.search(r"local\.mcp_on \? \{\s*mcp = \{", locals_tf)  # its secrets and their grants
    assert re.search(r"mcp_on\s*=\s*var\.deploy_mcp", locals_tf)
    for kind, name in (("google_storage_bucket", "mcp_oauth"), ("google_storage_bucket_iam_member", "mcp_oauth"),
                       ("google_cloud_run_v2_service", "mcp")):
        assert re.search(r"count\s*=\s*local\.mcp_on", block(kind, name)), name


def test_mcp_volume_and_gen2_as_before():
    mcp = block("google_cloud_run_v2_service", "mcp")
    assert '"EXECUTION_ENVIRONMENT_GEN2"' in mcp
    assert 'mount_options = ["uid=10001", "gid=10001"]' in mcp
    assert "mount_path = local.oauth_mount" in mcp
    assert '"300s"' in mcp and "max_instance_count = 1" in mcp and "min_instance_count = 0" in mcp
    assert 'FASTMCP_HOME = local.oauth_mount' in tf_files()["locals.tf"]
    assert 'oauth_mount  = "/oauth"' in tf_files()["locals.tf"]
    bucket = block("google_storage_bucket", "mcp_oauth")
    assert 'public_access_prevention    = "enforced"' in bucket
    assert '"roles/storage.objectAdmin"' in block("google_storage_bucket_iam_member", "mcp_oauth")


def test_mcp_is_public_only_while_sign_in_is_on():
    assert 'invoker_iam_disabled = var.mcp_auth != "none"' in block("google_cloud_run_v2_service", "mcp")
    locals_tf = tf_files()["locals.tf"]
    assert 'MCP_BASE_URL = local.mcp_url' in locals_tf
    variables = tf_files()["variables.tf"]
    assert '!var.deploy_mcp || var.mcp_auth == "none" || trimspace(var.mcp_oauth_client_id) != ""' in variables


def test_circleback_only_for_listed_people():
    locals_tf = tf_files()["locals.tf"]
    assert "circleback = { for p in var.circleback_people" in locals_tf
    assert '"circleback-key-${p}"' in locals_tf and '"circleback-signing-${p}"' in locals_tf
    assert '"CIRCLEBACK_API_KEY_${P}"' in locals_tf and '"CIRCLEBACK_SIGNING_SECRET_${P}"' in locals_tf
    assert "circleback-key-alex" not in all_tf()
    assert "{ for p in var.circleback_people" in tf_files()["outputs.tf"]


# ---------- Cloud Run ----------

def test_receiver_scales_to_zero_with_cpu_boost_and_request_only_cpu():
    recv = block("google_cloud_run_v2_service", "receiver")
    assert "min_instance_count = 0" in recv
    assert re.search(r"max_instance_count\s*=\s*1\b", recv)          # caps cost from junk traffic
    assert re.search(r"cpu_idle\s*=\s*true", recv)
    assert re.search(r"startup_cpu_boost\s*=\s*true", recv)
    assert '"30s"' in recv and "max_instance_request_concurrency = 40" in recv
    assert 'cpu = "1", memory = "1Gi"' in recv
    assert "invoker_iam_disabled = true" in recv
    assert "min_instance_count = 1" in tf_files()["cloud_run.tf"]     # the comment on going warm


def test_processor_is_private_and_only_internal_tasks_invokes_it():
    proc = block("google_cloud_run_v2_service", "processor")
    assert '"1800s"' in proc and "max_instance_request_concurrency = 1" in proc
    assert "max_instance_count = 1" in proc and 'cpu = "2", memory = "2Gi"' in proc
    invokers = re.findall(r'resource "google_cloud_run_v2_service_iam_member" "(\w+)" \{', all_tf())
    proc_invokers = [n for n in invokers
                     if "google_cloud_run_v2_service.processor" in block("google_cloud_run_v2_service_iam_member", n)]
    assert proc_invokers == ["processor_invoker_tasks"]
    grant = block("google_cloud_run_v2_service_iam_member", "processor_invoker_tasks")
    assert 'google_service_account.sa["tasks"].email' in grant and '"roles/run.invoker"' in grant
    assert "allUsers" not in grant
    assert "invoker_iam_disabled" not in proc


def test_no_allusers_grants():
    """Domain restricted sharing refuses allUsers; public services use invoker_iam_disabled instead."""
    assert '"allUsers"' not in all_tf()


def test_services_ignore_the_image_and_gcloud_client_fields():
    for name in ("processor", "receiver", "mcp"):
        body = block("google_cloud_run_v2_service", name)
        ignore = re.search(r"ignore_changes = \[(.*?)\n\s*\]", body, re.S).group(1)
        for field in ("template[0].containers[0].image", "client", "client_version"):
            assert field in ignore, (name, field)


def test_services_and_jobs_wait_for_an_image():
    locals_tf = tf_files()["locals.tf"]
    assert 'services_on = var.image != ""' in locals_tf
    for name in ("processor", "receiver"):
        assert "count = local.services_on ? 1 : 0" in block("google_cloud_run_v2_service", name)
    assert "local.services_on" in block("google_cloud_run_v2_service", "mcp")
    assert "for_each = local.services_on ? local.scheduler_jobs : {}" in block("google_cloud_scheduler_job", "job")


def test_service_urls_are_deterministic_and_carried_as_env():
    locals_tf = tf_files()["locals.tf"]
    assert '"https://${name}-${local.project_number}.${var.region}.run.app"' in locals_tf
    assert "data.google_project.this.number" in locals_tf
    for name in ("PROCESSOR_URL", "RECEIVER_URL", "SCHEDULER_SERVICE_ACCOUNT", "TASKS_SERVICE_ACCOUNT",
                 "SERVICE_ROLE", "REPO_SLUG", "REPO_BRANCH", "GITHUB_APP_SLUG", "GCP_PROJECT",
                 "TASKS_LOCATION", "TASKS_QUEUE"):
        assert name in locals_tf, name
    assert 'var.slack_test_mode ? { NUA_SLACK_TEST = "1" } : {}' in locals_tf
    # no service reads its own URL from its own resource (that would be a cycle)
    assert ".uri" not in all_tf()


def test_no_slack_click_allowlist():
    """Anyone in the channel may press the buttons, so Terraform passes no cofounder list."""
    assert "SLACK_COFOUNDERS" not in all_tf() and "slack_cofounders" not in all_tf()


# ---------- queue and scheduler ----------

def test_queue_settings():
    queue = block("google_cloud_tasks_queue", "nua")
    assert "max_concurrent_dispatches = 1" in queue
    assert "max_attempts = 5" in queue and 'min_backoff  = "60s"' in queue and 'max_backoff  = "1800s"' in queue
    assert 'queue_name   = "nua"' in tf_files()["locals.tf"]


def test_scheduler_jobs_run_only_with_schedules_on_and_use_oidc_for_the_receiver():
    sched = tf_files()["scheduler.tf"]
    job = block("google_cloud_scheduler_job", "job")
    assert "paused           = !(var.schedules_on && each.value.scheduled)" in job
    assert "ignore_changes" not in job                            # Terraform owns paused, not gcloud
    assert re.search(r'variable "schedules_on" \{[^}]*type\s*=\s*bool[^}]*default\s*=\s*false', tf_files()["variables.tf"])
    assert "time_zone        = local.time_zone" in job
    assert "time_zone    = var.time_zone" in tf_files()["locals.tf"]
    assert "audience              = local.receiver_url" in job
    assert 'google_service_account.sa["scheduler"].email' in job
    assert '"POST"' in job and '"Content-Type" = "application/json"' in job
    for name, scheduled, schedule, path, body in (
            ("sweep", True, "0 2 * * *", "/sweep", "{}"),
            ("sweep-granola", True, "30 * * * *", "/sweep", '{\\"granola_only\\": true}'),
            ("rollup", True, "0 8 * * 1", "/rollup", "{}"),
            ("rollup-bootstrap", False, "0 0 1 1 *", "/rollup", '{\\"bootstrap\\": true}')):
        line = next(line for line in sched.splitlines() if line.strip().startswith(f"{name} "))
        assert f"scheduled = {str(scheduled).lower()}" in line, name     # rollup-bootstrap is never scheduled
        assert f'schedule = "{schedule}"' in line and f'path = "{path}"' in line and f'body = "{body}"' in line, name


def test_scheduler_time_zone_is_a_variable_matching_the_pipeline_timezone():
    var = block_var("time_zone")
    assert attr(var, "default") == '"America/Los_Angeles"'
    assert attr(var, "type") == "string"
    assert "config/pipeline.yml" in attr(var, "description")
    assert "Los_Angeles" not in tf_files()["locals.tf"] and "Los_Angeles" not in tf_files()["scheduler.tf"]
    assert " PT" not in tf_files()["scheduler.tf"]
    example = (TF / "terraform.tfvars.example").read_text(encoding="utf-8")
    assert re.search(r'^time_zone\s*=\s*"America/Los_Angeles"', example, re.M)
    from nua import config as config_mod
    assert config_mod.load(ROOT).pipeline.timezone == "America/Los_Angeles"   # the two defaults agree


def test_service_agents_can_mint_oidc_tokens():
    sa = tf_files()["service_accounts.tf"]
    assert "@gcp-sa-cloudtasks.iam.gserviceaccount.com" in sa
    assert "@gcp-sa-cloudscheduler.iam.gserviceaccount.com" in sa
    assert '"roles/cloudtasks.enqueuer"' in sa and '"roles/iam.serviceAccountUser"' in sa


def test_builds_run_as_internal_build_with_only_what_a_build_needs():
    assert 'build     = "Cloud Build runner of infra/deploy.sh"' in tf_files()["locals.tf"]
    ar = tf_files()["artifact_registry.tf"]
    assert '"roles/artifactregistry.writer"' in block("google_artifact_registry_repository_iam_member", "build_writer")
    assert "google_artifact_registry_repository.nua.name" in block("google_artifact_registry_repository_iam_member", "build_writer")
    assert '"roles/storage.objectViewer"' in block("google_storage_bucket_iam_member", "build_source_reader")
    assert '"roles/logging.logWriter"' in block("google_project_iam_member", "build_log_writer")
    assert ar.count('google_service_account.sa["build"].email') == 3
    bucket = block("google_storage_bucket", "build_source")
    assert 'public_access_prevention    = "enforced"' in bucket and "age = 7" in bucket
    assert "logging: CLOUD_LOGGING_ONLY" in DEPLOY.read_text(encoding="utf-8")   # needed with a custom build account


# ---------- deploy.sh and .gcloudignore ----------

pytestmark_bash = pytest.mark.skipif(BASH is None, reason="needs bash")


def test_deploy_script_never_calls_terraform():
    text = DEPLOY.read_text(encoding="utf-8")
    code = "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("#"))
    assert not re.search(r"(^|[;&|(]|\brun)\s*terraform\s", code, re.M)
    for sub in ("init", "plan", "apply", "import", "destroy"):
        assert f"terraform {sub}" not in code, sub
    assert "--dry-run" in text and "--build-only" in text
    assert "service/Dockerfile" in text and "gcloud builds submit" in text
    assert "gcloud run services update" in text and "--image=" in text


@pytestmark_bash
def test_deploy_script_parses():
    assert subprocess.run([BASH, "-n", str(DEPLOY)]).returncode == 0


@pytest.fixture
def fake_gcloud(tmp_path: Path):
    """A `gcloud` that only logs its arguments, first on PATH."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log = tmp_path / "gcloud.log"
    fake = bin_dir / "gcloud"
    fake.write_text(f'#!/usr/bin/env bash\nprintf "%s\\n" "$*" >> "{log}"\n', encoding="utf-8")
    fake.chmod(fake.stat().st_mode | stat.S_IXUSR)

    def run(*args: str, **extra: str) -> tuple[int, str, list[str]]:
        log.unlink(missing_ok=True)
        env = {"PATH": f"{bin_dir}{os.pathsep}/usr/bin{os.pathsep}/bin", "TMPDIR": str(tmp_path),
               "PROJECT_ID": "discovery-test", "IMAGE_TAG": "t1", **extra}
        proc = subprocess.run([BASH, str(DEPLOY), *args], env=env, capture_output=True, text=True, timeout=60)
        calls = log.read_text(encoding="utf-8").splitlines() if log.exists() else []
        return proc.returncode, proc.stdout + proc.stderr, calls

    return run


@pytestmark_bash
def test_dry_run_prints_the_commands_and_runs_nothing(fake_gcloud):
    code, out, calls = fake_gcloud("--dry-run")
    assert code == 0, out
    assert calls == []
    image = "us-west1-docker.pkg.dev/discovery-test/nua/nua:t1"
    assert "gcloud builds submit" in out and image in out
    assert f"gcloud run services update nua-processor --project=discovery-test --region=us-west1 --image={image}" in out
    assert f"gcloud run services update nua-receiver --project=discovery-test --region=us-west1 --image={image}" in out
    assert "nua-mcp" not in out


@pytest.mark.skipif(BASH is None, reason="needs bash")
def test_deploy_builds_then_rolls_out(fake_gcloud):
    code, out, calls = fake_gcloud(SERVICES="nua-processor nua-receiver nua-mcp")
    assert code == 0, out
    assert calls[0].startswith("builds submit ")
    assert [c.split(" ")[3] for c in calls[1:]] == ["nua-processor", "nua-receiver", "nua-mcp"]
    code, out, calls = fake_gcloud("--build-only")
    assert code == 0, out
    assert len(calls) == 1 and 'image = "us-west1-docker.pkg.dev/discovery-test/nua/nua:t1"' in out
    # builds run as nua-build and stage their source in the Terraform-made bucket
    assert "--service-account=projects/discovery-test/serviceAccounts/nua-build@discovery-test.iam.gserviceaccount.com" in calls[0]
    assert "--gcs-source-staging-dir=gs://discovery-test-nua-build/source" in calls[0]
    code, out, _ = fake_gcloud("--bogus")
    assert code != 0


def test_gcloudignore_uploads_only_what_the_image_needs():
    lines = [line.strip() for line in (ROOT / ".gcloudignore").read_text(encoding="utf-8").splitlines()
             if line.strip() and not line.startswith("#")]
    assert lines[0] == "/*"                                          # everything out ...
    kept = {line[1:] for line in lines if line.startswith("!")}
    assert kept == {"/pyproject.toml", "/uv.lock", "/nua/", "/service/"}   # ... only the image's inputs back in
    dockerfile = (ROOT / "service" / "Dockerfile").read_text(encoding="utf-8")
    copied = set(re.findall(r"^COPY (?!--from)(.+?) \S+$", dockerfile, re.M))
    assert copied == {"pyproject.toml uv.lock", "nua", "service"}


# ---------- the image ----------

def test_image_user_matches_the_volume_mount_owner():
    dockerfile = (ROOT / "service" / "Dockerfile").read_text(encoding="utf-8")
    assert "--uid 10001" in dockerfile and "USER nua" in dockerfile


def test_registry_keeps_the_newest_10_images():
    repo = block("google_artifact_registry_repository", "nua")
    assert "cleanup_policy_dry_run = false" in repo
    assert 'action = "KEEP"' in repo and "keep_count = 10" in repo
    assert 'action = "DELETE"' in repo and 'tag_state  = "ANY"' in repo


def test_budget_alert_only_with_a_billing_account():
    budget = block("google_billing_budget", "monthly")
    assert 'count = var.billing_account != "" ? 1 : 0' in budget
    assert 'projects = ["projects/${local.project_number}"]' in budget      # this project only
    assert budget.count("threshold_rules {") == 4 and '"FORECASTED_SPEND"' in budget
    assert re.search(r'variable "monthly_budget_usd" \{[^}]*default\s*=\s*25', tf_files()["variables.tf"])
    assert '"billingbudgets.googleapis.com"' in tf_files()["apis.tf"]
    assert "user_project_override = true" in tf_files()["versions.tf"]


def test_image_makes_the_code_readable_by_the_app_user():
    """A source file saved owner-only (-rw-------) must not break the image: the app runs as uid 10001."""
    docker = (ROOT / "service" / "Dockerfile").read_text(encoding="utf-8")
    assert "chmod -R a+rX /app/nua /app/service" in docker
    assert docker.index("chmod -R a+rX") < docker.index("USER nua")


def test_image_installs_from_the_uv_lock():
    """The image installs the locked runtime dependencies with uv, without dev tools, and puts the
    venv on PATH. There is no separate constraints file: uv.lock is the only pin list."""
    dockerfile = (ROOT / "service" / "Dockerfile").read_text(encoding="utf-8")
    assert EM_DASH not in dockerfile
    assert "COPY --from=ghcr.io/astral-sh/uv:" in dockerfile
    assert "COPY pyproject.toml uv.lock ./" in dockerfile
    assert "RUN uv sync --frozen --no-dev" in dockerfile
    assert 'PATH="/app/.venv/bin:${PATH}"' in dockerfile and "UV_PROJECT_ENVIRONMENT=/app/.venv" in dockerfile
    assert "pip install" not in dockerfile and "constraints.txt" not in dockerfile
    assert not (ROOT / "service" / "constraints.txt").exists()


def test_uv_lock_pins_every_dependency_within_its_range():
    """uv.lock holds a version for every runtime and dev dependency in pyproject.toml, inside its
    range, and the dev tools live in the uv dev group rather than an extra."""
    import tomllib

    from packaging.requirements import Requirement
    from packaging.utils import canonicalize_name

    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    project = pyproject["project"]
    assert "optional-dependencies" not in project
    dev = pyproject["dependency-groups"]["dev"]
    lock_text = (ROOT / "uv.lock").read_text(encoding="utf-8")
    assert EM_DASH not in lock_text
    lock = tomllib.loads(lock_text)
    versions = {canonicalize_name(p["name"]): p["version"] for p in lock["package"]}
    for spec in [*project["dependencies"], *dev]:
        req = Requirement(spec)
        name = canonicalize_name(req.name)
        assert name in versions, f"{req.name} is not in uv.lock"
        assert req.specifier.contains(versions[name]), f"{req.name}=={versions[name]} is outside {req.specifier}"
    assert versions["fastmcp"] == "4.0.10"
    [discovery] = [p for p in lock["package"] if p["name"] == "nua-discovery"]
    assert {canonicalize_name(Requirement(s).name) for s in dev} <= {
        canonicalize_name(d["name"]) for d in discovery["dev-dependencies"]["dev"]}


# ---------- comments that describe the setup ----------

def test_env_example_runs_the_services_with_env_file():
    """The services never load .env: only `python -m nua` and the `local` role do. Running them
    yourself needs uvicorn's --env-file, or SERVICE_ROLE (read at import) and every setting in .env are
    ignored and you get the receiver whatever the file says."""
    text = (ROOT / ".env.example").read_text(encoding="utf-8")
    assert "uvicorn --env-file .env service.main:app" in text
    assert "(uvicorn service.main:app)" not in text
    import nua.settings
    doc = " ".join((nua.settings.__doc__ or "").split())
    assert "only by `python -m nua`" in doc and "--env-file" in doc
    assert "On a local clone, `.env` at the repo root is loaded first" not in doc


def test_env_example_describes_the_sign_in_store_and_the_cli_root():
    text = (ROOT / ".env.example").read_text(encoding="utf-8")
    assert EM_DASH not in text
    assert "keeps sign-in tokens valid across restarts" not in text
    assert "FASTMCP_HOME" in text
    assert "NUA_ROOT" in text


def test_cloud_run_services_are_deletable():
    """deletion_protection is off on every Cloud Run service, so terraform destroy can remove them."""
    run = tf_files()["cloud_run.tf"] + tf_files()["mcp.tf"]
    services = run.count('resource "google_cloud_run_v2_service"')
    assert services >= 2
    assert run.count("deletion_protection = false") == services
