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

output "frontend_private_ips" {
  description = "Frontend instance private IPs keyed by two-digit index — these are the addresses baked into each NGINX node's upstream block."
  value       = module.frontend.private_ips_by_key
}

output "frontend_ssh_commands" {
  description = "SSH commands for the frontend instances, keyed by two-digit index."
  value       = module.frontend.ssh_commands_by_key
}

output "nginx_private_ips" {
  description = "NGINX/Keepalived node private IPs keyed by role name (nginx-a = MASTER, nginx-b = BACKUP)."
  value       = module.nginx.private_ips_by_key
}

output "vip_address" {
  description = "The floating VIP Keepalived moves between the two NGINX nodes."
  value       = var.vip_address
}

output "lb_url" {
  description = "Client-facing URL once the VIP is up. Reachable from the CloudCore host itself, matching this platform's loopback-first access model."
  value       = "http://${var.vip_address}/"
}
