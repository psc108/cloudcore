# 02 — Basic Compute
# VPC + single compute instance.

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
    }
  }
}
