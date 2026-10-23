#!/usr/bin/env bash
# Nua Discovery on AWS: build the image and roll it out to the receiver and the worker (code only).
#
# Infrastructure (function settings, env vars, secrets, IAM, queue, schedules) is Terraform's, in
# infra/aws/terraform/. This script zips the source the image needs, uploads it to the build bucket, runs
# the CodeBuild project (no local Docker needed), then points the Lambda receiver at the new image and
# starts a new deployment of the ECS worker so it resolves the new `live` tag. Terraform ignores the
# receiver's image, so the two never fight. It never runs terraform.
#
# Usage (from anywhere, with the AWS CLI logged in):
#   bash infra/aws/deploy.sh               build, push, roll out to the receiver and the worker
#   bash infra/aws/deploy.sh --build-only  build and push only (first-time setup: the receiver does not
#                                          exist yet); prints the image to set as `image` in tfvars
#   bash infra/aws/deploy.sh --restart     no build: start fresh receiver environments and a new worker
#                                          deployment, so both read changed secrets
#   bash infra/aws/deploy.sh --dry-run     print the commands, run nothing
#
# Optional: AWS_REGION (default: region in infra/aws/terraform/terraform.tfvars, then the CLI's region),
# STACK (default: stack in tfvars, else nua), IMAGE_TAG (default <UTC time>-<git short sha>),
# AWS_ACCOUNT_ID (default: from aws sts get-caller-identity), AWS_PROFILE as usual.
#
# Every build also moves the `live` tag, which the worker runs. The build project, bucket and image repo
# come from the stage 1 apply, so apply stage 1 first.

set -euo pipefail

dry_run=
build_only=
restart=
for arg in "$@"; do
  case "${arg}" in
    --dry-run) dry_run=1 ;;
    --build-only) build_only=1 ;;
    --restart) restart=1 ;;
    -h|--help) sed -n '2,25p' "$0"; exit 0 ;;
    *) echo "unknown argument ${arg} (expected --dry-run, --build-only, --restart)" >&2; exit 2 ;;
  esac
done
if [ -n "${build_only}" ] && [ -n "${restart}" ]; then
  echo "--build-only and --restart do not go together" >&2; exit 2
fi

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
if [ -z "${REGION}" ] && [ -z "${dry_run}" ]; then REGION="$(aws configure get region 2>/dev/null || true)"; fi
: "${REGION:?set AWS_REGION, or region in infra/aws/terraform/terraform.tfvars}"

if [ -n "${AWS_ACCOUNT_ID:-}" ]; then
  ACCOUNT="${AWS_ACCOUNT_ID}"
elif [ -n "${dry_run}" ]; then
  ACCOUNT="<account-id>"
else
  ACCOUNT="$(aws sts get-caller-identity --query Account --output text)"
fi
if [ -z "${IMAGE_TAG:-}" ]; then
  sha="$(git -C "${ROOT}" rev-parse --short HEAD 2>/dev/null || echo nogit)"
  IMAGE_TAG="$(date -u +%Y%m%d-%H%M%S)-${sha}"
fi

REPO_URL="${ACCOUNT}.dkr.ecr.${REGION}.amazonaws.com/${STACK}-images"
IMAGE="${REPO_URL}:${IMAGE_TAG}"
BUCKET="${STACK}-build-${ACCOUNT}-${REGION}"
SOURCE_KEY="source/${IMAGE_TAG}.zip"
PROJECT="${STACK}-build"
FUNCTION="${STACK}-receiver"
CLUSTER="${STACK}-cluster"
SERVICE="${STACK}-worker"

# Print a command; run it unless --dry-run.
run() {
  printf '+'; printf ' %q' "$@"; printf '\n'
  if [ -z "${dry_run}" ]; then "$@"; fi
}

restart_receiver_and_worker() {
  # A configuration update gives the function fresh environments, which load the secrets again at
  # their cold start. Terraform ignores the description.
  run aws lambda update-function-configuration --region="${REGION}" --function-name="${FUNCTION}" \
    --description="restarted $(date -u +%Y-%m-%dT%H:%M:%SZ)" --query=LastUpdateStatus --output=text
  run aws lambda wait function-updated --region="${REGION}" --function-name="${FUNCTION}"
  # A new deployment resolves the `live` tag and the secrets again. A running worker task is replaced;
  # a task it was running is retried later (service/worker.py).
  run aws ecs update-service --region="${REGION}" --cluster="${CLUSTER}" --service="${SERVICE}" \
    --force-new-deployment --query=service.serviceName --output=text
}

if [ -n "${restart}" ]; then
  restart_receiver_and_worker
  echo
  if [ -n "${dry_run}" ]; then echo "Dry run: nothing was restarted."; else echo "Restarted ${FUNCTION} and ${SERVICE}."; fi
  exit 0
fi

# ---------- build ----------

# Only what the image needs (service/Dockerfile copies pyproject.toml, uv.lock, nua/ and service/).
WORK="$(mktemp -d)"
trap 'rm -rf "${WORK}"' EXIT
ZIP="${WORK}/source.zip"
(cd "${ROOT}" && run zip -qr "${ZIP}" pyproject.toml uv.lock nua service -x '*/__pycache__/*' '*.pyc' '*/.DS_Store' 'nua/setup/static/*')
run aws s3 cp "${ZIP}" "s3://${BUCKET}/${SOURCE_KEY}" --region="${REGION}" --only-show-errors

start=(aws codebuild start-build --region="${REGION}" --project-name="${PROJECT}" --source-type-override=S3
       --source-location-override="${BUCKET}/${SOURCE_KEY}"
       --environment-variables-override="name=IMAGE_TAG,value=${IMAGE_TAG},type=PLAINTEXT"
       --query=build.id --output=text)
printf '+'; printf ' %q' "${start[@]}"; printf '\n'
if [ -n "${dry_run}" ]; then
  echo "# then poll: aws codebuild batch-get-builds --ids <build id> until SUCCEEDED (about 5 minutes)"
else
  build_id="$("${start[@]}")"
  echo "Building ${IMAGE} (${build_id}); this takes about 5 minutes."
  while :; do
    status="$(aws codebuild batch-get-builds --region="${REGION}" --ids="${build_id}" \
      --query='builds[0].buildStatus' --output=text)"
    case "${status}" in
      SUCCEEDED) break ;;
      IN_PROGRESS) sleep 15 ;;
      *)
        echo "Build ${build_id} ended ${status}. Its log:" >&2
        echo "  aws logs tail /aws/codebuild/${PROJECT} --since 1h --region ${REGION}" >&2
        exit 1 ;;
    esac
  done
fi

if [ -n "${build_only}" ]; then
  echo
  if [ -n "${dry_run}" ]; then echo "Dry run: nothing was built."; else echo "Built ${IMAGE}"; fi
  echo "Set it in infra/aws/terraform/terraform.tfvars:  image = \"${IMAGE}\""
  exit 0
fi

# ---------- roll out: only the image changes, every other setting stays as Terraform set it ----------

run aws lambda update-function-code --region="${REGION}" --function-name="${FUNCTION}" --image-uri="${IMAGE}" \
  --query=LastUpdateStatus --output=text
run aws lambda wait function-updated --region="${REGION}" --function-name="${FUNCTION}"
run aws ecs update-service --region="${REGION}" --cluster="${CLUSTER}" --service="${SERVICE}" \
  --force-new-deployment --query=service.serviceName --output=text

echo
if [ -n "${dry_run}" ]; then echo "Dry run: nothing was built or deployed."; else echo "Deployed ${IMAGE} to ${FUNCTION} and ${SERVICE}"; fi
