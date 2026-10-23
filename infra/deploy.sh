#!/usr/bin/env bash
# Discovery Base: build the image and roll it out to the Cloud Run services (code only).
#
# Infrastructure (services, settings, env vars, secrets, IAM, queue, Scheduler) is Terraform's, in
# infra/terraform/. This script only builds service/Dockerfile with Cloud Build, pushes it to Artifact
# Registry and points the existing services at the new image. Terraform ignores the image, so the two
# never fight (infra/README.md). It never runs terraform.
#
# Usage (from anywhere, with gcloud logged in):
#   PROJECT_ID=my-project bash infra/deploy.sh               build, push, roll out to every service
#   PROJECT_ID=my-project bash infra/deploy.sh --build-only  build and push only (first-time setup:
#                                                            the services do not exist yet); prints
#                                                            the image to set as `image` in tfvars
#   PROJECT_ID=my-project bash infra/deploy.sh --dry-run     print the commands, run nothing
#
# Optional: REGION (default us-west1), IMAGE_TAG (default <UTC time>-<git short sha>), SERVICES
# (default "discovery-processor discovery-receiver"; add discovery-mcp when deploy_mcp is on, e.g.
# SERVICES="discovery-processor discovery-receiver discovery-mcp").
#
# The build runs as discovery-build and uploads its source to <project>-discovery-build, both made by Terraform
# (artifact_registry.tf), so stage 1 must be applied first.

set -euo pipefail

dry_run=
build_only=
for arg in "$@"; do
  case "${arg}" in
    --dry-run) dry_run=1 ;;
    --build-only) build_only=1 ;;
    -h|--help) sed -n '2,22p' "$0"; exit 0 ;;
    *) echo "unknown argument ${arg} (expected --dry-run, --build-only)" >&2; exit 2 ;;
  esac
done

: "${PROJECT_ID:?set PROJECT_ID, e.g. my-discovery-project}"
REGION="${REGION:-us-west1}"
AR_REPO=discovery
SERVICES="${SERVICES:-discovery-processor discovery-receiver}"

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
if [ -z "${IMAGE_TAG:-}" ]; then
  sha="$(git -C "${REPO_ROOT}" rev-parse --short HEAD 2>/dev/null || echo nogit)"
  IMAGE_TAG="$(date -u +%Y%m%d-%H%M%S)-${sha}"
fi
IMAGE="${REGION}-docker.pkg.dev/${PROJECT_ID}/${AR_REPO}/discovery:${IMAGE_TAG}"
BUILD_SA="projects/${PROJECT_ID}/serviceAccounts/discovery-build@${PROJECT_ID}.iam.gserviceaccount.com"
SOURCE_DIR="gs://${PROJECT_ID}-discovery-build/source"

# Print a command; run it unless --dry-run.
run() {
  printf '+'; printf ' %q' "$@"; printf '\n'
  if [ -z "${dry_run}" ]; then "$@"; fi
}

# The Dockerfile lives in service/, so Cloud Build needs a small config instead of --tag.
# .gcloudignore at the repo root keeps the upload to what the image needs.
BUILD_CONFIG="$(mktemp)"
trap 'rm -f "${BUILD_CONFIG}"' EXIT
cat > "${BUILD_CONFIG}" <<YAML
steps:
  - name: gcr.io/cloud-builders/docker
    args: ["build", "-f", "service/Dockerfile", "-t", "${IMAGE}", "."]
images: ["${IMAGE}"]
options:
  logging: CLOUD_LOGGING_ONLY
YAML
if [ -n "${dry_run}" ]; then
  echo "# build config:"
  sed 's/^/#   /' "${BUILD_CONFIG}"
fi

run gcloud builds submit "${REPO_ROOT}" --project="${PROJECT_ID}" --config="${BUILD_CONFIG}" \
  --service-account="${BUILD_SA}" --gcs-source-staging-dir="${SOURCE_DIR}"

if [ -n "${build_only}" ]; then
  echo
  if [ -n "${dry_run}" ]; then echo "Dry run: nothing was built."; else echo "Built ${IMAGE}"; fi
  echo "Set it in infra/terraform/terraform.tfvars:  image = \"${IMAGE}\""
  exit 0
fi

# Only the image changes: every other setting stays as Terraform set it.
for service in ${SERVICES}; do
  run gcloud run services update "${service}" --project="${PROJECT_ID}" --region="${REGION}" --image="${IMAGE}"
done

echo
if [ -n "${dry_run}" ]; then echo "Dry run: nothing was built or deployed."; else echo "Deployed ${IMAGE} to: ${SERVICES}"; fi
