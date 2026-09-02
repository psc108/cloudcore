# Internet Gateway module
#
# Attaches a single internet gateway to a VPC. Required for any subnet to
# have internet-routable traffic when combined with a route table that has
# a 0.0.0.0/0 → <igw_id> route.

locals {
  common_tags = {
    Environment = var.environment
    Project     = var.project
    Owner       = var.owner
    ManagedBy   = "opentofu"
  }
}

resource "cloudcore_internet_gateway" "this" {
  count = var.enabled ? 1 : 0

  name   = "${var.project}-${var.environment}-igw"
  vpc_id = var.vpc_id
  tags   = merge(local.common_tags, var.tags)
}
