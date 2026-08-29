output "vpc_ids" {
  description = "VPC IDs keyed by resource key."
  value       = module.vpc.vpc_ids_by_key
}

output "private_ips" {
  description = "Instance private IPs keyed by resource key."
  value       = module.compute.private_ips_by_key
}

output "lb_dns" {
  description = "Load balancer DNS names keyed by resource key."
  value       = module.lb.lb_dns_names_by_key
}
