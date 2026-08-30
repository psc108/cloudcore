resource "cloudcore_security_group" "this" {
  for_each = var.enabled ? var.security_groups : {}

  name        = "${var.project}-${var.environment}-${each.key}"
  description = each.value.description
  vpc_id      = var.vpc_id
  tags        = merge(local.common_tags, var.tags, { Name = "${var.project}-${var.environment}-${each.key}" })

  dynamic "ingress_rules" {
    for_each = each.value.ingress_rules
    content {
      protocol    = ingress_rules.value.ip_protocol
      from_port   = ingress_rules.value.from_port
      to_port     = ingress_rules.value.to_port
      cidr        = ingress_rules.value.cidr
      description = lookup(ingress_rules.value, "description", "")
    }
  }

  dynamic "egress_rules" {
    for_each = each.value.egress_rules
    content {
      protocol    = egress_rules.value.ip_protocol
      from_port   = egress_rules.value.from_port
      to_port     = egress_rules.value.to_port
      cidr        = egress_rules.value.cidr
      description = lookup(egress_rules.value, "description", "")
    }
  }
}
