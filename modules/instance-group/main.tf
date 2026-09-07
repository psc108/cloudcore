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
  # length(...) > 0 ? ... : null, not the bare variable: the provider's
  # stringsToList collapses an empty API response into a null list (to
  # match Terraform's "attribute omitted" convention), so a *configured*
  # empty list here would plan as `[]` but read back as `null` after
  # apply — "provider produced inconsistent result after apply". Passing
  # null explicitly when empty keeps the plan and the post-apply state
  # in agreement.
  security_group_ids = length(var.security_group_ids) > 0 ? var.security_group_ids : null
  usb_device_ids     = length(var.usb_device_ids) > 0 ? var.usb_device_ids : null
  user_data          = var.user_data
  tags               = merge(local.common_tags, var.tags, {
    Name          = "${var.project}-${var.environment}-${var.name}-${each.key}"
    InstanceGroup = var.name
  })
}
