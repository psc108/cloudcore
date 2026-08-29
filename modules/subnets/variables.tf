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
  description = "ID of the VPC these subnets belong to."
  type        = string
}

variable "vpc_cidr_block" {
  description = "CIDR block of the VPC. Required when any subnet uses newbits/netnum allocation."
  type        = string
  default     = null
}

variable "subnets" {
  description = <<-EOT
    Map of subnet definitions. Keys are caller-chosen stable identifiers (e.g. "web-a", "db-b").
    - cidr_block : explicit CIDR. Mutually exclusive with newbits+netnum.
    - newbits    : bits to extend the VPC CIDR prefix by (cidrsubnet). Mutually exclusive with cidr_block.
    - netnum     : subnet number within the extended prefix (cidrsubnet). Mutually exclusive with cidr_block.
    - zone       : logical zone label (e.g. "a", "b"). Used in naming only — CloudCore has no AZ concept.
    - public     : whether this is a public-facing subnet. Informational tag only.
  EOT
  type = map(object({
    cidr_block = optional(string)
    newbits    = optional(number)
    netnum     = optional(number)
    zone       = optional(string, "a")
    public     = optional(bool, false)
    tags       = optional(map(string), {})
  }))
  default = {}
}
