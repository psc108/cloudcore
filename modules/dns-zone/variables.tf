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

variable "zones" {
  description = <<-EOT
    Map of DNS zone definitions. Keys are caller-chosen stable identifiers.
    - name : fully-qualified zone name (e.g. "myapp.cloudcore.local").
  EOT
  type = map(object({
    name = string
  }))
  default = {}
}
