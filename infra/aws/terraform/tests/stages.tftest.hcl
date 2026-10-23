# Offline checks of both stages with a mocked AWS provider: no credentials, no AWS calls.
#   cd infra/aws/terraform && terraform init -backend=false && terraform test

mock_provider "aws" {
  mock_data "aws_caller_identity" {
    defaults = { account_id = "123456789012" }
  }
  mock_data "aws_partition" {
    defaults = { partition = "aws" }
  }
  mock_data "aws_availability_zones" {
    defaults = { names = ["us-west-2a", "us-west-2b"] }
  }
  mock_resource "aws_sqs_queue" {
    defaults = { arn = "arn:aws:sqs:us-west-2:123456789012:nua-tasks", url = "https://sqs.us-west-2.amazonaws.com/123456789012/nua-tasks" }
  }
  mock_resource "aws_ecr_repository" {
    defaults = { arn = "arn:aws:ecr:us-west-2:123456789012:repository/nua-images", repository_url = "123456789012.dkr.ecr.us-west-2.amazonaws.com/nua-images" }
  }
  mock_resource "aws_secretsmanager_secret" {
    defaults = { arn = "arn:aws:secretsmanager:us-west-2:123456789012:secret:nua/x-AbCdEf" }
  }
  mock_resource "aws_lambda_function" {
    defaults = { arn = "arn:aws:lambda:us-west-2:123456789012:function:nua-receiver" }
  }
  mock_resource "aws_lambda_function_url" {
    defaults = { function_url = "https://abc123.lambda-url.us-west-2.on.aws/" }
  }
  mock_resource "aws_cloudwatch_log_group" {
    defaults = { arn = "arn:aws:logs:us-west-2:123456789012:log-group:/nua" }
  }
  mock_resource "aws_dynamodb_table" {
    defaults = { arn = "arn:aws:dynamodb:us-west-2:123456789012:table/nua-task-names" }
  }
  mock_resource "aws_s3_bucket" {
    defaults = { arn = "arn:aws:s3:::nua-build-123456789012-us-west-2" }
  }
  mock_resource "aws_iam_role" {
    defaults = { arn = "arn:aws:iam::123456789012:role/nua" }
  }
  mock_resource "aws_ecs_task_definition" {
    defaults = { arn = "arn:aws:ecs:us-west-2:123456789012:task-definition/nua-worker:1" }
  }
  mock_resource "aws_appautoscaling_policy" {
    defaults = { arn = "arn:aws:autoscaling:us-west-2:123456789012:scalingPolicy:x" }
  }
}

variables {
  repo_slug = "owner/discovery"
}

run "stage_1" {
  command = apply # simulated: the mocked provider makes no AWS call

  assert {
    condition     = output.stage == 1 && length(aws_lambda_function.receiver) == 0 && length(aws_ecs_service.worker) == 0
    error_message = "stage 1 must not create the receiver or the worker service"
  }
  assert {
    condition     = length(aws_scheduler_schedule.job) == 0
    error_message = "stage 1 has no schedules"
  }
  assert {
    condition     = length(aws_secretsmanager_secret.secret) == 9
    error_message = "9 secrets by default"
  }
  assert {
    condition     = aws_sqs_queue.tasks.visibility_timeout_seconds > 1800 && jsondecode(aws_sqs_queue.tasks.redrive_policy).maxReceiveCount == 5
    error_message = "visibility timeout above 30 minutes and a DLQ after 5 receives"
  }
}

run "stage_2" {
  command = apply # simulated: the mocked provider makes no AWS call

  variables {
    image             = "123456789012.dkr.ecr.us-west-2.amazonaws.com/nua-images:t1"
    circleback_people = ["alex"]
  }

  assert {
    condition     = output.stage == 2 && output.receiver_url == "https://abc123.lambda-url.us-west-2.on.aws"
    error_message = "stage 2 creates the receiver; its URL has no trailing slash"
  }
  assert {
    condition     = length(aws_secretsmanager_secret.secret) == 11
    error_message = "two more secrets per Circleback person"
  }
  assert {
    condition     = alltrue([for k, s in aws_scheduler_schedule.job : s.state == "DISABLED"])
    error_message = "every schedule is disabled until schedules_on"
  }
  assert {
    condition     = strcontains(aws_lambda_function.receiver[0].environment[0].variables.NUA_AWS_SECRETS, "CIRCLEBACK_SIGNING_SECRET_ALEX=nua/circleback-signing-alex")
    error_message = "the receiver loads its secrets by name"
  }
  assert {
    condition     = !strcontains(aws_lambda_function.receiver[0].environment[0].variables.NUA_AWS_SECRETS, "gemini")
    error_message = "model keys never reach the receiver"
  }
  assert {
    condition     = aws_ecs_service.worker[0].desired_count == 0 && aws_appautoscaling_target.worker[0].max_capacity == 1
    error_message = "the worker scales between 0 and 1"
  }
}

run "go_live" {
  command = apply # simulated: the mocked provider makes no AWS call

  variables {
    image              = "123456789012.dkr.ecr.us-west-2.amazonaws.com/nua-images:t1"
    schedules_on       = true
    time_zone          = "Europe/London"
    receiver_keep_warm = true
  }

  assert {
    condition     = length(aws_scheduler_schedule.keep_warm) == 1 && strcontains(aws_iam_role_policy.scheduler.policy, "lambda:InvokeFunction")
    error_message = "receiver_keep_warm adds the 5-minute invocation and lets the scheduler role make it"
  }

  assert {
    condition     = aws_scheduler_schedule.job["sweep"].state == "ENABLED" && aws_scheduler_schedule.job["sweep-granola"].state == "ENABLED" && aws_scheduler_schedule.job["rollup"].state == "ENABLED"
    error_message = "schedules_on enables sweep, sweep-granola and rollup"
  }
  assert {
    condition     = aws_scheduler_schedule.job["rollup-bootstrap"].state == "DISABLED"
    error_message = "rollup-bootstrap is never scheduled"
  }
  assert {
    condition     = aws_scheduler_schedule.job["sweep"].schedule_expression_timezone == "Europe/London"
    error_message = "schedules follow time_zone"
  }
  assert {
    condition     = jsondecode(aws_scheduler_schedule.job["sweep-granola"].target[0].input) == { kind = "sweep", payload = { granola_only = true } }
    error_message = "sweep-granola sends the task the GCP receiver would enqueue"
  }
}
