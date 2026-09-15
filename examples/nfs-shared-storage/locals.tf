locals {
  sfx     = var.suffix != "" ? "-${var.suffix}" : ""
  vpc_key = "main${local.sfx}"

  # Same fixed host-level Loki address every other example points at
  # (haFullStack-LLD.md §12.3) — see examples/ha-frontend-lb/files/
  # promtail-config.yml for the full content/reasoning; copied here
  # verbatim rather than referenced cross-module, matching this
  # project's own each-example-is-self-contained convention.
  promtail_config = file("${path.module}/files/promtail-config.yml")

  # One of the two shares module.nfs already creates ("data",
  # "backups") — matching the Ansible port's own single-share mount,
  # not an attempt to mirror its exact share name ("shared-data").
  nfs_ip        = values(module.nfs.private_ips_by_key)[0]
  nfs_share     = "data"
  nfs_mount_dir = "/mnt/shared"

  app_user_data = templatefile("${path.module}/files/cloud-init.yaml.tftpl", {
    nfs_ip          = local.nfs_ip
    nfs_share       = local.nfs_share
    nfs_mount_dir   = local.nfs_mount_dir
    promtail_config = local.promtail_config
  })
}
