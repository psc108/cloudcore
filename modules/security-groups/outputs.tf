output "security_group_ids_by_key" {
  description = "Stable security group ID strings keyed by the caller-supplied key from var.security_groups."
  value       = local.security_group_ids
}

output "security_group_ids_list" {
  description = "All security group IDs as a flat list."
  value       = values(local.security_group_ids)
}
