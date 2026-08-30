# 05 — Full Stack
# VPC + subnets + security groups + web instance group + ALB.
# Reference example covering all provider resources and all modules.

module "vpc" {
  source = "../../modules/vpc"

  project     = var.project
  environment = var.environment
  owner       = var.owner

  vpcs = {
    (local.vpc_key) = { cidr_block = var.cidr_block }
  }
}

module "subnets" {
  source = "../../modules/subnets"

  project        = var.project
  environment    = var.environment
  owner          = var.owner
  vpc_id         = module.vpc.vpc_ids_by_key[local.vpc_key]
  vpc_cidr_block = var.cidr_block

  subnets = {
    "web-a${local.sfx}" = { newbits = 8, netnum = 1, public = true, zone = "a" }
    "web-b${local.sfx}" = { newbits = 8, netnum = 2, public = true, zone = "b" }
  }
}

module "security_groups" {
  source = "../../modules/security-groups"

  project     = var.project
  environment = var.environment
  owner       = var.owner
  vpc_id      = module.vpc.vpc_ids_by_key[local.vpc_key]

  security_groups = {
    "web${local.sfx}" = {
      description = "Web tier — HTTP and SSH"
      ingress_rules = {
        http = { ip_protocol = "tcp", from_port = 80, to_port = 80, cidr = "0.0.0.0/0" }
        ssh  = { ip_protocol = "tcp", from_port = 22, to_port = 22, cidr = "0.0.0.0/0" }
      }
      egress_rules = {
        all = { ip_protocol = "-1", cidr = "0.0.0.0/0" }
      }
    }
  }
}

module "web" {
  source = "../../modules/instance-group"

  project     = var.project
  environment = var.environment
  owner       = var.owner

  name            = "web${local.sfx}"
  image_id        = "ubuntu-22.04"
  flavor          = var.instance_flavor
  count_instances = var.instance_count
  vpc_id          = module.vpc.vpc_ids_by_key[local.vpc_key]
  subnet_id       = module.subnets.subnet_ids_by_key["web-a${local.sfx}"]
  security_group_ids = module.security_groups.security_group_ids_list
  user_data       = local.nginx_user_data
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
      subnet_ids = values(module.subnets.public_subnet_ids)
      internal   = false
    }
  }
}
