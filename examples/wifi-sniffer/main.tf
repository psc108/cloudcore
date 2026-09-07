# WiFi Sniffer
# VPC + subnet + security group + a single compute instance running Kismet
# (fed by a passed-through USB WiFi adapter in monitor mode), fronted by a
# network (L4) load balancer so the live dashboard is reachable by opening
# a plain URL in a browser — no SSH tunnel, no client install. See output
# `desktop_url`. `ssh_commands` is still there for deeper analysis
# (aircrack-ng, tshark, hcxtools all installed) and to retrieve the
# Kismet-generated dashboard login.
#
# Same architecture as the Ghidra workstation template, for the same
# reason: the load balancer is deliberately "network" (mode tcp in the
# underlying HAProxy), not "application" — HAProxy's HTTP mode sets
# `option http-server-close`, which closes the connection after every
# request/response, and Kismet's live dashboard is WebSocket-driven
# (confirmed from its source — devicetracker.cc registers a real
# boost::beast websocket route), so HTTP mode would break it outright.

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
    "sniffer${local.sfx}" = { newbits = 8, netnum = 1, public = true, zone = "a" }
  }
}

module "security_groups" {
  source = "../../modules/security-groups"

  project     = var.project
  environment = var.environment
  owner       = var.owner
  vpc_id      = module.vpc.vpc_ids_by_key[local.vpc_key]

  security_groups = {
    "sniffer${local.sfx}" = {
      description = "WiFi sniffer — SSH + dashboard, scoped to admin_cidr"
      ingress_rules = {
        ssh       = { ip_protocol = "tcp", from_port = 22, to_port = 22, cidr = var.admin_cidr }
        dashboard = { ip_protocol = "tcp", from_port = var.lb_port, to_port = var.lb_port, cidr = var.admin_cidr }
      }
      egress_rules = {
        all = { ip_protocol = "-1", cidr = "0.0.0.0/0" }
      }
    }
  }
}

module "sniffer" {
  source = "../../modules/instance-group"

  project     = var.project
  environment = var.environment
  owner       = var.owner

  name               = "sniffer${local.sfx}"
  image_id           = "ubuntu-22.04"
  flavor             = var.instance_flavor
  count_instances    = 1
  vpc_id             = module.vpc.vpc_ids_by_key[local.vpc_key]
  subnet_id          = module.subnets.subnet_ids_by_key["sniffer${local.sfx}"]
  security_group_ids = module.security_groups.security_group_ids_list
  usb_device_ids     = [var.usb_device_id]
  user_data          = local.sniffer_user_data
}

module "lb" {
  source = "../../modules/load-balancer"

  project     = var.project
  environment = var.environment
  owner       = var.owner

  load_balancers = {
    "sniffer${local.sfx}" = {
      type       = "network"
      vpc_id     = module.vpc.vpc_ids_by_key[local.vpc_key]
      subnet_ids = [module.subnets.subnet_ids_by_key["sniffer${local.sfx}"]]
      internal   = false
    }
  }
}

# No module wraps target groups/listeners yet, so these are the raw
# provider resources — same pattern the Ghidra template uses. TCP
# passthrough straight to Kismet's web port (80 — see cloud-init: Kismet
# is configured off its 2501 default onto 80, matching the one guest port
# this platform's per-instance hostfwd/target-group plumbing forwards to).
resource "cloudcore_lb_target_group" "sniffer" {
  lb_id    = module.lb.lb_ids_by_key["sniffer${local.sfx}"]
  name     = "${var.project}-${var.environment}-sniffer-tg${local.sfx}"
  port     = 80
  protocol = "tcp"

  targets = [
    {
      instance_id = module.sniffer.instance_ids_by_key["01"]
      port        = 80
    }
  ]
}

resource "cloudcore_lb_listener" "sniffer" {
  lb_id           = module.lb.lb_ids_by_key["sniffer${local.sfx}"]
  port            = var.lb_port
  protocol        = "tcp"
  target_group_id = cloudcore_lb_target_group.sniffer.id
}
