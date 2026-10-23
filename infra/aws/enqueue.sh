#!/usr/bin/env bash
# Nua Discovery on AWS: queue one task by hand and wake the worker.
#
# The AWS stand-in for `gcloud scheduler jobs run <job>`: EventBridge Scheduler has no "run now", and
# rollup-bootstrap is never scheduled. It sends the message a schedule would send to the queue and sets
# the worker service to one task, as every enqueue in the app does (nua/tasks.py SqsQueue).
#
# Usage (from anywhere, with the AWS CLI logged in):
#   bash infra/aws/enqueue.sh rollup '{"bootstrap": true}'     the bootstrap roll-up, after a backfill
#   bash infra/aws/enqueue.sh sweep                            the nightly sweep, now
#   bash infra/aws/enqueue.sh sweep '{"granola_only": true}'   the hourly Granola sweep, now
#   bash infra/aws/enqueue.sh --dry-run KIND [PAYLOAD]         print the commands, run nothing
#
# KIND is a task kind from nua/ports.py (process_call, backfill, apply_belief_change, backcheck, reindex,
# rollup, sweep, ask); PAYLOAD a JSON object (default {}). Optional: AWS_REGION, STACK, AWS_ACCOUNT_ID,
# as for deploy.sh.

set -euo pipefail

dry_run=
args=()
for arg in "$@"; do
  case "${arg}" in
    --dry-run) dry_run=1 ;;
    -h|--help) sed -n '2,17p' "$0"; exit 0 ;;
    *) args+=("${arg}") ;;
  esac
done
[ "${#args[@]}" -ge 1 ] && [ "${#args[@]}" -le 2 ] || { echo "usage: enqueue.sh [--dry-run] KIND [PAYLOAD_JSON]" >&2; exit 2; }
KIND="${args[0]}"
PAYLOAD='{}'
[ "${#args[@]}" -lt 2 ] || PAYLOAD="${args[1]}"
case "${KIND}" in
  process_call|backfill|apply_belief_change|backcheck|reindex|rollup|sweep|ask) ;;
  *) echo "unknown task kind ${KIND}" >&2; exit 2 ;;
esac
case "${PAYLOAD}" in
  \{*\}) ;;
  *) echo "PAYLOAD must be a JSON object, e.g. '{\"bootstrap\": true}'" >&2; exit 2 ;;
esac

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
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
if [ -n "${AWS_ACCOUNT_ID:-}" ]; then
  ACCOUNT="${AWS_ACCOUNT_ID}"
elif [ -n "${dry_run}" ]; then
  ACCOUNT="<account-id>"
else
  ACCOUNT="$(aws sts get-caller-identity --query Account --output text)"
fi
QUEUE_URL="https://sqs.${REGION}.amazonaws.com/${ACCOUNT}/${STACK}-tasks"

run() {
  printf '+'; printf ' %q' "$@"; printf '\n'
  if [ -z "${dry_run}" ]; then "$@"; fi
}

run aws sqs send-message --region="${REGION}" --queue-url="${QUEUE_URL}" \
  --message-body="{\"kind\": \"${KIND}\", \"payload\": ${PAYLOAD}}" --query=MessageId --output=text
run aws ecs update-service --region="${REGION}" --cluster="${STACK}-cluster" --service="${STACK}-worker" \
  --desired-count=1 --query=service.desiredCount --output=text
echo
if [ -n "${dry_run}" ]; then
  echo "Dry run: nothing was queued."
else
  echo "Queued ${KIND}. The worker starts in a minute or two: aws logs tail /ecs/${STACK}-worker --follow --region ${REGION}"
fi
