# The receiver: Lambda function `nua-receiver` from the one image, public through a Function URL.
#
# The image runs uvicorn as on Cloud Run; the Lambda Web Adapter extension (service/Dockerfile) turns each
# invocation into an HTTP request to it. Every route checks its own signature (Granola, Circleback,
# GitHub, Slack), so the URL needs no AWS auth. /sweep and /rollup refuse every call here: the schedules
# write to SQS directly (schedules.tf).
#
# Lambda freezes the environment the moment a response is sent. The receiver already does all its work
# before it answers (service/main.py; on Cloud Run it gets no CPU afterwards either), so nothing is lost.
# Only /tmp is writable: the clone lives at /tmp/nua-repo and HOME is /tmp.
#
# Slack wants an answer within 3 seconds, cold start included. A cold start here is the image start,
# uvicorn and one Secrets Manager call (about 1.5 to 3 seconds at one full vCPU, receiver_memory_mb =
# 1769). If clicks after a quiet spell fail often, set receiver_keep_warm = true: an invocation every 5
# minutes keeps an environment warm for cents a month (provisioned concurrency would guarantee it, for
# about $15 to $20 a month, and needs a published version and an alias that every deploy moves).
#
# Code deploys and Terraform do not fight: infra/aws/deploy.sh sets the image with the AWS CLI and this
# resource ignores image_uri (and the description deploy.sh --restart stamps). Everything else is
# Terraform's: change it here, not with the CLI, or the next apply reverts it.

resource "aws_cloudwatch_log_group" "receiver" {
  name              = "/aws/lambda/${local.name.receiver}"
  retention_in_days = var.log_retention_days
}

resource "aws_iam_role" "receiver" {
  name = local.name.receiver
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "lambda.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
}

resource "aws_iam_role_policy" "receiver" {
  name = "receiver"
  role = aws_iam_role.receiver.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid      = "Logs"
        Effect   = "Allow"
        Action   = ["logs:CreateLogStream", "logs:PutLogEvents"]
        Resource = "${aws_cloudwatch_log_group.receiver.arn}:*"
      },
      {
        Sid      = "Enqueue"
        Effect   = "Allow"
        Action   = "sqs:SendMessage"
        Resource = aws_sqs_queue.tasks.arn
      },
      {
        Sid      = "TaskNames"
        Effect   = "Allow"
        Action   = ["dynamodb:PutItem", "dynamodb:DeleteItem"]
        Resource = aws_dynamodb_table.task_names.arn
      },
      {
        Sid      = "WakeWorker"
        Effect   = "Allow"
        Action   = "ecs:UpdateService"
        Resource = local.worker_service_arn
      },
      {
        Sid      = "Secrets"
        Effect   = "Allow"
        Action   = "secretsmanager:GetSecretValue"
        Resource = local.secret_arns.receiver
      },
      {
        Sid      = "SecretsBatch"
        Effect   = "Allow"
        Action   = "secretsmanager:BatchGetSecretValue"
        Resource = "*" # no resource scope; each secret is still checked against GetSecretValue above
      },
    ]
  })
}

resource "aws_lambda_function" "receiver" {
  count = local.services_on ? 1 : 0

  function_name = local.name.receiver
  role          = aws_iam_role.receiver.arn
  package_type  = "Image"
  image_uri     = var.image
  architectures = [local.lambda_arch]
  memory_size   = var.receiver_memory_mb
  timeout       = 30

  reserved_concurrent_executions = var.receiver_max_concurrency

  ephemeral_storage {
    size = 2048 # MB of /tmp, for the clone
  }

  environment {
    variables = local.receiver_env
  }

  logging_config {
    log_format = "Text"
    log_group  = aws_cloudwatch_log_group.receiver.name
  }

  lifecycle {
    ignore_changes = [image_uri, description]
  }

  depends_on = [aws_iam_role_policy.receiver]
}

resource "aws_lambda_function_url" "receiver" {
  count = local.services_on ? 1 : 0

  function_name      = aws_lambda_function.receiver[0].function_name
  authorization_type = "NONE"
  invoke_mode        = "BUFFERED"
}

# A public Function URL needs both statements: InvokeFunctionUrl for the URL, and InvokeFunction limited
# to calls that come through the URL (so the function itself is not invokable by any AWS account).
resource "aws_lambda_permission" "url" {
  count = local.services_on ? 1 : 0

  statement_id           = "FunctionUrlPublic"
  action                 = "lambda:InvokeFunctionUrl"
  function_name          = aws_lambda_function.receiver[0].function_name
  principal              = "*"
  function_url_auth_type = "NONE"
}

resource "aws_lambda_permission" "url_invoke" {
  count = local.services_on ? 1 : 0

  statement_id             = "FunctionUrlInvoke"
  action                   = "lambda:InvokeFunction"
  function_name            = aws_lambda_function.receiver[0].function_name
  principal                = "*"
  invoked_via_function_url = true
}

# ---------- optional keep-warm ----------
# An empty event every 5 minutes. The Web Adapter passes non-HTTP events to POST /events, which answers
# 404 at once; the point is only that an environment stays started.

resource "aws_scheduler_schedule" "keep_warm" {
  count = local.services_on && var.receiver_keep_warm ? 1 : 0

  name                = "${var.stack}-receiver-keep-warm"
  group_name          = aws_scheduler_schedule_group.schedules.name
  schedule_expression = "rate(5 minutes)"
  description         = "Keeps one receiver environment warm (receiver_keep_warm)"

  flexible_time_window {
    mode = "OFF"
  }

  target {
    arn      = aws_lambda_function.receiver[0].arn
    role_arn = aws_iam_role.scheduler.arn
    input    = "{}"

    retry_policy {
      maximum_retry_attempts = 0
    }
  }
}
