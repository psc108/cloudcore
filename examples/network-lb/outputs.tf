output "vpc_ids" {
  description = "VPC IDs keyed by resource key."
  value       = module.vpc.vpc_ids_by_key
}

output "private_ips" {
  description = "Instance private IPs keyed by resource key."
  value       = module.compute.private_ips_by_key
}

output "lb_ids" {
  description = "Load balancer IDs keyed by resource key."
  value       = module.lb.lb_ids_by_key
}
