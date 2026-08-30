resource "cloudcore_nfs_server" "this" {
  for_each = var.enabled ? var.nfs_servers : {}

  name    = "${var.project}-${var.environment}-${each.key}"
  vpc_id  = each.value.vpc_id
  flavor  = each.value.flavor
  disk_gb = each.value.disk_gb
  tags    = merge(local.common_tags, var.tags, { Name = "${var.project}-${var.environment}-${each.key}" })

  shares = [for s in each.value.shares : {
    name    = s.name
    clients = s.clients
  }]
}
