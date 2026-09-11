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
  description = "Frontend instance private IPs keyed by two-digit index — these are the addresses baked into the ProxySQL/NGINX nodes' upstream block."
  value       = module.frontend.private_ips_by_key
}

output "frontend_ssh_commands" {
  description = "SSH commands for the frontend instances, keyed by two-digit index."
  value       = module.frontend.ssh_commands_by_key
}

output "vip_address" {
  description = "The floating VIP Keepalived moves between the two ProxySQL/NGINX nodes."
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
  description = "ProxySQL/NGINX node private IPs keyed by role name (proxysql-a = Keepalived MASTER, proxysql-b = BACKUP). No ssh_commands output exists for this tier — modules/compute (used here, same as mysql_bootstrap/ca) doesn't expose ssh_endpoint, matching the existing convention for those tiers."
  value       = module.proxysql.private_ips_by_key
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

output "rabbitmq_seed_ip" {
  description = "The seed RabbitMQ node's private IP."
  value       = module.rabbitmq_seed.private_ips_by_key
}

output "rabbitmq_joiner_ips" {
  description = "The two joiner RabbitMQ nodes' private IPs, keyed by role."
  value       = module.rabbitmq_joiners.private_ips_by_key
}

output "rabbitmq_status_url" {
  description = "Live RabbitMQ cluster status page — proves cluster membership and a real publish/consume round-trip through the full real path."
  value       = "http://${var.vip_address}/rabbitmq-status.html"
}

output "ca_private_ip" {
  description = "The single step-ca node's private IP (haFullStack-LLD.md §5 — deliberately not HA, a Lab simplification). No ssh_commands output exists for this tier — modules/compute (used here, same as mysql_bootstrap/proxysql) doesn't expose ssh_endpoint, matching the existing convention for those tiers."
  value       = module.ca.private_ips_by_key
}

output "tls_lb_url" {
  description = "Client-facing HTTPS URL, terminated on NGINX itself with a CA-issued cert (haFullStack-LLD.md §5.3.1)."
  value       = "https://${var.vip_address}/"
}

output "tls_status_url" {
  description = "Live TLS/mTLS trust status page — a real handshake against every TLS-enabled listener via the VIP, proving cert-chain trust and mTLS enforcement across the board, not just that a port is open."
  value       = "http://${var.vip_address}/tls-status.html"
}
