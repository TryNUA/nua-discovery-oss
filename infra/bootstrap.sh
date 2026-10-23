#!/usr/bin/env bash
# Nua Discovery: set up (or re-check) the whole GCP side in one re-runnable command.
#
# It walks docs/self-hosting.md steps 2, 6 and 7 for you: checks your tools and both gcloud logins,
# optionally creates the project and links billing, enables the two bootstrap APIs, creates the
# Terraform state bucket, runs terraform init, writes terraform.tfvars, applies stage 1, pushes the
# secrets from .env, builds the first image, applies stage 2, restarts the services when a secret
# changed, and prints the URLs to give Slack, GitHub and your recorder.
#
# Every step checks first and skips when it is already done, so run it again after any fix. Each
# terraform apply shows the plan and asks first (unless --yes). It stops with instructions when an
# input is missing, e.g. the GitHub App ids (`uv run python -m nua github-app`) or the Slack keys.
#
# Usage (from anywhere):
#   bash infra/bootstrap.sh --project my-discovery-project [--region us-west1]
#   bash infra/bootstrap.sh --project ID --create-project --billing-account XXXXXX-XXXXXX-XXXXXX
#   bash infra/bootstrap.sh --yes        apply without asking (still prints each plan)
#   bash infra/bootstrap.sh --dry-run    print the commands, run nothing, change no file
#
# PROJECT_ID and REGION may also come from the environment or terraform.tfvars. Values are never
# printed: secrets go from .env to Secret Manager through infra/push-secrets.sh only.

set -euo pipefail

dry_run=
yes=
create_project=
billing_account=
project_arg=
region_arg=
while [ $# -gt 0 ]; do
  case "$1" in
    --dry-run) dry_run=1 ;;
    --yes|-y) yes=1 ;;
    --create-project) create_project=1 ;;
    --billing-account) billing_account="${2:?--billing-account needs an id}"; shift ;;
    --billing-account=*) billing_account="${1#*=}" ;;
    --project) project_arg="${2:?--project needs an id}"; shift ;;
    --project=*) project_arg="${1#*=}" ;;
    --region) region_arg="${2:?--region needs a region}"; shift ;;
    --region=*) region_arg="${1#*=}" ;;
    -h|--help) sed -n '2,21p' "$0"; exit 0 ;;
    *) echo "unknown argument $1 (try --help)" >&2; exit 2 ;;
  esac
  shift
done

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TF_DIR="${ROOT}/infra/terraform"
TFVARS="${TF_DIR}/terraform.tfvars"
EXAMPLE="${TF_DIR}/terraform.tfvars.example"
ENV_FILE="${ENV_FILE:-${ROOT}/.env}"

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
    printf '?'; printf ' %q' "$@"; printf '\n'
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
  if [ -n "${dry_run}" ]; then
    run terraform -chdir="${TF_DIR}" plan -input=false -out="${plan}"
    run terraform -chdir="${TF_DIR}" apply -input=false "${plan}"
    return 0
  fi
  printf '+ terraform -chdir=infra/terraform plan -input=false -detailed-exitcode -out=%s\n' "${plan##*/}"
  terraform -chdir="${TF_DIR}" plan -input=false -detailed-exitcode -out="${plan}" || status=$?
  case "${status}" in
    0) rm -f "${plan}"; skip "${label}: no changes"; return 0 ;;
    2) ;;
    *) rm -f "${plan}"; die "terraform plan failed (see above). An auth error: gcloud auth application-default login." ;;
  esac
  confirm "Apply ${label}?"
  printf '+ terraform -chdir=infra/terraform apply -input=false %s\n' "${plan##*/}"
  if ! terraform -chdir="${TF_DIR}" apply -input=false "${plan}"; then
    rm -f "${plan}"
    die "terraform apply failed. If it says the Cloud Tasks or Cloud Scheduler service agent does not exist yet, wait a minute and run this script again."
  fi
  rm -f "${plan}"
}

# ---------- 1. tools and logins ----------

say "Tools and logins"
missing_tools=
for tool in gcloud terraform uv curl; do
  command -v "${tool}" >/dev/null 2>&1 || missing_tools+=" ${tool}"
done
if [ -n "${missing_tools}" ]; then
  msg="missing:${missing_tools}. Install: gcloud https://cloud.google.com/sdk/docs/install, terraform 1.9+ https://developer.hashicorp.com/terraform/install, uv https://docs.astral.sh/uv/, curl from your package manager."
  if [ -n "${dry_run}" ]; then echo "   warning: ${msg}"; else die "${msg}"; fi
else
  skip "gcloud, terraform, uv and curl found"
fi
if check gcloud auth print-access-token; then
  skip "gcloud is logged in"
elif [ -z "${dry_run}" ]; then
  die "gcloud is not logged in. Run: gcloud auth login"
fi
if check gcloud auth application-default print-access-token; then
  skip "Application Default Credentials (used by Terraform) are set"
elif [ -z "${dry_run}" ]; then
  die "Terraform's login (Application Default Credentials) is missing or expired. Run: gcloud auth application-default login"
fi

# ---------- 2. project, billing, bootstrap APIs ----------

tf_project="$(tfvar "${TFVARS}" project_id)"
example_project="$(tfvar "${EXAMPLE}" project_id)"
[ "${tf_project}" = "${example_project}" ] && tf_project=
PROJECT_ID="${project_arg:-${PROJECT_ID:-${tf_project}}}"
[ -n "${PROJECT_ID}" ] || die "no project. Pass --project <id> (add --create-project to create it)."
if [ -n "${tf_project}" ] && [ "${tf_project}" != "${PROJECT_ID}" ]; then
  die "terraform.tfvars has project_id = \"${tf_project}\" but you asked for ${PROJECT_ID}. Fix one of them."
fi
REGION="${region_arg:-${REGION:-$(tfvar "${TFVARS}" region)}}"
REGION="${REGION:-us-west1}"

say "Project ${PROJECT_ID} in ${REGION}"
if check gcloud projects describe "${PROJECT_ID}"; then
  skip "project exists"
elif [ -n "${create_project}" ]; then
  run gcloud projects create "${PROJECT_ID}"
elif [ -z "${dry_run}" ]; then
  die "project ${PROJECT_ID} not found (or you cannot see it). Create it with --create-project, or check the id and your login."
fi

billing_on=
if [ -z "${dry_run}" ]; then
  billing_on="$(gcloud billing projects describe "${PROJECT_ID}" --format='value(billingEnabled)' 2>/dev/null || true)"
fi
if [ "${billing_on}" = "True" ] || [ "${billing_on}" = "true" ]; then
  skip "billing is linked"
elif [ -n "${billing_account}" ]; then
  run gcloud billing projects link "${PROJECT_ID}" --billing-account="${billing_account}"
elif [ -n "${dry_run}" ]; then
  echo "? gcloud billing projects describe ${PROJECT_ID}  (pass --billing-account to link one)"
else
  die "billing is not linked. List accounts with: gcloud billing accounts list, then re-run with --billing-account XXXXXX-XXXXXX-XXXXXX"
fi

BOOT_APIS="cloudresourcemanager.googleapis.com serviceusage.googleapis.com"
enabled=
if [ -z "${dry_run}" ]; then
  enabled="$(gcloud services list --enabled --project="${PROJECT_ID}" --format='value(config.name)' 2>/dev/null || true)"
fi
need_apis=
for api in ${BOOT_APIS}; do
  printf '%s\n' "${enabled}" | grep -qx "${api}" || need_apis+=" ${api}"
done
if [ -n "${need_apis}" ]; then
  # shellcheck disable=SC2086
  run gcloud services enable ${need_apis} --project="${PROJECT_ID}"
else
  skip "Cloud Resource Manager and Service Usage APIs are on"
fi

PROJECT_NUMBER="<project number>"
if [ -z "${dry_run}" ]; then
  PROJECT_NUMBER="$(gcloud projects describe "${PROJECT_ID}" --format='value(projectNumber)')"
fi
RECEIVER_URL="https://nua-receiver-${PROJECT_NUMBER}.${REGION}.run.app"
echo "   Receiver URL (deterministic, live after stage 2): ${RECEIVER_URL}"
echo "   The Slack app and GitHub App only need this URL, so you can create them now."

# ---------- 3. state bucket and terraform init ----------

say "Terraform state"
BUCKET="gs://${PROJECT_ID}-tfstate"
if check gcloud storage buckets describe "${BUCKET}" --project="${PROJECT_ID}"; then
  skip "state bucket ${BUCKET} exists"
else
  run gcloud storage buckets create "${BUCKET}" --project="${PROJECT_ID}" --location="${REGION}" \
    --uniform-bucket-level-access --public-access-prevention
  run gcloud storage buckets update "${BUCKET}" --versioning
fi
if [ -f "${TF_DIR}/.terraform/terraform.tfstate" ] && grep -q "\"bucket\": *\"${PROJECT_ID}-tfstate\"" "${TF_DIR}/.terraform/terraform.tfstate"; then
  skip "terraform is initialized against ${BUCKET}"
else
  run terraform -chdir="${TF_DIR}" init -input=false -backend-config="bucket=${PROJECT_ID}-tfstate" -backend-config="prefix=nua"
fi

# ---------- 4. inputs: terraform.tfvars and .env ----------

say "Inputs"
if [ -f "${TFVARS}" ]; then
  skip "terraform.tfvars exists"
elif [ -n "${dry_run}" ]; then
  echo "# would copy terraform.tfvars.example to terraform.tfvars"
else
  cp "${EXAMPLE}" "${TFVARS}"
  echo "   created terraform.tfvars from the example"
fi
TFV="${TFVARS}"
[ -f "${TFV}" ] || TFV="${EXAMPLE}"
[ "$(tfvar "${TFV}" project_id)" = "${PROJECT_ID}" ] || set_tfvar project_id "${PROJECT_ID}"
[ "$(tfvar "${TFV}" region)" = "${REGION}" ] || set_tfvar region "${REGION}"

# The Scheduler's time_zone follows timezone in config/pipeline.yml, which sets call-id dates.
pipeline_tz="$(sed -nE 's/^timezone:[[:space:]]*["'"'"']?([^"'"'"'#[:space:]]+).*/\1/p' "${ROOT}/config/pipeline.yml" 2>/dev/null | head -n 1 || true)"
pipeline_tz="${pipeline_tz:-America/Los_Angeles}"
tf_tz="$(tfvar "${TFV}" time_zone)"
if [ "${tf_tz}" != "${pipeline_tz}" ]; then
  if [ -n "${tf_tz}" ] && [ "${tf_tz}" != "$(tfvar "${EXAMPLE}" time_zone)" ]; then
    echo "   warning: terraform.tfvars has time_zone = \"${tf_tz}\" but config/pipeline.yml has timezone: ${pipeline_tz}; using ${pipeline_tz}"
  fi
  set_tfvar time_zone "${pipeline_tz}"
fi

problems=
for key in repo_slug github_app_id github_app_installation_id github_app_slug; do
  value="$(tfvar "${TFV}" "${key}")"
  if [ -z "${value}" ] || [ "${value}" = "$(tfvar "${EXAMPLE}" "${key}")" ]; then
    problems+=$'\n'"  terraform.tfvars: ${key} is not set"
  fi
done
case "${problems}" in *github_app*) problems+=$'\n'"    -> uv run python -m nua github-app --repo <owner/name> --receiver-url ${RECEIVER_URL}" ;; esac
for key in GEMINI_API_KEY SLACK_BOT_TOKEN SLACK_SIGNING_SECRET GITHUB_APP_PRIVATE_KEY GITHUB_WEBHOOK_SECRET; do
  env_has "${key}" || problems+=$'\n'"  .env: ${key} is empty"
done
case "${problems}" in *SLACK_*) problems+=$'\n'"    -> uv run python -m nua slack-manifest --receiver-url ${RECEIVER_URL}, create the app, install it, copy the two values" ;; esac
case "${problems}" in *GEMINI_API_KEY*) problems+=$'\n'"    -> a paid-tier key from https://aistudio.google.com/apikey" ;; esac
if [ -n "${problems}" ]; then
  if [ -n "${dry_run}" ]; then
    echo "   warning, a real run stops here:${problems}"
  else
    die "fill these in, then run this script again (it picks up where it stopped):${problems}"
  fi
else
  skip "tfvars has the repo and GitHub App ids; .env has the model, Slack and GitHub keys"
fi

CIRCLEBACK_PEOPLE="$(tfvar_raw "${TFV}" circleback_people | tr -d '[]",' | xargs || true)"
DEPLOY_MCP=
[ "$(tfvar_raw "${TFV}" deploy_mcp)" = "true" ] && DEPLOY_MCP=1
for person in ${CIRCLEBACK_PEOPLE}; do
  P="$(echo "${person}" | tr '[:lower:]' '[:upper:]' | tr -c 'A-Z0-9\n' '_')"
  env_has "CIRCLEBACK_API_KEY_${P}" || echo "   note: CIRCLEBACK_API_KEY_${P} is empty in .env (the sweep cannot catch ${person}'s missed calls)"
done

# ---------- 5. stage 1, secrets, image, stage 2 ----------

services_before=
if check gcloud run services describe nua-receiver --project="${PROJECT_ID}" --region="${REGION}"; then
  services_before=1
fi

image="$(tfvar "${TFV}" image)"
if [ -z "${image}" ]; then
  say "Terraform stage 1 (APIs, accounts, empty secrets, registry, queue)"
  plan_and_apply "stage 1"
fi

say "Secrets from .env"
secrets_log="$(mktemp)"
trap 'rm -f "${secrets_log}"' EXIT
push=(env PROJECT_ID="${PROJECT_ID}" ENV_FILE="${ENV_FILE}" CIRCLEBACK_PEOPLE="${CIRCLEBACK_PEOPLE}" DEPLOY_MCP="${DEPLOY_MCP}" bash "${ROOT}/infra/push-secrets.sh")
if [ -n "${dry_run}" ]; then
  run "${push[@]}"
else
  printf '+ bash infra/push-secrets.sh  (CIRCLEBACK_PEOPLE="%s" DEPLOY_MCP=%s)\n' "${CIRCLEBACK_PEOPLE}" "${DEPLOY_MCP:-0}"
  "${push[@]}" | tee "${secrets_log}"
fi
secrets_changed=
grep -qE '[[:space:]]set$' "${secrets_log}" && secrets_changed=1

if [ -z "${image}" ]; then
  say "First image"
  build=(env PROJECT_ID="${PROJECT_ID}" REGION="${REGION}" bash "${ROOT}/infra/deploy.sh" --build-only)
  if [ -n "${dry_run}" ]; then
    run "${build[@]}"
    set_tfvar image "${REGION}-docker.pkg.dev/${PROJECT_ID}/nua/nua:<tag>"
  else
    build_log="$(mktemp)"
    printf '+ PROJECT_ID=%s REGION=%s bash infra/deploy.sh --build-only\n' "${PROJECT_ID}" "${REGION}"
    "${build[@]}" | tee "${build_log}"
    image="$(sed -n 's/.*image = "\([^"]*\)".*/\1/p' "${build_log}" | tail -n 1)"
    rm -f "${build_log}"
    [ -n "${image}" ] || die "the build printed no image line; see above"
    set_tfvar image "${image}"
    echo "   image = \"${image}\" written to terraform.tfvars"
  fi
else
  skip "image is set in terraform.tfvars (new code ships with infra/deploy.sh, not this script)"
fi

say "Terraform stage 2 (Cloud Run services, Scheduler jobs, paused until go-live)"
plan_and_apply "stage 2"

# A running instance keeps the secret values it started with (docs/self-hosting.md, "Rotate a secret").
if [ -n "${services_before}" ] && [ -n "${secrets_changed}" ]; then
  say "Restart the services so they read the new secrets"
  services="nua-receiver nua-processor"
  [ -n "${DEPLOY_MCP}" ] && services+=" nua-mcp"
  stamp="$(date +%s)"
  for service in ${services}; do
    run gcloud run services update "${service}" --project="${PROJECT_ID}" --region="${REGION}" \
      --update-labels="secrets-refreshed=${stamp}"
  done
elif [ -z "${dry_run}" ]; then
  skip "no restart needed"
else
  echo "# a real run restarts nua-receiver and nua-processor here when a secret changed and they already ran"
fi

# ---------- 6. check and next steps ----------

say "Health"
if check curl -fsS --max-time 20 "${RECEIVER_URL}/healthz"; then
  skip "${RECEIVER_URL}/healthz answers"
elif [ -z "${dry_run}" ]; then
  echo "   warning: ${RECEIVER_URL}/healthz does not answer yet. A first start can take a minute; check the nua-receiver logs if it stays down."
fi

say "Done. Next"
cat <<EOF
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
   (terraform -chdir=infra/terraform output lists them all.)

   1. Slack app, if not done: uv run python -m nua slack-manifest
      If you created it with --no-events, turn Event Subscriptions on now (docs/self-hosting.md, step 9).
   2. GitHub App, if not done: uv run python -m nua github-app --repo <owner/name>
      Then redeliver its first ping under Advanced > Recent deliveries: it should get a 200 now.
   3. Connect your recorder (docs/self-hosting.md, step 8a Granola or 8b Circleback), put its signing
      secret in .env, and run this script again: it pushes the secret and restarts the services.
   4. Go live (step 10): set ingest_cutoff in config/pipeline.yml, then schedules_on = true in
      terraform.tfvars and run this script again (or terraform plan and apply).
EOF
[ -z "${dry_run}" ] || echo $'\nDry run: nothing was run or changed.'
