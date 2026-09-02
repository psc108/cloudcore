resource "cloudcore_security_group" "this" {
  for_each = var.enabled ? var.security_groups : {}

  name        = "${var.project}-${var.environment}-${each.key}"
  description = each.value.description
  vpc_id      = var.vpc_id
  tags        = merge(local.common_tags, var.tags, { Name = "${var.project}-${var.environment}-${each.key}" })

  ingress_rules = [for k, r in each.value.ingress_rules : {
    protocol     = r.ip_protocol
    from_port    = r.from_port
    to_port      = r.to_port
    cidr         = r.cidr
    source_sg_id = r.source_sg_id
    description  = r.description
  }]

  egress_rules = [for k, r in each.value.egress_rules : {
    protocol     = r.ip_protocol
    from_port    = r.from_port
    to_port      = r.to_port
    cidr         = r.cidr
    source_sg_id = r.source_sg_id
    description  = r.description
  }]
}
