# HA Frontend Load Balancer — Lab / OpenTofu
# VPC + subnet + security groups + a frontend instance-group + two
# NGINX/Keepalived nodes reverse-proxying to it, sharing a floating VIP.
#
# Deliberately self-managed (NGINX + Keepalived on plain instances)
# rather than CloudCore's native load-balancer resource: this pattern is
# meant to generalize unchanged to On-Prem and AWS, where there may be no
# equivalent platform LB — see haFullStack-LLD.md §1.

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
    "main${local.sfx}" = { cidr_block = var.cidr_block, public = true, zone = "a" }
  }
}

module "security_groups" {
  source = "../../modules/security-groups"

  project     = var.project
  environment = var.environment
  owner       = var.owner
  vpc_id      = module.vpc.vpc_ids_by_key[local.vpc_key]

  security_groups = {
    "nginx${local.sfx}" = {
      description = "NGINX/Keepalived LB tier — client HTTP, SSH, VRRP/health between LB nodes"
      ingress_rules = {
        http = { ip_protocol = "tcp", from_port = 80, to_port = 80, cidr = "0.0.0.0/0" }
        ssh  = { ip_protocol = "tcp", from_port = 22, to_port = 22, cidr = var.admin_cidr }
        vrrp = { ip_protocol = "-1", cidr = local.bridge_cidr, description = "VRRP + health checks between LB nodes (protocol 112 isn't independently expressible here)" }
      }
      egress_rules = {
        all = { ip_protocol = "-1", cidr = "0.0.0.0/0" }
      }
    }
    "frontend${local.sfx}" = {
      description = "Frontend web tier — HTTP from the LB nodes' subnet only, plus SSH for debugging"
      ingress_rules = {
        http = { ip_protocol = "tcp", from_port = 80, to_port = 80, cidr = local.bridge_cidr }
        ssh  = { ip_protocol = "tcp", from_port = 22, to_port = 22, cidr = var.admin_cidr }
      }
      egress_rules = {
        all = { ip_protocol = "-1", cidr = "0.0.0.0/0" }
      }
    }
  }
}

module "frontend" {
  source = "../../modules/instance-group"

  project     = var.project
  environment = var.environment
  owner       = var.owner

  name                = "frontend${local.sfx}"
  image_id            = "ubuntu-22.04"
  flavor              = var.frontend_flavor
  count_instances     = var.frontend_count
  vpc_id              = module.vpc.vpc_ids_by_key[local.vpc_key]
  subnet_id           = module.subnets.subnet_ids_by_key["main${local.sfx}"]
  security_group_ids  = [module.security_groups.security_group_ids_by_key["frontend${local.sfx}"]]
  user_data           = local.frontend_user_data
}

# Per-node (not instance-group) since the two NGINX nodes need different
# Keepalived state/priority — modules/compute takes per-key user_data
# natively via its instances map, so this is a single module call rather
# than two separate resources.
module "nginx" {
  source = "../../modules/compute"

  project     = var.project
  environment = var.environment
  owner       = var.owner

  instances = local.nginx_instances
}
