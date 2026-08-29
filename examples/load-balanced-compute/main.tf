terraform {
  required_version = ">= 1.8.0"
  required_providers {
    cloudcore = {
      source  = "registry.terraform.io/cloudcore/cloudcore"
      version = ">= 0.1.0"
    }
  }
}

provider "cloudcore" {
  # api_url   = "https://api.cloudcore.example.com"
  # api_token = "..."
  # Or set CLOUDCORE_API_URL / CLOUDCORE_API_TOKEN env vars
}

module "vpc" {
  source = "../../modules/vpc"

  project     = "payments"
  environment = "prod"
  owner       = "platform-team"

  vpcs = {
    "main" = { cidr_block = "10.0.0.0/16" }
  }
}

module "compute" {
  source = "../../modules/compute"

  project     = "payments"
  environment = "prod"
  owner       = "platform-team"

  instances = {
    "web-01" = {
      image_id  = "ubuntu-22.04"
      flavor    = "standard.small"
      vpc_id    = module.vpc.vpc_ids_by_key["main"]
      subnet_id = "subnet-placeholder"
    }
  }
}

module "lb" {
  source = "../../modules/load-balancer"

  project     = "payments"
  environment = "prod"
  owner       = "platform-team"

  load_balancers = {
    "web" = {
      type       = "application"
      vpc_id     = module.vpc.vpc_ids_by_key["main"]
      subnet_ids = ["subnet-placeholder-a", "subnet-placeholder-b"]
    }
  }
}

output "vpc_ids"    { value = module.vpc.vpc_ids_by_key }
output "lb_dns"     { value = module.lb.lb_dns_names_by_key }
output "private_ips" { value = module.compute.private_ips_by_key }
