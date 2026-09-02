# 07 — NFS Shared Storage
# VPC + subnets + NFS server with two exports + two compute instances.
# Mirrors Ansible example 07 (nfs-shared-storage).

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
    "app${local.sfx}"     = { newbits = 8, netnum = 1, public = true,  zone = "a" }
    "storage${local.sfx}" = { newbits = 8, netnum = 2, public = false, zone = "a" }
  }
}

module "security_groups" {
  source = "../../modules/security-groups"

  project     = var.project
  environment = var.environment
  owner       = var.owner
  vpc_id      = module.vpc.vpc_ids_by_key[local.vpc_key]

  security_groups = {
    "app${local.sfx}" = {
      description = "App instances — SSH access"
      ingress_rules = {
        ssh = { ip_protocol = "tcp", from_port = 22, to_port = 22, cidr = "0.0.0.0/0" }
      }
      egress_rules = {
        all = { ip_protocol = "-1", cidr = "0.0.0.0/0" }
      }
    }
  }
}

module "nfs" {
  source = "../../modules/nfs-server"

  project     = var.project
  environment = var.environment
  owner       = var.owner

  nfs_servers = {
    "shared${local.sfx}" = {
      vpc_id  = module.vpc.vpc_ids_by_key[local.vpc_key]
      flavor  = var.nfs_flavor
      disk_gb = var.nfs_disk_gb
      shares = [
        { name = "data",    clients = "vpc" },
        { name = "backups", clients = "vpc" },
      ]
    }
  }
}

module "app" {
  source = "../../modules/instance-group"

  project     = var.project
  environment = var.environment
  owner       = var.owner

  name            = "app${local.sfx}"
  image_id        = "ubuntu-22.04"
  flavor          = var.instance_flavor
  count_instances = var.instance_count
  vpc_id          = module.vpc.vpc_ids_by_key[local.vpc_key]
  subnet_id       = module.subnets.subnet_ids_by_key["app${local.sfx}"]
  security_group_ids = module.security_groups.security_group_ids_list
}
