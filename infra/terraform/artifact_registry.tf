# The Docker repository infra/deploy.sh pushes the image to:
#   <region>-docker.pkg.dev/<project>/nua/nua:<tag>
# and what Cloud Build needs to build it there. Builds run as nua-build (service_accounts.tf), not the
# Compute Engine default account, which newer projects leave with no permissions. nua-build may read
# the source bucket, push to this repository and write build logs, nothing else.

resource "google_artifact_registry_repository" "nua" {
  location      = var.region
  repository_id = local.ar_repo
  format        = "DOCKER"
  description   = "Nua Discovery images"

  # Every deploy adds an image. Keep the 10 newest (the running one is always among them, and older
  # ones are there to roll back to); delete the rest once they are a week old. KEEP wins over DELETE.
  cleanup_policy_dry_run = false
  cleanup_policies {
    id     = "keep-newest-10"
    action = "KEEP"
    most_recent_versions {
      keep_count = 10
    }
  }
  cleanup_policies {
    id     = "delete-older"
    action = "DELETE"
    condition {
      tag_state  = "ANY"
      older_than = "604800s"
    }
  }

  depends_on = [google_project_service.apis]
}

resource "google_artifact_registry_repository_iam_member" "build_writer" {
  location   = google_artifact_registry_repository.nua.location
  repository = google_artifact_registry_repository.nua.name
  role       = "roles/artifactregistry.writer"
  member     = "serviceAccount:${google_service_account.sa["build"].email}"
}

# gcloud builds submit uploads the source tarball here (deploy.sh --gcs-source-staging-dir). Tarballs
# are only needed while a build runs, so they are deleted after 7 days.
resource "google_storage_bucket" "build_source" {
  name                        = local.build_bucket
  location                    = var.region
  uniform_bucket_level_access = true
  public_access_prevention    = "enforced"
  force_destroy               = true

  lifecycle_rule {
    condition {
      age = 7
    }
    action {
      type = "Delete"
    }
  }

  depends_on = [google_project_service.apis]
}

resource "google_storage_bucket_iam_member" "build_source_reader" {
  bucket = google_storage_bucket.build_source.name
  role   = "roles/storage.objectViewer"
  member = "serviceAccount:${google_service_account.sa["build"].email}"
}

# Build logs go to Cloud Logging only (deploy.sh's build config), which needs this.
resource "google_project_iam_member" "build_log_writer" {
  project = var.project_id
  role    = "roles/logging.logWriter"
  member  = "serviceAccount:${google_service_account.sa["build"].email}"
}
