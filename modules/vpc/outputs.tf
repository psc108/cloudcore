output "vpc_ids_by_key" {
  description = "VPC IDs keyed by the caller-supplied key from var.vpcs."
  value       = { for k, v in cloudcore_vpc.this : k => v.id }
}

output "vpc_cidr_blocks_by_key" {
  description = "VPC CIDR blocks keyed by the caller-supplied key from var.vpcs."
  value       = { for k, v in cloudcore_vpc.this : k => v.cidr_block }
}

output "host_hostnames_by_key" {
  description = "Which physical host each VPC actually landed on, keyed by the caller-supplied key — empty string for the local host, the peer's own hostname otherwise."
  value       = { for k, v in cloudcore_vpc.this : k => v.host_hostname }
}
