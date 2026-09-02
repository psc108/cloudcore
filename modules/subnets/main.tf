# Subnets module
#
# Creates cloudcore_subnet resources within a VPC. CIDR blocks can be
# specified explicitly (cidr_block) or derived via cidrsubnet() using
# the newbits+netnum pair. The module outputs match the old fake-ID
# interface so callers require no changes.

resource "cloudcore_subnet" "this" {
  for_each = var.enabled ? local.resolved_subnets : {}

  name       = "${var.project}-${var.environment}-${each.key}"
  vpc_id     = var.vpc_id
  cidr_block = each.value.cidr_block
  public     = each.value.public
  zone       = each.value.zone
  tags       = merge(local.common_tags, each.value.tags)
}
