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

variable "nfs_flavor" {
  description = "Compute flavor for the NFS server hosting the local apt repo and pinned-artifact cache (haFullStack-LLD.md §6)."
  type        = string
  default     = "standard.small"
}

variable "nfs_disk_gb" {
  description = "Data disk size for the NFS server. The apt repo snapshot (full package closure across every tier) plus the pinned .deb artifacts fit comfortably well under this default."
  type        = number
  default     = 20
}

variable "build_repo_now" {
  description = "Create the one-shot repo-builder instance this apply. Build once (leave true), confirm the NFS shares are populated, then set false on a later apply to tear the builder down while keeping the NFS server and its already-built shares — matches the 'download once, refresh only on OS bump or security patch' policy (haFullStack-LLD.md §6), not a continuously-reconciled resource."
  type        = bool
  default     = true
}

variable "repo_builder_flavor" {
  description = "Compute flavor for the one-shot repo-builder instance. Needs enough disk/memory headroom to apt-get download the full package closure before copying it to the NFS mount."
  type        = string
  default     = "standard.medium"
}
