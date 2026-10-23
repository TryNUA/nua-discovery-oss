# What to point the outside world at (docs/self-hosting-aws.md). Lambda Function URLs are not
# deterministic, so the receiver URLs exist only from stage 2 on (null before).

locals {
  receiver_url = local.services_on ? trimsuffix(aws_lambda_function_url.receiver[0].function_url, "/") : null
}

output "stage" {
  description = "1: no image yet, so no receiver, worker service or schedules. 2: everything."
  value       = local.services_on ? 2 : 1
}

output "account_id" {
  value = local.account_id
}

output "receiver_url" {
  value = local.receiver_url
}

output "granola_webhook_url" {
  description = "url of the Granola webhook endpoint (scopes [\"workspace\"])."
  value       = local.services_on ? "${local.receiver_url}/ingest/granola" : null
}

output "github_webhook_url" {
  description = "GitHub App push webhook URL."
  value       = local.services_on ? "${local.receiver_url}/github" : null
}

output "slack_interactivity_url" {
  description = "Slack app Interactivity Request URL."
  value       = local.services_on ? "${local.receiver_url}/slack/interact" : null
}

output "slack_events_url" {
  description = "Slack app Event Subscriptions Request URL (bot event app_mention)."
  value       = local.services_on ? "${local.receiver_url}/slack/events" : null
}

output "circleback_webhook_urls" {
  description = "One Circleback automation per person (tag \"discovery\"; outcomes Transcript, Notes, Attendees)."
  value       = local.services_on ? { for p in var.circleback_people : p => "${local.receiver_url}/ingest/circleback/${p}" } : {}
}

output "image_repository" {
  description = "Where infra/aws/deploy.sh pushes images."
  value       = aws_ecr_repository.images.repository_url
}

output "build_project" {
  value = aws_codebuild_project.build.name
}

output "build_bucket" {
  description = "Where infra/aws/deploy.sh uploads the source for CodeBuild."
  value       = aws_s3_bucket.build.id
}

output "queue_url" {
  value = aws_sqs_queue.tasks.url
}

output "dead_letter_queue_url" {
  value = aws_sqs_queue.dlq.url
}

output "worker_cluster" {
  value = aws_ecs_cluster.main.name
}

output "worker_service" {
  value = local.name.worker
}

output "log_groups" {
  value = {
    receiver = aws_cloudwatch_log_group.receiver.name
    worker   = aws_cloudwatch_log_group.worker.name
    build    = aws_cloudwatch_log_group.build.name
  }
}
