# Terraform and provider pins, and where the state lives.
#
# State sits in a GCS bucket created once by hand (infra/README.md, "First-time setup"), never by this
# configuration. The bucket and prefix are passed at init time so nothing project-specific is committed:
#   terraform init -backend-config="bucket=<project>-tfstate" -backend-config="prefix=discovery"
# For offline checks only: terraform init -backend=false && terraform validate

terraform {
  required_version = ">= 1.9" # cross-variable validation (variables.tf)

  required_providers {
    google = {
      source  = "hashicorp/google"
      version = "~> 8.5"
    }
  }

  backend "gcs" {}
}

provider "google" {
  project = var.project_id
  region  = var.region

  # Billing Budgets (budget.tf) needs a quota project for calls made with user credentials.
  billing_project       = var.project_id
  user_project_override = true
}
