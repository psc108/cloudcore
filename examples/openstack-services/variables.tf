variable "project" {
  description = "Project name used in resource naming and tags."
  type        = string
  default     = "example"
}

variable "environment" {
  description = "Environment name used in resource naming and tags."
  type        = string
  default     = "dev"
}

variable "owner" {
  description = "Owner tag value applied to all resources."
  type        = string
  default     = "platform-team"
}

variable "suffix" {
  description = "Optional suffix appended to resource names to keep them unique across runs."
  type        = string
  default     = ""
}

variable "cidr_block" {
  description = "CIDR block for the VPC."
  type        = string
  default     = "10.10.0.0/16"
}

variable "frontend_flavor" {
  description = "Compute flavor for the frontend instance."
  type        = string
  default     = "standard.small"
}

variable "backend_flavor" {
  description = "Compute flavor for backend and keystone instances."
  type        = string
  default     = "standard.medium"
}

variable "data_flavor" {
  description = "Compute flavor for mysql and rabbitmq instances."
  type        = string
  default     = "standard.medium"
}

variable "admin_flavor" {
  description = "Compute flavor for the admin/NFS server."
  type        = string
  default     = "standard.medium"
}

variable "admin_disk_gb" {
  description = "Storage disk size in GiB for the admin/NFS server."
  type        = number
  default     = 50
}
