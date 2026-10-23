#!/usr/bin/env bash
# Nua Discovery: copy secret values from your local .env into Secret Manager.
#
# Terraform creates the secrets empty (infra/terraform/secrets.tf) and never handles their values. This
# script fills them, once after stage 1 and again whenever a key changes. It reads each value from .env
# and pipes it straight to gcloud, so values are never printed, logged or put on a command line.
#
# For each secret it prints one word:
#   set          added the value from .env as a new version
#   unchanged    the latest version already equals .env; nothing added
#   placeholder  not in .env and the secret had no value yet; added "placeholder" so Cloud Run can start
#   kept         not in .env, but the secret already has a value; left alone
#   missing      the secret does not exist yet (run the stage 1 terraform apply first)
#
# A value that is empty, or still equal to its .env.example value, counts as "not in .env".
# After changing a secret that a running service uses, start a new revision so it reads the new value
# (infra/README.md, "Rotate a secret").
#
# Usage (from anywhere, with gcloud logged in):
#   bash infra/push-secrets.sh             push
#   bash infra/push-secrets.sh --dry-run   say what would happen, add nothing (reads the latest versions)
#
# Optional: PROJECT_ID (default: project_id in infra/terraform/terraform.tfvars), ENV_FILE (default .env),
# CIRCLEBACK_PEOPLE ("alex sam"; match circleback_people in tfvars), DEPLOY_MCP=1 (match deploy_mcp).

set -euo pipefail

dry_run=
for arg in "$@"; do
  case "${arg}" in
    --dry-run) dry_run=1 ;;
    -h|--help) sed -n '2,25p' "$0"; exit 0 ;;
    *) echo "unknown argument: ${arg} (try --help)" >&2; exit 2 ;;
  esac
done

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_FILE="${ENV_FILE:-${ROOT}/.env}"
EXAMPLE_FILE="${ROOT}/.env.example"
TFVARS="${ROOT}/infra/terraform/terraform.tfvars"

if [ -z "${PROJECT_ID:-}" ] && [ -f "${TFVARS}" ]; then
  PROJECT_ID="$(sed -n 's/^[[:space:]]*project_id[[:space:]]*=[[:space:]]*"\([^"]*\)".*/\1/p' "${TFVARS}" | head -n 1)"
fi
: "${PROJECT_ID:?set PROJECT_ID, or project_id in infra/terraform/terraform.tfvars}"
[ -f "${ENV_FILE}" ] || { echo "no env file at ${ENV_FILE}" >&2; exit 1; }

# "ENV_VAR secret-name" pairs, matching local.secret_env in infra/terraform/locals.tf.
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
if [ "${DEPLOY_MCP:-}" = "1" ]; then
  PAIRS+=$'\n'"MCP_OAUTH_CLIENT_SECRET mcp-oauth-client-secret"
  PAIRS+=$'\n'"MCP_JWT_SIGNING_KEY mcp-jwt-signing-key"
fi

# The value of KEY in a dotenv file: the first "KEY=" line, everything after the first "=".
value_of() {
  local file="$1" key="$2"
  [ -f "${file}" ] || return 0
  # One matching pair of surrounding quotes is dropped ("x" or 'x' -> x); everything else is sent as written.
  grep -m 1 "^${key}=" "${file}" | cut -d= -f2- | tr -d '\n' | sed -E 's/^"(.*)"$/\1/; s/^'"'"'(.*)'"'"'$/\1/' || true
}

missing=0
echo "Project ${PROJECT_ID}${dry_run:+ (dry run: nothing is added)}"
while read -r var secret; do
  [ -n "${var}" ] || continue
  if ! gcloud secrets describe "${secret}" --project="${PROJECT_ID}" >/dev/null 2>&1; then
    printf '  %-24s missing\n' "${secret}"
    missing=1
    continue
  fi
  value="$(value_of "${ENV_FILE}" "${var}")"
  if [ -n "${value}" ] && [ "${value}" = "$(value_of "${EXAMPLE_FILE}" "${var}")" ]; then
    value=""
  fi
  latest="$(gcloud secrets versions access latest --secret="${secret}" --project="${PROJECT_ID}" 2>/dev/null || true)"
  if [ -n "${value}" ]; then
    if [ "${value}" = "${latest}" ]; then
      action=unchanged
    else
      action=set
      [ -n "${dry_run}" ] || printf '%s' "${value}" | gcloud secrets versions add "${secret}" \
        --project="${PROJECT_ID}" --data-file=- >/dev/null
    fi
  elif [ -n "${latest}" ]; then
    action=kept
  else
    action=placeholder
    [ -n "${dry_run}" ] || printf 'placeholder' | gcloud secrets versions add "${secret}" \
      --project="${PROJECT_ID}" --data-file=- >/dev/null
  fi
  printf '  %-24s %s\n' "${secret}" "${action}"
done <<< "${PAIRS}"

if [ "${missing}" = 1 ]; then
  echo "Some secrets do not exist yet: run the stage 1 terraform apply first (infra/README.md)." >&2
  exit 1
fi
