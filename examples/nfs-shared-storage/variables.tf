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

variable "instance_flavor" {
  description = "Compute flavor for the app instances."
  type        = string
  default     = "standard.small"
}

variable "instance_count" {
  description = "Number of app instances to create."
  type        = number
  default     = 2
}

variable "nfs_flavor" {
  description = "Compute flavor for the NFS server."
  type        = string
  default     = "standard.medium"
}

variable "nfs_disk_gb" {
  description = "Storage disk size in GiB for the NFS server."
  type        = number
  default     = 50
}
