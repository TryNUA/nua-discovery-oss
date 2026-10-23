# EventBridge Scheduler: the GCP Scheduler jobs (infra/terraform/scheduler.tf), same names, times and
# time zone (var.time_zone), in schedule group `nua-schedules`. Created only with the services (stage 2).
#
# On GCP each job POSTs to the receiver's /sweep or /rollup with an OIDC token, and the receiver enqueues
# the task. Here each schedule sends that same task straight to SQS as `nua-scheduler`, so no token and no
# receiver call is needed (locals.tf has the bodies). The queue-depth alarm starts the worker (worker.tf).
#
# Terraform owns whether a schedule runs: `schedules_on` enables every one with `scheduled = true` and
# disables them all when false, the default, so schedules created in stage 2 wait until go-live. Do not
# enable or disable them with the CLI: the next apply puts it back.
# rollup-bootstrap is always disabled, and EventBridge has no "run now"; after a backfill, run
#   bash infra/aws/enqueue.sh rollup '{"bootstrap": true}'

resource "aws_scheduler_schedule_group" "schedules" {
  name = local.name.schedules
}

resource "aws_iam_role" "scheduler" {
  name = "${var.stack}-scheduler"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "scheduler.amazonaws.com" }
      Action    = "sts:AssumeRole"
      Condition = { StringEquals = { "aws:SourceAccount" = local.account_id } }
    }]
  })
}

resource "aws_iam_role_policy" "scheduler" {
  name = "scheduler"
  role = aws_iam_role.scheduler.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = concat(
      [{
        Sid      = "Enqueue"
        Effect   = "Allow"
        Action   = "sqs:SendMessage"
        Resource = aws_sqs_queue.tasks.arn
      }],
      local.services_on && var.receiver_keep_warm ? [{
        Sid      = "KeepWarm"
        Effect   = "Allow"
        Action   = "lambda:InvokeFunction"
        Resource = aws_lambda_function.receiver[0].arn
      }] : [],
    )
  })
}

resource "aws_scheduler_schedule" "job" {
  for_each = local.services_on ? local.schedules : {}

  name                         = "${var.stack}-${each.key}"
  group_name                   = aws_scheduler_schedule_group.schedules.name
  description                  = each.value.description
  schedule_expression          = each.value.cron
  schedule_expression_timezone = var.time_zone
  state                        = var.schedules_on && each.value.scheduled ? "ENABLED" : "DISABLED"

  flexible_time_window {
    mode = "OFF"
  }

  target {
    arn      = aws_sqs_queue.tasks.arn
    role_arn = aws_iam_role.scheduler.arn
    # The message nua/tasks.py encode_message writes; no name, like the receiver's /sweep and /rollup.
    input = jsonencode({ kind = each.value.kind, payload = each.value.payload })

    retry_policy {
      maximum_retry_attempts       = 3
      maximum_event_age_in_seconds = 3600
    }
  }

  depends_on = [aws_iam_role_policy.scheduler]
}
