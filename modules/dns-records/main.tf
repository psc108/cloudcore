resource "cloudcore_dns_record" "this" {
  for_each = var.enabled ? var.records : {}

  zone  = each.value.zone
  name  = each.value.name
  type  = each.value.type
  value = each.value.value
  ttl   = each.value.ttl
}
