output "nfs_server_ids_by_key" {
  description = "NFS server IDs keyed by the caller-supplied key from var.nfs_servers."
  value       = { for k, v in cloudcore_nfs_server.this : k => v.id }
}

output "private_ips_by_key" {
  description = "NFS server private IP addresses keyed by the caller-supplied key."
  value       = { for k, v in cloudcore_nfs_server.this : k => v.private_ip }
}

output "status_by_key" {
  description = "NFS server status keyed by the caller-supplied key."
  value       = { for k, v in cloudcore_nfs_server.this : k => v.status }
}
