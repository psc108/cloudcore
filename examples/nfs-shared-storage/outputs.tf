output "vpc_ids" {
  description = "VPC IDs keyed by resource key."
  value       = module.vpc.vpc_ids_by_key
}

output "nfs_server_ids" {
  description = "NFS server IDs keyed by resource key."
  value       = module.nfs.nfs_server_ids_by_key
}

output "nfs_private_ips" {
  description = "NFS server private IPs keyed by resource key."
  value       = module.nfs.private_ips_by_key
}

output "app_instance_ids" {
  description = "App instance IDs keyed by resource key."
  value       = module.app.instance_ids_by_key
}

output "app_private_ips" {
  description = "App instance private IPs keyed by resource key."
  value       = module.app.private_ips_by_key
}
