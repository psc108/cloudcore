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
  description = "Compute flavor for the web instances."
  type        = string
  default     = "standard.small"
}

variable "instance_count" {
  description = "Number of web instances in the group."
  type        = number
  default     = 2
}

variable "lb_port" {
  description = <<-EOT
    Host port the load balancer listens on (loopback-only, same access
    model as everything else in CloudCore — reachable from 127.0.0.1 on
    the CloudCore host itself). This is the cloudcore_lb_listener's own
    bind port, NOT the cloudcore_load_balancer resource's auto-assigned
    listen_port — creating any listener replaces that default frontend
    outright, so this is what actually matters. Chosen outside every
    other port range this platform auto-allocates (SSH 12200-12299,
    HTTP hostfwd 12800-12899, NFS SSH 12300-12399, LB auto-allocation
    8200-8299) and outside ghidra-workstation's own fixed 8600 — change
    it if it collides with something else already running on your host.
  EOT
  type        = number
  default     = 8601
}

# Leave blank to keep every web instance local (the default -- the
# resulting placement_overrides below is empty, a no-op). To put the
# second web instance on a paired remote host instead -- real
# cross-host clustering within this one tier -- set web_02_peer_id
# (see the Dashboard's Peers section, or the cloudcore_peers data
# source, for available hosts) AND web_02_peer_vpc_id/
# web_02_peer_subnet_id to THAT peer's own vpc_id/subnet_id. Those two
# aren't optional once peer_id is set: a remote peer has its own
# separate VPC/subnet catalogue, not this build's local one (see
# haFullStack-LLD.md §13). Same variable names as this example's own
# Ansible twin (ansible/examples/04-load-balanced-web.yml) for parity
# across both IaC front-ends.
variable "web_02_peer_id" {
  description = "ID of a paired remote peer to place the second web instance on instead of the local host. Requires web_02_peer_vpc_id/web_02_peer_subnet_id to also be set."
  type        = string
  default     = ""
}

variable "web_02_peer_vpc_id" {
  description = "The peer's own VPC ID for the second web instance — only meaningful when web_02_peer_id is set."
  type        = string
  default     = ""
}

variable "web_02_peer_subnet_id" {
  description = "The peer's own subnet ID for the second web instance — only meaningful when web_02_peer_id is set."
  type        = string
  default     = ""
}

# Same reasoning as web_02_peer_vpc_id/subnet_id: a peer's own security
# group catalogue is separate from this build's local one, and the API
# now rejects a security_group_ids entry it can't resolve locally at
# instance-create time (previously an unresolved id was silently
# accepted and produced a DROP-only iptables chain, leaving the
# instance completely unreachable with no error anywhere — see
# haFullStack-Findings-Log.md). Required whenever web_02_peer_id is set.
variable "web_02_peer_security_group_id" {
  description = "The peer's own security group ID for the second web instance — required when web_02_peer_id is set."
  type        = string
  default     = ""
}
