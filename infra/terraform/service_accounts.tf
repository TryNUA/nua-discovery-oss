# Service accounts and the IAM between them, least privilege as before (formerly infra/setup.sh).
#
#   discovery-receiver, discovery-processor, discovery-mcp   one per Cloud Run service (discovery-mcp only with deploy_mcp)
#   discovery-tasks                              the identity Cloud Tasks calls the processor as
#   discovery-scheduler                          the identity Cloud Scheduler signs its OIDC tokens with
#   discovery-build                              the identity Cloud Build runs infra/deploy.sh's builds as
#
# Grants on Cloud Run services (who may invoke them) are in cloud_run.tf and mcp.tf, grants on secrets
# in secrets.tf, the sign-in bucket grant in mcp.tf, discovery-build's grants in artifact_registry.tf.

resource "google_service_account" "sa" {
  for_each = local.service_accounts

  account_id   = "discovery-${each.key}"
  display_name = each.value

  depends_on = [google_project_service.apis]
}

# Everyone who enqueues needs to create tasks (project-wide, as before) ...
resource "google_project_iam_member" "enqueuer" {
  for_each = toset(local.enqueuers)

  project = var.project_id
  role    = "roles/cloudtasks.enqueuer"
  member  = "serviceAccount:${google_service_account.sa[each.key].email}"
}

# ... and to attach discovery-tasks' OIDC token to them (actAs on discovery-tasks only).
resource "google_service_account_iam_member" "enqueuer_acts_as_tasks" {
  for_each = toset(local.enqueuers)

  service_account_id = google_service_account.sa["tasks"].name
  role               = "roles/iam.serviceAccountUser"
  member             = "serviceAccount:${google_service_account.sa[each.key].email}"
}

# The Cloud Tasks and Cloud Scheduler service agents mint the OIDC tokens. Newer projects grant this
# through their service agent roles already; these bindings are harmless if so. The agents appear when
# their API is enabled; if a first apply says one does not exist yet, apply again a minute later.
resource "google_service_account_iam_member" "tasks_agent_token_creator" {
  service_account_id = google_service_account.sa["tasks"].name
  role               = "roles/iam.serviceAccountTokenCreator"
  member             = "serviceAccount:service-${local.project_number}@gcp-sa-cloudtasks.iam.gserviceaccount.com"

  depends_on = [google_project_service.apis]
}

resource "google_service_account_iam_member" "scheduler_agent_token_creator" {
  service_account_id = google_service_account.sa["scheduler"].name
  role               = "roles/iam.serviceAccountTokenCreator"
  member             = "serviceAccount:service-${local.project_number}@gcp-sa-cloudscheduler.iam.gserviceaccount.com"

  depends_on = [google_project_service.apis]
}
