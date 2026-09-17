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
  default     = "10.20.0.0/16"
}

variable "instance_flavor" {
  description = "Compute flavor for the backend instances."
  type        = string
  default     = "standard.medium"
}

variable "instance_count" {
  description = "Number of backend instances in the group."
  type        = number
  default     = 2
}

# Leave blank to keep every db instance local (the default -- the
# resulting placement_overrides below is empty, a no-op). To put the
# second db instance on a paired remote host instead -- real
# cross-host clustering within this one tier -- set db_02_peer_id
# (see the Dashboard's Peers section, or the cloudcore_peers data
# source, for available hosts) AND db_02_peer_vpc_id/
# db_02_peer_subnet_id to THAT peer's own vpc_id/subnet_id. Those two
# aren't optional once peer_id is set: a remote peer has its own
# separate VPC/subnet catalogue, not this build's local one (see
# haFullStack-LLD.md §13).
variable "db_02_peer_id" {
  description = "ID of a paired remote peer to place the second db instance on instead of the local host. Requires db_02_peer_vpc_id/db_02_peer_subnet_id to also be set."
  type        = string
  default     = ""
}

variable "db_02_peer_vpc_id" {
  description = "The peer's own VPC ID for the second db instance — only meaningful when db_02_peer_id is set."
  type        = string
  default     = ""
}

variable "db_02_peer_subnet_id" {
  description = "The peer's own subnet ID for the second db instance — only meaningful when db_02_peer_id is set."
  type        = string
  default     = ""
}

# Same reasoning as db_02_peer_vpc_id/subnet_id: a peer's own security
# group catalogue is separate from this build's local one, and the API
# now rejects a security_group_ids entry it can't resolve locally at
# instance-create time (previously an unresolved id was silently
# accepted and produced a DROP-only iptables chain, leaving the
# instance completely unreachable with no error anywhere — see
# haFullStack-Findings-Log.md). Required whenever db_02_peer_id is set.
variable "db_02_peer_security_group_id" {
  description = "The peer's own security group ID for the second db instance — required when db_02_peer_id is set."
  type        = string
  default     = ""
}
