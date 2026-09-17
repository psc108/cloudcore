variable "enabled" {
  description = "Master create/destroy switch for this module."
  type        = bool
  default     = true
}

variable "environment" {
  description = "Deployment environment used in naming and tags."
  type        = string
}

variable "project" {
  description = "Project name used in naming and tags."
  type        = string
}

variable "owner" {
  description = "Owning team or individual. Used in tags."
  type        = string
}

variable "tags" {
  description = "Additional tags merged over the mandatory tag set."
  type        = map(string)
  default     = {}
}

variable "name" {
  description = "Base name for instances in this group. Instances are named <name>-01, <name>-02, etc."
  type        = string
}

variable "image_id" {
  description = "Image ID for all instances in the group."
  type        = string
  default     = "ubuntu-22.04"
}

variable "flavor" {
  description = "Compute flavor for all instances in the group."
  type        = string
  default     = "standard.small"
}

variable "count_instances" {
  description = "Number of instances to create in the group."
  type        = number
  default     = 2
}

variable "vpc_id" {
  description = "VPC ID for all instances in the group."
  type        = string
}

variable "subnet_id" {
  description = "Subnet ID for all instances in the group."
  type        = string
}

variable "security_group_ids" {
  description = "Security group IDs to attach to all instances in the group."
  type        = list(string)
  default     = []
}

variable "usb_device_ids" {
  description = "Host USB device IDs (\"vendor_id:product_id\", from the cloudcore_usb_devices data source) to pass through to every instance in the group. Mutable in place — see cloudcore_instance's usb_device_ids for details."
  type        = list(string)
  default     = []
}

variable "user_data" {
  description = "Cloud-init user data applied to all instances in the group."
  type        = string
  default     = null
}

variable "users" {
  description = "Extra users to create at boot via cloud-init on every instance in the group, each with optional NOPASSWD sudo. The CloudCore inter-instance keypair is automatically added to authorized_keys and installed in ~/.ssh/ for outbound use, same as the default image user."
  type = list(object({
    username      = string
    sudo          = optional(bool, false)
    ssh_keys      = optional(list(string), [])
    password_hash = optional(string, null)
  }))
  default = []
}

variable "peer_id" {
  description = "Default host for every instance in the group (see the cloudcore_peers data source) — null means local. Overridden per-instance by placement_overrides below where a key is present there."
  type        = string
  default     = null
}

variable "placement_overrides" {
  description = <<-EOT
    Per-instance placement overrides, keyed by the same two-digit index
    outputs.tf's own _by_key outputs use ("01", "02", ...) — e.g.
    { "02" = { peer_id = "<peer-id>", vpc_id = "<peer's-vpc-id>", subnet_id = "<peer's-subnet-id>", security_group_ids = ["<peer's-sg-id>"] } }
    puts just the second instance on that peer, while the rest follow
    var.peer_id/var.vpc_id/var.subnet_id/var.security_group_ids (or stay
    local if peer_id is also unset). vpc_id/subnet_id/security_group_ids
    all need overriding together with peer_id, not just peer_id alone: a
    paired peer has its own separate VPC/subnet/security-group catalogue,
    not the group's local one — the id the rest of
    those ids won't exist there. Omitting security_group_ids on a
    peer-placed entry is a hard error at apply time (the API now rejects
    an id it can't resolve locally, rather than silently applying zero
    rules and leaving the instance unreachable — see
    haFullStack-Findings-Log.md). Any field left out of a given entry
    falls back to the group's own default. This is what makes real
    mixed-host clustering possible within one group: e.g. a 3-node tier
    with 2 nodes local and 1 on a paired remote host, to actually
    demonstrate clustering across machines, not just within one.
  EOT
  type = map(object({
    peer_id            = optional(string)
    vpc_id             = optional(string)
    subnet_id          = optional(string)
    security_group_ids = optional(list(string))
  }))
  default = {}
}
