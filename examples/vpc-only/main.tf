# 01 — VPC Only
# Simplest possible example — create a single VPC.
# Run: tofu init && tofu apply

terraform {
  required_version = ">= 1.8.0"
  required_providers {
    cloudcore = {
      source  = "registry.terraform.io/cloudcore/cloudcore"
      version = ">= 0.1.0"
    }
  }
}

provider "cloudcore" {}

variable "project"     { default = "example" }
variable "environment" { default = "dev" }
variable "owner"       { default = "platform-team" }
variable "suffix"      { default = "" }

locals {
  sfx = var.suffix != "" ? "-${var.suffix}" : ""
}

module "vpc" {
  source = "../../modules/vpc"

  project     = var.project
  environment = var.environment
  owner       = var.owner

  vpcs = {
    "main${local.sfx}" = { cidr_block = "10.10.0.0/16" }
  }
}

output "vpc_ids" { value = module.vpc.vpc_ids_by_key }
