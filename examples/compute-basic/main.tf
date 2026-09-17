# 02 — Basic Compute
# VPC + subnets + security groups + single compute instance.

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
    "web${local.sfx}" = { newbits = 8, netnum = 1, public = true, zone = "a" }
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
      description = "Web instance — SSH access"
      ingress_rules = {
        ssh = { ip_protocol = "tcp", from_port = 22, to_port = 22, cidr = "0.0.0.0/0" }
      }
      egress_rules = {
        all = { ip_protocol = "-1", cidr = "0.0.0.0/0" }
      }
    }
  }
}

module "compute" {
  source = "../../modules/compute"

  project     = var.project
  environment = var.environment
  owner       = var.owner

  # vpc_id/subnet_id/security_group_ids/peer_id swap together to the
  # peer's own catalogue when peer_id is set -- see variables.tf's own
  # comment for why. Empty peer_id (the default) keeps this instance
  # local, unchanged default behavior.
  instances = {
    "web-01${local.sfx}" = {
      image_id           = "ubuntu-22.04"
      flavor             = var.instance_flavor
      vpc_id             = var.peer_id != "" ? var.peer_vpc_id : module.vpc.vpc_ids_by_key[local.vpc_key]
      subnet_id          = var.peer_id != "" ? var.peer_subnet_id : module.subnets.subnet_ids_by_key["web${local.sfx}"]
      security_group_ids = var.peer_id != "" ? [var.peer_security_group_id] : module.security_groups.security_group_ids_list
      user_data          = local.promtail_user_data
      peer_id            = var.peer_id != "" ? var.peer_id : null
    }
  }
}
