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

variable "records" {
  description = <<-EOT
    Map of DNS record definitions. Keys are caller-chosen stable identifiers.
    - zone  : name of the DNS zone this record belongs to.
    - name  : record name (e.g. "www", "@").
    - type  : record type: A, CNAME, or TXT.
    - value : record value (IP address, hostname, or text).
    - ttl   : time-to-live in seconds (default 300).
  EOT
  type = map(object({
    zone  = string
    name  = string
    type  = string
    value = string
    ttl   = optional(number, 300)
  }))
  default = {}
}
