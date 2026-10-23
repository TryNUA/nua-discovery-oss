# Terraform and provider pins, where the state lives, and the tags every resource carries.
#
# State sits in an S3 bucket that infra/aws/bootstrap.sh creates (never this configuration: the state
# cannot live in a bucket it manages). Locking uses S3's own conditional writes (`use_lockfile`,
# Terraform 1.10 and up), so there is no DynamoDB lock table. Bucket, key and region are passed at init:
#   terraform init -backend-config="bucket=nua-tfstate-<account>-<region>" -backend-config="key=nua/terraform.tfstate" -backend-config="region=<region>"
# For offline checks only: terraform init -backend=false && terraform validate

terraform {
  required_version = ">= 1.10" # S3 native state locking (use_lockfile)

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 6.0"
    }
  }

  backend "s3" {
    use_lockfile = true
    encrypt      = true
  }
}

provider "aws" {
  region = var.region

  # Every taggable resource gets these, so a teardown can be checked with the Resource Groups Tagging
  # API (infra/aws/bootstrap.sh --destroy lists whatever still carries them).
  default_tags {
    tags = {
      app   = "nua-discovery"
      stack = var.stack
    }
  }
}

data "aws_caller_identity" "current" {}
data "aws_partition" "current" {}
