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
  description = "Map of VPC definitions. Keys are stable caller-chosen identifiers."
  type = map(object({
    cidr_block  = string
    dns_support = optional(bool, true)
  }))
  default = {}
}
