# The worker: ECS Fargate service `nua-worker` in cluster `nua-cluster`, 0 or 1 task, running
# `python -m service.worker` from the one image. It takes the processor's place: tasks can run 30
# minutes, past Lambda's 15, so it pulls them from SQS instead of being called (service/worker.py).
#
# Scale to zero, three ways:
# - every enqueue sets the desired count to 1 (nua/tasks.py SqsQueue, as the receiver and the worker),
#   so a call or a Slack question starts the worker at once;
# - the worker sets it back to 0 and exits after worker_idle_seconds with an empty queue;
# - two alarms on the queue depth back both up: waiting messages (visible + delayed) for a minute set 1
#   (this is also what starts the worker for the schedules, which write to SQS directly), and nothing at
#   all (visible + in flight + delayed) for 30 minutes sets 0 (a worker that cannot start or cannot scale
#   itself in). In-flight messages are left out of the first: a running worker already has them, and
#   counting them would restart the worker for one more idle spell after every task (SQS metrics lag).
# A start takes a minute or two: Fargate places the task and pulls the image before the first message.
#
# Terraform owns everything here except the desired count (ignored, it moves all day) and the image:
# the task definition runs the `live` tag, and infra/aws/deploy.sh starts a new deployment after each
# build so the service resolves it again.

resource "aws_cloudwatch_log_group" "worker" {
  name              = "/ecs/${local.name.worker}"
  retention_in_days = var.log_retention_days
}

resource "aws_ecs_cluster" "main" {
  name = local.name.cluster

  setting {
    name  = "containerInsights"
    value = "disabled"
  }
}

# ---------- roles: execution (pull the image, read the secrets, write logs) and task (the app) ----------

resource "aws_iam_role" "worker_execution" {
  name = "${local.name.worker}-execution"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "ecs-tasks.amazonaws.com" }
      Action    = "sts:AssumeRole"
      Condition = { StringEquals = { "aws:SourceAccount" = local.account_id } }
    }]
  })
}

resource "aws_iam_role_policy" "worker_execution" {
  name = "execution"
  role = aws_iam_role.worker_execution.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid      = "EcrLogin"
        Effect   = "Allow"
        Action   = "ecr:GetAuthorizationToken"
        Resource = "*" # this action has no resource scope
      },
      {
        Sid      = "EcrPull"
        Effect   = "Allow"
        Action   = ["ecr:BatchCheckLayerAvailability", "ecr:BatchGetImage", "ecr:GetDownloadUrlForLayer"]
        Resource = aws_ecr_repository.images.arn
      },
      {
        Sid      = "Logs"
        Effect   = "Allow"
        Action   = ["logs:CreateLogStream", "logs:PutLogEvents"]
        Resource = "${aws_cloudwatch_log_group.worker.arn}:*"
      },
      {
        Sid      = "Secrets"
        Effect   = "Allow"
        Action   = "secretsmanager:GetSecretValue"
        Resource = local.secret_arns.worker
      },
    ]
  })
}

resource "aws_iam_role" "worker_task" {
  name = local.name.worker
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "ecs-tasks.amazonaws.com" }
      Action    = "sts:AssumeRole"
      Condition = { StringEquals = { "aws:SourceAccount" = local.account_id } }
    }]
  })
}

resource "aws_iam_role_policy" "worker_task" {
  name = "worker"
  role = aws_iam_role.worker_task.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "Queue"
        Effect = "Allow"
        Action = [
          "sqs:ReceiveMessage", "sqs:DeleteMessage", "sqs:ChangeMessageVisibility",
          "sqs:GetQueueAttributes", "sqs:SendMessage",
        ]
        Resource = aws_sqs_queue.tasks.arn
      },
      {
        Sid      = "TaskNames"
        Effect   = "Allow"
        Action   = ["dynamodb:PutItem", "dynamodb:DeleteItem"]
        Resource = aws_dynamodb_table.task_names.arn
      },
      {
        Sid      = "ScaleItself"
        Effect   = "Allow"
        Action   = "ecs:UpdateService"
        Resource = local.worker_service_arn
      },
    ]
  })
}

# ---------- task definition and service ----------

resource "aws_ecs_task_definition" "worker" {
  count = local.services_on ? 1 : 0

  family                   = local.name.worker
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = var.worker_cpu
  memory                   = var.worker_memory_mb
  execution_role_arn       = aws_iam_role.worker_execution.arn
  task_role_arn            = aws_iam_role.worker_task.arn

  runtime_platform {
    operating_system_family = "LINUX"
    cpu_architecture        = local.ecs_arch
  }

  container_definitions = jsonencode([{
    name        = "worker"
    image       = local.worker_image
    essential   = true
    command     = ["python", "-m", "service.worker"]
    stopTimeout = 120 # Fargate's maximum; a task still running then is redelivered later
    environment = [for k, v in local.worker_env : { name = k, value = v }]
    secrets     = [for k, s in local.secret_env.worker : { name = k, valueFrom = aws_secretsmanager_secret.secret[s].arn }]
    logConfiguration = {
      logDriver = "awslogs"
      options = {
        awslogs-group         = aws_cloudwatch_log_group.worker.name
        awslogs-region        = var.region
        awslogs-stream-prefix = "worker"
      }
    }
  }])

  depends_on = [aws_iam_role_policy.worker_execution]
}

resource "aws_ecs_service" "worker" {
  count = local.services_on ? 1 : 0

  name            = local.name.worker
  cluster         = aws_ecs_cluster.main.id
  task_definition = aws_ecs_task_definition.worker[0].arn
  launch_type     = "FARGATE"
  desired_count   = 0

  # Never two workers: a deploy stops the old task before it starts the new one.
  deployment_minimum_healthy_percent = 0
  deployment_maximum_percent         = 100

  enable_ecs_managed_tags = true
  propagate_tags          = "SERVICE" # tasks carry the app and stack tags too

  network_configuration {
    subnets          = aws_subnet.public[*].id
    security_groups  = [aws_security_group.worker.id]
    assign_public_ip = true # outbound internet without a NAT gateway (network.tf)
  }

  lifecycle {
    ignore_changes = [desired_count]
  }

  depends_on = [aws_iam_role_policy.worker_task]
}

# ---------- queue-depth backstop ----------

resource "aws_appautoscaling_target" "worker" {
  count = local.services_on ? 1 : 0

  service_namespace  = "ecs"
  resource_id        = "service/${aws_ecs_cluster.main.name}/${aws_ecs_service.worker[0].name}"
  scalable_dimension = "ecs:service:DesiredCount"
  min_capacity       = 0
  max_capacity       = 1
}

resource "aws_appautoscaling_policy" "worker" {
  for_each = local.services_on ? { wake = 1, sleep = 0 } : {}

  name               = "${local.name.worker}-${each.key}"
  service_namespace  = aws_appautoscaling_target.worker[0].service_namespace
  resource_id        = aws_appautoscaling_target.worker[0].resource_id
  scalable_dimension = aws_appautoscaling_target.worker[0].scalable_dimension
  policy_type        = "StepScaling"

  step_scaling_policy_configuration {
    adjustment_type         = "ExactCapacity"
    cooldown                = 60
    metric_aggregation_type = "Maximum"

    step_adjustment {
      scaling_adjustment          = each.value
      metric_interval_lower_bound = each.key == "wake" ? 0 : null
      metric_interval_upper_bound = each.key == "sleep" ? 0 : null
    }
  }
}

locals {
  depth_alarms = {
    wake  = { comparison = "GreaterThanOrEqualToThreshold", periods = 1, missing = "notBreaching", expression = "visible + delayed", what = "has waiting work: start the worker" }
    sleep = { comparison = "LessThanThreshold", periods = 30, missing = "breaching", expression = "visible + inflight + delayed", what = "empty for 30 minutes: stop the worker" }
  }
}

resource "aws_cloudwatch_metric_alarm" "worker" {
  for_each = local.services_on ? local.depth_alarms : {}

  alarm_name          = "${local.name.worker}-${each.key}"
  alarm_description   = "${local.name.queue} ${each.value.what}"
  comparison_operator = each.value.comparison
  threshold           = 1
  evaluation_periods  = each.value.periods
  # SQS stops sending metrics for a queue idle six hours; no data then means an empty queue.
  treat_missing_data = each.value.missing
  alarm_actions      = [aws_appautoscaling_policy.worker[each.key].arn]

  metric_query {
    id          = "depth"
    expression  = each.value.expression
    label       = "Messages on ${local.name.queue}"
    return_data = true
  }

  dynamic "metric_query" {
    for_each = {
      for id, metric in {
        visible  = "ApproximateNumberOfMessagesVisible"
        inflight = "ApproximateNumberOfMessagesNotVisible"
        delayed  = "ApproximateNumberOfMessagesDelayed"
      } : id => metric if strcontains(each.value.expression, id)
    }
    content {
      id          = metric_query.key
      return_data = false
      metric {
        namespace   = "AWS/SQS"
        metric_name = metric_query.value
        dimensions  = { QueueName = aws_sqs_queue.tasks.name }
        period      = 60
        stat        = "Maximum"
      }
    }
  }
}
