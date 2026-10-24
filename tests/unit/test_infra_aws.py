"""Offline checks of infra/aws/: the Terraform files read as text, and the scripts run against a fake
`aws` (and `terraform`) that only log or answer from a temp dir. Nothing here reaches AWS.

`terraform fmt -check`, `terraform validate` and the mocked-provider plans in
infra/aws/terraform/tests/ are run by hand (docs/self-hosting-aws.md, "Checking a change offline").
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
from nua.ports import TASK_KINDS

AWS = ROOT / "infra" / "aws"
TF = AWS / "terraform"
GCP_TF = ROOT / "infra" / "terraform"
BASH = shutil.which("bash")
EM_DASH = chr(0x2014)
needs_bash = pytest.mark.skipif(BASH is None, reason="needs bash")
SCRIPTS = ("bootstrap.sh", "deploy.sh", "push-secrets.sh", "enqueue.sh")


def tf(name: str) -> str:
    return (TF / name).read_text(encoding="utf-8")


def all_tf() -> str:
    return "\n".join(p.read_text(encoding="utf-8") for p in sorted(TF.glob("*.tf")))


def block(kind: str, name: str, text: str | None = None) -> str:
    """The body of `resource "<kind>" "<name>" { ... }`, by brace matching."""
    text = all_tf() if text is None else text
    m = re.search(rf'^(resource|data|variable) "{re.escape(kind)}"(?: "{re.escape(name)}")? \{{', text, re.M)
    assert m, f"no {kind}.{name}"
    depth, i = 1, m.end()
    while depth:
        depth += {"{": 1, "}": -1}.get(text[i], 0)
        i += 1
    return text[m.end():i - 1]


def variable(name: str, text: str) -> str:
    m = re.search(rf'^variable "{name}" \{{', text, re.M)
    assert m, name
    depth, i = 1, m.end()
    while depth:
        depth += {"{": 1, "}": -1}.get(text[i], 0)
        i += 1
    return text[m.end():i - 1]


# ---------- layout and hygiene ----------

def test_files_are_in_place():
    for name in ("versions.tf", "variables.tf", "locals.tf", "network.tf", "secrets.tf", "queue.tf", "build.tf",
                 "receiver.tf", "worker.tf", "schedules.tf", "budget.tf", "outputs.tf", "terraform.tfvars.example",
                 ".terraform.lock.hcl", "tests/stages.tftest.hcl"):
        assert (TF / name).exists(), name
    for name in SCRIPTS:
        path = AWS / name
        assert path.exists() and os.access(path, os.X_OK), name
    assert (ROOT / "docs" / "self-hosting-aws.md").exists() and (ROOT / "service" / "worker.py").exists()


def test_no_em_dashes():
    paths = [*TF.glob("*.tf"), TF / "terraform.tfvars.example", TF / "tests" / "stages.tftest.hcl",
             *(AWS / s for s in SCRIPTS), ROOT / "docs" / "self-hosting-aws.md", ROOT / "service" / "worker.py",
             ROOT / "nua" / "tasks.py", ROOT / "tests" / "unit" / "test_aws.py"]
    for path in paths:
        assert EM_DASH not in path.read_text(encoding="utf-8"), path


@needs_bash
@pytest.mark.parametrize("script", SCRIPTS)
def test_scripts_parse(script):
    assert subprocess.run([BASH, "-n", str(AWS / script)]).returncode == 0


def test_s3_backend_with_native_locking():
    versions = tf("versions.tf")
    assert re.search(r'backend "s3" \{\s*use_lockfile = true', versions)
    assert 'required_version = ">= 1.10"' in versions
    assert 'source  = "hashicorp/aws"' in versions


def test_every_resource_is_tagged_and_named_for_the_stack():
    versions = tf("versions.tf")
    assert re.search(r'default_tags \{\s*tags = \{\s*app\s*= "nua-discovery"\s*stack = var.stack', versions)
    stack = variable("stack", tf("variables.tf"))
    assert 'default     = "nua"' in stack and '"^nua(-[a-z0-9]+)*$"' in stack
    # names come from var.stack, never a literal
    for m in re.finditer(r'^\s*(?:name|function_name|family|bucket|alarm_name)\s*=\s*"([^"]*)"', all_tf(), re.M):
        value = m.group(1)
        assert "${var.stack}" in value or "${local.name." in value or value.isupper() or value in (
            "id", "containerInsights", "expires_at",                         # attribute and setting names
            "build", "receiver", "execution", "worker", "scheduler"), m.group(0)  # inline policies, inside a nua- role
    assert re.search(r'receiver\s*=\s*"\$\{var.stack\}-receiver"', tf("locals.tf"))


def test_teardown_removes_everything():
    """Destroy must not leave data behind: buckets and the image repo empty themselves, secrets go at once,
    and every log group is declared here (with retention) so it is deleted too."""
    assert "force_destroy = true" in block("aws_s3_bucket", "build")
    assert re.search(r"force_delete\s*=\s*true", block("aws_ecr_repository", "images"))
    assert "recovery_window_in_days = 0" in block("aws_secretsmanager_secret", "secret")
    groups = re.findall(r'resource "aws_cloudwatch_log_group" "(\w+)"', all_tf())
    assert sorted(groups) == ["build", "receiver", "worker"]
    for g in groups:
        assert "retention_in_days = var.log_retention_days" in block("aws_cloudwatch_log_group", g)
    assert "log_group  = aws_cloudwatch_log_group.receiver.name" in block("aws_lambda_function", "receiver")
    assert "group_name = aws_cloudwatch_log_group.build.name" in block("aws_codebuild_project", "build")


# ---------- secrets ----------

SECRET_VALUE_PATTERNS = [
    r"sk-ant-[A-Za-z0-9_-]{10,}", r"sk-[A-Za-z0-9]{20,}", r"AIza[0-9A-Za-z_-]{20,}", r"xox[abpr]-[0-9A-Za-z-]{10,}",
    r"whsec_[A-Za-z0-9]{10,}", r"-----BEGIN [A-Z ]*PRIVATE KEY-----", r"gh[pousr]_[A-Za-z0-9]{20,}",
]


def test_no_secret_values_in_terraform():
    text = all_tf() + tf("terraform.tfvars.example")
    for pattern in SECRET_VALUE_PATTERNS:
        assert not re.search(pattern, text), pattern
    assert "aws_secretsmanager_secret_version" not in text and "secret_string" not in text


def test_secret_split_matches_gcp():
    """The same secrets, env vars and receiver/processor split as infra/terraform/locals.tf."""
    def pairs(text: str, component: str) -> set[tuple[str, str]]:
        body = re.search(rf"{component} = merge\(\s*\{{(.*?)\}}", text, re.S).group(1)
        return set(re.findall(r'(\w+)\s*=\s*"([a-z-]+)"', body))
    aws, gcp = tf("locals.tf"), (GCP_TF / "locals.tf").read_text(encoding="utf-8")
    assert pairs(aws, "receiver") == pairs(gcp, "receiver")
    assert pairs(aws, "worker") == pairs(gcp, "processor")
    assert ("GEMINI_API_KEY", "gemini-api-key") not in pairs(aws, "receiver")
    assert 'secret_name = { for s in local.secrets : s => "${var.stack}/${s}" }' in aws


def test_push_secrets_pairs_match_gcp():
    def pairs(path: Path) -> list[str]:
        return re.search(r'PAIRS="(.*?)"', path.read_text(encoding="utf-8"), re.S).group(1).splitlines()
    assert pairs(AWS / "push-secrets.sh") == pairs(ROOT / "infra" / "push-secrets.sh")


def test_each_component_reads_only_its_secrets():
    receiver = block("aws_iam_role_policy", "receiver")
    assert "local.secret_arns.receiver" in receiver and "secret_arns.worker" not in receiver
    execution = block("aws_iam_role_policy", "worker_execution")
    assert "local.secret_arns.worker" in execution and "secret_arns.receiver" not in execution
    assert "secretsmanager" not in block("aws_iam_role_policy", "worker_task")
    # ECS injects the worker's secrets by ARN; the receiver loads its own at cold start by name
    assert "valueFrom = aws_secretsmanager_secret.secret[s].arn" in block("aws_ecs_task_definition", "worker")
    assert "NUA_AWS_SECRETS = join" in tf("locals.tf")


# ---------- receiver, worker, queue ----------

def test_receiver_is_a_public_function_url_that_ignores_the_image():
    fn = block("aws_lambda_function", "receiver")
    assert 'package_type  = "Image"' in fn and "timeout       = 30" in fn
    assert "ignore_changes = [image_uri, description]" in fn
    assert "count = local.services_on ? 1 : 0" in fn
    assert 'authorization_type = "NONE"' in block("aws_lambda_function_url", "receiver")
    assert 'function_url_auth_type = "NONE"' in block("aws_lambda_permission", "url")
    invoke = block("aws_lambda_permission", "url_invoke")
    assert '"lambda:InvokeFunction"' in invoke and "invoked_via_function_url = true" in invoke
    env = tf("locals.tf")
    for line in ('HOME         = "/tmp"', 'AWS_LWA_READINESS_CHECK_PATH = "/healthz"', 'QUEUE_BACKEND              = "sqs"'):
        assert line in env, line


def test_worker_runs_one_task_at_a_time_and_scales_between_zero_and_one():
    task = block("aws_ecs_task_definition", "worker")
    assert 'command     = ["python", "-m", "service.worker"]' in task
    assert "image       = local.worker_image" in task and ':live"' in tf("locals.tf")
    svc = block("aws_ecs_service", "worker")
    assert "desired_count   = 0" in svc and "ignore_changes = [desired_count]" in svc
    assert "deployment_maximum_percent         = 100" in svc and "deployment_minimum_healthy_percent = 0" in svc
    assert "assign_public_ip = true" in svc
    target = block("aws_appautoscaling_target", "worker")
    assert "min_capacity       = 0" in target and "max_capacity       = 1" in target
    assert 'expression = "visible + delayed"' in tf("worker.tf")
    assert 'expression = "visible + inflight + delayed"' in tf("worker.tf")
    # the worker group has no inbound rule
    assert "ingress" not in block("aws_security_group", "worker")
    assert "aws_nat_gateway" not in all_tf()


def test_queue_mirrors_cloud_tasks():
    queue = block("aws_sqs_queue", "tasks")
    timeout = int(re.search(r"visibility_timeout_seconds = (\d+)", queue).group(1))
    assert timeout > 1800
    assert "maxReceiveCount     = 5" in queue and "deadLetterTargetArn = aws_sqs_queue.dlq.arn" in queue
    names = block("aws_dynamodb_table", "task_names")
    assert 'billing_mode = "PAY_PER_REQUEST"' in names and 'attribute_name = "expires_at"' in names
    from nua.tasks import NAME_HOLD_SECONDS, retry_delay
    gcp_queue = (GCP_TF / "tasks.tf").read_text(encoding="utf-8")
    assert 'min_backoff  = "60s"' in gcp_queue and retry_delay(0) == 60
    assert 'max_backoff  = "1800s"' in gcp_queue and retry_delay(10) == 1800
    assert NAME_HOLD_SECONDS == 3600


# ---------- schedules ----------

def test_schedules_match_the_gcp_jobs():
    gcp = (GCP_TF / "scheduler.tf").read_text(encoding="utf-8")
    aws = tf("locals.tf")
    expected = {   # GCP cron -> EventBridge cron (six fields, ? in one day field, MON not 1)
        "sweep": ("0 2 * * *", "cron(0 2 * * ? *)", "true"),
        "sweep-granola": ("30 * * * *", "cron(30 * * * ? *)", "true"),
        "rollup": ("0 8 * * 1", "cron(0 8 ? * MON *)", "true"),
        "rollup-bootstrap": ("0 0 1 1 *", "cron(0 0 1 1 ? *)", "false"),
    }
    for name, (gcp_cron, aws_cron, scheduled) in expected.items():
        g = next(line for line in gcp.splitlines() if line.strip().startswith(f"{name} "))
        a = next(line for line in aws.splitlines() if line.strip().startswith(f"{name} "))
        assert f'schedule = "{gcp_cron}"' in g and f"scheduled = {scheduled}" in g, name
        assert f'cron = "{aws_cron}"' in a and f"scheduled = {scheduled}" in a, name
    assert 'kind = "sweep", payload = { granola_only = true }' in aws
    assert 'kind = "rollup", payload = { bootstrap = true }' in aws
    job = block("aws_scheduler_schedule", "job")
    assert 'state                        = var.schedules_on && each.value.scheduled ? "ENABLED" : "DISABLED"' in job
    assert "schedule_expression_timezone = var.time_zone" in job
    assert "for_each = local.services_on ? local.schedules : {}" in job
    assert "arn      = aws_sqs_queue.tasks.arn" in job


def test_time_zone_variable_mirrors_gcp():
    def parts(text: str) -> tuple[str, str]:
        body = variable("time_zone", text)
        return (re.search(r'default\s*=\s*"([^"]+)"', body).group(1),
                re.search(r"condition\s*=\s*(.+)", body).group(1))
    assert parts(tf("variables.tf")) == parts((GCP_TF / "variables.tf").read_text(encoding="utf-8"))
    assert re.search(r'variable "schedules_on" \{[^}]*default\s*=\s*false', tf("variables.tf"))


def test_budget_only_with_emails():
    budget = block("aws_budgets_budget", "monthly")
    assert "count = length(var.budget_emails) > 0 ? 1 : 0" in budget
    assert '"FORECASTED"' in budget and "[50, \"ACTUAL\"]" in budget


def test_build_matches_the_architecture():
    build = block("aws_codebuild_project", "build")
    assert 'var.architecture == "arm64" ? "ARM_CONTAINER" : "LINUX_CONTAINER"' in build
    assert "privileged_mode = true" in build
    assert "architectures = [local.lambda_arch]" in block("aws_lambda_function", "receiver")
    assert "cpu_architecture        = local.ecs_arch" in block("aws_ecs_task_definition", "worker")
    lifecycle = block("aws_ecr_lifecycle_policy", "keep_10")
    assert "countNumber = 10" in lifecycle


def test_image_carries_the_lambda_adapter_and_takes_the_base_image_as_an_argument():
    docker = (ROOT / "service" / "Dockerfile").read_text(encoding="utf-8")
    assert re.search(r"COPY --from=public\.ecr\.aws/awsguru/aws-lambda-adapter:\d+\.\d+\.\d+ /lambda-adapter "
                     r"/opt/extensions/lambda-adapter", docker)
    assert "ARG PYTHON_IMAGE=python:3.12-slim\nFROM ${PYTHON_IMAGE}" in docker
    assert "public.ecr.aws/docker/library/python:3.12-slim" in tf("build.tf")
    assert '--build-arg PYTHON_IMAGE="$PYTHON_IMAGE"' in tf("build.tf")


def test_enqueue_kinds_match_the_task_kinds():
    text = (AWS / "enqueue.sh").read_text(encoding="utf-8")
    kinds = re.search(r"^\s*(process_call\|[\w|]+)\)", text, re.M).group(1).split("|")
    assert tuple(kinds) == TASK_KINDS


# ---------- scripts against a fake aws ----------

FAKE_AWS = r"""#!/usr/bin/env bash
# Logs every call. Secrets live in $STORE/secrets/<name with / as __>/ (exists = created), versions 1, 2, ...
printf '%s\n' "$*" >> "$STORE/calls.log"
secret=""
for a in "$@"; do case "$a" in --secret-id=*) secret="${a#--secret-id=}";; esac; done
dir="$STORE/secrets/${secret//\//__}"
case "$1 $2" in
  "secretsmanager describe-secret") [ -d "$dir" ]; exit ;;
  "secretsmanager get-secret-value")
    last=$(ls "$dir" 2>/dev/null | sort -n | tail -1); [ -n "$last" ] || exit 254
    cat "$dir/$last"; exit 0 ;;
  "secretsmanager put-secret-value")
    n=$(( $(ls "$dir" | wc -l) + 1 )); cat > "$dir/$n"; exit 0 ;;
  "sts get-caller-identity") echo 111122223333; exit 0 ;;
  "codebuild start-build") echo "nua-build:abc"; exit 0 ;;
  "codebuild batch-get-builds") echo "${BUILD_STATUS:-SUCCEEDED}"; exit 0 ;;
esac
exit 0
"""


@pytest.fixture
def fake_aws(tmp_path: Path):
    store = tmp_path / "store"
    (store / "secrets").mkdir(parents=True)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for tool, body in (("aws", FAKE_AWS), ("zip", '#!/usr/bin/env bash\nprintf "zip %s\\n" "$*" >> "$STORE/calls.log"\n'),
                       ("terraform", '#!/usr/bin/env bash\nprintf "terraform %s\\n" "$*" >> "$STORE/calls.log"\n'),
                       ("curl", '#!/usr/bin/env bash\nprintf "curl %s\\n" "$*" >> "$STORE/calls.log"\n')):
        path = bin_dir / tool
        path.write_text(body, encoding="utf-8")
        path.chmod(path.stat().st_mode | stat.S_IXUSR)

    def run(script: str, *args: str, env_text: str | None = None, create: tuple[str, ...] = (), **extra):
        for name in create:
            (store / "secrets" / name.replace("/", "__")).mkdir(exist_ok=True)
        env_file = tmp_path / ".env"
        env_file.write_text(env_text or "", encoding="utf-8")
        (store / "calls.log").unlink(missing_ok=True)
        env = {"PATH": f"{bin_dir}{os.pathsep}/usr/bin{os.pathsep}/bin", "STORE": str(store), "HOME": str(tmp_path),
               "TMPDIR": str(tmp_path), "AWS_REGION": "us-west-2", "ENV_FILE": str(env_file), "IMAGE_TAG": "t1", **extra}
        p = subprocess.run([BASH, str(AWS / script), *args], env=env, capture_output=True, text=True, timeout=120)
        log = store / "calls.log"
        calls = log.read_text(encoding="utf-8").splitlines() if log.exists() else []
        return p.returncode, p.stdout + p.stderr, calls, store

    return run


CORE = ["granola-api-key", "granola-webhook-secret", "github-app-private-key", "github-webhook-secret",
        "slack-bot-token", "slack-signing-secret", "anthropic-api-key", "openai-api-key", "gemini-api-key"]
ENV = ("SLACK_BOT_TOKEN=xoxb-real\nGEMINI_API_KEY=gem-secret\n"
       "GITHUB_APP_PRIVATE_KEY=-----BEGIN RSA PRIVATE KEY-----\\nabc\\n-----END RSA PRIVATE KEY-----\n")


def versions(store: Path, name: str) -> list[str]:
    d = store / "secrets" / f"nua__{name}"
    return [(d / v).read_text() for v in sorted(os.listdir(d), key=int)]


def status(out: str, name: str) -> str:
    return next(line for line in out.splitlines() if line.strip().startswith(f"nua/{name} ")).split()[-1]


@needs_bash
def test_push_secrets_stores_values_without_printing_them(fake_aws):
    code, out, calls, store = fake_aws("push-secrets.sh", env_text=ENV, create=tuple(f"nua/{n}" for n in CORE))
    assert code == 0, out
    assert versions(store, "slack-bot-token") == ["xoxb-real"]
    assert versions(store, "github-app-private-key") == ["-----BEGIN RSA PRIVATE KEY-----\\nabc\\n-----END RSA PRIVATE KEY-----"]
    assert versions(store, "openai-api-key") == ["placeholder"]
    assert status(out, "slack-bot-token") == "set" and status(out, "openai-api-key") == "placeholder"
    for value in ("xoxb-real", "gem-secret", "BEGIN RSA"):
        assert value not in out and value not in "\n".join(calls)       # never printed, never on a command line
    assert any("--secret-string=file:///dev/stdin" in c for c in calls)

    code, out, _, store = fake_aws("push-secrets.sh", env_text="SLACK_BOT_TOKEN=xoxb-real\n")
    assert status(out, "slack-bot-token") == "unchanged" and status(out, "gemini-api-key") == "kept"
    code, out, _, store = fake_aws("push-secrets.sh", "--dry-run", env_text="SLACK_BOT_TOKEN=xoxb-rotated\n")
    assert status(out, "slack-bot-token") == "set" and versions(store, "slack-bot-token") == ["xoxb-real"]


@needs_bash
def test_push_secrets_missing_containers_and_circleback(fake_aws):
    code, out, _, _ = fake_aws("push-secrets.sh", env_text=ENV, create=tuple(f"nua/{n}" for n in CORE[:-1]))
    assert code == 1 and status(out, "gemini-api-key") == "missing" and "stage 1 terraform apply" in out
    extra = ("nua/circleback-key-alex", "nua/circleback-signing-alex")
    code, out, _, store = fake_aws("push-secrets.sh", env_text="CIRCLEBACK_API_KEY_ALEX=cb\n",
                                   create=tuple(f"nua/{n}" for n in CORE) + extra, CIRCLEBACK_PEOPLE="alex")
    assert code == 0, out
    assert versions(store, "circleback-key-alex") == ["cb"] and status(out, "circleback-signing-alex") == "placeholder"


@needs_bash
def test_deploy_dry_run_runs_nothing(fake_aws):
    code, out, calls, _ = fake_aws("deploy.sh", "--dry-run", AWS_ACCOUNT_ID="111122223333")
    assert code == 0, out
    assert calls == []
    image = "111122223333.dkr.ecr.us-west-2.amazonaws.com/nua-images:t1"
    assert "aws codebuild start-build" in out and "--project-name=nua-build" in out
    assert "s3://nua-build-111122223333-us-west-2/source/t1.zip" in out
    assert f"--function-name=nua-receiver --image-uri={image}" in out
    assert "--cluster=nua-cluster --service=nua-worker --force-new-deployment" in out


@needs_bash
def test_deploy_builds_then_rolls_out(fake_aws):
    code, out, calls, _ = fake_aws("deploy.sh")
    assert code == 0, out
    verbs = [" ".join(c.split()[:2]) for c in calls]
    assert verbs == ["sts get-caller-identity", "zip -qr", "s3 cp", "codebuild start-build", "codebuild batch-get-builds",
                     "lambda update-function-code", "lambda wait", "ecs update-service"]
    assert "pyproject.toml uv.lock nua service" in calls[1]
    code, out, calls, _ = fake_aws("deploy.sh", "--build-only")
    assert code == 0 and 'image = "111122223333.dkr.ecr.us-west-2.amazonaws.com/nua-images:t1"' in out
    assert not any(c.startswith(("lambda", "ecs")) for c in calls)
    code, out, calls, _ = fake_aws("deploy.sh", BUILD_STATUS="FAILED")
    assert code == 1 and "ended FAILED" in out and not any(c.startswith("lambda") for c in calls)
    code, out, calls, _ = fake_aws("deploy.sh", "--restart")
    assert code == 0 and [c.split()[1] for c in calls[1:]] == ["update-function-configuration", "wait", "update-service"]
    assert fake_aws("deploy.sh", "--bogus")[0] == 2


@needs_bash
def test_enqueue_sends_the_task_and_wakes_the_worker(fake_aws):
    code, out, calls, _ = fake_aws("enqueue.sh", "rollup", '{"bootstrap": true}')
    assert code == 0, out
    assert calls[1].startswith("sqs send-message")
    assert '--message-body={"kind": "rollup", "payload": {"bootstrap": true}}' in calls[1]
    assert "--queue-url=https://sqs.us-west-2.amazonaws.com/111122223333/nua-tasks" in calls[1]
    assert calls[2].startswith("ecs update-service") and "--desired-count=1" in calls[2]
    assert fake_aws("enqueue.sh", "nope")[0] == 2
    assert fake_aws("enqueue.sh", "sweep", "[1]")[0] == 2


@needs_bash
def test_bootstrap_dry_run_runs_nothing_and_prints_both_stages(fake_aws):
    code, out, calls, _ = fake_aws("bootstrap.sh", "--dry-run", AWS_ACCOUNT_ID="111122223333")
    assert code == 0, out
    assert calls == []
    assert "aws s3api create-bucket --bucket nua-tfstate-111122223333-us-west-2" in out
    assert "-backend-config=bucket=nua-tfstate-111122223333-us-west-2" in out
    assert out.count(" plan -input=false") == 2 and "deploy.sh --build-only" in out
    assert "push-secrets.sh" in out and "slack-manifest --receiver-url" in out and "github-app --repo" in out
    assert "Dry run: nothing was run or changed." in out


@needs_bash
def test_bootstrap_destroy_dry_run_lists_leftovers(fake_aws):
    code, out, calls, _ = fake_aws("bootstrap.sh", "--dry-run", "--destroy", "--delete-state-bucket",
                                   AWS_ACCOUNT_ID="111122223333")
    assert code == 0, out
    assert calls == []
    assert " plan -input=false -destroy" in out
    assert "aws ecs delete-task-definitions" in out          # ECS keeps deregistered ones as INACTIVE
    assert "resourcegroupstaggingapi get-resources" in out and "Key=app\\,Values=nua-discovery" in out
    assert "aws s3api delete-bucket --bucket nua-tfstate-111122223333-us-west-2" in out
    assert fake_aws("bootstrap.sh", "--delete-state-bucket")[0] == 2      # only with --destroy
