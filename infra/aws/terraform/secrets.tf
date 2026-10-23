# Secrets Manager: one secret per value, empty, and who may read it. Never the values: infra/aws/push-secrets.sh
# adds them from .env, so no secret value is ever in a .tf file or in Terraform state.
#
# Secret name                       env var                              read by
# nua/granola-api-key               GRANOLA_API_KEY                      receiver, worker (sweep)
# nua/granola-webhook-secret        GRANOLA_WEBHOOK_SECRET               receiver
# nua/anthropic-api-key             ANTHROPIC_API_KEY                    worker
# nua/openai-api-key                OPENAI_API_KEY                       worker
# nua/gemini-api-key                GEMINI_API_KEY                       worker (paid tier)
# nua/github-app-private-key        GITHUB_APP_PRIVATE_KEY               receiver, worker
# nua/github-webhook-secret         GITHUB_WEBHOOK_SECRET                receiver
# nua/slack-bot-token               SLACK_BOT_TOKEN                      receiver, worker
# nua/slack-signing-secret          SLACK_SIGNING_SECRET                 receiver
# nua/circleback-key-<person>       CIRCLEBACK_API_KEY_<PERSON>          receiver (calendar start), worker (sweep)
# nua/circleback-signing-<person>   CIRCLEBACK_SIGNING_SECRET_<PERSON>   receiver
#
# ("nua" is var.stack.) The names after the prefix are the GCP secret names (infra/terraform/secrets.tf).
# circleback-* exist only for circleback_people. The table is derived from local.secret_env (locals.tf).
#
# The receiver reads its secrets at cold start (NUA_AWS_SECRETS, nua/settings.py load_aws_secrets); ECS
# injects the worker's when a task starts. A running receiver or worker keeps the values it started
# with: after a change, run infra/aws/deploy.sh --restart.
#
# recovery_window_in_days = 0: a destroy, or dropping a person from circleback_people, deletes the
# secret and its value at once, so the same name can be created again right away. Unlike on GCP
# (deletion_policy = "ABANDON"), nothing is left behind: keep your .env.

resource "aws_secretsmanager_secret" "secret" {
  for_each = local.secrets

  name                    = local.secret_name[each.value]
  description             = "nua-discovery: ${each.value} (value from infra/aws/push-secrets.sh)"
  recovery_window_in_days = 0
}
