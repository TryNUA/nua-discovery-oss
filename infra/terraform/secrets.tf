# Secret Manager: the secret containers and who may read each one. Never the values: they are added
# with gcloud (infra/README.md), so no secret value is ever in a .tf file or in Terraform state.
#
# Secret name                   env var in the services              read by
# granola-api-key               GRANOLA_API_KEY                      receiver, processor (sweep)
# granola-webhook-secret        GRANOLA_WEBHOOK_SECRET               receiver
# anthropic-api-key             ANTHROPIC_API_KEY                    processor, mcp
# openai-api-key                OPENAI_API_KEY                       processor, mcp
# gemini-api-key                GEMINI_API_KEY                       processor, mcp (paid tier)
# github-app-private-key        GITHUB_APP_PRIVATE_KEY               receiver, processor, mcp
# github-webhook-secret         GITHUB_WEBHOOK_SECRET                receiver
# slack-bot-token               SLACK_BOT_TOKEN                      receiver, processor
# slack-signing-secret          SLACK_SIGNING_SECRET                 receiver
# circleback-key-<person>       CIRCLEBACK_API_KEY_<PERSON>          receiver (calendar start), processor (sweep)
# circleback-signing-<person>   CIRCLEBACK_SIGNING_SECRET_<PERSON>   receiver
# mcp-oauth-client-secret       MCP_OAUTH_CLIENT_SECRET              mcp
# mcp-jwt-signing-key           MCP_JWT_SIGNING_KEY                  mcp (any long random string; never
#                                                                    change it: it signs sign-in tokens and
#                                                                    names and encrypts the sign-in store, so
#                                                                    a new key signs everyone out)
#
# mcp and the two mcp- secrets exist only with deploy_mcp; circleback-* only for circleback_people
# (circleback-key-<person> matches circleback_key_name in config/people.yml). The table is derived
# from local.secret_env (locals.tf), the same map the services mount their secrets from.
#
# deletion_policy = "ABANDON": switching deploy_mcp off or dropping a person from circleback_people
# (or a terraform destroy) removes the secret from Terraform but keeps it and its values in the
# project, as setup.sh did. Switching it back on later needs an import first (infra/README.md).

resource "google_secret_manager_secret" "secret" {
  for_each = local.secrets

  secret_id       = each.value
  deletion_policy = "ABANDON"

  replication {
    auto {}
  }

  depends_on = [google_project_service.apis]
}

resource "google_secret_manager_secret_iam_member" "reader" {
  for_each = local.secret_readers

  secret_id = google_secret_manager_secret.secret[each.value.secret].secret_id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.sa[each.value.service].email}"
}
