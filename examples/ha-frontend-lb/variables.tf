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
  description = "Virtual IP that Keepalived floats between the two NGINX nodes. Must be a free address in the Lab bridge's real subnet (192.168.100.0/24, set up by api/setup-network.sh) and outside its DHCP range (192.168.100.10-254) — the default was the address used to empirically validate multicast VRRP over ccbr0 before this template was written."
  type        = string
  default     = "192.168.100.5"
}

variable "nginx_flavor" {
  description = "Compute flavor for the two NGINX/Keepalived nodes."
  type        = string
  default     = "standard.small"
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
  description = "Compute flavor for the two ProxySQL nodes."
  type        = string
  default     = "standard.small"
}
