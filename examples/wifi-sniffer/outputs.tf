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
  value       = module.sniffer.private_ips_by_key
}

output "desktop_url" {
  description = <<-EOT
    Open this directly in a browser — right away, no need to wait. For
    about the first couple of minutes (driver build + package installs)
    it may fail to load; after that it shows a "still building" page,
    then switches to the real Kismet dashboard automatically once ready.
    Same URL throughout. First visit to the real dashboard prompts you
    to set your own admin username/password right there in the browser
    (Kismet's own first-run setup, confirmed directly — no
    auto-generated credential to retrieve, no SSH needed for this part).
  EOT
  value       = "http://127.0.0.1:${var.lb_port}/"
}

output "ssh_commands" {
  description = <<-EOT
    SSH commands for the instance, keyed by two-digit index. Deeper
    analysis tools are all installed here: aircrack-ng suite, tshark,
    hcxtools/hcxdumptool (WPA handshake capture/conversion), tcpdump.
    Capture files Kismet writes land in /home/ubuntu/kismet-logs/.
  EOT
  value       = module.sniffer.ssh_commands_by_key
}

output "direct_url_via_ssh_tunnel" {
  description = <<-EOT
    Fallback access that bypasses the load balancer entirely — useful if
    you ever need to debug the instance independent of it. Run:
    ssh -p <port> -L 8080:localhost:80 ubuntu@127.0.0.1
    (Ctrl-C to close), then open http://127.0.0.1:8080/.
  EOT
  value = {
    for k, port in module.sniffer.ssh_ports_by_key :
    k => "ssh -p ${port} -L 8080:localhost:80 ubuntu@127.0.0.1"
  }
}
