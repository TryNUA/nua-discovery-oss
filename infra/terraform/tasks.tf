# The Cloud Tasks queue `discovery`: one task at a time; a 5xx is retried from 60s up to 30 minutes apart,
# 5 attempts in all. dispatchDeadline (1800s) is a per-task field, set by nua/tasks.py on every task.

resource "google_cloud_tasks_queue" "nua" {
  name     = local.queue_name
  location = var.region

  rate_limits {
    max_concurrent_dispatches = 1
  }

  retry_config {
    max_attempts = 5
    min_backoff  = "60s"
    max_backoff  = "1800s"
  }

  depends_on = [google_project_service.apis]
}
