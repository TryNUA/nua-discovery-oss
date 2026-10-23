# The Google APIs the pipeline uses. ABANDON: removing one from this list, or a terraform destroy,
# leaves the API enabled rather than switching it off under anything else in the project.

locals {
  apis = toset([
    "run.googleapis.com",
    "cloudtasks.googleapis.com",
    "cloudscheduler.googleapis.com",
    "secretmanager.googleapis.com",
    "artifactregistry.googleapis.com",
    "cloudbuild.googleapis.com",
    "iam.googleapis.com",
    "iamcredentials.googleapis.com",
    "storage.googleapis.com",
    "logging.googleapis.com",
    "billingbudgets.googleapis.com",
  ])
}

resource "google_project_service" "apis" {
  for_each = local.apis

  service         = each.value
  deletion_policy = "ABANDON"
}
