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
  description = "Place the WHOLE group on a paired remote host instead of the local one (see the cloudcore_peers data source) — scalar, not per-instance: every instance in the group lands on the same host. Per-instance mixed placement within one group isn't supported; use separate cloudcore_instance/module.compute resources with different peer_id values if that's needed."
  type        = string
  default     = null
}
