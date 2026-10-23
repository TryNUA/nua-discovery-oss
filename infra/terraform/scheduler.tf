# Cloud Scheduler jobs, all in var.time_zone, all POSTing to the receiver with an OIDC token
# signed as nua-scheduler and addressed to the receiver's URL (the receiver checks both). Created only
# with the services (stage 2).
#
# Terraform owns whether a job runs: `schedules_on` (variables.tf) runs every job with `scheduled = true`
# and pauses them all when false, the default, so jobs created in stage 2 wait until go-live is done
# (infra/README.md). Do not pause or resume with gcloud: the next apply puts it back.
# rollup-bootstrap is always paused; run it by hand after a backfill:
#   gcloud scheduler jobs run rollup-bootstrap --location=<region>
# (if gcloud refuses because the job is paused: resume it, run it, pause it again)

locals {
  scheduler_jobs = {
    sweep            = { scheduled = true, schedule = "0 2 * * *", path = "/sweep", body = "{}", description = "Nightly safety-net sweep, 2am ${local.time_zone}" }
    sweep-granola    = { scheduled = true, schedule = "30 * * * *", path = "/sweep", body = "{\"granola_only\": true}", description = "Hourly Granola-only sweep at :30, for notes a webhook missed" }
    rollup           = { scheduled = true, schedule = "0 8 * * 1", path = "/rollup", body = "{}", description = "Weekly roll-up, Mondays 8am ${local.time_zone}" }
    rollup-bootstrap = { scheduled = false, schedule = "0 0 1 1 *", path = "/rollup", body = "{\"bootstrap\": true}", description = "Bootstrap roll-up; run by hand after a backfill, never scheduled" }
  }
}

resource "google_cloud_scheduler_job" "job" {
  for_each = local.services_on ? local.scheduler_jobs : {}

  name             = each.key
  region           = var.region
  schedule         = each.value.schedule
  time_zone        = local.time_zone
  description      = each.value.description
  attempt_deadline = "60s"
  paused           = !(var.schedules_on && each.value.scheduled)

  http_target {
    uri         = "${local.receiver_url}${each.value.path}"
    http_method = "POST"
    body        = base64encode(each.value.body)
    headers     = { "Content-Type" = "application/json" }

    oidc_token {
      service_account_email = google_service_account.sa["scheduler"].email
      audience              = local.receiver_url
    }
  }

  depends_on = [
    google_cloud_run_v2_service.receiver,
    google_service_account_iam_member.scheduler_agent_token_creator,
  ]
}
