locals {
  sfx       = var.suffix != "" ? "-${var.suffix}" : ""
  vpc_key   = "main${local.sfx}"
  subnet_id = "subnet-${replace(var.cidr_block, "/[./]/", "-")}-1"
}
