output "id" {
  description = "Internet gateway ID, or empty string when disabled."
  value       = var.enabled ? cloudcore_internet_gateway.this[0].id : ""
}

output "internet_gateway" {
  description = "Full internet gateway resource object, or null when disabled."
  value       = var.enabled ? cloudcore_internet_gateway.this[0] : null
}
