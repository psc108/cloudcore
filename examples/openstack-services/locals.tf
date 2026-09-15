locals {
  sfx     = var.suffix != "" ? "-${var.suffix}" : ""
  vpc_key = "main${local.sfx}"

  # Same fixed host-level Loki address every other example points at
  # (haFullStack-LLD.md §12.3) — see examples/ha-frontend-lb/files/
  # promtail-config.yml for the full content/reasoning; copied here
  # verbatim rather than referenced cross-module, matching this
  # project's own each-example-is-self-contained convention.
  promtail_config = file("${path.module}/files/promtail-config.yml")

  frontend_user_data = templatefile("${path.module}/files/frontend-cloud-init.yaml.tftpl", {
    promtail_config = local.promtail_config
  })

  # Backend and Keystone are both illustrative stand-ins (no real
  # application), sharing one template — see its own header comment.
  backend_user_data = templatefile("${path.module}/files/placeholder-cloud-init.yaml.tftpl", {
    promtail_config = local.promtail_config
  })
  keystone_user_data = templatefile("${path.module}/files/placeholder-cloud-init.yaml.tftpl", {
    promtail_config = local.promtail_config
  })

  mysql_user_data = templatefile("${path.module}/files/mysql-cloud-init.yaml.tftpl", {
    promtail_config = local.promtail_config
  })

  rabbitmq_user_data = templatefile("${path.module}/files/rabbitmq-cloud-init.yaml.tftpl", {
    promtail_config = local.promtail_config
  })
}
