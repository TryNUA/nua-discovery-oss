# Inputs. Copy terraform.tfvars.example to terraform.tfvars (gitignored) and fill it in.

variable "region" {
  description = "AWS region for everything, e.g. us-west-2."
  type        = string
  default     = "us-west-2"
}

variable "stack" {
  description = "Name prefix of every resource and value of the `stack` tag. Change it only to run a second copy in the same account."
  type        = string
  default     = "nua"

  validation {
    condition     = can(regex("^nua(-[a-z0-9]+)*$", var.stack)) && length(var.stack) <= 20
    error_message = "stack must be \"nua\" or start with \"nua-\" (lower case letters, digits and -), at most 20 characters."
  }
}

variable "repo_slug" {
  description = "The GitHub repo the pipeline reads and writes, owner/name."
  type        = string

  validation {
    condition     = can(regex("^[^/\\s]+/[^/\\s]+$", var.repo_slug))
    error_message = "repo_slug must look like owner/name."
  }
}

variable "repo_branch" {
  description = "The branch the pipeline reads and writes."
  type        = string
  default     = "main"
}

# The GitHub App needs the receiver URL, which AWS only knows once the receiver exists (stage 2), so
# these may stay empty on the first run. Until they are set, the routes that read the repo answer 503.
variable "github_app_id" {
  description = "GitHub App id (the app's settings page). Empty until the app exists."
  type        = string
  default     = ""
}

variable "github_app_installation_id" {
  description = "GitHub App installation id (the number in the installation URL). Empty until installed."
  type        = string
  default     = ""
}

variable "github_app_slug" {
  description = "GitHub App slug (its name in the URL); its pushes show as <slug>[bot]."
  type        = string
  default     = ""
}

variable "image" {
  description = <<-EOT
    Container image for the receiver, as printed by infra/aws/deploy.sh --build-only, e.g.
    <account>.dkr.ecr.<region>.amazonaws.com/nua-images:<tag>. Empty (stage 1): no Lambda function, ECS
    service or schedules are created. Once they exist this value is only used if the function is
    recreated; later images are rolled out by infra/aws/deploy.sh, and Terraform ignores the image.
  EOT
  type        = string
  default     = ""
}

variable "architecture" {
  description = "CPU architecture of the image, the Lambda function, the Fargate task and the CodeBuild host: x86_64 or arm64."
  type        = string
  default     = "x86_64"

  validation {
    condition     = contains(["x86_64", "arm64"], var.architecture)
    error_message = "architecture must be x86_64 or arm64."
  }
}

variable "circleback_people" {
  description = "Person keys from config/people.yml who record on Circleback, e.g. [\"alex\"]. Empty: no Circleback."
  type        = list(string)
  default     = []

  validation {
    condition     = alltrue([for p in var.circleback_people : can(regex("^[a-z0-9_-]+$", p))])
    error_message = "circleback_people entries are person keys from config/people.yml (lower case letters, digits, - and _)."
  }
}

variable "slack_test_mode" {
  description = "Staging: set NUA_SLACK_TEST=1 on the receiver and the worker so every Slack post goes to #discovery-test."
  type        = bool
  default     = false
}

variable "schedules_on" {
  description = "Run the schedules (sweep, sweep-granola, rollup). False disables them all; set true at go-live, once Slack, the webhooks and ingest_cutoff are in place. rollup-bootstrap stays disabled either way."
  type        = bool
  default     = false
}

variable "time_zone" {
  description = "IANA time zone the schedules run in (the 2am sweep, the Monday 8am rollup), e.g. Europe/London. Must match `timezone` in config/pipeline.yml, which sets call-id dates and ingest stamps."
  type        = string
  default     = "America/Los_Angeles"

  validation {
    condition     = can(regex("^[A-Za-z][A-Za-z0-9_+-]*(/[A-Za-z0-9_+-]+)*$", var.time_zone))
    error_message = "time_zone must be an IANA time zone name such as America/Los_Angeles, Europe/London or UTC."
  }
}

variable "receiver_memory_mb" {
  description = "Lambda memory for the receiver. CPU scales with it; 1769 MB is one full vCPU, which keeps a cold start inside Slack's 3 seconds."
  type        = number
  default     = 1769
}

variable "receiver_keep_warm" {
  description = "Invoke the receiver every 5 minutes so Slack clicks after a quiet spell rarely meet a cold start. Costs cents a month."
  type        = bool
  default     = false
}

variable "receiver_max_concurrency" {
  description = "Reserved concurrency for the receiver, which caps it (and the cost of junk traffic). null: unreserved. Needs an account concurrency quota above 100; new accounts start at 10."
  type        = number
  default     = null
}

variable "worker_cpu" {
  description = "Fargate CPU units for the worker (1024 = 1 vCPU). The work is mostly waiting on model APIs."
  type        = number
  default     = 1024
}

variable "worker_memory_mb" {
  description = "Fargate memory for the worker in MB; must be valid for worker_cpu (1 vCPU: 2048 to 8192)."
  type        = number
  default     = 2048
}

variable "worker_idle_seconds" {
  description = "The worker scales itself to zero after this long with an empty queue. Every start costs a minute or two of pulling the image, so shorter is cheaper but slower for the next task."
  type        = number
  default     = 120
}

variable "log_retention_days" {
  description = "Retention of the receiver, worker and build log groups."
  type        = number
  default     = 30
}

variable "budget_emails" {
  description = "Who gets the monthly budget alert emails. Empty: no budget. The budget covers the whole account, so a dedicated account is best."
  type        = list(string)
  default     = []
}

variable "monthly_budget_usd" {
  description = "Monthly budget in USD. Email at 50%, 90% and 100% of it, and when the month is forecast to pass it."
  type        = number
  default     = 25
}
