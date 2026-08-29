# 04 — Network Load Balancer (L4 NLB)
# VPC + two backend instances + internal L4 NLB.
# Use for non-HTTP workloads: databases, game servers, message queues, etc.

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
    "db-01${local.sfx}" = {
      image_id  = "ubuntu-22.04"
      flavor    = var.instance_flavor
      vpc_id    = module.vpc.vpc_ids_by_key[local.vpc_key]
      subnet_id = local.subnet_id
    }
    "db-02${local.sfx}" = {
      image_id  = "ubuntu-22.04"
      flavor    = var.instance_flavor
      vpc_id    = module.vpc.vpc_ids_by_key[local.vpc_key]
      subnet_id = local.subnet_id
    }
  }
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
      subnet_ids = [local.subnet_id]
      internal   = true
    }
  }
}
