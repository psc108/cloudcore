# Instance Group module
#
# Models a horizontally-scaled set of identical instances — the CloudCore
# equivalent of an Auto Scaling Group. All instances share the same image,
# flavor, subnet, security groups, and user data.
#
# Instances are named: <project>-<environment>-<name>-01, -02, etc.
# Scaling up adds instances; scaling down removes the highest-numbered ones.

resource "cloudcore_instance" "this" {
  for_each = local.instance_keys

  name               = "${var.project}-${var.environment}-${var.name}-${each.key}"
  image_id           = var.image_id
  flavor             = var.flavor
  vpc_id             = var.vpc_id
  subnet_id          = var.subnet_id
  security_group_ids = var.security_group_ids
  user_data          = var.user_data
  tags               = merge(local.common_tags, var.tags, {
    Name          = "${var.project}-${var.environment}-${var.name}-${each.key}"
    InstanceGroup = var.name
  })
}
