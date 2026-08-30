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

variable "nfs_servers" {
  description = <<-EOT
    Map of NFS server definitions. Keys are caller-chosen stable identifiers.
    Servers are named: <project>-<environment>-<key>.

    - vpc_id  : VPC ID the NFS server is attached to.
    - flavor  : compute flavor (default "standard.medium").
    - disk_gb : storage disk size in GiB (default 20).
    - shares  : list of NFS export definitions.
      - name    : export name (becomes /exports/<name>).
      - clients : client access spec (default "vpc" — all VPC hosts).
  EOT
  type = map(object({
    vpc_id  = string
    flavor  = optional(string, "standard.medium")
    disk_gb = optional(number, 20)
    shares = optional(list(object({
      name    = string
      clients = optional(string, "vpc")
    })), [])
  }))
  default = {}
}
