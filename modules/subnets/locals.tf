locals {
  common_tags = {
    Environment = var.environment
    Project     = var.project
    Owner       = var.owner
    ManagedBy   = "opentofu"
  }

  # Resolve each subnet's CIDR.
  # If cidr_block is set explicitly, use it directly.
  # Otherwise derive via cidrsubnet(vpc_cidr_block, newbits, netnum).
  # Example: vpc 10.0.0.0/16, newbits=8, netnum=1 → 10.0.1.0/24
  resolved_subnets = var.enabled ? {
    for k, v in var.subnets : k => merge(v, {
      cidr_block = v.cidr_block != null ? v.cidr_block : cidrsubnet(
        var.vpc_cidr_block,
        v.newbits,
        v.netnum
      )
    })
  } : {}
}
