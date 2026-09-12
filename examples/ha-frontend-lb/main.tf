# HA Frontend Load Balancer — Lab / OpenTofu
# VPC + subnet + security groups + a frontend instance-group + two
# ProxySQL nodes that also run NGINX/Keepalived, sharing a floating VIP.
#
# Deliberately self-managed (NGINX + Keepalived on plain instances)
# rather than CloudCore's native load-balancer resource: this pattern is
# meant to generalize unchanged to On-Prem and AWS, where there may be no
# equivalent platform LB — see haFullStack-LLD.md §1.
#
# NGINX/Keepalived is co-located on the ProxySQL nodes rather than its
# own dedicated pair — cuts node count, and only the node actively
# holding the VIP ever serves real traffic anyway, so pairing the LB
# layer with the tier least disrupted by that (ProxySQL, itself already
# GR-aware and idle between requests) costs nothing functionally. See
# locals.tf's proxysql_instances / nginx_stream_conf for how the
# resulting self-reference (this tier's own stream{} config needing to
# know its own node's address) is resolved.

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
    "mysql${local.sfx}" = {
      description = "MySQL Group Replication tier — client port + GR peer traffic within the bridge subnet"
      ingress_rules = {
        mysql = { ip_protocol = "tcp", from_port = 3306, to_port = 3306, cidr = local.bridge_cidr }
        gr    = { ip_protocol = "tcp", from_port = 33061, to_port = 33061, cidr = local.bridge_cidr }
        ssh   = { ip_protocol = "tcp", from_port = 22, to_port = 22, cidr = var.admin_cidr }
      }
      egress_rules = {
        all = { ip_protocol = "-1", cidr = "0.0.0.0/0" }
      }
    }
    # ProxySQL + NGINX/Keepalived, co-located (haFullStack-LLD.md §1/§2) —
    # merged from two previously separate tiers, so this SG carries both
    # ProxySQL's own internal ingress rules (client/admin, MySQL nodes'
    # subnet only) and what used to be the standalone "nginx" SG's
    # client-facing/VRRP rules.
    "proxysql${local.sfx}" = {
      description = "ProxySQL + NGINX/Keepalived LB tier — client HTTP/HTTPS, ProxySQL client port, SSH, VRRP/health between LB nodes"
      ingress_rules = {
        client = { ip_protocol = "tcp", from_port = 6033, to_port = 6033, cidr = local.bridge_cidr }
        http   = { ip_protocol = "tcp", from_port = 80, to_port = 80, cidr = "0.0.0.0/0" }
        https  = { ip_protocol = "tcp", from_port = 443, to_port = 443, cidr = "0.0.0.0/0", description = "Client-facing TLS termination — haFullStack-LLD.md §5.3.1" }
        ssh    = { ip_protocol = "tcp", from_port = 22, to_port = 22, cidr = var.admin_cidr }
        vrrp   = { ip_protocol = "-1", cidr = local.bridge_cidr, description = "VRRP + health checks between LB nodes (protocol 112 isn't independently expressible here)" }
      }
      egress_rules = {
        all = { ip_protocol = "-1", cidr = "0.0.0.0/0" }
      }
    }
    "keystone${local.sfx}" = {
      description = "Keystone identity tier — API (plain + TLS) from the NGINX nodes' subnet only, plus SSH"
      ingress_rules = {
        api     = { ip_protocol = "tcp", from_port = 5000, to_port = 5000, cidr = local.bridge_cidr }
        api_tls = { ip_protocol = "tcp", from_port = 5443, to_port = 5443, cidr = local.bridge_cidr, description = "Apache mod_ssl + mTLS — haFullStack-LLD.md §5.3.1" }
        ssh     = { ip_protocol = "tcp", from_port = 22, to_port = 22, cidr = var.admin_cidr }
      }
      egress_rules = {
        all = { ip_protocol = "-1", cidr = "0.0.0.0/0" }
      }
    }
    "memcached${local.sfx}" = {
      description = "memcached — Keystone tier's subnet only, never exposed beyond it; plus SSH"
      ingress_rules = {
        memcache = { ip_protocol = "tcp", from_port = 11211, to_port = 11211, cidr = local.bridge_cidr }
        ssh      = { ip_protocol = "tcp", from_port = 22, to_port = 22, cidr = var.admin_cidr }
      }
      egress_rules = {
        all = { ip_protocol = "-1", cidr = "0.0.0.0/0" }
      }
    }
    "rabbitmq${local.sfx}" = {
      description = "RabbitMQ tier — AMQP (plain + TLS)/management from nginx SG's subnet, Erlang clustering within the bridge subnet, plus SSH"
      ingress_rules = {
        amqp     = { ip_protocol = "tcp", from_port = 5672, to_port = 5672, cidr = local.bridge_cidr }
        amqp_tls = { ip_protocol = "tcp", from_port = 5671, to_port = 5671, cidr = local.bridge_cidr, description = "TLS + mTLS listener (verify_peer, fail_if_no_peer_cert) — haFullStack-LLD.md §5.3.1" }
        mgmt     = { ip_protocol = "tcp", from_port = 15672, to_port = 15672, cidr = local.bridge_cidr }
        epmd     = { ip_protocol = "tcp", from_port = 4369, to_port = 4369, cidr = local.bridge_cidr }
        erldist  = { ip_protocol = "tcp", from_port = 25672, to_port = 25672, cidr = local.bridge_cidr }
        ssh      = { ip_protocol = "tcp", from_port = 22, to_port = 22, cidr = var.admin_cidr }
      }
      egress_rules = {
        all = { ip_protocol = "-1", cidr = "0.0.0.0/0" }
      }
    }
    # Backend application tier. Its own inbound needs are just SSH +
    # HTTP from the shared LB (the LB's own :8080 listener proxies to
    # backend nodes' local nginx on :80 — see nginx-backend.conf.tftpl).
    # Its *outbound* access to MySQL/ProxySQL/Keystone/RabbitMQ needs no
    # new rule anywhere else: every one of those tiers' existing ingress
    # rules is already scoped to local.bridge_cidr (the whole shared
    # bridge subnet, not a specific peer security group — the
    # already-established pattern every tier in this file uses, not
    # source_sg_id-based rules, which this module supports but nothing
    # in this example has ever actually exercised), and backend's own
    # bridge-mode instances land on that exact same subnet.
    "backend${local.sfx}" = {
      description = "Backend application tier — HTTP from the shared LB tier's subnet, SSH for debugging. The application itself is deployed manually, not by this template."
      ingress_rules = {
        http = { ip_protocol = "tcp", from_port = 80, to_port = 80, cidr = local.bridge_cidr }
        ssh  = { ip_protocol = "tcp", from_port = 22, to_port = 22, cidr = var.admin_cidr }
      }
      egress_rules = {
        all = { ip_protocol = "-1", cidr = "0.0.0.0/0" }
      }
    }
    "ca${local.sfx}" = {
      description = "step-ca — issuance/renewal API + plain-HTTP root/intermediate cert serving, from the bridge subnet, plus SSH"
      ingress_rules = {
        api   = { ip_protocol = "tcp", from_port = 8443, to_port = 8443, cidr = local.bridge_cidr }
        certs = { ip_protocol = "tcp", from_port = 8080, to_port = 8080, cidr = local.bridge_cidr, description = "Plain-HTTP root/intermediate fetch — avoids a chicken-and-egg TLS bootstrap for every other node's initial fetch" }
        ssh   = { ip_protocol = "tcp", from_port = 22, to_port = 22, cidr = var.admin_cidr }
      }
      egress_rules = {
        all = { ip_protocol = "-1", cidr = "0.0.0.0/0" }
      }
    }
  }
}

# Both Keystone nodes need identical Fernet key material from first boot —
# generated once here and injected into both nodes' user_data, no runtime
# cross-node coordination needed (haFullStack-LLD.md §3.3.1). File "0" is
# the primary (encrypt+decrypt) key, "1" the secondary (decrypt-only,
# needed by keystone-manage fernet_setup's own rotation model even though
# this Lab slice never rotates) — byte_length=32 matches Fernet's expected
# key size exactly, and random_id's .b64_url output is byte-for-byte the
# same format keystone-manage fernet_setup itself writes to
# /etc/keystone/fernet-keys/, confirmed directly against a real install.
resource "random_id" "fernet_key0" { byte_length = 32 }
resource "random_id" "fernet_key1" { byte_length = 32 }

# All 3 RabbitMQ nodes need an identical Erlang cookie — RabbitMQ refuses
# to cluster nodes with mismatched cookies (haFullStack-LLD.md §4.1),
# same reasoning as the Fernet keys above. 20 bytes matches
# rabbitmq-server's own default cookie length (confirmed directly against
# a real install: /var/lib/rabbitmq/.erlang.cookie is 56 base64 characters
# — 20 raw bytes base64-encoded, no special padding requirement unlike
# F-025's Fernet keys, since Erlang's cookie comparison is a plain string
# match, not a base64-decode).
resource "random_id" "erlang_cookie" { byte_length = 20 }

# Every certificate-requesting node needs this to authenticate to the CA
# for issuance/renewal — same shared-secret pattern as the Fernet keys
# and Erlang cookie above (haFullStack-LLD.md §5.3.1).
resource "random_id" "ca_provisioner_password" { byte_length = 24 }

# Single node, deliberately not HA — haFullStack-LLD.md §5.1's flagged
# Lab simplification. Created before every other tier's user_data is
# rendered (all of them now need its IP) — modules/compute, not
# instance-group, purely because every other single/paired-role node in
# this stack already uses it and there's no reason to introduce a third
# module type for a one-node case.
module "ca" {
  source = "../../modules/compute"

  project     = var.project
  environment = var.environment
  owner       = var.owner

  instances = local.ca_instance
}

module "frontend" {
  source = "../../modules/instance-group"

  project     = var.project
  environment = var.environment
  owner       = var.owner

  name               = "frontend${local.sfx}"
  image_id           = "ubuntu-22.04"
  flavor             = var.frontend_flavor
  count_instances    = var.frontend_count
  vpc_id             = module.vpc.vpc_ids_by_key[local.vpc_key]
  subnet_id          = module.subnets.subnet_ids_by_key["main${local.sfx}"]
  security_group_ids = [module.security_groups.security_group_ids_by_key["frontend${local.sfx}"]]
  user_data          = local.frontend_user_data
}

# Split into two module calls (bootstrap node, then joiners) rather than
# one 3-key modules/compute call: the joiners' user_data needs the
# bootstrap node's real IP, and a single module call can't reference its
# own private_ips_by_key output from within itself (haFullStack-LLD.md
# §2.3.2/§2.7). This ordering also happens to solve the bootstrap-before-
# join sequencing risk at the Terraform level — the joiners' cloud-init
# still has its own wait/retry loop for GR readiness specifically,
# independent of instance-creation ordering (see mysql-cloud-init.yaml.tftpl).
module "mysql_bootstrap" {
  source = "../../modules/compute"

  project     = var.project
  environment = var.environment
  owner       = var.owner

  instances = local.mysql_bootstrap_instances
}

module "mysql_replicas" {
  source = "../../modules/compute"

  project     = var.project
  environment = var.environment
  owner       = var.owner

  instances = local.mysql_replica_instances
}

# modules/compute, not instance-group: unlike the pre-merge ProxySQL
# tier (identical config on both nodes), the two nodes now need different
# Keepalived state/priority (MASTER/BACKUP) — same reasoning as the
# now-retired standalone "nginx" module, which this absorbs.
module "proxysql" {
  source = "../../modules/compute"

  project     = var.project
  environment = var.environment
  owner       = var.owner

  instances = local.proxysql_instances
}

module "memcached" {
  source = "../../modules/instance-group"

  project     = var.project
  environment = var.environment
  owner       = var.owner

  name               = "memcached${local.sfx}"
  image_id           = "ubuntu-22.04"
  flavor             = var.memcached_flavor
  count_instances    = 2
  vpc_id             = module.vpc.vpc_ids_by_key[local.vpc_key]
  subnet_id          = module.subnets.subnet_ids_by_key["main${local.sfx}"]
  security_group_ids = [module.security_groups.security_group_ids_by_key["memcached${local.sfx}"]]
  user_data          = local.memcached_user_data
}

# instance-group, not compute: unlike MySQL's bootstrap/joiner split or
# NGINX's MASTER/BACKUP split, both Keystone nodes run genuinely identical
# config — no per-node role. keystone-manage db_sync and bootstrap are
# both safe to run unconditionally and concurrently on every node
# (confirmed directly: re-running each against an already-initialized
# database is a clean no-op, not a duplicate/error) — the "only needs to
# run on one node" framing in haFullStack-LLD.md §3.3.1 describes the
# logical effect, not a requirement for genuinely identical user_data.
module "keystone" {
  source = "../../modules/instance-group"

  project     = var.project
  environment = var.environment
  owner       = var.owner

  name               = "keystone${local.sfx}"
  image_id           = "ubuntu-22.04"
  flavor             = var.keystone_flavor
  count_instances    = 2
  vpc_id             = module.vpc.vpc_ids_by_key[local.vpc_key]
  subnet_id          = module.subnets.subnet_ids_by_key["main${local.sfx}"]
  security_group_ids = [module.security_groups.security_group_ids_by_key["keystone${local.sfx}"]]
  user_data          = local.keystone_user_data
}

# Split into two module calls (seed, then joiners) — same reasoning as
# mysql_bootstrap/mysql_replicas above: RabbitMQ clustering is
# asymmetric (join_cluster runs on the joiner against an already-running
# seed), and the joiners' user_data needs the seed's real IP, which a
# single module call can't self-reference (haFullStack-LLD.md §4.1/§4.3.2).
# instance-group, not compute: identical config on both nodes, no
# per-node role — the application layer (installed manually afterward,
# not by this template) is what would introduce any real asymmetry, not
# this infrastructure. Same reasoning as Keystone.
module "backend" {
  source = "../../modules/instance-group"

  project     = var.project
  environment = var.environment
  owner       = var.owner

  name               = "backend${local.sfx}"
  image_id           = "ubuntu-22.04"
  flavor             = var.backend_flavor
  count_instances    = 2
  vpc_id             = module.vpc.vpc_ids_by_key[local.vpc_key]
  subnet_id          = module.subnets.subnet_ids_by_key["main${local.sfx}"]
  security_group_ids = [module.security_groups.security_group_ids_by_key["backend${local.sfx}"]]
  user_data          = local.backend_user_data
}

module "rabbitmq_seed" {
  source = "../../modules/compute"

  project     = var.project
  environment = var.environment
  owner       = var.owner

  instances = local.rabbitmq_seed_instance
}

module "rabbitmq_joiners" {
  source = "../../modules/compute"

  project     = var.project
  environment = var.environment
  owner       = var.owner

  instances = local.rabbitmq_joiner_instances
}
