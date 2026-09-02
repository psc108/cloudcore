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
  value       = module.db.private_ips_by_key
}

output "lb_ids" {
  description = "Load balancer IDs keyed by resource key."
  value       = module.lb.lb_ids_by_key
}

output "lb_listen_ports" {
  description = "Host port each load balancer listens on, keyed by resource key."
  value       = module.lb.lb_listen_ports_by_key
}

output "ssh_commands" {
  description = "SSH commands for each instance, keyed by resource key."
  value       = module.db.ssh_commands_by_key
}
