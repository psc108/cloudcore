output "lb_ids_by_key" {
  description = "Load balancer IDs keyed by the caller-supplied key from var.load_balancers."
  value       = { for k, v in cloudcore_load_balancer.this : k => v.id }
}

output "lb_dns_names_by_key" {
  description = "Load balancer DNS names keyed by the caller-supplied key from var.load_balancers."
  value       = { for k, v in cloudcore_load_balancer.this : k => v.dns_name }
}

output "lb_listen_ports_by_key" {
  description = "Host port each load balancer listens on, keyed by the caller-supplied key."
  value       = { for k, v in cloudcore_load_balancer.this : k => v.listen_port }
}

output "lb_endpoints_by_key" {
  description = "Usable HTTP endpoint for each load balancer (http://127.0.0.1:<port>), keyed by the caller-supplied key."
  value       = { for k, v in cloudcore_load_balancer.this : k => "http://127.0.0.1:${v.listen_port}" }
}
