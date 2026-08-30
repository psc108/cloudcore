output "security_group_ids_by_key" {
  description = "Security group IDs keyed by the caller-supplied key from var.security_groups."
  value       = { for k, v in cloudcore_security_group.this : k => v.id }
}

output "security_group_ids_list" {
  description = "All security group IDs as a flat list."
  value       = [for v in cloudcore_security_group.this : v.id]
}
