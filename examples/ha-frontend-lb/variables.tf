variable "project" {
  description = "Project name used in resource naming and tags."
  type        = string
  default     = "example"
}

variable "environment" {
  description = "Environment name used in resource naming and tags."
  type        = string
  default     = "dev"
}

variable "owner" {
  description = "Owner tag value applied to all resources."
  type        = string
  default     = "platform-team"
}

variable "suffix" {
  description = "Optional suffix appended to resource names to keep them unique across runs."
  type        = string
  default     = ""
}

variable "cidr_block" {
  description = "CIDR block for the VPC/subnet objects. Lab bridged instances get their real address from the host's ccbr0 DHCP pool (see local.bridge_cidr in locals.tf), not from this value — it exists for API bookkeeping and tagging only in this environment."
  type        = string
  default     = "10.20.0.0/16"
}

variable "admin_cidr" {
  description = "CIDR allowed SSH access to every tier. Every port on this platform is only reachable via the CloudCore host itself (loopback-bound dashboard, ssh_endpoint forwarding) regardless of this value, matching the rest of the example templates."
  type        = string
  default     = "0.0.0.0/0"
}

variable "vip_address" {
  description = "Virtual IP that Keepalived floats between the two ProxySQL/NGINX nodes. Must be a free address in the Lab bridge's real subnet (192.168.100.0/24, set up by api/setup-network.sh) and outside its DHCP range (192.168.100.10-254) — the default was the address used to empirically validate multicast VRRP over ccbr0 before this template was written."
  type        = string
  default     = "192.168.100.5"
}

variable "frontend_flavor" {
  description = "Compute flavor for the frontend web instances."
  type        = string
  default     = "standard.small"
}

variable "frontend_count" {
  description = "Number of frontend instances in the pool NGINX proxies to."
  type        = number
  default     = 2
}

variable "mysql_flavor" {
  description = "Compute flavor for the three MySQL Group Replication nodes."
  type        = string
  default     = "standard.medium"
}

variable "proxysql_flavor" {
  description = "Compute flavor for the two ProxySQL nodes, which also run NGINX/Keepalived (haFullStack-LLD.md §1/§2 — merged tiers)."
  type        = string
  default     = "standard.small"
}

variable "keystone_flavor" {
  description = "Compute flavor for the two Keystone identity nodes."
  type        = string
  default     = "standard.small"
}

variable "memcached_flavor" {
  description = "Compute flavor for the two memcached nodes."
  type        = string
  default     = "standard.nano"
}

variable "rabbitmq_flavor" {
  description = "Compute flavor for the three RabbitMQ nodes."
  type        = string
  default     = "standard.small"
}

variable "ca_flavor" {
  description = "Compute flavor for the single step-ca node."
  type        = string
  default     = "standard.nano"
}

variable "admin_password" {
  description = "Single shared password applied to every admin/service account this stack creates: MySQL's root (both 'root'@'localhost' for local/manual admin work and 'root'@'%' for remote application access, including Keystone's own keystone.conf [database] connection), replication/monitor/app/keystone-user/ssp_* accounts, Keystone's bootstrap admin user (and env.sh's OS_PASSWORD, which must match it) plus its 22 system-domain service accounts, and RabbitMQ's admin user plus its 13 application service accounts. Lab-only convenience, not a production secret-management pattern — every one of these already shared a single hardcoded placeholder value per service; this just makes that placeholder settable in one place. Does not affect purely internal, non-login secrets (VRRP auth, the Erlang cookie, Keystone's Fernet keys, the CA provisioner password), which stay independently random/generated."
  type        = string
  default     = "changeme-admin"
  sensitive   = true
}

variable "backend_flavor" {
  description = "Compute flavor for the two backend application nodes. standard.medium's 20GB disk covers the stated ~10GB requirement (2.5GB compressed app + ~2.5GB decompression + running footprint) with headroom; its 2048MB RAM is reasonable for decompressing/running an application of unspecified size. The application itself is installed manually after this infrastructure exists — not provisioned by this template."
  type        = string
  default     = "standard.medium"
}

variable "nfs_flavor" {
  description = "Compute flavor for the shared NFS server — a single lightweight node serving one export, same tier as this stack's other non-DB support nodes (keystone_flavor, rabbitmq_flavor)."
  type        = string
  default     = "standard.small"
}

variable "nfs_disk_gb" {
  description = "Storage disk size in GiB for the shared NFS server. Application-prep requirement is a 10GB minimum."
  type        = number
  default     = 10
}

# ── Per-node peer placement ──────────────────────────────────────────────────
# Every clustered/multi-node tier in this stack gets PER-NODE placement, not
# whole-tier: this is what makes real multi-machine clustering demos possible
# (individual members landing on different physical hosts), the original
# motivation for this whole feature. Only each tier's anchor node (the first/
# bootstrap/seed node — "a" for the modules/compute tiers, "01" for the
# instance-group tiers) stays local-only with no picker; every other node
# gets its own <tier>_<node>_peer_id/_peer_vpc_id/_peer_subnet_id/
# _peer_security_group_id set. Leave a node's *_peer_id blank to keep it
# local (the default -- unchanged behavior). To place it on a paired remote
# host instead, set *_peer_id (see the Dashboard's Peers section, or the
# cloudcore_peers data source, for available hosts) AND *_peer_vpc_id/
# *_peer_subnet_id to THAT peer's own vpc_id/subnet_id. Those two aren't
# optional once peer_id is set: a remote peer has its own separate VPC/
# subnet catalogue, not this build's local one (see haFullStack-LLD.md
# §13). *_peer_security_group_id is likewise required whenever *_peer_id is
# set -- a peer's own security group catalogue is separate from this
# build's local one, and the API now rejects a security_group_ids entry it
# can't resolve locally at instance-create time (previously an unresolved
# id was silently accepted and produced a DROP-only iptables chain, leaving
# the instance completely unreachable with no error anywhere — see
# haFullStack-Findings-Log.md).

# ca -- single node, deliberately not HA (haFullStack-LLD.md §5.1's flagged
# Lab simplification) -- bare peer vars, same as any other single-instance
# template, prefixed with the tier name since this file has many tiers.
variable "ca_peer_id" {
  description = "ID of a paired remote peer to place the ca instance on instead of the local host. Requires ca_peer_vpc_id/ca_peer_subnet_id/ca_peer_security_group_id to also be set."
  type        = string
  default     = ""
}

variable "ca_peer_vpc_id" {
  description = "The peer's own VPC ID for the ca instance — only meaningful when ca_peer_id is set."
  type        = string
  default     = ""
}

variable "ca_peer_subnet_id" {
  description = "The peer's own subnet ID for the ca instance — only meaningful when ca_peer_id is set."
  type        = string
  default     = ""
}

variable "ca_peer_security_group_id" {
  description = "The peer's own security group ID for the ca instance — required when ca_peer_id is set."
  type        = string
  default     = ""
}

# frontend -- variable-count instance-group (var.frontend_count, default 2).
# Only the second instance ("02") gets a peer var-set, matching
# load-balanced-web's own reference pattern exactly.
variable "frontend_02_peer_id" {
  description = "ID of a paired remote peer to place the second frontend instance on instead of the local host. Requires frontend_02_peer_vpc_id/frontend_02_peer_subnet_id/frontend_02_peer_security_group_id to also be set."
  type        = string
  default     = ""
}

variable "frontend_02_peer_vpc_id" {
  description = "The peer's own VPC ID for the second frontend instance — only meaningful when frontend_02_peer_id is set."
  type        = string
  default     = ""
}

variable "frontend_02_peer_subnet_id" {
  description = "The peer's own subnet ID for the second frontend instance — only meaningful when frontend_02_peer_id is set."
  type        = string
  default     = ""
}

variable "frontend_02_peer_security_group_id" {
  description = "The peer's own security group ID for the second frontend instance — required when frontend_02_peer_id is set."
  type        = string
  default     = ""
}

# mysql -- Group Replication cluster: bootstrap node "a" (anchor, stays
# local-only) + replica nodes "b"/"c", each independently placeable.
variable "mysql_b_peer_id" {
  description = "ID of a paired remote peer to place MySQL replica node b on instead of the local host. Requires mysql_b_peer_vpc_id/mysql_b_peer_subnet_id/mysql_b_peer_security_group_id to also be set."
  type        = string
  default     = ""
}

variable "mysql_b_peer_vpc_id" {
  description = "The peer's own VPC ID for MySQL replica node b — only meaningful when mysql_b_peer_id is set."
  type        = string
  default     = ""
}

variable "mysql_b_peer_subnet_id" {
  description = "The peer's own subnet ID for MySQL replica node b — only meaningful when mysql_b_peer_id is set."
  type        = string
  default     = ""
}

variable "mysql_b_peer_security_group_id" {
  description = "The peer's own security group ID for MySQL replica node b — required when mysql_b_peer_id is set."
  type        = string
  default     = ""
}

variable "mysql_c_peer_id" {
  description = "ID of a paired remote peer to place MySQL replica node c on instead of the local host. Requires mysql_c_peer_vpc_id/mysql_c_peer_subnet_id/mysql_c_peer_security_group_id to also be set."
  type        = string
  default     = ""
}

variable "mysql_c_peer_vpc_id" {
  description = "The peer's own VPC ID for MySQL replica node c — only meaningful when mysql_c_peer_id is set."
  type        = string
  default     = ""
}

variable "mysql_c_peer_subnet_id" {
  description = "The peer's own subnet ID for MySQL replica node c — only meaningful when mysql_c_peer_id is set."
  type        = string
  default     = ""
}

variable "mysql_c_peer_security_group_id" {
  description = "The peer's own security group ID for MySQL replica node c — required when mysql_c_peer_id is set."
  type        = string
  default     = ""
}

# proxysql -- MASTER/BACKUP Keepalived pair: node "a" (MASTER, anchor,
# stays local-only) + node "b" (BACKUP, independently placeable).
variable "proxysql_b_peer_id" {
  description = "ID of a paired remote peer to place ProxySQL/NGINX node b (BACKUP) on instead of the local host. Requires proxysql_b_peer_vpc_id/proxysql_b_peer_subnet_id/proxysql_b_peer_security_group_id to also be set."
  type        = string
  default     = ""
}

variable "proxysql_b_peer_vpc_id" {
  description = "The peer's own VPC ID for ProxySQL/NGINX node b — only meaningful when proxysql_b_peer_id is set."
  type        = string
  default     = ""
}

variable "proxysql_b_peer_subnet_id" {
  description = "The peer's own subnet ID for ProxySQL/NGINX node b — only meaningful when proxysql_b_peer_id is set."
  type        = string
  default     = ""
}

variable "proxysql_b_peer_security_group_id" {
  description = "The peer's own security group ID for ProxySQL/NGINX node b — required when proxysql_b_peer_id is set."
  type        = string
  default     = ""
}

# memcached -- fixed 2-node instance-group. Only the second instance ("02")
# gets a peer var-set, matching load-balanced-web's own reference pattern.
variable "memcached_02_peer_id" {
  description = "ID of a paired remote peer to place the second memcached instance on instead of the local host. Requires memcached_02_peer_vpc_id/memcached_02_peer_subnet_id/memcached_02_peer_security_group_id to also be set."
  type        = string
  default     = ""
}

variable "memcached_02_peer_vpc_id" {
  description = "The peer's own VPC ID for the second memcached instance — only meaningful when memcached_02_peer_id is set."
  type        = string
  default     = ""
}

variable "memcached_02_peer_subnet_id" {
  description = "The peer's own subnet ID for the second memcached instance — only meaningful when memcached_02_peer_id is set."
  type        = string
  default     = ""
}

variable "memcached_02_peer_security_group_id" {
  description = "The peer's own security group ID for the second memcached instance — required when memcached_02_peer_id is set."
  type        = string
  default     = ""
}

# keystone -- fixed 2-node instance-group (genuinely identical config, no
# per-node role — see main.tf's own comment). Only the second instance
# ("02") gets a peer var-set, matching load-balanced-web's own reference
# pattern.
variable "keystone_02_peer_id" {
  description = "ID of a paired remote peer to place the second keystone instance on instead of the local host. Requires keystone_02_peer_vpc_id/keystone_02_peer_subnet_id/keystone_02_peer_security_group_id to also be set."
  type        = string
  default     = ""
}

variable "keystone_02_peer_vpc_id" {
  description = "The peer's own VPC ID for the second keystone instance — only meaningful when keystone_02_peer_id is set."
  type        = string
  default     = ""
}

variable "keystone_02_peer_subnet_id" {
  description = "The peer's own subnet ID for the second keystone instance — only meaningful when keystone_02_peer_id is set."
  type        = string
  default     = ""
}

variable "keystone_02_peer_security_group_id" {
  description = "The peer's own security group ID for the second keystone instance — required when keystone_02_peer_id is set."
  type        = string
  default     = ""
}

# backend -- fixed 2-node instance-group (genuinely identical config, no
# per-node role — see main.tf's own comment). Only the second instance
# ("02") gets a peer var-set, matching load-balanced-web's own reference
# pattern.
variable "backend_02_peer_id" {
  description = "ID of a paired remote peer to place the second backend instance on instead of the local host. Requires backend_02_peer_vpc_id/backend_02_peer_subnet_id/backend_02_peer_security_group_id to also be set."
  type        = string
  default     = ""
}

variable "backend_02_peer_vpc_id" {
  description = "The peer's own VPC ID for the second backend instance — only meaningful when backend_02_peer_id is set."
  type        = string
  default     = ""
}

variable "backend_02_peer_subnet_id" {
  description = "The peer's own subnet ID for the second backend instance — only meaningful when backend_02_peer_id is set."
  type        = string
  default     = ""
}

variable "backend_02_peer_security_group_id" {
  description = "The peer's own security group ID for the second backend instance — required when backend_02_peer_id is set."
  type        = string
  default     = ""
}

# rabbitmq -- clustered: seed node "a" (anchor, stays local-only) + joiner
# nodes "b"/"c", each independently placeable.
variable "rabbitmq_b_peer_id" {
  description = "ID of a paired remote peer to place RabbitMQ joiner node b on instead of the local host. Requires rabbitmq_b_peer_vpc_id/rabbitmq_b_peer_subnet_id/rabbitmq_b_peer_security_group_id to also be set."
  type        = string
  default     = ""
}

variable "rabbitmq_b_peer_vpc_id" {
  description = "The peer's own VPC ID for RabbitMQ joiner node b — only meaningful when rabbitmq_b_peer_id is set."
  type        = string
  default     = ""
}

variable "rabbitmq_b_peer_subnet_id" {
  description = "The peer's own subnet ID for RabbitMQ joiner node b — only meaningful when rabbitmq_b_peer_id is set."
  type        = string
  default     = ""
}

variable "rabbitmq_b_peer_security_group_id" {
  description = "The peer's own security group ID for RabbitMQ joiner node b — required when rabbitmq_b_peer_id is set."
  type        = string
  default     = ""
}

variable "rabbitmq_c_peer_id" {
  description = "ID of a paired remote peer to place RabbitMQ joiner node c on instead of the local host. Requires rabbitmq_c_peer_vpc_id/rabbitmq_c_peer_subnet_id/rabbitmq_c_peer_security_group_id to also be set."
  type        = string
  default     = ""
}

variable "rabbitmq_c_peer_vpc_id" {
  description = "The peer's own VPC ID for RabbitMQ joiner node c — only meaningful when rabbitmq_c_peer_id is set."
  type        = string
  default     = ""
}

variable "rabbitmq_c_peer_subnet_id" {
  description = "The peer's own subnet ID for RabbitMQ joiner node c — only meaningful when rabbitmq_c_peer_id is set."
  type        = string
  default     = ""
}

variable "rabbitmq_c_peer_security_group_id" {
  description = "The peer's own security group ID for RabbitMQ joiner node c — required when rabbitmq_c_peer_id is set."
  type        = string
  default     = ""
}

