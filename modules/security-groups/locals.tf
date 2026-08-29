locals {
  common_tags = {
    Environment = var.environment
    Project     = var.project
    Owner       = var.owner
    ManagedBy   = "opentofu"
  }

  # Produce stable security group ID strings.
  # Format: sg-<project>-<environment>-<key>
  security_group_ids = var.enabled ? {
    for k in keys(var.security_groups) :
    k => "sg-${var.project}-${var.environment}-${k}"
  } : {}
}
