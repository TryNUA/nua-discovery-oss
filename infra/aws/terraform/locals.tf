# Names, ARNs and the per-component settings the other files share.

locals {
  # Stage gate (docs/self-hosting-aws.md). Stage 1 leaves `image` empty: network, roles, secret
  # containers, the image repo, the build project and the queue are created, but no Lambda function, ECS
  # service or schedule, since the function cannot be created without an image. Stage 2 sets `image`.
  services_on = var.image != ""

  account_id = data.aws_caller_identity.current.account_id
  partition  = data.aws_partition.current.partition

  name = {
    receiver    = "${var.stack}-receiver"
    worker      = "${var.stack}-worker"
    cluster     = "${var.stack}-cluster"
    queue       = "${var.stack}-tasks"
    dlq         = "${var.stack}-tasks-dlq"
    names_table = "${var.stack}-task-names"
    images      = "${var.stack}-images"
    build       = "${var.stack}-build"
    schedules   = "${var.stack}-schedules"
  }
  build_bucket = "${var.stack}-build-${local.account_id}-${var.region}"

  # The worker's service ARN, built rather than read from the service: the receiver's role needs it in
  # stage 1, before the service exists, and reading it would make the roles wait for stage 2.
  worker_service_arn = "arn:${local.partition}:ecs:${var.region}:${local.account_id}:service/${local.name.cluster}/${local.name.worker}"

  # The worker runs the moving `live` tag, which every build also pushes; deploy.sh starts a new ECS
  # deployment so the service resolves it again. The receiver runs the exact tag deploy.sh sets.
  worker_image = "${aws_ecr_repository.images.repository_url}:live"

  lambda_arch = var.architecture
  ecs_arch    = var.architecture == "arm64" ? "ARM64" : "X86_64"

  # Per-person env var suffix, as nua/settings.py person_var builds it: alex -> ALEX.
  circleback = { for p in var.circleback_people : p => replace(upper(p), "/[^A-Z0-9]/", "_") }

  # ---------- env vars ----------

  common_env = merge(
    {
      REPO_SLUG                  = var.repo_slug
      REPO_BRANCH                = var.repo_branch
      GITHUB_APP_ID              = var.github_app_id
      GITHUB_APP_INSTALLATION_ID = var.github_app_installation_id
      GITHUB_APP_SLUG            = var.github_app_slug
      QUEUE_BACKEND              = "sqs"
      SQS_QUEUE_URL              = aws_sqs_queue.tasks.url
      TASK_NAMES_TABLE           = aws_dynamodb_table.task_names.name
      WORKER_CLUSTER             = local.name.cluster
      WORKER_SERVICE             = local.name.worker
      NUA_TASK_MAX_ATTEMPTS      = "5"
    },
    var.slack_test_mode ? { NUA_SLACK_TEST = "1" } : {},
  )

  receiver_env = merge(
    local.common_env,
    {
      SERVICE_ROLE = "receiver"
      HOME         = "/tmp" # Lambda's file system is read-only outside /tmp
      NUA_REPO_DIR = "/tmp/nua-repo"
      # Lambda Web Adapter (service/Dockerfile): forward to uvicorn on 8080 once /healthz answers.
      AWS_LWA_PORT                 = "8080"
      AWS_LWA_READINESS_CHECK_PATH = "/healthz"
      # Secrets the receiver loads at startup (nua/settings.py load_aws_secrets): ENV_VAR=secret-name,...
      NUA_AWS_SECRETS = join(",", [for var_name, secret in local.secret_env.receiver : "${var_name}=${local.secret_name[secret]}"])
    },
  )

  worker_env = merge(
    local.common_env,
    {
      SERVICE_ROLE        = "processor"
      WORKER_IDLE_SECONDS = tostring(var.worker_idle_seconds)
    },
  )

  # ---------- secrets: env var -> secret, per component ----------
  # The same names and split as infra/terraform/locals.tf, under "<stack>/" (nua/granola-api-key). The
  # single source for what the receiver loads, what ECS injects, and who may read each secret.

  secret_env = {
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
    worker = merge(
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
  }

  secrets     = toset(flatten([for component, m in local.secret_env : values(m)]))
  secret_name = { for s in local.secrets : s => "${var.stack}/${s}" }
  secret_arns = {
    for component, m in local.secret_env : component => [for s in toset(values(m)) : aws_secretsmanager_secret.secret[s].arn]
  }

  # ---------- schedules (schedules.tf) ----------
  # The same jobs, times and bodies as infra/terraform/scheduler.tf. GCP posts {} or {"granola_only":
  # true} to /sweep and {"bootstrap": ...} to /rollup, and the receiver enqueues exactly these tasks.
  # EventBridge cron has six fields and needs ? in day-of-month or day-of-week; MON, not 1 (1 is Sunday).

  schedules = {
    sweep            = { scheduled = true, cron = "cron(0 2 * * ? *)", kind = "sweep", payload = { granola_only = false }, description = "Nightly safety-net sweep, 2am ${var.time_zone}" }
    sweep-granola    = { scheduled = true, cron = "cron(30 * * * ? *)", kind = "sweep", payload = { granola_only = true }, description = "Hourly Granola-only sweep at :30, for notes a webhook missed" }
    rollup           = { scheduled = true, cron = "cron(0 8 ? * MON *)", kind = "rollup", payload = { bootstrap = false }, description = "Weekly roll-up, Mondays 8am ${var.time_zone}" }
    rollup-bootstrap = { scheduled = false, cron = "cron(0 0 1 1 ? *)", kind = "rollup", payload = { bootstrap = true }, description = "Bootstrap roll-up; run by hand after a backfill (infra/aws/enqueue.sh), never scheduled" }
  }
}
