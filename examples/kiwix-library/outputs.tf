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
  description = "Instance private IPs keyed by two-digit index."
  value       = module.kiwix.private_ips_by_key
}

output "desktop_url" {
  description = <<-EOT
    Open this directly in a browser — right away, no need to wait. For
    about the first minute (while the instance is still booting) it may
    fail to load; after that it shows a "still building" page, then
    switches to the real Kiwix library homepage automatically once ready.
    Same URL throughout — no need to refresh, retry, or change anything.
    No login, no password.
  EOT
  value       = module.lb.lb_endpoints_by_key["kiwix${local.sfx}"]
}

output "ssh_commands" {
  description = "SSH commands for the instance, keyed by two-digit index. Use this to add more ZIM files to /opt/kiwix/data/ later, or manage the box directly."
  value       = module.kiwix.ssh_commands_by_key
}

output "direct_url_via_ssh_tunnel" {
  description = <<-EOT
    Fallback access that bypasses the load balancer entirely — useful if
    you ever need to debug the instance independent of it. Run:
    ssh -p <port> -L 8080:localhost:80 ubuntu@127.0.0.1
    (Ctrl-C to close), then open http://127.0.0.1:8080/.
  EOT
  value = {
    for k, port in module.kiwix.ssh_ports_by_key :
    k => "ssh -p ${port} -L 8080:localhost:80 ubuntu@127.0.0.1"
  }
}
