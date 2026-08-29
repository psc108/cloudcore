resource "cloudcore_vpc" "this" {
  for_each = var.enabled ? var.vpcs : {}

  name        = "${var.project}-${var.environment}-${each.key}"
  cidr_block  = each.value.cidr_block
  dns_support = each.value.dns_support
  tags        = merge(local.common_tags, var.tags, { Name = "${var.project}-${var.environment}-${each.key}" })
}
