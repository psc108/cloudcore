output "vpc_ids" {
  description = "VPC IDs keyed by resource key."
  value       = module.vpc.vpc_ids_by_key
}

output "vpc_cidr_blocks" {
  description = "VPC CIDR blocks keyed by resource key."
  value       = module.vpc.vpc_cidr_blocks_by_key
}

output "subnet_ids" {
  description = "Subnet IDs keyed by subnet key."
  value       = module.subnets.subnet_ids_by_key
}

output "subnet_cidrs" {
  description = "Resolved subnet CIDR blocks keyed by subnet key."
  value       = module.subnets.subnet_cidr_blocks_by_key
}
