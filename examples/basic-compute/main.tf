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

module "compute" {
  source = "../../modules/compute"

  project     = "example"
  environment = "dev"
  owner       = "platform-team"

  instances = {
    "web-01" = {
      image_id  = "ubuntu-22.04"
      flavor    = "standard.small"
      vpc_id    = module.vpc.vpc_ids_by_key["main"]
      subnet_id = "subnet-01"
    }
  }
}

output "vpc_ids"     { value = module.vpc.vpc_ids_by_key }
output "private_ips" { value = module.compute.private_ips_by_key }
