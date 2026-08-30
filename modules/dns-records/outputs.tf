output "record_keys" {
  description = "Caller-supplied keys for all managed records."
  value       = keys(cloudcore_dns_record.this)
}

output "record_values_by_key" {
  description = "Record values keyed by the caller-supplied key from var.records."
  value       = { for k, v in cloudcore_dns_record.this : k => v.value }
}

output "record_ttls_by_key" {
  description = "Record TTLs keyed by the caller-supplied key from var.records."
  value       = { for k, v in cloudcore_dns_record.this : k => v.ttl }
}
