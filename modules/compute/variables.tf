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

variable "instances" {
  description = "Map of compute instance definitions. Keys are stable caller-chosen identifiers."
  type = map(object({
    image_id           = string
    flavor             = string
    vpc_id             = string
    subnet_id          = string
    security_group_ids = optional(list(string), [])
    user_data          = optional(string, null)
    peer_id            = optional(string, null) # place this instance on a paired remote host instead of the local one — see the cloudcore_peers data source

    users = optional(list(object({
      username      = string
      sudo          = optional(bool, false)
      ssh_keys      = optional(list(string), [])
      password_hash = optional(string, null)
    })), [])
  }))
  default = {}
}
