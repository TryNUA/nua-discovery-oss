# One image for the three Cloud Run services: receiver, processor and mcp.
# SERVICE_ROLE (set per service by infra/terraform) picks the app in service/main.py.
# The AWS deployment (infra/aws) runs the same image as the Lambda receiver and the ECS worker
# (`python -m service.worker`); see docs/self-hosting-aws.md.
#
# Build from the repo root:
#   docker build -f service/Dockerfile -t nua .
#
# Config, transcripts and digests are never baked in: each service clones the repo into
# NUA_REPO_DIR at startup (nua/settings.py) and reads config from that clone. Prompts are code
# (nua/prompts/), so a prompt change ships with a new image.
#
# PYTHON_IMAGE: AWS CodeBuild passes the same image from ECR Public (public.ecr.aws/docker/library/...),
# since anonymous Docker Hub pulls from shared build hosts hit its rate limit.
ARG PYTHON_IMAGE=python:3.12-slim
FROM ${PYTHON_IMAGE}

# uv installs the locked dependencies (uv.lock). Pin the uv image tag alongside uv.lock upgrades.
COPY --from=ghcr.io/astral-sh/uv:0.11.17 /uv /uvx /bin/

# AWS Lambda Web Adapter: on Lambda it runs as an extension and turns each invocation into an HTTP request
# to uvicorn below (infra/aws/terraform/receiver.tf). Anywhere else, Cloud Run included, nothing starts it.
COPY --from=public.ecr.aws/awsguru/aws-lambda-adapter:1.0.1 /lambda-adapter /opt/extensions/lambda-adapter

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_NO_CACHE=1 \
    UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/app/.venv \
    PATH="/app/.venv/bin:${PATH}" \
    PORT=8080 \
    NUA_REPO_DIR=/tmp/nua-repo

# git: the services keep a shallow clone and commit through it (nua/gitops.py).
RUN apt-get update \
 && apt-get install -y --no-install-recommends git ca-certificates \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
# uv.lock pins every dependency to the versions the tests passed on. Dependencies install first so
# code changes reuse that layer; --frozen fails the build instead of re-resolving, --no-dev skips pytest.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project
COPY nua ./nua
COPY service ./service
# COPY keeps the source's file modes, and the app runs as a non-root user below: make every file
# readable (and every folder enterable) whatever modes the files had on the machine that built it.
RUN chmod -R a+rX /app/nua /app/service \
 && uv sync --frozen --no-dev --no-editable

# uid 10001 is also in infra/terraform/mcp.tf: nua-mcp's sign-in volume (/oauth) is mounted owned by it.
RUN useradd --create-home --uid 10001 nua
USER nua

EXPOSE 8080
# Cloud Run terminates TLS in front of the container, so trust its forwarded headers.
CMD exec uvicorn service.main:app --host 0.0.0.0 --port "${PORT}" --proxy-headers --forwarded-allow-ips="*"
