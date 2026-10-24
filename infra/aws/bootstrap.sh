#!/usr/bin/env bash
# Nua Discovery on AWS: set up (or re-check) the whole AWS side in one re-runnable command, or tear it down.
#
# It walks docs/self-hosting-aws.md steps 2 to 4 for you: checks your tools and your AWS login, creates
# the Terraform state bucket, runs terraform init, writes terraform.tfvars, applies stage 1, pushes the
# secrets from .env, builds the first image with CodeBuild, applies stage 2, restarts the receiver and
# worker when a secret changed, and prints the receiver URL and what to do next.
#
# Every step checks first and skips when it is already done, so run it again after any fix: once the
# Slack app and GitHub App exist (they need the receiver URL this prints), run it again to push their
# secrets and ids. Each terraform apply shows the plan and asks first (unless --yes).
#
# Usage (from anywhere):
#   bash infra/aws/bootstrap.sh [--region us-west-2]
#   bash infra/aws/bootstrap.sh --yes        apply without asking (still prints each plan)
#   bash infra/aws/bootstrap.sh --dry-run    print the commands, run nothing, change no file
#   bash infra/aws/bootstrap.sh --destroy [--delete-state-bucket] [--yes]
#       destroy everything Terraform made (asks you to type the stack name), list anything still
#       tagged app=nua-discovery for this stack, and with --delete-state-bucket also empty and delete the
#       state bucket (only then is nothing left)
#
# AWS credentials come from the usual places (AWS_PROFILE, aws configure, aws sso login). AWS_REGION
# and STACK may also come from the environment or terraform.tfvars. Values are never printed: secrets go
# from .env to Secrets Manager through infra/aws/push-secrets.sh only.

set -euo pipefail

dry_run=
yes=
destroy=
delete_state=
region_arg=
while [ $# -gt 0 ]; do
  case "$1" in
    --dry-run) dry_run=1 ;;
    --yes|-y) yes=1 ;;
    --destroy) destroy=1 ;;
    --delete-state-bucket) delete_state=1 ;;
    --region) region_arg="${2:?--region needs a region}"; shift ;;
    --region=*) region_arg="${1#*=}" ;;
    -h|--help) sed -n '2,25p' "$0"; exit 0 ;;
    *) echo "unknown argument $1 (try --help)" >&2; exit 2 ;;
  esac
  shift
done
if [ -n "${delete_state}" ] && [ -z "${destroy}" ]; then
  echo "--delete-state-bucket only goes with --destroy" >&2; exit 2
fi

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
TF_DIR="${ROOT}/infra/aws/terraform"
TFVARS="${TF_DIR}/terraform.tfvars"
EXAMPLE="${TF_DIR}/terraform.tfvars.example"
GCP_TFVARS="${ROOT}/infra/terraform/terraform.tfvars"
ENV_FILE="${ENV_FILE:-${ROOT}/.env}"
export AWS_PAGER=""

say() { printf '\n== %s\n' "$*"; }
skip() { printf '   ok: %s\n' "$*"; }
die() { printf '\nStopped: %s\n' "$*" >&2; exit 1; }

# Print a command; run it unless --dry-run.
run() {
  printf '+'; printf ' %q' "$@"; printf '\n'
  if [ -z "${dry_run}" ]; then "$@"; fi
}

# A read-only check: run quietly and return its status. In a dry run nothing runs: it prints the
# check and counts it as "not done yet", so the commands that would follow are printed too.
check() {
  if [ -n "${dry_run}" ]; then
    printf '? '; printf ' %q' "$@"; printf '\n'
    return 1
  fi
  "$@" >/dev/null 2>&1
}

confirm() {
  [ -n "${yes}" ] && return 0
  [ -n "${dry_run}" ] && return 0
  if [ ! -t 0 ] && [ -z "${BOOTSTRAP_ASSUME_TTY:-}" ]; then
    die "$1 needs a yes; run in a terminal or pass --yes"
  fi
  local answer
  read -r -p "$1 [y/N] " answer || true
  case "${answer}" in y|Y|yes|YES) return 0 ;; *) die "not applied. Re-run when ready." ;; esac
}

# tfvars helpers. String values only; commented lines are ignored.
tfvar() {
  local file="$1" key="$2"
  [ -f "${file}" ] || return 0
  sed -n "s/^[[:space:]]*${key}[[:space:]]*=[[:space:]]*\"\([^\"]*\)\".*/\1/p" "${file}" | head -n 1
}
tfvar_raw() {
  local file="$1" key="$2"
  [ -f "${file}" ] || return 0
  sed -n "s/^[[:space:]]*${key}[[:space:]]*=[[:space:]]*\([^#]*\).*/\1/p" "${file}" | head -n 1 | sed 's/[[:space:]]*$//'
}
# Set key = "value" in tfvars, keeping a replaced line's alignment, or append it.
set_tfvar() {
  local key="$1" value="$2"
  if [ -n "${dry_run}" ]; then echo "# would set ${key} = \"${value}\" in ${TFVARS}"; return 0; fi
  local tmp
  tmp="$(mktemp "${TF_DIR}/.tfvars.XXXXXX")"
  awk -v key="${key}" -v value="${value}" '
    !done && $0 ~ "^[ \t]*" key "[ \t]*=" { match($0, "^[ \t]*" key "[ \t]*=[ \t]*"); print substr($0, 1, RLENGTH) "\"" value "\""; done = 1; next }
    { print }
    END { if (!done) print key " = \"" value "\"" }' "${TFVARS}" > "${tmp}"
  mv "${tmp}" "${TFVARS}"
}
# Is KEY set in .env to something other than empty or its .env.example value? Never prints the value.
env_has() {
  local key="$1" value example
  [ -f "${ENV_FILE}" ] || return 1
  value="$(grep -m 1 "^${key}=" "${ENV_FILE}" | cut -d= -f2- || true)"
  example="$(grep -m 1 "^${key}=" "${ROOT}/.env.example" 2>/dev/null | cut -d= -f2- || true)"
  [ -n "${value}" ] && [ "${value}" != "${example}" ] && [ "${value}" != "placeholder" ]
}

# terraform plan, then apply the saved plan after a yes. Skips when there is nothing to change.
plan_and_apply() {
  local label="$1" plan="${TF_DIR}/bootstrap.tfplan" status=0
  shift
  if [ -n "${dry_run}" ]; then
    run terraform -chdir="${TF_DIR}" plan -input=false "$@" -out="${plan}"
    run terraform -chdir="${TF_DIR}" apply -input=false "${plan}"
    return 0
  fi
  printf '+ terraform -chdir=infra/aws/terraform plan -input=false %s-detailed-exitcode -out=%s\n' "${*:+$* }" "${plan##*/}"
  terraform -chdir="${TF_DIR}" plan -input=false "$@" -detailed-exitcode -out="${plan}" || status=$?
  case "${status}" in
    0) rm -f "${plan}"; skip "${label}: no changes"; return 0 ;;
    2) ;;
    *) rm -f "${plan}"; die "terraform plan failed (see above). An auth error: check aws sts get-caller-identity." ;;
  esac
  confirm "Apply ${label}?"
  printf '+ terraform -chdir=infra/aws/terraform apply -input=false %s\n' "${plan##*/}"
  if ! terraform -chdir="${TF_DIR}" apply -input=false "${plan}"; then
    rm -f "${plan}"
    die "terraform apply failed. A first ECS or Auto Scaling use in an account can fail once while AWS creates its service-linked role: run this script again. Otherwise see docs/self-hosting-aws.md, Troubleshooting."
  fi
  rm -f "${plan}"
}

# ---------- 1. tools and login ----------

say "Tools and login"
missing_tools=
for tool in aws terraform zip curl; do
  command -v "${tool}" >/dev/null 2>&1 || missing_tools+=" ${tool}"
done
if [ -n "${missing_tools}" ]; then
  msg="missing:${missing_tools}. Install: AWS CLI v2 https://docs.aws.amazon.com/cli/latest/userguide/getting-started-install.html, terraform 1.10+ https://developer.hashicorp.com/terraform/install, zip and curl from your package manager."
  if [ -n "${dry_run}" ]; then echo "   warning: ${msg}"; else die "${msg}"; fi
else
  skip "aws, terraform, zip and curl found"
fi
if [ -z "${dry_run}" ]; then
  tf_version="$(terraform version | sed -n 's/^Terraform v\([0-9]*\)\.\([0-9]*\).*/\1 \2/p' | head -n 1)"
  read -r tf_major tf_minor <<< "${tf_version:-0 0}"
  if [ "${tf_major}" -lt 1 ] || { [ "${tf_major}" -eq 1 ] && [ "${tf_minor}" -lt 10 ]; }; then
    die "Terraform 1.10 or newer is needed (S3 state locking). You have $(terraform version | head -n 1)."
  fi
fi

ACCOUNT="${AWS_ACCOUNT_ID:-<account-id>}"
if [ -z "${dry_run}" ]; then
  ACCOUNT="$(aws sts get-caller-identity --query Account --output text 2>/dev/null)" \
    || die "the AWS CLI is not logged in. Run aws configure, or aws sso login (and set AWS_PROFILE)."
  skip "logged in to account ${ACCOUNT} as $(aws sts get-caller-identity --query Arn --output text)"
else
  echo "? aws sts get-caller-identity"
fi

STACK="${STACK:-$(tfvar "${TFVARS}" stack)}"
STACK="${STACK:-nua}"
REGION="${region_arg:-${AWS_REGION:-$(tfvar "${TFVARS}" region)}}"
if [ -z "${REGION}" ] && [ -z "${dry_run}" ]; then REGION="$(aws configure get region 2>/dev/null || true)"; fi
REGION="${REGION:-us-west-2}"
tf_region="$(tfvar "${TFVARS}" region)"
if [ -n "${tf_region}" ] && [ "${tf_region}" != "${REGION}" ] && [ -n "${destroy}" ]; then
  die "terraform.tfvars has region = \"${tf_region}\" but you asked for ${REGION}."
fi
export AWS_REGION="${REGION}"
STATE_BUCKET="${STACK}-tfstate-${ACCOUNT}-${REGION}"
STATE_KEY="${STACK}/terraform.tfstate"
FUNCTION="${STACK}-receiver"
echo "   Stack ${STACK} in ${REGION}"

# ---------- 2. state bucket and terraform init ----------

terraform_init() {
  if [ -f "${TF_DIR}/.terraform/terraform.tfstate" ] && grep -q "\"bucket\": *\"${STATE_BUCKET}\"" "${TF_DIR}/.terraform/terraform.tfstate"; then
    skip "terraform is initialized against s3://${STATE_BUCKET}"
  else
    run terraform -chdir="${TF_DIR}" init -input=false -reconfigure -backend-config="bucket=${STATE_BUCKET}" \
      -backend-config="key=${STATE_KEY}" -backend-config="region=${REGION}"
  fi
}

# ---------- teardown ----------

# Delete every object version and delete marker in a versioned bucket, 1000 at a time.
empty_bucket() {
  local bucket="$1" kind batch
  batch="$(mktemp)"
  for kind in Versions DeleteMarkers; do
    while :; do
      aws s3api list-object-versions --bucket "${bucket}" --max-items 1000 \
        --query "{Objects: ${kind}[].{Key: Key, VersionId: VersionId}, Quiet: \`true\`}" --output json > "${batch}"
      grep -q '"Key"' "${batch}" || break
      aws s3api delete-objects --bucket "${bucket}" --delete "file://${batch}" >/dev/null
    done
  done
  rm -f "${batch}"
}

if [ -n "${destroy}" ]; then
  say "Destroy stack ${STACK} in account ${ACCOUNT}, ${REGION}"
  [ -f "${TFVARS}" ] || [ -n "${dry_run}" ] || die "no ${TFVARS}: Terraform needs the inputs it applied with."
  if check aws s3api head-bucket --bucket "${STATE_BUCKET}" || [ -n "${dry_run}" ]; then
    terraform_init
    if [ -z "${yes}" ] && [ -z "${dry_run}" ]; then
      [ -t 0 ] || [ -n "${BOOTSTRAP_ASSUME_TTY:-}" ] || die "--destroy needs a typed confirmation; run in a terminal or pass --yes"
      echo "This deletes the receiver, worker, queue, schedules, images, logs and every ${STACK}/ secret with its value."
      read -r -p "Type the stack name (${STACK}) to destroy it: " answer || true
      [ "${answer:-}" = "${STACK}" ] || die "not destroyed."
    fi
    plan_and_apply "destroy" -destroy
    # Terraform only deregisters task definitions; ECS keeps them as INACTIVE until they are deleted.
    say "Inactive task definitions"
    if [ -n "${dry_run}" ]; then
      echo "+ aws ecs delete-task-definitions (every INACTIVE ${STACK}-worker revision)"
    else
      inactive=$(aws ecs list-task-definitions --region "${REGION}" --family-prefix "${STACK}-worker" \
        --status INACTIVE --query 'taskDefinitionArns' --output text 2>/dev/null || true)
      if [ -n "${inactive}" ] && [ "${inactive}" != "None" ]; then
        # shellcheck disable=SC2086  # one word per ARN; the API takes at most 10 per call
        printf '%s\n' ${inactive} | xargs -n 10 aws ecs delete-task-definitions --region "${REGION}" \
          --query 'taskDefinitions[].taskDefinitionArn' --output text --task-definitions
      else
        echo "   ok: none"
      fi
    fi
    # The image went with the repo: a later bootstrap starts again from stage 1 and a fresh build.
    if [ -f "${TFVARS}" ] && [ -n "$(tfvar "${TFVARS}" image)" ]; then set_tfvar image ""; fi
  elif [ -z "${dry_run}" ]; then
    echo "   no state bucket s3://${STATE_BUCKET}: nothing for Terraform to destroy"
  fi

  say "Anything left"
  run aws resourcegroupstaggingapi get-resources --region "${REGION}" \
    --tag-filters "Key=app,Values=nua-discovery" "Key=stack,Values=${STACK}" \
    --query 'ResourceTagMappingList[].ResourceARN' --output text
  run aws iam list-roles --query "Roles[?starts_with(RoleName, '${STACK}-')].RoleName" --output text
  for prefix in "/aws/lambda/${STACK}-" "/ecs/${STACK}-" "/aws/codebuild/${STACK}-"; do
    run aws logs describe-log-groups --region "${REGION}" --log-group-name-prefix "${prefix}" \
      --query 'logGroups[].logGroupName' --output text
  done
  cat <<EOF
   Expected: nothing above but the state bucket arn:aws:s3:::${STATE_BUCKET} (kept unless
   --delete-state-bucket). The tagging API can show a just-deleted resource for a few minutes; run
   --destroy again to re-check. AWS's own service-linked roles (AWSServiceRoleForECS,
   AWSServiceRoleForApplicationAutoScaling_ECSService) stay: they are free and shared by the account.
   If nothing else in the account uses ECS and they did not exist before, remove them with
   aws iam delete-service-linked-role --role-name <name>.
EOF

  if [ -n "${delete_state}" ]; then
    say "State bucket"
    if check aws s3api head-bucket --bucket "${STATE_BUCKET}" || [ -n "${dry_run}" ]; then
      if [ -z "${yes}" ] && [ -z "${dry_run}" ]; then
        confirm "Empty and delete s3://${STATE_BUCKET} (every state version)?"
      fi
      echo "+ empty s3://${STATE_BUCKET} (every object version and delete marker)"
      [ -n "${dry_run}" ] || empty_bucket "${STATE_BUCKET}"
      run aws s3api delete-bucket --bucket "${STATE_BUCKET}" --region "${REGION}"
    elif [ -z "${dry_run}" ]; then
      skip "no state bucket"
    fi
  fi
  [ -z "${dry_run}" ] || echo $'\nDry run: nothing was run or changed.'
  exit 0
fi

say "Terraform state"
if check aws s3api head-bucket --bucket "${STATE_BUCKET}"; then
  skip "state bucket s3://${STATE_BUCKET} exists"
else
  if [ "${REGION}" = "us-east-1" ]; then
    run aws s3api create-bucket --bucket "${STATE_BUCKET}" --region "${REGION}"
  else
    run aws s3api create-bucket --bucket "${STATE_BUCKET}" --region "${REGION}" \
      --create-bucket-configuration "LocationConstraint=${REGION}"
  fi
  run aws s3api put-bucket-versioning --bucket "${STATE_BUCKET}" --versioning-configuration Status=Enabled
  run aws s3api put-public-access-block --bucket "${STATE_BUCKET}" --public-access-block-configuration \
    BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true
  run aws s3api put-bucket-tagging --bucket "${STATE_BUCKET}" \
    --tagging "TagSet=[{Key=app,Value=nua-discovery},{Key=stack,Value=${STACK}}]"
fi
terraform_init

# ---------- 3. inputs: terraform.tfvars and .env ----------

say "Inputs"
if [ -f "${TFVARS}" ]; then
  skip "terraform.tfvars exists"
elif [ -n "${dry_run}" ]; then
  echo "# would copy terraform.tfvars.example to terraform.tfvars"
else
  cp "${EXAMPLE}" "${TFVARS}"
  echo "   created infra/aws/terraform/terraform.tfvars from the example"
fi
TFV="${TFVARS}"
[ -f "${TFV}" ] || TFV="${EXAMPLE}"
[ "$(tfvar "${TFV}" region)" = "${REGION}" ] || set_tfvar region "${REGION}"

# `nua github-app` writes its ids to the GCP tfvars (infra/terraform/terraform.tfvars); take them from
# there when this file has none yet.
for key in repo_slug github_app_id github_app_installation_id github_app_slug; do
  ours="$(tfvar "${TFV}" "${key}")"
  theirs="$(tfvar "${GCP_TFVARS}" "${key}")"
  if { [ -z "${ours}" ] || [ "${ours}" = "$(tfvar "${EXAMPLE}" "${key}")" ]; } && [ -n "${theirs}" ] \
      && [ "${theirs}" != "$(tfvar "${ROOT}/infra/terraform/terraform.tfvars.example" "${key}")" ]; then
    set_tfvar "${key}" "${theirs}"
    echo "   ${key} copied from infra/terraform/terraform.tfvars (written by nua github-app)"
  fi
done

repo_slug="$(tfvar "${TFV}" repo_slug)"
if [ -z "${repo_slug}" ] || [ "${repo_slug}" = "$(tfvar "${EXAMPLE}" repo_slug)" ]; then
  if [ -n "${dry_run}" ]; then
    echo "   warning, a real run stops here: set repo_slug in infra/aws/terraform/terraform.tfvars"
  else
    die "set repo_slug = \"owner/name\" (your private instance repo) in infra/aws/terraform/terraform.tfvars, then run this script again."
  fi
fi

# The rest can wait: the Slack app and the GitHub App need the receiver URL, which exists after stage 2.
later=
for key in github_app_id github_app_installation_id github_app_slug; do
  [ -n "$(tfvar "${TFV}" "${key}")" ] || later+=$'\n'"     terraform.tfvars: ${key}"
done
for key in GEMINI_API_KEY SLACK_BOT_TOKEN SLACK_SIGNING_SECRET GITHUB_APP_PRIVATE_KEY GITHUB_WEBHOOK_SECRET; do
  env_has "${key}" || later+=$'\n'"     .env: ${key}"
done
if [ -n "${later}" ]; then
  echo "   not set yet (fine on a first run; the steps at the end fill them in):${later}"
else
  skip "tfvars has the repo and GitHub App ids; .env has the model, Slack and GitHub keys"
fi

CIRCLEBACK_PEOPLE="$(tfvar_raw "${TFV}" circleback_people | tr -d '[]",' | xargs || true)"
for person in ${CIRCLEBACK_PEOPLE}; do
  P="$(echo "${person}" | tr '[:lower:]' '[:upper:]' | tr -c 'A-Z0-9\n' '_')"
  env_has "CIRCLEBACK_API_KEY_${P}" || echo "   note: CIRCLEBACK_API_KEY_${P} is empty in .env (the sweep cannot catch ${person}'s missed calls)"
done

# ---------- 4. stage 1, secrets, image, stage 2 ----------

services_before=
if check aws lambda get-function --function-name "${FUNCTION}" --region "${REGION}"; then
  services_before=1
fi

image="$(tfvar "${TFV}" image)"
if [ -z "${image}" ]; then
  say "Terraform stage 1 (network, roles, empty secrets, image repo, build project, queue)"
  plan_and_apply "stage 1"
fi

say "Secrets from .env"
secrets_log="$(mktemp)"
trap 'rm -f "${secrets_log}"' EXIT
push=(env AWS_REGION="${REGION}" STACK="${STACK}" ENV_FILE="${ENV_FILE}" CIRCLEBACK_PEOPLE="${CIRCLEBACK_PEOPLE}" bash "${ROOT}/infra/aws/push-secrets.sh")
if [ -n "${dry_run}" ]; then
  run "${push[@]}"
else
  printf '+ bash infra/aws/push-secrets.sh  (CIRCLEBACK_PEOPLE="%s")\n' "${CIRCLEBACK_PEOPLE}"
  "${push[@]}" | tee "${secrets_log}"
fi
secrets_changed=
grep -qE '[[:space:]]set$' "${secrets_log}" && secrets_changed=1

if [ -z "${image}" ]; then
  say "First image (CodeBuild, about 5 minutes)"
  build=(env AWS_REGION="${REGION}" STACK="${STACK}" AWS_ACCOUNT_ID="${ACCOUNT}" bash "${ROOT}/infra/aws/deploy.sh" --build-only)
  if [ -n "${dry_run}" ]; then
    run "${build[@]}"
    set_tfvar image "${ACCOUNT}.dkr.ecr.${REGION}.amazonaws.com/${STACK}-images:<tag>"
  else
    build_log="$(mktemp)"
    printf '+ bash infra/aws/deploy.sh --build-only\n'
    "${build[@]}" | tee "${build_log}"
    image="$(sed -n 's/.*image = "\([^"]*\)".*/\1/p' "${build_log}" | tail -n 1)"
    rm -f "${build_log}"
    [ -n "${image}" ] || die "the build printed no image line; see above"
    set_tfvar image "${image}"
    echo "   image = \"${image}\" written to terraform.tfvars"
  fi
else
  skip "image is set in terraform.tfvars (new code ships with infra/aws/deploy.sh, not this script)"
fi

say "Terraform stage 2 (receiver, worker service, schedules disabled until go-live)"
plan_and_apply "stage 2"

# A running receiver or worker keeps the secret values it started with.
if [ -n "${services_before}" ] && [ -n "${secrets_changed}" ]; then
  say "Restart the receiver and the worker so they read the new secrets"
  run env AWS_REGION="${REGION}" STACK="${STACK}" AWS_ACCOUNT_ID="${ACCOUNT}" bash "${ROOT}/infra/aws/deploy.sh" --restart
elif [ -z "${dry_run}" ]; then
  skip "no restart needed"
else
  echo "# a real run restarts the receiver and the worker here when a secret changed and they already ran"
fi

# ---------- 5. check and next steps ----------

RECEIVER_URL="https://<id>.lambda-url.${REGION}.on.aws"
if [ -z "${dry_run}" ]; then
  RECEIVER_URL="$(terraform -chdir="${TF_DIR}" output -raw receiver_url 2>/dev/null || true)"
fi

say "Health"
if [ -n "${RECEIVER_URL}" ] && check curl -fsS --max-time 30 "${RECEIVER_URL}/healthz"; then
  skip "${RECEIVER_URL}/healthz answers"
elif [ -z "${dry_run}" ]; then
  echo "   warning: ${RECEIVER_URL:-the receiver}/healthz does not answer yet. A first start can take a few seconds; check: aws logs tail /aws/lambda/${FUNCTION} --region ${REGION}"
fi

say "Done. Next"
cat <<EOF
   Receiver URL: ${RECEIVER_URL}
   Webhook URLs:
     GitHub App webhook   ${RECEIVER_URL}/github
     Slack interactivity  ${RECEIVER_URL}/slack/interact
     Slack events         ${RECEIVER_URL}/slack/events  (bot event app_mention)
     Granola webhook      ${RECEIVER_URL}/ingest/granola
EOF
for person in ${CIRCLEBACK_PEOPLE}; do
  echo "     Circleback (${person})   ${RECEIVER_URL}/ingest/circleback/${person}"
done
cat <<EOF
   (terraform -chdir=infra/aws/terraform output lists them all.)

   1. Slack app, if not done: uv run python -m nua slack-manifest --receiver-url ${RECEIVER_URL}
      then put SLACK_BOT_TOKEN and SLACK_SIGNING_SECRET in .env.
   2. GitHub App, if not done: uv run python -m nua github-app --repo <owner/name> --receiver-url ${RECEIVER_URL}
      It writes the key and webhook secret to .env and the ids to infra/terraform/terraform.tfvars; this
      script copies the ids over on its next run.
   3. Run this script again: it pushes the new secrets, applies the ids and restarts the receiver and
      the worker. Then redeliver the GitHub App's first ping under Advanced > Recent deliveries.
   4. Connect your recorder (docs/self-hosting.md, step 8a Granola or 8b Circleback, with the URLs
      above), put its signing secret in .env, and run this script again.
   5. Go live (docs/self-hosting-aws.md, step 8): set ingest_cutoff in config/pipeline.yml, then
      schedules_on = true in infra/aws/terraform/terraform.tfvars and run this script again.
EOF
[ -z "${dry_run}" ] || echo $'\nDry run: nothing was run or changed.'
