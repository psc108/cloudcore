output "subnet_ids_by_key" {
  description = "Stable subnet ID strings keyed by the caller-supplied key from var.subnets."
  value       = local.subnet_ids
}

output "subnet_cidr_blocks_by_key" {
  description = "Resolved subnet CIDR blocks keyed by the caller-supplied key from var.subnets."
  value       = { for k, v in local.resolved_subnets : k => v.cidr_block }
}

output "public_subnet_ids" {
  description = "Subnet IDs for subnets marked public = true."
  value       = { for k, v in local.resolved_subnets : k => local.subnet_ids[k] if v.public }
}

output "private_subnet_ids" {
  description = "Subnet IDs for subnets marked public = false."
  value       = { for k, v in local.resolved_subnets : k => local.subnet_ids[k] if !v.public }
}
