# The two Cloud Run services, both from the one image (service/Dockerfile; SERVICE_ROLE picks the app).
# Created only once `image` is set (stage 2, see locals.tf).
#
# Code deploys and Terraform do not fight: infra/deploy.sh rolls a new image out with gcloud, and every
# service here ignores the fields such a deploy changes (the image, the client gcloud records, the
# revision name and template labels/annotations gcloud sets). So `terraform plan` after a deploy shows
# no change, and a later apply never rolls a service back to the image in terraform.tfvars. Everything
# else (env vars, secrets, scaling, CPU, memory, timeouts, accounts) is Terraform's: change it here,
# not with gcloud, or the next apply reverts it.
#
# Secret env vars always read the latest version. A running instance keeps the value it started with;
# after adding a new version, start a new revision (infra/README.md, "Rotate a secret").

# ---------- processor: private, one task at a time, 30 minute timeout ----------

resource "google_cloud_run_v2_service" "processor" {
  count = local.services_on ? 1 : 0

  name                = local.processor_name
  location            = var.region
  ingress             = "INGRESS_TRAFFIC_ALL"
  deletion_protection = false # terraform destroy, or removing it here, deletes it

  template {
    service_account                  = google_service_account.sa["processor"].email
    timeout                          = "1800s"
    max_instance_request_concurrency = 1

    scaling {
      min_instance_count = 0
      max_instance_count = 1
    }

    containers {
      image = var.image

      resources {
        limits   = { cpu = "2", memory = "2Gi" }
        cpu_idle = true # CPU only while a request is open (the gcloud default the old script relied on)
      }

      dynamic "env" {
        for_each = local.plain_env["processor"]
        content {
          name  = env.key
          value = env.value
        }
      }

      dynamic "env" {
        for_each = local.secret_env["processor"]
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

  depends_on = [google_secret_manager_secret_iam_member.reader]
}

# Only Cloud Tasks (as nua-tasks) may call the processor.
resource "google_cloud_run_v2_service_iam_member" "processor_invoker_tasks" {
  count = local.services_on ? 1 : 0

  name     = google_cloud_run_v2_service.processor[0].name
  location = var.region
  role     = "roles/run.invoker"
  member   = "serviceAccount:${google_service_account.sa["tasks"].email}"
}

# ---------- receiver: public, scales to zero, CPU only during requests, startup CPU boost ----------
#
# Slack wants an interaction acknowledged within 3 seconds, cold start included. The receiver keeps
# that path short: it never clones the repo for a click (only the routes that read repo files, recorder
# ingest and /github, clone on demand), imports nothing heavy on the way, and does all its work before
# it answers, since a throttled instance gets no CPU after the response. startup_cpu_boost speeds up
# the cold start itself. A click after a long quiet spell can still miss the 3 seconds now and then
# (Slack shows an error and the person clicks again); that is accepted. If clicks fail in practice,
# keep one instance warm: set min_instance_count = 1 below and apply (infra/README.md, "Day 2").

resource "google_cloud_run_v2_service" "receiver" {
  count = local.services_on ? 1 : 0

  name                = local.receiver_name
  location            = var.region
  ingress             = "INGRESS_TRAFFIC_ALL"
  deletion_protection = false # terraform destroy, or removing it here, deletes it

  # Public: Granola, GitHub, Slack and Scheduler call it, and every route checks its own signature or
  # token. Cloud Run skips the invoker IAM check instead of granting run.invoker to allUsers, which the
  # organization's domain restricted sharing policy refuses (it allows only acme.dev members).
  invoker_iam_disabled = true

  template {
    service_account                  = google_service_account.sa["receiver"].email
    timeout                          = "30s"
    max_instance_request_concurrency = 40

    scaling {
      min_instance_count = 0
      max_instance_count = 1 # one instance takes 40 requests at once; more would only serve junk traffic
    }

    containers {
      image = var.image

      resources {
        limits            = { cpu = "1", memory = "1Gi" }
        cpu_idle          = true # CPU only while a request is open (--cpu-throttling)
        startup_cpu_boost = true # --cpu-boost
      }

      dynamic "env" {
        for_each = local.plain_env["receiver"]
        content {
          name  = env.key
          value = env.value
        }
      }

      dynamic "env" {
        for_each = local.secret_env["receiver"]
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

  depends_on = [google_secret_manager_secret_iam_member.reader]
}
