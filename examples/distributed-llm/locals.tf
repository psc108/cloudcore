locals {
  sfx     = var.suffix != "" ? "-${var.suffix}" : ""
  vpc_key = "main${local.sfx}"

  # Same fixed host-level Loki address every other example points at
  # (haFullStack-LLD.md §12.3) — copied verbatim rather than referenced
  # cross-module, matching this project's own each-example-is-
  # self-contained convention. llama-server/ggml-rpc-server both run as
  # plain systemd services, so their own stdout/stderr already reaches
  # Loki via the shared journal job below with no template-specific job
  # needed — matching every other job in this same shared file being a
  # harmless no-op wherever it doesn't apply.
  promtail_config = file("${path.module}/files/promtail-config.yml")

  worker_count = length(var.worker_peers)

  # Every key gets an override — there's no "local anchor" worker the
  # way other templates have one (e.g. load-balanced-web's own "01"),
  # since the entire point of this template is that every worker lands
  # on a different machine than the coordinator.
  worker_placement_overrides = {
    for idx, w in var.worker_peers :
    format("%02d", idx + 1) => {
      peer_id            = w.peer_id
      vpc_id             = w.peer_vpc_id
      subnet_id          = w.peer_subnet_id
      security_group_ids = [w.peer_security_group_id]
    }
  }

  worker_user_data = templatefile("${path.module}/files/worker-cloud-init.yaml.tftpl", {
    llama_archive_name = var.llama_archive_name
    llama_sha256       = var.llama_sha256
    rpc_port           = var.rpc_port
    threads            = var.threads
    promtail_config    = local.promtail_config
  })

  # The coordinator's own --rpc argument needs every worker's real
  # private IP — a genuine data dependency on module.workers, not a
  # hardcoded assumption: OpenTofu sequences this automatically (workers
  # are created, and their IPs known, before the coordinator's own
  # user_data is even rendered).
  rpc_servers = join(",", [for ip in module.workers.private_ips_list : "${ip}:${var.rpc_port}"])

  coordinator_user_data = templatefile("${path.module}/files/coordinator-cloud-init.yaml.tftpl", {
    llama_archive_name = var.llama_archive_name
    llama_sha256       = var.llama_sha256
    model_filename     = var.model_filename
    model_sha256       = var.model_sha256
    http_port          = var.http_port
    context_size       = var.context_size
    threads            = var.threads
    rpc_servers        = local.rpc_servers
    promtail_config    = local.promtail_config
  })
}
