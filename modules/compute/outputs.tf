output "instance_ids_by_key" {
  description = "Instance IDs keyed by the caller-supplied key from var.instances."
  value       = { for k, v in cloudcore_instance.this : k => v.id }
}

output "private_ips_by_key" {
  description = "Private IP addresses keyed by the caller-supplied key from var.instances."
  value       = { for k, v in cloudcore_instance.this : k => v.private_ip }
}

output "public_ips_by_key" {
  description = "Public IP addresses keyed by the caller-supplied key from var.instances."
  value       = { for k, v in cloudcore_instance.this : k => v.public_ip }
}
