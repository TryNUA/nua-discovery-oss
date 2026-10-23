#!/usr/bin/env bash
# Nua Discovery on AWS: copy secret values from your local .env into Secrets Manager.
#
# Terraform creates the secrets empty (infra/aws/terraform/secrets.tf) and never handles their values.
# This script fills them, once after stage 1 and again whenever a key changes. It reads each value from
# .env and pipes it straight to the AWS CLI, so values are never printed, logged or put on a command line.
# The secret names are the GCP ones under "<stack>/" (nua/granola-api-key), the env var names the same.
#
# For each secret it prints one word:
#   set          stored the value from .env as the secret's new current value
#   unchanged    the current value already equals .env; nothing stored
#   placeholder  not in .env and the secret had no value yet; stored "placeholder" so the worker can start
#   kept         not in .env, but the secret already has a value; left alone
#   missing      the secret does not exist yet (run the stage 1 terraform apply first)
#
# A value that is empty, or still equal to its .env.example value, counts as "not in .env".
# After changing a secret that a running receiver or worker uses, run infra/aws/deploy.sh --restart.
#
# Usage (from anywhere, with the AWS CLI logged in):
#   bash infra/aws/push-secrets.sh             push
#   bash infra/aws/push-secrets.sh --dry-run   say what would happen, store nothing (reads the current values)
#
# Optional: AWS_REGION (default: region in infra/aws/terraform/terraform.tfvars), STACK (default: stack
# in tfvars, else nua), ENV_FILE (default .env), CIRCLEBACK_PEOPLE ("alex sam"; default: circleback_people
# in tfvars).

set -euo pipefail

dry_run=
for arg in "$@"; do
  case "${arg}" in
    --dry-run) dry_run=1 ;;
    -h|--help) sed -n '2,26p' "$0"; exit 0 ;;
    *) echo "unknown argument: ${arg} (try --help)" >&2; exit 2 ;;
  esac
done

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
ENV_FILE="${ENV_FILE:-${ROOT}/.env}"
EXAMPLE_FILE="${ROOT}/.env.example"
TFVARS="${ROOT}/infra/aws/terraform/terraform.tfvars"
export AWS_PAGER=""

tfvar() {
  [ -f "${TFVARS}" ] || return 0
  sed -n "s/^[[:space:]]*$1[[:space:]]*=[[:space:]]*\"\([^\"]*\)\".*/\1/p" "${TFVARS}" | head -n 1
}

STACK="${STACK:-$(tfvar stack)}"
STACK="${STACK:-nua}"
REGION="${AWS_REGION:-${AWS_DEFAULT_REGION:-$(tfvar region)}}"
: "${REGION:?set AWS_REGION, or region in infra/aws/terraform/terraform.tfvars}"
if [ -z "${CIRCLEBACK_PEOPLE+x}" ] && [ -f "${TFVARS}" ]; then
  CIRCLEBACK_PEOPLE="$(sed -n 's/^[[:space:]]*circleback_people[[:space:]]*=[[:space:]]*\[\([^]]*\)\].*/\1/p' "${TFVARS}" \
    | head -n 1 | tr -d '",' || true)"
fi
[ -f "${ENV_FILE}" ] || { echo "no env file at ${ENV_FILE}" >&2; exit 1; }

# "ENV_VAR secret-name" pairs, matching local.secret_env in infra/aws/terraform/locals.tf.
PAIRS="GRANOLA_API_KEY granola-api-key
GRANOLA_WEBHOOK_SECRET granola-webhook-secret
GITHUB_APP_PRIVATE_KEY github-app-private-key
GITHUB_WEBHOOK_SECRET github-webhook-secret
SLACK_BOT_TOKEN slack-bot-token
SLACK_SIGNING_SECRET slack-signing-secret
ANTHROPIC_API_KEY anthropic-api-key
OPENAI_API_KEY openai-api-key
GEMINI_API_KEY gemini-api-key"
for person in ${CIRCLEBACK_PEOPLE:-}; do
  P="$(echo "${person}" | tr '[:lower:]' '[:upper:]' | tr -c 'A-Z0-9\n' '_')"
  PAIRS+=$'\n'"CIRCLEBACK_API_KEY_${P} circleback-key-${person}"
  PAIRS+=$'\n'"CIRCLEBACK_SIGNING_SECRET_${P} circleback-signing-${person}"
done

# The value of KEY in a dotenv file: the first "KEY=" line, everything after the first "=".
value_of() {
  local file="$1" key="$2"
  [ -f "${file}" ] || return 0
  # One matching pair of surrounding quotes is dropped ("x" or 'x' -> x); everything else is sent as written.
  grep -m 1 "^${key}=" "${file}" | cut -d= -f2- | tr -d '\n' | sed -E 's/^"(.*)"$/\1/; s/^'"'"'(.*)'"'"'$/\1/' || true
}

missing=0
echo "Region ${REGION}, secrets ${STACK}/*${dry_run:+ (dry run: nothing is stored)}"
while read -r var name; do
  [ -n "${var}" ] || continue
  secret="${STACK}/${name}"
  if ! aws secretsmanager describe-secret --secret-id="${secret}" --region="${REGION}" >/dev/null 2>&1; then
    printf '  %-32s missing\n' "${secret}"
    missing=1
    continue
  fi
  value="$(value_of "${ENV_FILE}" "${var}")"
  if [ -n "${value}" ] && [ "${value}" = "$(value_of "${EXAMPLE_FILE}" "${var}")" ]; then
    value=""
  fi
  latest="$(aws secretsmanager get-secret-value --secret-id="${secret}" --region="${REGION}" \
    --query=SecretString --output=text 2>/dev/null || true)"
  if [ -n "${value}" ]; then
    if [ "${value}" = "${latest}" ]; then
      action=unchanged
    else
      action=set
      # file:///dev/stdin: the value goes through a pipe, never onto a command line.
      [ -n "${dry_run}" ] || printf '%s' "${value}" | aws secretsmanager put-secret-value --secret-id="${secret}" \
        --region="${REGION}" --secret-string=file:///dev/stdin >/dev/null
    fi
  elif [ -n "${latest}" ]; then
    action=kept
  else
    action=placeholder
    [ -n "${dry_run}" ] || printf 'placeholder' | aws secretsmanager put-secret-value --secret-id="${secret}" \
      --region="${REGION}" --secret-string=file:///dev/stdin >/dev/null
  fi
  printf '  %-32s %s\n' "${secret}" "${action}"
done <<< "${PAIRS}"

if [ "${missing}" = 1 ]; then
  echo "Some secrets do not exist yet: run the stage 1 terraform apply first (docs/self-hosting-aws.md)." >&2
  exit 1
fi
