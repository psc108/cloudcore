output "vpc_ids" {
  description = "VPC IDs keyed by resource key."
  value       = module.vpc.vpc_ids_by_key
}

output "vpc_cidr_blocks" {
  description = "VPC CIDR blocks keyed by resource key."
  value       = module.vpc.vpc_cidr_blocks_by_key
}
