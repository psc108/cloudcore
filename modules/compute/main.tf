resource "cloudcore_instance" "this" {
  for_each = var.enabled ? var.instances : {}

  name               = "${var.project}-${var.environment}-${each.key}"
  image_id           = each.value.image_id
  flavor             = each.value.flavor
  vpc_id             = each.value.vpc_id
  subnet_id          = each.value.subnet_id
  security_group_ids = each.value.security_group_ids
  user_data          = each.value.user_data
  tags               = merge(local.common_tags, var.tags, { Name = "${var.project}-${var.environment}-${each.key}" })
}
