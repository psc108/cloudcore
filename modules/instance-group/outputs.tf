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

output "public_ips_by_key" {
  description = "Public IP addresses keyed by two-digit index (127.0.0.1 for SLIRP instances)."
  value       = { for k, v in cloudcore_instance.this : k => v.public_ip }
}

output "ssh_ports_by_key" {
  description = "Host SSH port forwarded to each instance, keyed by two-digit index."
  value       = { for k, v in cloudcore_instance.this : k => v.ssh_port }
}

output "ssh_commands_by_key" {
  description = "Full SSH commands keyed by two-digit index. Run directly: $(tofu output -raw ssh_commands[\"01\"])"
  value       = { for k, v in cloudcore_instance.this : k => "ssh ${v.ssh_endpoint}" }
}
