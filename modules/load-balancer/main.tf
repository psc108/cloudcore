resource "cloudcore_load_balancer" "this" {
  for_each = var.enabled ? var.load_balancers : {}

  name       = "${var.project}-${var.environment}-${each.key}"
  type       = each.value.type
  vpc_id     = each.value.vpc_id
  subnet_ids = each.value.subnet_ids
  internal   = each.value.internal
  tags       = merge(local.common_tags, var.tags, { Name = "${var.project}-${var.environment}-${each.key}" })
}
