locals {
  sfx     = var.suffix != "" ? "-${var.suffix}" : ""
  vpc_key = "main${local.sfx}"

  # Same fixed host-level Loki address every other example points at
  # (haFullStack-LLD.md §12.3) — see examples/ha-frontend-lb/files/
  # promtail-config.yml for the full content/reasoning; copied here
  # verbatim rather than referenced cross-module, matching this
  # project's own each-example-is-self-contained convention.
  promtail_config = file("${path.module}/files/promtail-config.yml")

  sniffer_user_data = templatefile("${path.module}/files/cloud-init.yaml.tftpl", {
    rtl8812au_driver_ref    = var.rtl8812au_driver_ref
    rtl8812au_driver_sha256 = var.rtl8812au_driver_sha256
    promtail_config         = local.promtail_config
  })
}
