# The task queue: SQS standard queue `nua-tasks` with a dead-letter queue, and the DynamoDB table that
# gives task names their Cloud Tasks meaning (nua/tasks.py SqsQueue).
#
# Cloud Tasks queue (infra/terraform/tasks.tf)     here
# one task at a time                               one worker task, one message at a time (service/worker.py)
# 5 attempts                                       the worker's fifth attempt always ends the task; the DLQ
#                                                  (maxReceiveCount 5) catches messages whose worker died
# backoff 60s to 1800s                             the worker sets the visibility timeout to the same backoff
# dispatch deadline 1800s                          visibility timeout 2100s, above the 25-minute budget plus
#                                                  the clone; the runner stops the task at its budget
# task names (dedupe about an hour)                conditional put in nua-task-names, held an hour (TTL)
# schedule_time                                    DelaySeconds (15 minutes at most; longer rides in not_before)
#
# Standard, not FIFO: FIFO queues have no per-message delay, and the names table already dedupes.

resource "aws_sqs_queue" "dlq" {
  name                      = local.name.dlq
  message_retention_seconds = 1209600 # 14 days, the most SQS keeps
  sqs_managed_sse_enabled   = true
}

resource "aws_sqs_queue" "tasks" {
  name                       = local.name.queue
  visibility_timeout_seconds = 2100   # 35 minutes: above the 30-minute task limit
  message_retention_seconds  = 345600 # 4 days
  receive_wait_time_seconds  = 20
  sqs_managed_sse_enabled    = true

  redrive_policy = jsonencode({
    deadLetterTargetArn = aws_sqs_queue.dlq.arn
    maxReceiveCount     = 5
  })
}

resource "aws_dynamodb_table" "task_names" {
  name         = local.name.names_table
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "id"

  attribute {
    name = "id"
    type = "S"
  }

  # DynamoDB deletes expired names within a day or two; nua/tasks.py treats an expired one as free at once.
  ttl {
    attribute_name = "expires_at"
    enabled        = true
  }
}
