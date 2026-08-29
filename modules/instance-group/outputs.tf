output "instance_ids_by_key" {
  description = "Instance IDs keyed by two-digit index (\"01\", \"02\", ...)."
  value       = { for k, v in cloudcore_instance.this : k => v.id }
}

output "private_ips_by_key" {
  description = "Private IP addresses keyed by two-digit index."
  value       = { for k, v in cloudcore_instance.this : k => v.private_ip }
}

output "private_ips_list" {
  description = "All private IPs as a flat list — useful for LB backend registration."
  value       = [for v in cloudcore_instance.this : v.private_ip]
}
