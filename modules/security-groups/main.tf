resource "cloudcore_security_group" "this" {
  for_each = var.enabled ? var.security_groups : {}

  name        = "${var.project}-${var.environment}-${each.key}"
  description = each.value.description
  vpc_id      = var.vpc_id
  tags        = merge(local.common_tags, var.tags, { Name = "${var.project}-${var.environment}-${each.key}" })

  ingress_rules = [for k, r in each.value.ingress_rules : {
    protocol    = r.ip_protocol
    from_port   = lookup(r, "from_port", null)
    to_port     = lookup(r, "to_port", null)
    cidr        = r.cidr
    description = lookup(r, "description", "")
  }]

  egress_rules = [for k, r in each.value.egress_rules : {
    protocol    = r.ip_protocol
    from_port   = lookup(r, "from_port", null)
    to_port     = lookup(r, "to_port", null)
    cidr        = r.cidr
    description = lookup(r, "description", "")
  }]
}
