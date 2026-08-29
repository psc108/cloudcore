locals {
  sfx       = var.suffix != "" ? "-${var.suffix}" : ""
  vpc_key   = "main${local.sfx}"
  subnet_id = "subnet-${replace(var.cidr_block, "/[./]/", "-")}-1"

  nginx_user_data = <<-EOT
    #cloud-config
    packages: [nginx]
    runcmd:
      - systemctl enable --now nginx
  EOT
}
