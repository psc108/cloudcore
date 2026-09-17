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

variable "instance_flavor" {
  description = "Compute flavor for the web instance."
  type        = string
  default     = "standard.small"
}

variable "dns_zone" {
  description = "DNS zone name to create (e.g. myapp.cloudcore.local)."
  type        = string
  default     = "example.cloudcore.local"
}

# Leave blank to keep the web instance local (the default -- unchanged
# behavior). To place it on a paired remote host instead, set peer_id
# (see the Dashboard's Peers section, or the cloudcore_peers data
# source, for available hosts) AND peer_vpc_id/peer_subnet_id to THAT
# peer's own vpc_id/subnet_id. Those two aren't optional once peer_id
# is set: a remote peer has its own separate VPC/subnet catalogue, not
# this build's local one (see haFullStack-LLD.md §13).
variable "peer_id" {
  description = "ID of a paired remote peer to place the web instance on instead of the local host. Requires peer_vpc_id/peer_subnet_id/peer_security_group_id to also be set."
  type        = string
  default     = ""
}

variable "peer_vpc_id" {
  description = "The peer's own VPC ID for the web instance — only meaningful when peer_id is set."
  type        = string
  default     = ""
}

variable "peer_subnet_id" {
  description = "The peer's own subnet ID for the web instance — only meaningful when peer_id is set."
  type        = string
  default     = ""
}

# Same reasoning as peer_vpc_id/subnet_id: a peer's own security group
# catalogue is separate from this build's local one, and the API now
# rejects a security_group_ids entry it can't resolve locally at
# instance-create time (previously an unresolved id was silently
# accepted and produced a DROP-only iptables chain, leaving the
# instance completely unreachable with no error anywhere — see
# haFullStack-Findings-Log.md). Required whenever peer_id is set.
variable "peer_security_group_id" {
  description = "The peer's own security group ID for the web instance — required when peer_id is set."
  type        = string
  default     = ""
}
