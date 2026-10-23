# What to point the outside world at (infra/README.md, "First-time setup", step 8). The URLs are the
# deterministic Cloud Run URLs (locals.tf), so they are known from stage 1 on.

output "project_number" {
  value = local.project_number
}

output "image_repository" {
  description = "Where infra/deploy.sh pushes images."
  value       = "${var.region}-docker.pkg.dev/${var.project_id}/${local.ar_repo}/discovery"
}

output "build_service_account" {
  description = "The account infra/deploy.sh runs Cloud Build as."
  value       = google_service_account.sa["build"].email
}

output "build_source_bucket" {
  description = "Where infra/deploy.sh uploads the source for Cloud Build."
  value       = google_storage_bucket.build_source.name
}

output "receiver_url" {
  value = local.receiver_url
}

output "processor_url" {
  value = local.processor_url
}

output "granola_webhook_url" {
  description = "url of the Granola webhook endpoint (scopes [\"workspace\"])."
  value       = "${local.receiver_url}/ingest/granola"
}

output "github_webhook_url" {
  description = "GitHub App push webhook URL."
  value       = "${local.receiver_url}/github"
}

output "slack_interactivity_url" {
  description = "Slack app Interactivity Request URL."
  value       = "${local.receiver_url}/slack/interact"
}

output "slack_events_url" {
  description = "Slack app Event Subscriptions Request URL (bot event app_mention)."
  value       = "${local.receiver_url}/slack/events"
}

output "circleback_webhook_urls" {
  description = "One Circleback automation per person (tag \"discovery\"; outcomes Transcript, Notes, Attendees)."
  value       = { for p in var.circleback_people : p => "${local.receiver_url}/ingest/circleback/${p}" }
}

output "mcp_connector_url" {
  description = "MCP connector URL (only with deploy_mcp)."
  value       = local.mcp_on ? "${local.mcp_url}/mcp" : null
}

output "mcp_oauth_callback_url" {
  description = "Callback URL for the MCP OAuth app (only with deploy_mcp; check service/mcp.py and FastMCP's provider docs)."
  value       = local.mcp_on ? "${local.mcp_url}/auth/callback" : null
}

output "stage" {
  description = "1: no image yet, so no Cloud Run services or Scheduler jobs. 2: everything."
  value       = local.services_on ? 2 : 1
}
