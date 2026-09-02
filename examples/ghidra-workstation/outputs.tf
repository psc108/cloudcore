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
  value       = module.ghidra.private_ips_by_key
}

output "desktop_url" {
  description = <<-EOT
    Open this directly in a browser — right away, no need to wait. For
    about the first 90 seconds (while the instance is still booting) it
    may fail to load; after that it shows a "still building" page, then
    switches to the real desktop automatically once ready (a few minutes
    total). Same URL throughout — no need to refresh, retry, or change
    anything. Enter the vnc_password you set when prompted.
  EOT
  value       = "http://127.0.0.1:${var.lb_port}/vnc.html"
}

output "ssh_commands" {
  description = "SSH commands for the workstation, keyed by two-digit index. Use this to log in, upload samples (scp), or download Ghidra project files."
  value       = module.ghidra.ssh_commands_by_key
}

output "vnc_tunnel_commands" {
  description = <<-EOT
    Fallback graphical access that bypasses the load balancer entirely —
    useful if you ever need to debug the desktop independently of it.
    Run one of these, then open http://127.0.0.1:6080/vnc.html. Ctrl-C
    closes the tunnel.
  EOT
  value = {
    for k, port in module.ghidra.ssh_ports_by_key :
    k => "ssh -p ${port} -L 6080:localhost:80 ubuntu@127.0.0.1"
  }
}
