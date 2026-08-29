locals {
  sfx       = var.suffix != "" ? "-${var.suffix}" : ""
  vpc_key   = "main${local.sfx}"
  subnet_id = "subnet-${replace(var.cidr_block, "/[./]/", "-")}-1"

  nginx_user_data = { for name in ["web-01", "web-02", "web-03"] : name => <<-EOT
    #cloud-config
    packages: [nginx]
    runcmd:
      - systemctl enable --now nginx
      - echo ${name} > /var/www/html/index.html
  EOT
  }
}
