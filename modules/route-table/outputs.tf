output "id" {
  description = "Route table ID, or empty string when disabled."
  value       = var.enabled ? cloudcore_route_table.this[0].id : ""
}

output "route_table" {
  description = "Full route table resource object, or null when disabled."
  value       = var.enabled ? cloudcore_route_table.this[0] : null
}
