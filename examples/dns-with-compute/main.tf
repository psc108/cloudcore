# 06 — DNS with Compute
# VPC + subnets + security groups + single instance + DNS zone + A record.
# Mirrors Ansible example 03 (compute-with-dns).

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
      description = "Web instance — HTTP and SSH"
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

module "compute" {
  source = "../../modules/compute"

  project     = var.project
  environment = var.environment
  owner       = var.owner

  instances = {
    "web-01${local.sfx}" = {
      image_id           = "ubuntu-22.04"
      flavor             = var.instance_flavor
      vpc_id             = module.vpc.vpc_ids_by_key[local.vpc_key]
      subnet_id          = module.subnets.subnet_ids_by_key["web${local.sfx}"]
      security_group_ids = module.security_groups.security_group_ids_list
    }
  }
}

module "dns_zone" {
  source = "../../modules/dns-zone"

  project     = var.project
  environment = var.environment
  owner       = var.owner

  zones = {
    "app${local.sfx}" = { name = var.dns_zone }
  }
}

module "dns_records" {
  source = "../../modules/dns-records"

  project     = var.project
  environment = var.environment
  owner       = var.owner

  records = {
    "web${local.sfx}" = {
      zone  = module.dns_zone.zone_names_by_key["app${local.sfx}"]
      name  = "web"
      type  = "A"
      value = values(module.compute.private_ips_by_key)[0]
      ttl   = 300
    }
  }
}
