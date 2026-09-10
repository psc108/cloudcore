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

output "mysql_status_url" {
  description = "Live MySQL cluster status page — proves the cluster is real through the full request path, not a direct DB connection."
  value       = "http://${var.vip_address}/mysql-status.html"
}

output "mysql_bootstrap_ip" {
  description = "The bootstrap MySQL node's private IP (server-id 1)."
  value       = module.mysql_bootstrap.private_ips_by_key
}

output "mysql_replica_ips" {
  description = "The two joiner MySQL nodes' private IPs (server-id 2 and 3), keyed by role."
  value       = module.mysql_replicas.private_ips_by_key
}

output "proxysql_private_ips" {
  description = "ProxySQL instance private IPs keyed by two-digit index."
  value       = module.proxysql.private_ips_by_key
}

output "proxysql_ssh_commands" {
  description = "SSH commands for the ProxySQL instances, keyed by two-digit index."
  value       = module.proxysql.ssh_commands_by_key
}

output "keystone_private_ips" {
  description = "Keystone instance private IPs keyed by two-digit index."
  value       = module.keystone.private_ips_by_key
}

output "keystone_ssh_commands" {
  description = "SSH commands for the Keystone instances, keyed by two-digit index."
  value       = module.keystone.ssh_commands_by_key
}

output "memcached_private_ips" {
  description = "memcached instance private IPs keyed by two-digit index."
  value       = module.memcached.private_ips_by_key
}

output "keystone_status_url" {
  description = "Live Keystone status page — proves token issuance and cross-node validation through the full real path."
  value       = "http://${var.vip_address}/keystone-status.html"
}
