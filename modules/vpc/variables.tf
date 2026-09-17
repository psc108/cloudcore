variable "enabled" {
  type    = bool
  default = true
}

variable "environment" {
  type = string
}

variable "project" {
  type = string
}

variable "owner" {
  type = string
}

variable "tags" {
  type    = map(string)
  default = {}
}

variable "vpcs" {
  description = <<-EOT
    Map of VPC definitions. Keys are stable caller-chosen identifiers.
    Leave peer_id unset (the default) to create this VPC locally,
    unchanged default behavior. Set it to a paired peer's own id (see
    the cloudcore_peers data source) to create it there instead — a
    genuinely separate VPC on that peer's own catalogue, not a
    reference to one that must already exist.
  EOT
  type = map(object({
    cidr_block  = string
    dns_support = optional(bool, true)
    peer_id     = optional(string)
  }))
  default = {}
}
