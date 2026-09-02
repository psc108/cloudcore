# Ghidra Workstation
# VPC + subnet + security group + a single compute instance running an
# XFCE desktop with Ghidra, fronted by a network (L4) load balancer so the
# desktop is reachable by opening a plain URL in a browser — no SSH tunnel,
# no VNC client. See output `desktop_url`. `ssh_commands` is still there
# for CLI access (uploading samples, pulling project files out).
#
# The load balancer is deliberately "network" (mode tcp in the underlying
# HAProxy), not "application": HAProxy's HTTP mode sets
# `option http-server-close`, which closes the connection after every
# request/response — fatal to a WebSocket. TCP passthrough avoids that.

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
    "ghidra${local.sfx}" = { newbits = 8, netnum = 1, public = true, zone = "a" }
  }
}

module "security_groups" {
  source = "../../modules/security-groups"

  project     = var.project
  environment = var.environment
  owner       = var.owner
  vpc_id      = module.vpc.vpc_ids_by_key[local.vpc_key]

  security_groups = {
    "ghidra${local.sfx}" = {
      description = "Ghidra workstation — SSH + desktop, scoped to admin_cidr"
      ingress_rules = {
        ssh     = { ip_protocol = "tcp", from_port = 22, to_port = 22, cidr = var.admin_cidr }
        desktop = { ip_protocol = "tcp", from_port = var.lb_port, to_port = var.lb_port, cidr = var.admin_cidr }
      }
      egress_rules = {
        all = { ip_protocol = "-1", cidr = "0.0.0.0/0" }
      }
    }
  }
}

module "ghidra" {
  source = "../../modules/instance-group"

  project     = var.project
  environment = var.environment
  owner       = var.owner

  name               = "ghidra${local.sfx}"
  image_id           = "ubuntu-22.04"
  flavor             = var.instance_flavor
  count_instances    = 1
  vpc_id             = module.vpc.vpc_ids_by_key[local.vpc_key]
  subnet_id          = module.subnets.subnet_ids_by_key["ghidra${local.sfx}"]
  security_group_ids = module.security_groups.security_group_ids_list
  user_data          = local.ghidra_user_data
}

module "lb" {
  source = "../../modules/load-balancer"

  project     = var.project
  environment = var.environment
  owner       = var.owner

  load_balancers = {
    "ghidra${local.sfx}" = {
      type       = "network"
      vpc_id     = module.vpc.vpc_ids_by_key[local.vpc_key]
      subnet_ids = [module.subnets.subnet_ids_by_key["ghidra${local.sfx}"]]
      internal   = false
    }
  }
}

# No module wraps target groups/listeners yet, so these are the raw
# provider resources. TCP passthrough straight to the instance's noVNC
# port (80) — see the comment at the top of this file for why not HTTP.
resource "cloudcore_lb_target_group" "ghidra" {
  lb_id    = module.lb.lb_ids_by_key["ghidra${local.sfx}"]
  name     = "${var.project}-${var.environment}-ghidra-tg${local.sfx}"
  port     = 80
  protocol = "tcp"

  targets = [
    {
      instance_id = module.ghidra.instance_ids_by_key["01"]
      port        = 80
    }
  ]
}

resource "cloudcore_lb_listener" "ghidra" {
  lb_id           = module.lb.lb_ids_by_key["ghidra${local.sfx}"]
  port            = var.lb_port
  protocol        = "tcp"
  target_group_id = cloudcore_lb_target_group.ghidra.id
}
