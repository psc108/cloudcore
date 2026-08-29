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

variable "name" {
  description = "Base name for instances in this group. Instances are named <name>-01, <name>-02, etc."
  type        = string
}

variable "image_id" {
  description = "Image ID for all instances in the group."
  type        = string
  default     = "ubuntu-22.04"
}

variable "flavor" {
  description = "Compute flavor for all instances in the group."
  type        = string
  default     = "standard.small"
}

variable "count_instances" {
  description = "Number of instances to create in the group."
  type        = number
  default     = 2
}

variable "vpc_id" {
  description = "VPC ID for all instances in the group."
  type        = string
}

variable "subnet_id" {
  description = "Subnet ID for all instances in the group."
  type        = string
}

variable "security_group_ids" {
  description = "Security group IDs to attach to all instances in the group."
  type        = list(string)
  default     = []
}

variable "user_data" {
  description = "Cloud-init user data applied to all instances in the group."
  type        = string
  default     = null
}
