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
  description = "CIDR block for the VPC."
  type        = string
  default     = "10.10.0.0/16"
}

variable "frontend_flavor" {
  description = "Compute flavor for the frontend instance."
  type        = string
  default     = "standard.small"
}

variable "backend_flavor" {
  description = "Compute flavor for backend and keystone instances."
  type        = string
  default     = "standard.medium"
}

variable "data_flavor" {
  description = "Compute flavor for mysql and rabbitmq instances."
  type        = string
  default     = "standard.medium"
}

variable "admin_flavor" {
  description = "Compute flavor for the admin/NFS server."
  type        = string
  default     = "standard.medium"
}

variable "admin_disk_gb" {
  description = "Storage disk size in GiB for the admin/NFS server."
  type        = number
  default     = 50
}

# ── Per-tier peer placement ──────────────────────────────────────────────────
# Each of this example's 5 single-instance tiers (frontend, backend, mysql,
# keystone, rabbitmq) gets its own peer_id/vpc_id/subnet_id/security_group_id
# set, prefixed by that tier's own name to avoid collisions between tiers
# sharing this one file. Leave a tier's <tier>_peer_id blank to keep that
# instance local (the default -- unchanged behavior). To place it on a
# paired remote host instead, set <tier>_peer_id (see the Dashboard's Peers
# section, or the cloudcore_peers data source, for available hosts) AND
# <tier>_peer_vpc_id/<tier>_peer_subnet_id to THAT peer's own vpc_id/
# subnet_id. Those two aren't optional once peer_id is set: a remote peer
# has its own separate VPC/subnet catalogue, not this build's local one
# (see haFullStack-LLD.md §13). <tier>_peer_security_group_id is likewise
# required whenever <tier>_peer_id is set -- a peer's own security group
# catalogue is separate from this build's local one, and the API now
# rejects a security_group_ids entry it can't resolve locally at
# instance-create time (previously an unresolved id was silently accepted
# and produced a DROP-only iptables chain, leaving the instance completely
# unreachable with no error anywhere — see haFullStack-Findings-Log.md).

variable "frontend_peer_id" {
  description = "ID of a paired remote peer to place the frontend instance on instead of the local host. Requires frontend_peer_vpc_id/frontend_peer_subnet_id/frontend_peer_security_group_id to also be set."
  type        = string
  default     = ""
}

variable "frontend_peer_vpc_id" {
  description = "The peer's own VPC ID for the frontend instance — only meaningful when frontend_peer_id is set."
  type        = string
  default     = ""
}

variable "frontend_peer_subnet_id" {
  description = "The peer's own subnet ID for the frontend instance — only meaningful when frontend_peer_id is set."
  type        = string
  default     = ""
}

variable "frontend_peer_security_group_id" {
  description = "The peer's own security group ID for the frontend instance — required when frontend_peer_id is set."
  type        = string
  default     = ""
}

variable "backend_peer_id" {
  description = "ID of a paired remote peer to place the backend instance on instead of the local host. Requires backend_peer_vpc_id/backend_peer_subnet_id/backend_peer_security_group_id to also be set."
  type        = string
  default     = ""
}

variable "backend_peer_vpc_id" {
  description = "The peer's own VPC ID for the backend instance — only meaningful when backend_peer_id is set."
  type        = string
  default     = ""
}

variable "backend_peer_subnet_id" {
  description = "The peer's own subnet ID for the backend instance — only meaningful when backend_peer_id is set."
  type        = string
  default     = ""
}

variable "backend_peer_security_group_id" {
  description = "The peer's own security group ID for the backend instance — required when backend_peer_id is set."
  type        = string
  default     = ""
}

variable "mysql_peer_id" {
  description = "ID of a paired remote peer to place the mysql instance on instead of the local host. Requires mysql_peer_vpc_id/mysql_peer_subnet_id/mysql_peer_security_group_id to also be set."
  type        = string
  default     = ""
}

variable "mysql_peer_vpc_id" {
  description = "The peer's own VPC ID for the mysql instance — only meaningful when mysql_peer_id is set."
  type        = string
  default     = ""
}

variable "mysql_peer_subnet_id" {
  description = "The peer's own subnet ID for the mysql instance — only meaningful when mysql_peer_id is set."
  type        = string
  default     = ""
}

variable "mysql_peer_security_group_id" {
  description = "The peer's own security group ID for the mysql instance — required when mysql_peer_id is set."
  type        = string
  default     = ""
}

variable "keystone_peer_id" {
  description = "ID of a paired remote peer to place the keystone instance on instead of the local host. Requires keystone_peer_vpc_id/keystone_peer_subnet_id/keystone_peer_security_group_id to also be set."
  type        = string
  default     = ""
}

variable "keystone_peer_vpc_id" {
  description = "The peer's own VPC ID for the keystone instance — only meaningful when keystone_peer_id is set."
  type        = string
  default     = ""
}

variable "keystone_peer_subnet_id" {
  description = "The peer's own subnet ID for the keystone instance — only meaningful when keystone_peer_id is set."
  type        = string
  default     = ""
}

variable "keystone_peer_security_group_id" {
  description = "The peer's own security group ID for the keystone instance — required when keystone_peer_id is set."
  type        = string
  default     = ""
}

variable "rabbitmq_peer_id" {
  description = "ID of a paired remote peer to place the rabbitmq instance on instead of the local host. Requires rabbitmq_peer_vpc_id/rabbitmq_peer_subnet_id/rabbitmq_peer_security_group_id to also be set."
  type        = string
  default     = ""
}

variable "rabbitmq_peer_vpc_id" {
  description = "The peer's own VPC ID for the rabbitmq instance — only meaningful when rabbitmq_peer_id is set."
  type        = string
  default     = ""
}

variable "rabbitmq_peer_subnet_id" {
  description = "The peer's own subnet ID for the rabbitmq instance — only meaningful when rabbitmq_peer_id is set."
  type        = string
  default     = ""
}

variable "rabbitmq_peer_security_group_id" {
  description = "The peer's own security group ID for the rabbitmq instance — required when rabbitmq_peer_id is set."
  type        = string
  default     = ""
}
