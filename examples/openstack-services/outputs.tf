output "vpc_id" {
  description = "VPC ID."
  value       = module.vpc.vpc_ids_by_key[local.vpc_key]
}

output "frontend_ip" {
  description = "Frontend instance private IP."
  value       = module.frontend.private_ips_by_key
}

output "backend_ip" {
  description = "Backend instance private IP."
  value       = module.backend.private_ips_by_key
}

output "mysql_ip" {
  description = "MySQL instance private IP."
  value       = module.mysql.private_ips_by_key
}

output "keystone_ip" {
  description = "Keystone instance private IP."
  value       = module.keystone.private_ips_by_key
}

output "rabbitmq_ip" {
  description = "RabbitMQ instance private IP."
  value       = module.rabbitmq.private_ips_by_key
}

output "admin_nfs_id" {
  description = "Admin/NFS server ID."
  value       = module.admin_nfs.nfs_server_ids_by_key
}

output "admin_nfs_ip" {
  description = "Admin/NFS server private IP."
  value       = module.admin_nfs.private_ips_by_key
}

output "frontend_lb_dns" {
  description = "Frontend load balancer DNS name."
  value       = module.lb.lb_dns_names_by_key["frontend-lb${local.sfx}"]
}

output "backend_lb_dns" {
  description = "Backend load balancer DNS name."
  value       = module.lb.lb_dns_names_by_key["backend-lb${local.sfx}"]
}
