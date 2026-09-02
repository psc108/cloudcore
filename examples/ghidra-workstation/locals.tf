locals {
  sfx     = var.suffix != "" ? "-${var.suffix}" : ""
  vpc_key = "main${local.sfx}"

  ghidra_user_data = templatefile("${path.module}/files/cloud-init.yaml.tftpl", {
    vnc_password       = var.vnc_password
    ghidra_version     = var.ghidra_version
    ghidra_release_tag = var.ghidra_release_tag
    ghidra_zip_name    = var.ghidra_zip_name
    ghidra_sha256      = var.ghidra_sha256
  })
}
