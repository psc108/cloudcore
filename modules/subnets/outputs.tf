output "subnet_ids_by_key" {
  description = "Subnet IDs keyed by the caller-supplied key from var.subnets."
  value       = { for k, v in cloudcore_subnet.this : k => v.id }
}

output "subnet_cidr_blocks_by_key" {
  description = "Resolved subnet CIDR blocks keyed by the caller-supplied key from var.subnets."
  value       = { for k, v in cloudcore_subnet.this : k => v.cidr_block }
}

output "public_subnet_ids" {
  description = "Subnet IDs for subnets marked public = true."
  value       = { for k, v in cloudcore_subnet.this : k => v.id if v.public }
}

output "private_subnet_ids" {
  description = "Subnet IDs for subnets marked public = false."
  value       = { for k, v in cloudcore_subnet.this : k => v.id if !v.public }
}

output "subnets" {
  description = "Full subnet resource objects keyed by caller-supplied key."
  value       = cloudcore_subnet.this
}
