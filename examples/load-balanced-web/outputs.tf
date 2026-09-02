output "vpc_ids" {
  description = "VPC IDs keyed by resource key."
  value       = module.vpc.vpc_ids_by_key
}

output "subnet_ids" {
  description = "Subnet IDs keyed by subnet key."
  value       = module.subnets.subnet_ids_by_key
}

output "security_group_ids" {
  description = "Security group IDs keyed by group key."
  value       = module.security_groups.security_group_ids_by_key
}

output "private_ips" {
  description = "Instance private IPs keyed by index."
  value       = module.web.private_ips_by_key
}

output "lb_dns" {
  description = "Load balancer DNS names keyed by resource key."
  value       = module.lb.lb_dns_names_by_key
}

output "lb_endpoints" {
  description = "Usable load balancer endpoints (http://127.0.0.1:<port>) keyed by resource key."
  value       = module.lb.lb_endpoints_by_key
}

output "ssh_commands" {
  description = "SSH commands for each instance, keyed by resource key."
  value       = module.web.ssh_commands_by_key
}
