locals {
  sfx     = var.suffix != "" ? "-${var.suffix}" : ""
  vpc_key = "main${local.sfx}"

  # Same fixed host-level Loki address every other example points at
  # (haFullStack-LLD.md §12.3) — see examples/ha-frontend-lb/files/
  # promtail-config.yml for the full content/reasoning; copied here
  # verbatim rather than referenced cross-module, matching this
  # project's own each-example-is-self-contained convention.
  promtail_config = file("${path.module}/files/promtail-config.yml")

  kiwix_user_data = templatefile("${path.module}/files/cloud-init.yaml.tftpl", {
    kiwix_tools_version = var.kiwix_tools_version
    kiwix_tools_url     = var.kiwix_tools_url
    kiwix_tools_sha256  = var.kiwix_tools_sha256
    zim_url             = var.zim_url
    zim_filename        = var.zim_filename
    zim_md5             = var.zim_md5
    promtail_config     = local.promtail_config
  })
}
