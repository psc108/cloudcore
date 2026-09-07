locals {
  sfx     = var.suffix != "" ? "-${var.suffix}" : ""
  vpc_key = "main${local.sfx}"

  kiwix_user_data = templatefile("${path.module}/files/cloud-init.yaml.tftpl", {
    kiwix_tools_version = var.kiwix_tools_version
    kiwix_tools_url     = var.kiwix_tools_url
    kiwix_tools_sha256  = var.kiwix_tools_sha256
    zim_url             = var.zim_url
    zim_filename        = var.zim_filename
    zim_md5             = var.zim_md5
  })
}
