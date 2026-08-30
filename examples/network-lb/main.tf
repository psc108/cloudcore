# 04 — Network Load Balancer (L4 NLB)
# VPC + subnets + security groups + instance group + internal L4 NLB.
# Use for non-HTTP workloads: databases, message queues, game servers, etc.

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
    "db${local.sfx}" = { newbits = 8, netnum = 1, public = false, zone = "a" }
  }
}

module "security_groups" {
  source = "../../modules/security-groups"

  project     = var.project
  environment = var.environment
  owner       = var.owner
  vpc_id      = module.vpc.vpc_ids_by_key[local.vpc_key]

  security_groups = {
    "db${local.sfx}" = {
      description = "Database tier — internal access only"
      ingress_rules = {
        internal = { ip_protocol = "tcp", from_port = 0, to_port = 65535, cidr = var.cidr_block }
      }
      egress_rules = {
        all = { ip_protocol = "-1", cidr = "0.0.0.0/0" }
      }
    }
  }
}

module "db" {
  source = "../../modules/instance-group"

  project     = var.project
  environment = var.environment
  owner       = var.owner

  name            = "db${local.sfx}"
  image_id        = "ubuntu-22.04"
  flavor          = var.instance_flavor
  count_instances = var.instance_count
  vpc_id          = module.vpc.vpc_ids_by_key[local.vpc_key]
  subnet_id       = module.subnets.subnet_ids_by_key["db${local.sfx}"]
  security_group_ids = module.security_groups.security_group_ids_list
}

module "lb" {
  source = "../../modules/load-balancer"

  project     = var.project
  environment = var.environment
  owner       = var.owner

  load_balancers = {
    "nlb${local.sfx}" = {
      type       = "network"
      vpc_id     = module.vpc.vpc_ids_by_key[local.vpc_key]
      subnet_ids = values(module.subnets.private_subnet_ids)
      internal   = true
    }
  }
}
