# Subnets module
#
# CloudCore does not have a subnet API resource — subnets are logical
# constructs derived from the VPC CIDR. This module resolves subnet CIDRs
# and produces stable subnet_id strings consumed by the compute and
# load-balancer modules.
#
# subnet_id format: subnet-<project>-<environment>-<key>
# This is deterministic and safe to use as a for_each key.

locals {
  subnet_ids = {
    for k, v in local.resolved_subnets :
    k => "subnet-${var.project}-${var.environment}-${k}"
  }
}
