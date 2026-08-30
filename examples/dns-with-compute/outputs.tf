output "vpc_ids" {
  description = "VPC IDs keyed by resource key."
  value       = module.vpc.vpc_ids_by_key
}

output "subnet_ids" {
  description = "Subnet IDs keyed by subnet key."
  value       = module.subnets.subnet_ids_by_key
}

output "private_ips" {
  description = "Instance private IPs keyed by resource key."
  value       = module.compute.private_ips_by_key
}

output "dns_zone_names" {
  description = "DNS zone names keyed by zone key."
  value       = module.dns_zone.zone_names_by_key
}

output "dns_record_values" {
  description = "DNS record values keyed by record key."
  value       = module.dns_records.record_values_by_key
}
