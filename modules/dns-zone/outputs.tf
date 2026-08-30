output "zone_names_by_key" {
  description = "Zone names keyed by the caller-supplied key from var.zones."
  value       = { for k, v in cloudcore_dns_zone.this : k => v.name }
}

output "zone_created_at_by_key" {
  description = "Zone creation timestamps keyed by the caller-supplied key from var.zones."
  value       = { for k, v in cloudcore_dns_zone.this : k => v.created_at }
}
