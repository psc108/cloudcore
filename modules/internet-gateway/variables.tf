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
  description = "ID of the VPC to attach the internet gateway to."
  type        = string
}
