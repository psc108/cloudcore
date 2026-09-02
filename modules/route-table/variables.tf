variable "enabled" {
  description = "Master create/destroy switch for this module."
  type        = bool
  default     = true
}

variable "project" {
  description = "Project name used in naming and tags."
  type        = string
}

variable "environment" {
  description = "Deployment environment used in naming and tags."
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
  description = "ID of the VPC this route table belongs to."
  type        = string
}

variable "subnet_ids" {
  description = "Subnet IDs to associate with this route table."
  type        = list(string)
  default     = []
}

variable "routes" {
  description = <<-EOT
    List of routes to add to the table.
    - destination_cidr : destination CIDR (e.g. "0.0.0.0/0")
    - gateway_id       : target gateway ID or "local" for VPC-local routing
  EOT
  type = list(object({
    destination_cidr = string
    gateway_id       = string
  }))
  default = []
}

variable "name_suffix" {
  description = "Optional suffix appended to the route table name to distinguish multiple tables in the same VPC."
  type        = string
  default     = "main"
}
