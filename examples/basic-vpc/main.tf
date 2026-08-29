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

module "vpc" {
  source = "../../modules/vpc"

  project     = "example"
  environment = "dev"
  owner       = "platform-team"

  vpcs = {
    "main" = { cidr_block = "10.0.0.0/16" }
  }
}

output "vpc_ids" { value = module.vpc.vpc_ids_by_key }
