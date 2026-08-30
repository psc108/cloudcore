# 08 — OpenStack Services Stack
# VPC + public/private subnets + 4 security groups.
# 6 named instances: frontend, backend, mysql, keystone, rabbitmq, admin.
# admin is also an NFS server with two exports (config, data).
# Two load balancers:
#   frontend-lb  → frontend instance (public-facing L7 ALB, port 80)
#   backend-lb   → backend, mysql, keystone, rabbitmq, admin/NFS (internal L4 NLB)
# Mirrors Ansible example 08 (openstack-services).

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
    "public${local.sfx}"  = { newbits = 8, netnum = 1, public = true,  zone = "a" }
    "private${local.sfx}" = { newbits = 8, netnum = 2, public = false, zone = "a" }
  }
}

module "security_groups" {
  source = "../../modules/security-groups"

  project     = var.project
  environment = var.environment
  owner       = var.owner
  vpc_id      = module.vpc.vpc_ids_by_key[local.vpc_key]

  security_groups = {
    "frontend${local.sfx}" = {
      description = "Frontend — HTTP public + SSH"
      ingress_rules = {
        http = { ip_protocol = "tcp", from_port = 80,  to_port = 80,  cidr = "0.0.0.0/0" }
        ssh  = { ip_protocol = "tcp", from_port = 22,  to_port = 22,  cidr = "0.0.0.0/0" }
      }
      egress_rules = {
        all = { ip_protocol = "-1", cidr = "0.0.0.0/0" }
      }
    }
    "backend${local.sfx}" = {
      description = "Backend services — internal only"
      ingress_rules = {
        app  = { ip_protocol = "tcp", from_port = 8080, to_port = 8080, cidr = var.cidr_block }
        ssh  = { ip_protocol = "tcp", from_port = 22,   to_port = 22,   cidr = var.cidr_block }
      }
      egress_rules = {
        all = { ip_protocol = "-1", cidr = "0.0.0.0/0" }
      }
    }
    "data${local.sfx}" = {
      description = "Data tier — MySQL, RabbitMQ, NFS"
      ingress_rules = {
        mysql   = { ip_protocol = "tcp", from_port = 3306,  to_port = 3306,  cidr = var.cidr_block }
        amqp    = { ip_protocol = "tcp", from_port = 5672,  to_port = 5672,  cidr = var.cidr_block }
        nfs     = { ip_protocol = "tcp", from_port = 2049,  to_port = 2049,  cidr = var.cidr_block }
        ssh     = { ip_protocol = "tcp", from_port = 22,    to_port = 22,    cidr = var.cidr_block }
      }
      egress_rules = {
        all = { ip_protocol = "-1", cidr = "0.0.0.0/0" }
      }
    }
    "identity${local.sfx}" = {
      description = "Keystone identity service"
      ingress_rules = {
        keystone_public  = { ip_protocol = "tcp", from_port = 5000, to_port = 5000, cidr = var.cidr_block }
        keystone_admin   = { ip_protocol = "tcp", from_port = 35357, to_port = 35357, cidr = var.cidr_block }
        ssh              = { ip_protocol = "tcp", from_port = 22,   to_port = 22,   cidr = var.cidr_block }
      }
      egress_rules = {
        all = { ip_protocol = "-1", cidr = "0.0.0.0/0" }
      }
    }
  }
}

# ── Compute instances ────────────────────────────────────────────────────────

module "frontend" {
  source = "../../modules/instance-group"

  project     = var.project
  environment = var.environment
  owner       = var.owner

  name            = "frontend${local.sfx}"
  image_id        = "ubuntu-22.04"
  flavor          = var.frontend_flavor
  count_instances = 1
  vpc_id          = module.vpc.vpc_ids_by_key[local.vpc_key]
  subnet_id       = module.subnets.subnet_ids_by_key["public${local.sfx}"]
  security_group_ids = [
    module.security_groups.security_group_ids_by_key["frontend${local.sfx}"],
  ]
  tags = { Role = "frontend" }
}

module "backend" {
  source = "../../modules/instance-group"

  project     = var.project
  environment = var.environment
  owner       = var.owner

  name            = "backend${local.sfx}"
  image_id        = "ubuntu-22.04"
  flavor          = var.backend_flavor
  count_instances = 1
  vpc_id          = module.vpc.vpc_ids_by_key[local.vpc_key]
  subnet_id       = module.subnets.subnet_ids_by_key["private${local.sfx}"]
  security_group_ids = [
    module.security_groups.security_group_ids_by_key["backend${local.sfx}"],
  ]
  tags = { Role = "backend" }
}

module "mysql" {
  source = "../../modules/instance-group"

  project     = var.project
  environment = var.environment
  owner       = var.owner

  name            = "mysql${local.sfx}"
  image_id        = "ubuntu-22.04"
  flavor          = var.data_flavor
  count_instances = 1
  vpc_id          = module.vpc.vpc_ids_by_key[local.vpc_key]
  subnet_id       = module.subnets.subnet_ids_by_key["private${local.sfx}"]
  security_group_ids = [
    module.security_groups.security_group_ids_by_key["data${local.sfx}"],
  ]
  tags = { Role = "mysql" }
}

module "keystone" {
  source = "../../modules/instance-group"

  project     = var.project
  environment = var.environment
  owner       = var.owner

  name            = "keystone${local.sfx}"
  image_id        = "ubuntu-22.04"
  flavor          = var.backend_flavor
  count_instances = 1
  vpc_id          = module.vpc.vpc_ids_by_key[local.vpc_key]
  subnet_id       = module.subnets.subnet_ids_by_key["private${local.sfx}"]
  security_group_ids = [
    module.security_groups.security_group_ids_by_key["identity${local.sfx}"],
  ]
  tags = { Role = "keystone" }
}

module "rabbitmq" {
  source = "../../modules/instance-group"

  project     = var.project
  environment = var.environment
  owner       = var.owner

  name            = "rabbitmq${local.sfx}"
  image_id        = "ubuntu-22.04"
  flavor          = var.data_flavor
  count_instances = 1
  vpc_id          = module.vpc.vpc_ids_by_key[local.vpc_key]
  subnet_id       = module.subnets.subnet_ids_by_key["private${local.sfx}"]
  security_group_ids = [
    module.security_groups.security_group_ids_by_key["data${local.sfx}"],
  ]
  tags = { Role = "rabbitmq" }
}

# ── Admin / NFS server ───────────────────────────────────────────────────────
# The admin instance is provisioned as an NFS server so all services can share
# config and data volumes (e.g. /exports/config, /exports/data).

module "admin_nfs" {
  source = "../../modules/nfs-server"

  project     = var.project
  environment = var.environment
  owner       = var.owner

  nfs_servers = {
    "admin${local.sfx}" = {
      vpc_id  = module.vpc.vpc_ids_by_key[local.vpc_key]
      flavor  = var.admin_flavor
      disk_gb = var.admin_disk_gb
      shares = [
        { name = "config", clients = "vpc" },
        { name = "data",   clients = "vpc" },
      ]
    }
  }
}

# ── Load balancers ───────────────────────────────────────────────────────────

module "lb" {
  source = "../../modules/load-balancer"

  project     = var.project
  environment = var.environment
  owner       = var.owner

  load_balancers = {
    # Public-facing L7 ALB — routes HTTP traffic to the frontend instance
    "frontend-lb${local.sfx}" = {
      type       = "application"
      vpc_id     = module.vpc.vpc_ids_by_key[local.vpc_key]
      subnet_ids = [module.subnets.subnet_ids_by_key["public${local.sfx}"]]
      internal   = false
    }
    # Internal L4 NLB — ties together all backend services
    "backend-lb${local.sfx}" = {
      type       = "network"
      vpc_id     = module.vpc.vpc_ids_by_key[local.vpc_key]
      subnet_ids = [module.subnets.subnet_ids_by_key["private${local.sfx}"]]
      internal   = true
    }
  }
}
