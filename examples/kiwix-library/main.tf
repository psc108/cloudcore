# Kiwix Library
# VPC + subnet + security group + a single compute instance running
# kiwix-serve, fronted by a plain HTTP load balancer — open desktop_url in
# a browser and the content library is just there. No SSH tunnel, no VNC
# client, no password: kiwix-serve has no built-in auth in this config
# (matches its use case — sharing reference content, not gating it).
#
# Unlike the Ghidra workstation template, this uses a plain "application"
# (HTTP) load balancer with instance auto-discovery — the same proven
# pattern as the load-balanced-web example — rather than an explicit
# target group/listener. kiwix-serve is a normal stateless HTTP server
# (no WebSocket), so none of the TCP-passthrough/heartbeat machinery
# Ghidra's noVNC needs applies here.

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
    "kiwix${local.sfx}" = { newbits = 8, netnum = 1, public = true, zone = "a" }
  }
}

module "security_groups" {
  source = "../../modules/security-groups"

  project     = var.project
  environment = var.environment
  owner       = var.owner
  vpc_id      = module.vpc.vpc_ids_by_key[local.vpc_key]

  security_groups = {
    "kiwix${local.sfx}" = {
      description = "Kiwix library — SSH + HTTP, scoped to admin_cidr"
      ingress_rules = {
        ssh  = { ip_protocol = "tcp", from_port = 22, to_port = 22, cidr = var.admin_cidr }
        http = { ip_protocol = "tcp", from_port = 80, to_port = 80, cidr = var.admin_cidr }
      }
      egress_rules = {
        all = { ip_protocol = "-1", cidr = "0.0.0.0/0" }
      }
    }
  }
}

module "kiwix" {
  source = "../../modules/instance-group"

  project     = var.project
  environment = var.environment
  owner       = var.owner

  name               = "kiwix${local.sfx}"
  image_id           = "ubuntu-22.04"
  flavor             = var.instance_flavor
  count_instances    = 1
  vpc_id             = module.vpc.vpc_ids_by_key[local.vpc_key]
  subnet_id          = module.subnets.subnet_ids_by_key["kiwix${local.sfx}"]
  security_group_ids = module.security_groups.security_group_ids_list
  user_data          = local.kiwix_user_data
}

module "lb" {
  source = "../../modules/load-balancer"

  project     = var.project
  environment = var.environment
  owner       = var.owner

  load_balancers = {
    "kiwix${local.sfx}" = {
      type       = "application"
      vpc_id     = module.vpc.vpc_ids_by_key[local.vpc_key]
      subnet_ids = [module.subnets.subnet_ids_by_key["kiwix${local.sfx}"]]
      internal   = false
    }
  }

  # Auto-discovery (this LB has no explicit listener/target group) only
  # picks up instances that already exist in the VPC at LB-creation time —
  # force the instance to be created first so it's guaranteed to be seen.
  depends_on = [module.kiwix]
}
