# Names, URLs and the per-service settings the other files share.

data "google_project" "this" {
  project_id = var.project_id
}

locals {
  # Stage gate (infra/README.md, "First-time setup"). Stage 1 leaves `image` empty: APIs, accounts,
  # secret containers, the registry and the queue are created, but no Cloud Run service or Scheduler job,
  # since a service cannot start without an image and secret values. Stage 2 sets `image`.
  services_on = var.image != ""
  mcp_on      = var.deploy_mcp

  queue_name   = "discovery"
  ar_repo      = "discovery"
  build_bucket = "${var.project_id}-discovery-build"
  time_zone    = "America/Los_Angeles"
  oauth_bucket = var.oauth_bucket != "" ? var.oauth_bucket : "${var.project_id}-discovery-mcp-oauth"
  oauth_mount  = "/oauth"

  receiver_name  = "discovery-receiver"
  processor_name = "discovery-processor"
  mcp_name       = "discovery-mcp"

  # Cloud Run's deterministic service URL: https://[TAG---]SERVICE_NAME-PROJECT_NUMBER.REGION.run.app
  # (https://cloud.google.com/run/docs/triggering/https-request, checked 2026-10-04). It is known before
  # the service exists, so the services can carry their own and each other's URLs in one revision
  # without a self-reference. Cloud Run falls back to a random URL only when that DNS label is over 63
  # characters; "discovery-processor-<12 digit number>" is 26.
  project_number = data.google_project.this.number
  url_of         = { for name in [local.receiver_name, local.processor_name, local.mcp_name] : name => "https://${name}-${local.project_number}.${var.region}.run.app" }
  receiver_url   = local.url_of[local.receiver_name]
  processor_url  = local.url_of[local.processor_name]
  mcp_url        = local.url_of[local.mcp_name]

  # One service account per Cloud Run service, plus discovery-tasks (the identity Cloud Tasks calls the
  # processor as), discovery-scheduler (the identity Cloud Scheduler signs its OIDC tokens with) and
  # discovery-build (the identity Cloud Build runs infra/deploy.sh's builds as).
  service_accounts = merge(
    {
      receiver  = "Discovery Base receiver"
      processor = "Discovery Base processor"
      tasks     = "Cloud Tasks caller of the processor"
      scheduler = "Cloud Scheduler caller of the receiver"
      build     = "Cloud Build runner of infra/deploy.sh"
    },
    local.mcp_on ? { mcp = "Discovery Base MCP server" } : {},
  )
  sa_email = { for k, _ in local.service_accounts : k => "discovery-${k}@${var.project_id}.iam.gserviceaccount.com" }

  # Everyone who enqueues tasks (receiver, processor, and discovery-mcp when deployed).
  enqueuers = concat(["receiver", "processor"], local.mcp_on ? ["mcp"] : [])

  # Per-person env var suffix, as discovery/settings.py person_var builds it: alex -> ALEX.
  circleback = { for p in var.circleback_people : p => replace(upper(p), "/[^A-Z0-9]/", "_") }

  # ---------- env vars ----------

  common_env = merge(
    {
      REPO_SLUG                  = var.repo_slug
      REPO_BRANCH                = var.repo_branch
      GITHUB_APP_ID              = var.github_app_id
      GITHUB_APP_INSTALLATION_ID = var.github_app_installation_id
      GITHUB_APP_SLUG            = var.github_app_slug
      GCP_PROJECT                = var.project_id
      TASKS_LOCATION             = var.region
      TASKS_QUEUE                = local.queue_name
      TASKS_SERVICE_ACCOUNT      = local.sa_email["tasks"]
      PROCESSOR_URL              = local.processor_url
    },
    var.slack_test_mode ? { DISCOVERY_SLACK_TEST = "1" } : {},
  )

  plain_env = {
    processor = merge(local.common_env, { SERVICE_ROLE = "processor" })
    receiver = merge(
      local.common_env,
      {
        SERVICE_ROLE              = "receiver"
        RECEIVER_URL              = local.receiver_url # the audience Scheduler tokens must carry
        SCHEDULER_SERVICE_ACCOUNT = local.sa_email["scheduler"]
      },
    )
    # Sign-in needs the service's own URL (MCP_BASE_URL); it is deterministic, so the first revision
    # already has sign-in on. With mcp_auth = "none" the service is never made public (cloud_run.tf).
    mcp = merge(
      local.common_env,
      { SERVICE_ROLE = "mcp", FASTMCP_HOME = local.oauth_mount, MCP_AUTH = var.mcp_auth },
      var.mcp_auth == "none" ? {} : { MCP_OAUTH_CLIENT_ID = var.mcp_oauth_client_id, MCP_BASE_URL = local.mcp_url },
    )
  }

  # ---------- secrets: env var -> Secret Manager secret, per service ----------
  # The single source for both the secret env refs (cloud_run.tf) and who may read each secret
  # (secrets.tf): a service account reads exactly the secrets its service mounts.

  secret_env = merge(
    {
      receiver = merge(
        {
          GRANOLA_API_KEY        = "granola-api-key"
          GRANOLA_WEBHOOK_SECRET = "granola-webhook-secret"
          GITHUB_APP_PRIVATE_KEY = "github-app-private-key"
          GITHUB_WEBHOOK_SECRET  = "github-webhook-secret"
          SLACK_BOT_TOKEN        = "slack-bot-token"
          SLACK_SIGNING_SECRET   = "slack-signing-secret"
        },
        { for p, P in local.circleback : "CIRCLEBACK_API_KEY_${P}" => "circleback-key-${p}" },
        { for p, P in local.circleback : "CIRCLEBACK_SIGNING_SECRET_${P}" => "circleback-signing-${p}" },
      )
      processor = merge(
        {
          GRANOLA_API_KEY        = "granola-api-key"
          GITHUB_APP_PRIVATE_KEY = "github-app-private-key"
          SLACK_BOT_TOKEN        = "slack-bot-token"
          ANTHROPIC_API_KEY      = "anthropic-api-key"
          OPENAI_API_KEY         = "openai-api-key"
          GEMINI_API_KEY         = "gemini-api-key"
        },
        { for p, P in local.circleback : "CIRCLEBACK_API_KEY_${P}" => "circleback-key-${p}" },
      )
    },
    local.mcp_on ? {
      mcp = {
        GITHUB_APP_PRIVATE_KEY  = "github-app-private-key"
        MCP_OAUTH_CLIENT_SECRET = "mcp-oauth-client-secret"
        MCP_JWT_SIGNING_KEY     = "mcp-jwt-signing-key"
        ANTHROPIC_API_KEY       = "anthropic-api-key"
        OPENAI_API_KEY          = "openai-api-key"
        GEMINI_API_KEY          = "gemini-api-key"
      }
    } : {},
  )

  secrets = toset(flatten([for svc, m in local.secret_env : values(m)]))

  secret_readers = merge([
    for svc, m in local.secret_env : { for s in toset(values(m)) : "${s}/${svc}" => { secret = s, service = svc } }
  ]...)
}
