# The image repo and the build: ECR repo `nua-images` (keeps the 10 newest images), the source bucket,
# and CodeBuild project `nua-build`, which builds service/Dockerfile on AWS so you need no local Docker.
# infra/aws/deploy.sh zips the source, uploads it to the bucket and starts a build (like Cloud Build on GCP).

resource "aws_ecr_repository" "images" {
  name                 = local.name.images
  image_tag_mutability = "MUTABLE" # every build also moves the `live` tag the worker runs
  force_delete         = true      # a destroy removes the images too

  image_scanning_configuration {
    scan_on_push = false
  }
}

resource "aws_ecr_lifecycle_policy" "keep_10" {
  repository = aws_ecr_repository.images.name
  policy = jsonencode({
    rules = [{
      rulePriority = 1
      description  = "Keep the 10 newest images"
      selection    = { tagStatus = "any", countType = "imageCountMoreThan", countNumber = 10 }
      action       = { type = "expire" }
    }]
  })
}

# ---------- source bucket ----------

resource "aws_s3_bucket" "build" {
  bucket        = local.build_bucket
  force_destroy = true
}

resource "aws_s3_bucket_public_access_block" "build" {
  bucket                  = aws_s3_bucket.build.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_lifecycle_configuration" "build" {
  bucket = aws_s3_bucket.build.id

  rule {
    id     = "expire-sources"
    status = "Enabled"
    filter {
      prefix = "source/"
    }
    expiration {
      days = 7
    }
  }
}

# ---------- CodeBuild ----------

resource "aws_cloudwatch_log_group" "build" {
  name              = "/aws/codebuild/${local.name.build}"
  retention_in_days = var.log_retention_days
}

resource "aws_iam_role" "build" {
  name = local.name.build
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "codebuild.amazonaws.com" }
      Action    = "sts:AssumeRole"
      Condition = { StringEquals = { "aws:SourceAccount" = local.account_id } }
    }]
  })
}

resource "aws_iam_role_policy" "build" {
  name = "build"
  role = aws_iam_role.build.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid      = "Logs"
        Effect   = "Allow"
        Action   = ["logs:CreateLogStream", "logs:PutLogEvents"]
        Resource = "${aws_cloudwatch_log_group.build.arn}:*"
      },
      {
        Sid      = "Source"
        Effect   = "Allow"
        Action   = ["s3:GetObject", "s3:GetObjectVersion"]
        Resource = "${aws_s3_bucket.build.arn}/source/*"
      },
      {
        Sid      = "EcrLogin"
        Effect   = "Allow"
        Action   = "ecr:GetAuthorizationToken"
        Resource = "*" # this action has no resource scope
      },
      {
        Sid    = "EcrPush"
        Effect = "Allow"
        Action = [
          "ecr:BatchCheckLayerAvailability", "ecr:BatchGetImage", "ecr:GetDownloadUrlForLayer",
          "ecr:InitiateLayerUpload", "ecr:UploadLayerPart", "ecr:CompleteLayerUpload", "ecr:PutImage",
        ]
        Resource = aws_ecr_repository.images.arn
      },
    ]
  })
}

locals {
  # The Dockerfile's base image from ECR Public: anonymous Docker Hub pulls from CodeBuild's shared
  # hosts often hit Docker Hub's rate limit.
  python_image = "public.ecr.aws/docker/library/python:3.12-slim"

  buildspec = <<-YAML
    version: 0.2
    phases:
      build:
        on-failure: ABORT
        commands:
          - aws ecr get-login-password --region "$AWS_REGION" | docker login --username AWS --password-stdin "$REGISTRY"
          - docker build -f service/Dockerfile --build-arg PYTHON_IMAGE="$PYTHON_IMAGE" -t "$REPO_URL:$IMAGE_TAG" -t "$REPO_URL:live" .
          - docker push "$REPO_URL:$IMAGE_TAG"
          - docker push "$REPO_URL:live"
  YAML
}

resource "aws_codebuild_project" "build" {
  name          = local.name.build
  description   = "Builds service/Dockerfile for nua-discovery (infra/aws/deploy.sh)"
  service_role  = aws_iam_role.build.arn
  build_timeout = 30

  artifacts {
    type = "NO_ARTIFACTS"
  }

  environment {
    # The build host matches var.architecture, so the image matches the Lambda function and the task.
    type            = var.architecture == "arm64" ? "ARM_CONTAINER" : "LINUX_CONTAINER"
    image           = var.architecture == "arm64" ? "aws/codebuild/amazonlinux2-aarch64-standard:3.0" : "aws/codebuild/amazonlinux2-x86_64-standard:5.0"
    compute_type    = "BUILD_GENERAL1_SMALL"
    privileged_mode = true # docker build

    environment_variable {
      name  = "REGISTRY"
      value = split("/", aws_ecr_repository.images.repository_url)[0]
    }
    environment_variable {
      name  = "REPO_URL"
      value = aws_ecr_repository.images.repository_url
    }
    environment_variable {
      name  = "PYTHON_IMAGE"
      value = local.python_image
    }
    environment_variable {
      name  = "IMAGE_TAG"
      value = "manual" # deploy.sh overrides it per build
    }
  }

  # deploy.sh overrides the location per build (source/<tag>.zip).
  source {
    type      = "S3"
    location  = "${aws_s3_bucket.build.id}/source/latest.zip"
    buildspec = local.buildspec
  }

  logs_config {
    cloudwatch_logs {
      group_name = aws_cloudwatch_log_group.build.name
    }
  }

  depends_on = [aws_iam_role_policy.build]
}
