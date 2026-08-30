resource "cloudcore_dns_zone" "this" {
  for_each = var.enabled ? var.zones : {}

  name = each.value.name
}
