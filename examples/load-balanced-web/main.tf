# 03 — Load-Balanced Web (L7 ALB)
# VPC + subnets + security groups + instance group + application load balancer.

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
    "web${local.sfx}"   = { newbits = 8, netnum = 1, public = true, zone = "a" }
    "web-b${local.sfx}" = { newbits = 8, netnum = 2, public = true, zone = "b" }
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
      description = "Web tier — HTTP and SSH"
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

module "web" {
  source = "../../modules/instance-group"

  project     = var.project
  environment = var.environment
  owner       = var.owner

  name               = "web${local.sfx}"
  image_id           = "ubuntu-22.04"
  flavor             = var.instance_flavor
  count_instances    = var.instance_count
  vpc_id             = module.vpc.vpc_ids_by_key[local.vpc_key]
  subnet_id          = module.subnets.subnet_ids_by_key["web${local.sfx}"]
  security_group_ids = module.security_groups.security_group_ids_list
  user_data          = local.nginx_user_data

  # Empty when web_02_peer_id is unset -- every instance stays local,
  # unchanged default behavior. See variables.tf's own comment for why
  # vpc_id/subnet_id travel together with peer_id here.
  placement_overrides = var.web_02_peer_id != "" ? {
    "02" = {
      peer_id            = var.web_02_peer_id
      vpc_id             = var.web_02_peer_vpc_id
      subnet_id          = var.web_02_peer_subnet_id
      security_group_ids = [var.web_02_peer_security_group_id]
    }
  } : {}
}

module "lb" {
  source = "../../modules/load-balancer"

  project     = var.project
  environment = var.environment
  owner       = var.owner

  load_balancers = {
    "alb${local.sfx}" = {
      type       = "application"
      vpc_id     = module.vpc.vpc_ids_by_key[local.vpc_key]
      subnet_ids = values(module.subnets.public_subnet_ids)
      internal   = false
    }
  }
}

# Registers both web instances as real haproxy backends — previously
# missing entirely (module.lb created the load balancer with nothing
# routing to it). cloudcore_lb_listener.port is required and distinct
# from the LB resource's own computed listen_port: creating any listener
# replaces that default frontend outright, so 80 here is what actually
# matters, matching the web tier's own SG rule.
resource "cloudcore_lb_target_group" "web" {
  lb_id    = module.lb.lb_ids_by_key["alb${local.sfx}"]
  name     = "web${local.sfx}"
  port     = 80
  protocol = "http"

  targets = [
    for k, id in module.web.instance_ids_by_key : {
      instance_id = id
      port        = 80
    }
  ]
}

resource "cloudcore_lb_listener" "web" {
  lb_id           = module.lb.lb_ids_by_key["alb${local.sfx}"]
  port            = var.lb_port
  protocol        = "http"
  target_group_id = cloudcore_lb_target_group.web.id
}
