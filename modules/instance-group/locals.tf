locals {
  common_tags = {
    Environment = var.environment
    Project     = var.project
    Owner       = var.owner
    ManagedBy   = "opentofu"
  }

  # Build a map of instance keys: { "01" = {}, "02" = {}, ... }
  instance_keys = var.enabled ? {
    for i in range(var.count_instances) :
    format("%02d", i + 1) => {}
  } : {}
}
