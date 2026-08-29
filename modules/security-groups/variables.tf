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

variable "security_groups" {
  description = <<-EOT
    Map of security group definitions. Keys are caller-chosen stable identifiers.
    CloudCore does not have a security group API resource — groups are logical
    constructs that produce stable ID strings consumed by the compute module's
    security_group_ids field.

    - description   : human-readable description (informational).
    - ingress_rules : map of ingress rule definitions.
    - egress_rules  : map of egress rule definitions.

    Rule fields:
    - description  : rule description.
    - ip_protocol  : "tcp", "udp", "icmp", or "-1" for all traffic.
    - from_port    : start of port range (omit for protocol "-1").
    - to_port      : end of port range (omit for protocol "-1").
    - cidr         : source/destination CIDR (e.g. "0.0.0.0/0").
  EOT
  type = map(object({
    description = optional(string, "Managed by OpenTofu")
    ingress_rules = optional(map(object({
      description = optional(string, "")
      ip_protocol = string
      from_port   = optional(number)
      to_port     = optional(number)
      cidr        = optional(string)
    })), {})
    egress_rules = optional(map(object({
      description = optional(string, "")
      ip_protocol = string
      from_port   = optional(number)
      to_port     = optional(number)
      cidr        = optional(string)
    })), {})
  }))
  default = {}
}
