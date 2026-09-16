# Instance Group module
#
# Models a horizontally-scaled set of identical instances — the CloudCore
# equivalent of an Auto Scaling Group. All instances share the same image,
# flavor, security groups, and user data. The one thing that CAN differ
# per instance is *where* it lands — peer_id, and (since a remote peer
# has its own separate VPC/subnet catalogue) vpc_id/subnet_id along with
# it, via placement_overrides — everything else is uniform by design.
#
# Instances are named: <project>-<environment>-<name>-01, -02, etc.
# Scaling up adds instances; scaling down removes the highest-numbered ones.

locals {
  # try(...) rather than a plain index: not every key in instance_keys
  # necessarily has an override entry, and object attribute access on a
  # map that doesn't contain the key would otherwise error the whole
  # plan rather than just falling through to the group's own default.
  #
  # A plain conditional, not coalesce(): coalesce() errors outright if
  # *every* argument is null, which is the common case here (no
  # override AND no group-level peer_id at all, i.e. an ordinary local
  # group) — it's not a null-safe "first non-null or null" the way it
  # might look.
  effective_peer_id = {
    for k in keys(local.instance_keys) :
    k => try(var.placement_overrides[k].peer_id, null) != null ? var.placement_overrides[k].peer_id : var.peer_id
  }
  effective_vpc_id = {
    for k in keys(local.instance_keys) :
    k => try(var.placement_overrides[k].vpc_id, null) != null ? var.placement_overrides[k].vpc_id : var.vpc_id
  }
  effective_subnet_id = {
    for k in keys(local.instance_keys) :
    k => try(var.placement_overrides[k].subnet_id, null) != null ? var.placement_overrides[k].subnet_id : var.subnet_id
  }
}

resource "cloudcore_instance" "this" {
  for_each = local.instance_keys

  name      = "${var.project}-${var.environment}-${var.name}-${each.key}"
  image_id  = var.image_id
  flavor    = var.flavor
  vpc_id    = local.effective_vpc_id[each.key]
  subnet_id = local.effective_subnet_id[each.key]
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
  users              = var.users
  peer_id            = local.effective_peer_id[each.key]
  tags = merge(local.common_tags, var.tags, {
    Name          = "${var.project}-${var.environment}-${var.name}-${each.key}"
    InstanceGroup = var.name
  })
}
