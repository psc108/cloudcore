resource "cloudcore_nfs_server" "this" {
  for_each = var.enabled ? var.nfs_servers : {}

  name    = "${var.project}-${var.environment}-${each.key}"
  vpc_id  = each.value.vpc_id
  flavor  = each.value.flavor
  disk_gb = each.value.disk_gb
  tags    = merge(local.common_tags, var.tags, { Name = "${var.project}-${var.environment}-${each.key}" })

  dynamic "shares" {
    for_each = each.value.shares
    content {
      name    = shares.value.name
      clients = shares.value.clients
    }
  }
}
