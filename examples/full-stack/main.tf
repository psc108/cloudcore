# 05 — Full Stack
# VPC + three web instances + ALB.
# Reference "everything the provider supports" example.

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

module "compute" {
  source = "../../modules/compute"

  project     = var.project
  environment = var.environment
  owner       = var.owner

  instances = {
    "web-01${local.sfx}" = {
      image_id  = "ubuntu-22.04"
      flavor    = "standard.small"
      vpc_id    = module.vpc.vpc_ids_by_key["main${local.sfx}"]
      subnet_id = "subnet-local-01"
      user_data = "#cloud-config\npackages: [nginx]\nruncmd:\n  - systemctl enable --now nginx\n  - echo web-01 > /var/www/html/index.html\n"
    }
    "web-02${local.sfx}" = {
      image_id  = "ubuntu-22.04"
      flavor    = "standard.small"
      vpc_id    = module.vpc.vpc_ids_by_key["main${local.sfx}"]
      subnet_id = "subnet-local-01"
      user_data = "#cloud-config\npackages: [nginx]\nruncmd:\n  - systemctl enable --now nginx\n  - echo web-02 > /var/www/html/index.html\n"
    }
    "web-03${local.sfx}" = {
      image_id  = "ubuntu-22.04"
      flavor    = "standard.small"
      vpc_id    = module.vpc.vpc_ids_by_key["main${local.sfx}"]
      subnet_id = "subnet-local-01"
      user_data = "#cloud-config\npackages: [nginx]\nruncmd:\n  - systemctl enable --now nginx\n  - echo web-03 > /var/www/html/index.html\n"
    }
  }
}

module "lb" {
  source = "../../modules/load-balancer"

  project     = var.project
  environment = var.environment
  owner       = var.owner

  load_balancers = {
    "alb${local.sfx}" = {
      type       = "application"
      vpc_id     = module.vpc.vpc_ids_by_key["main${local.sfx}"]
      subnet_ids = ["subnet-local-01"]
      internal   = false
    }
  }
}

output "vpc_ids"     { value = module.vpc.vpc_ids_by_key }
output "private_ips" { value = module.compute.private_ips_by_key }
output "lb_dns"      { value = module.lb.lb_dns_names_by_key }
