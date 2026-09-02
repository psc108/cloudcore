# Route Table module
#
# Creates a route table and associates it with the supplied subnets.
# A route of 0.0.0.0/0 → <igw_id> makes associated subnets public.
# The local VPC CIDR route is implicit; it does not need to be listed.

locals {
  common_tags = {
    Environment = var.environment
    Project     = var.project
    Owner       = var.owner
    ManagedBy   = "opentofu"
  }
}

resource "cloudcore_route_table" "this" {
  count = var.enabled ? 1 : 0

  name       = "${var.project}-${var.environment}-rt-${var.name_suffix}"
  vpc_id     = var.vpc_id
  subnet_ids = var.subnet_ids
  routes     = var.routes
  tags       = merge(local.common_tags, var.tags)
}
