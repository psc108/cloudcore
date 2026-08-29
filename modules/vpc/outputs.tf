output "vpc_ids_by_key" {
  description = "VPC IDs keyed by the caller-supplied key from var.vpcs."
  value       = { for k, v in cloudcore_vpc.this : k => v.id }
}

output "vpc_cidr_blocks_by_key" {
  description = "VPC CIDR blocks keyed by the caller-supplied key from var.vpcs."
  value       = { for k, v in cloudcore_vpc.this : k => v.cidr_block }
}
