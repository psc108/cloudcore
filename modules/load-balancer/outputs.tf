output "lb_ids_by_key" {
  description = "Load balancer IDs keyed by the caller-supplied key from var.load_balancers."
  value       = { for k, v in cloudcore_load_balancer.this : k => v.id }
}

output "lb_dns_names_by_key" {
  description = "Load balancer DNS names keyed by the caller-supplied key from var.load_balancers."
  value       = { for k, v in cloudcore_load_balancer.this : k => v.dns_name }
}
