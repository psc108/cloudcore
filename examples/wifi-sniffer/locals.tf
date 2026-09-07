locals {
  sfx     = var.suffix != "" ? "-${var.suffix}" : ""
  vpc_key = "main${local.sfx}"

  sniffer_user_data = templatefile("${path.module}/files/cloud-init.yaml.tftpl", {
    rtl8812au_driver_ref = var.rtl8812au_driver_ref
  })
}
