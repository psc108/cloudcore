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

variable "vpc_id" {
  description = "VPC ID that these security groups belong to."
  type        = string
}

variable "security_groups" {
  description = <<-EOT
    Map of security group definitions. Keys are caller-chosen stable identifiers.
    Groups are named: <project>-<environment>-<key>.

    - description   : human-readable description.
    - ingress_rules : map of ingress rule definitions.
    - egress_rules  : map of egress rule definitions.

    Rule fields:
    - ip_protocol  : "tcp", "udp", "icmp", or "-1" (all traffic).
    - from_port    : start of port range (omit for protocol "-1").
    - to_port      : end of port range (omit for protocol "-1").
    - cidr         : IPv4 source/destination CIDR. Mutually exclusive with source_sg_id.
    - cidr_ipv6    : IPv6 source/destination CIDR. Can combine with cidr for dual-stack.
    - source_sg_id : source security group ID. Traffic from any instance in that
                     group is allowed. Mutually exclusive with cidr/cidr_ipv6.
    - description  : rule description (optional).
  EOT
  type = map(object({
    description = optional(string, "Managed by OpenTofu")
    ingress_rules = optional(map(object({
      ip_protocol  = string
      from_port    = optional(number)
      to_port      = optional(number)
      cidr         = optional(string)
      cidr_ipv6    = optional(string)
      source_sg_id = optional(string)
      description  = optional(string, "")
    })), {})
    egress_rules = optional(map(object({
      ip_protocol  = string
      from_port    = optional(number)
      to_port      = optional(number)
      cidr         = optional(string)
      cidr_ipv6    = optional(string)
      source_sg_id = optional(string)
      description  = optional(string, "")
    })), {})
  }))
  default = {}
}
