# Inputs. Copy terraform.tfvars.example to terraform.tfvars (gitignored) and fill it in.

variable "project_id" {
  description = "GCP project id, e.g. my-discovery-project."
  type        = string
}

variable "region" {
  description = "Region for Cloud Run, Cloud Tasks, Cloud Scheduler and Artifact Registry."
  type        = string
  default     = "us-west1"
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

variable "github_app_id" {
  description = "GitHub App id (the app's settings page)."
  type        = string
}

variable "github_app_installation_id" {
  description = "GitHub App installation id (the number in the installation URL)."
  type        = string
}

variable "github_app_slug" {
  description = "GitHub App slug (its name in the URL); its pushes show as <slug>[bot]."
  type        = string
}

variable "image" {
  description = <<-EOT
    Container image for the Cloud Run services, as printed by infra/deploy.sh --build-only, e.g.
    us-west1-docker.pkg.dev/<project>/nua/nua:<tag>. Empty (stage 1): no Cloud Run services and no
    Scheduler jobs are created. Once the services exist this value is only used if one is recreated;
    later images are rolled out by infra/deploy.sh, and Terraform ignores the image (cloud_run.tf).
  EOT
  type        = string
  default     = ""
}

variable "deploy_mcp" {
  description = "Also create the MCP server: nua-mcp account, its secrets, the sign-in bucket and the nua-mcp service."
  type        = bool
  default     = false
}

variable "mcp_auth" {
  description = "MCP sign-in: github, google, or none (sign-in off, and nua-mcp stays private). Only read with deploy_mcp."
  type        = string
  default     = "github"

  validation {
    condition     = contains(["github", "google", "none"], var.mcp_auth)
    error_message = "mcp_auth must be github, google or none."
  }
}

variable "mcp_oauth_client_id" {
  description = "Client id of the OAuth app for MCP sign-in (not a secret). Required with deploy_mcp unless mcp_auth is none."
  type        = string
  default     = ""

  validation {
    condition     = !var.deploy_mcp || var.mcp_auth == "none" || trimspace(var.mcp_oauth_client_id) != ""
    error_message = "Set mcp_oauth_client_id (the OAuth app for MCP sign-in), or set mcp_auth = \"none\"."
  }
}

variable "oauth_bucket" {
  description = "Bucket for the MCP server's sign-in state. Empty: <project_id>-nua-mcp-oauth. Only with deploy_mcp."
  type        = string
  default     = ""
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
  description = "Staging: set NUA_SLACK_TEST=1 on every service so every Slack post goes to #discovery-test."
  type        = bool
  default     = false
}

variable "schedules_on" {
  description = "Run the Scheduler jobs (sweep, sweep-granola, rollup). False pauses them all; set true at go-live, once Slack, the webhooks and ingest_cutoff are in place. rollup-bootstrap stays paused either way."
  type        = bool
  default     = false
}

variable "time_zone" {
  description = "IANA time zone the Scheduler jobs run in (the 2am sweep, the Monday 8am rollup), e.g. Europe/London. Must match `timezone` in config/pipeline.yml, which sets call-id dates and ingest stamps."
  type        = string
  default     = "America/Los_Angeles"

  validation {
    condition     = can(regex("^[A-Za-z][A-Za-z0-9_+-]*(/[A-Za-z0-9_+-]+)*$", var.time_zone))
    error_message = "time_zone must be an IANA time zone name such as America/Los_Angeles, Europe/London or UTC."
  }
}

variable "billing_account" {
  description = "Billing account id (gcloud billing projects describe <project>), e.g. 0X0X0X-0X0X0X-0X0X0X. Empty: no budget alert."
  type        = string
  default     = ""
}

variable "monthly_budget_usd" {
  description = "Monthly budget for this project in USD. Billing admins get an email at 50%, 90% and 100% of it, and when the month is forecast to pass it."
  type        = number
  default     = 25
}
