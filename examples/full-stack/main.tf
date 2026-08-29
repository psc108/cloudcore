# 05 — Full Stack
# VPC + three web instances + ALB.
# Reference example covering all provider resources.

module "vpc" {
  source = "../../modules/vpc"

  project     = var.project
  environment = var.environment
  owner       = var.owner

  vpcs = {
    (local.vpc_key) = { cidr_block = var.cidr_block }
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
      flavor    = var.instance_flavor
      vpc_id    = module.vpc.vpc_ids_by_key[local.vpc_key]
      subnet_id = local.subnet_id
      user_data = local.nginx_user_data["web-01"]
    }
    "web-02${local.sfx}" = {
      image_id  = "ubuntu-22.04"
      flavor    = var.instance_flavor
      vpc_id    = module.vpc.vpc_ids_by_key[local.vpc_key]
      subnet_id = local.subnet_id
      user_data = local.nginx_user_data["web-02"]
    }
    "web-03${local.sfx}" = {
      image_id  = "ubuntu-22.04"
      flavor    = var.instance_flavor
      vpc_id    = module.vpc.vpc_ids_by_key[local.vpc_key]
      subnet_id = local.subnet_id
      user_data = local.nginx_user_data["web-03"]
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
      vpc_id     = module.vpc.vpc_ids_by_key[local.vpc_key]
      subnet_ids = [local.subnet_id]
      internal   = false
    }
  }
}
