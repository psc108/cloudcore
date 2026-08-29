# 01 — VPC Only
# Simplest possible example — a VPC with two logical subnets.

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
    "web${local.sfx}" = { newbits = 8, netnum = 1, public = true,  zone = "a" }
    "db${local.sfx}"  = { newbits = 8, netnum = 2, public = false, zone = "a" }
  }
}
