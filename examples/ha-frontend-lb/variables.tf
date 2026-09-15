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

variable "logging_flavor" {
  description = "Compute flavor for the single centralized-logging node (Loki + Grafana) — a Lab debugging aid, same tier as this stack's other non-DB support nodes (keystone_flavor, rabbitmq_flavor)."
  type        = string
  default     = "standard.small"
}

