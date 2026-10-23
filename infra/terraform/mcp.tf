# The MCP server, only with deploy_mcp = true (off by default; the code stays in the repo). Its account,
# grants and two secrets are in service_accounts.tf and secrets.tf, keyed off the same switch.
#
# Sign-in is enforced by the app (Google or GitHub OAuth, the people in config/people.yml only). Its sign-in state
# (OAuth client registrations, pending sign-ins and token mappings) must survive scale to zero and
# deploys, or every teammate's connector must sign in and register again. So it lives in a bucket
# mounted at /oauth as FASTMCP_HOME, owned by the image's user (uid 10001, service/Dockerfile). Only
# discovery-mcp can read it; nothing in it is public. One instance at most, so two never write it at once.
#
# setup.sh had to deploy discovery-mcp private with sign-in off first, because MCP_BASE_URL was known only
# afterwards. The URL is deterministic here (locals.tf), so the first revision has sign-in on, and the
# public binding is created only after the service. With mcp_auth = "none" the service stays private.

resource "google_storage_bucket" "mcp_oauth" {
  count = local.mcp_on ? 1 : 0

  name                        = local.oauth_bucket
  location                    = upper(var.region)
  uniform_bucket_level_access = true
  public_access_prevention    = "enforced"
  deletion_policy             = "ABANDON" # turning deploy_mcp off keeps everyone's sign-ins

  depends_on = [google_project_service.apis]
}

resource "google_storage_bucket_iam_member" "mcp_oauth" {
  count = local.mcp_on ? 1 : 0

  bucket = google_storage_bucket.mcp_oauth[0].name
  role   = "roles/storage.objectAdmin"
  member = "serviceAccount:${google_service_account.sa["mcp"].email}"
}

resource "google_cloud_run_v2_service" "mcp" {
  count = local.mcp_on && local.services_on ? 1 : 0

  name                = local.mcp_name
  location            = var.region
  ingress             = "INGRESS_TRAFFIC_ALL"
  deletion_protection = false # so deploy_mcp = false can remove it

  # Public only while sign-in is on. Like the receiver (cloud_run.tf), Cloud Run skips the invoker IAM
  # check instead of granting run.invoker to allUsers, which domain restricted sharing refuses. The
  # switch and the MCP_AUTH env change in the same service update, so a revision with sign-in off is
  # never public.
  invoker_iam_disabled = var.mcp_auth != "none"

  template {
    service_account                  = google_service_account.sa["mcp"].email
    timeout                          = "300s" # for ask
    max_instance_request_concurrency = 20
    execution_environment            = "EXECUTION_ENVIRONMENT_GEN2" # Cloud Storage volumes need gen2

    scaling {
      min_instance_count = 0
      max_instance_count = 1
    }

    volumes {
      name = "oauth"
      gcs {
        bucket        = google_storage_bucket.mcp_oauth[0].name
        read_only     = false
        mount_options = ["uid=10001", "gid=10001"]
      }
    }

    containers {
      image = var.image

      resources {
        limits   = { cpu = "1", memory = "1Gi" }
        cpu_idle = true
      }

      volume_mounts {
        name       = "oauth"
        mount_path = local.oauth_mount
      }

      dynamic "env" {
        for_each = local.plain_env["mcp"]
        content {
          name  = env.key
          value = env.value
        }
      }

      dynamic "env" {
        for_each = local.secret_env["mcp"]
        content {
          name = env.key
          value_source {
            secret_key_ref {
              secret  = google_secret_manager_secret.secret[env.value].secret_id
              version = "latest"
            }
          }
        }
      }
    }
  }

  lifecycle {
    ignore_changes = [
      client,
      client_version,
      template[0].revision,
      template[0].labels,
      template[0].annotations,
      template[0].containers[0].image,
    ]
  }

  depends_on = [
    google_secret_manager_secret_iam_member.reader,
    google_storage_bucket_iam_member.mcp_oauth,
  ]
}
