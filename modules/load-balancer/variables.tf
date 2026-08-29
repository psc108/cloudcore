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

variable "load_balancers" {
  description = "Map of load balancer definitions. Keys are stable caller-chosen identifiers."
  type = map(object({
    type       = string           # "network" (L4) or "application" (L7)
    vpc_id     = string
    subnet_ids = list(string)
    internal   = optional(bool, false)
  }))
  default = {}
}
