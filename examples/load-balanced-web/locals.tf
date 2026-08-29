locals {
  sfx     = var.suffix != "" ? "-${var.suffix}" : ""
  vpc_key = "main${local.sfx}"

  nginx_user_data = <<-EOT
    #cloud-config
    packages: [nginx]
    runcmd:
      - systemctl enable --now nginx
  EOT
}
