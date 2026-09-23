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

  # Empty map (no override — stays local, today's default behaviour)
  # unless coordinator_peer_id is actually set. Same shape
  # worker_placement_overrides already uses, applied to the
  # coordinator's own single "01" key instead of one per worker.
  coordinator_placement_overrides = var.coordinator_peer_id != "" ? {
    "01" = {
      peer_id            = var.coordinator_peer_id
      vpc_id             = var.coordinator_peer_vpc_id
      subnet_id          = var.coordinator_peer_subnet_id
      security_group_ids = [var.coordinator_peer_security_group_id]
    }
  } : {}

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

  # Direct request: keep worker_peers/RPC offloading fully intact for
  # later (better hardware), but make "coordinator alone, zero
  # workers" a genuinely, cleanly supported configuration right now —
  # not just "leave worker_peers empty and hope it degrades
  # gracefully". It didn't: with worker_count == 0, rpc_servers above
  # is an EMPTY string, and the old unconditional
  # `-ngl ${rpc_offload_layers} --rpc ${rpc_servers}` in the coordinator's
  # own ExecStart line would have collapsed to `--rpc  -ngl 37` (the
  # empty substitution merging two flags' own whitespace) — command-
  # line parsing would then read "-ngl" itself as --rpc's own value,
  # not a separate flag, breaking llama-server's own startup outright.
  # With zero workers this is now omitted entirely instead -- llama.cpp
  # already treats no -ngl / no --rpc as "run every layer locally, no
  # offload anywhere", exactly what a single-host deployment needs.
  llama_rpc_flags = local.worker_count > 0 ? "-ngl ${var.rpc_offload_layers} --rpc ${local.rpc_servers} " : ""

  # F-127 follow-up: the same `users` block ha-frontend-lb's own "ecs"
  # user already proves works (modules/instance-group passes it straight
  # through to cloudcore_instance) — a dedicated, independently
  # revocable, toggleable-off debug login for every instance this
  # template creates, not the CloudCore inter-instance keypair.
  claude_debug_users = var.enable_claude_debug_access ? [{
    username = "claude-debug"
    sudo     = true
    ssh_keys = [var.claude_debug_ssh_public_key]
  }] : []

  # JSON body of llama-server's own --webui-config-file — confirmed live
  # that its keys are flat top-level settings names matching the
  # frontend's own constants map (temperature/systemMessage/etc.), not
  # nested. jsonencode() rather than hand-built string interpolation so
  # webui_system_message's own free text is always safely escaped.
  webui_config_json = jsonencode({
    temperature   = var.webui_temperature
    systemMessage = var.webui_system_message
  })

  # Baked into the coordinator's own cloud-init as plain source (see
  # files/coordinator-cloud-init.yaml.tftpl's own write_files entry) —
  # matches how llama-server.service's own unit file is already
  # inlined; plain-text Python needs no build/pinned-artifact step.
  verify_proxy_source = file("${path.module}/files/verify_proxy.py")

  # Stage 5B — same inlining convention, a separate service from
  # verify-proxy.service (see sandbox_terminal.py's own module
  # docstring for why: it needs websockets + paramiko, deliberately not
  # bolted onto verify_proxy.py's zero-dependency stdlib posture).
  sandbox_terminal_source = file("${path.module}/files/sandbox_terminal.py")

  # Fixed host-level address, same convention as promtail_config's own
  # Loki target and the host-level package repo (192.168.100.1:8090) —
  # api/examples_listener.py's own dedicated, always-on bind. Confirmed
  # live this session that this exact address is reachable not just
  # from local guests but across the WireGuard tunnel from a
  # peer-placed coordinator's own host network too (192.168.100.0/24 is
  # in every paired peer's own AllowedIPs — see wireguard.py's
  # render_config), so one fixed address covers both placements with
  # no peer-specific templating needed.
  examples_api_base = "http://192.168.100.1:8083"

  # CloudCore Dashboard -- LLM Performance page's "live deployments"
  # registry (verify_proxy.py's own register_llm_deployment(), api/
  # llm_deployments_routes.py). Must match the CloudCore-assigned name
  # this coordinator instance will actually get, exactly reproducing
  # modules/instance-group's own "${project}-${environment}-${name}-${key}"
  # formula for module.coordinator's single ("01") instance below --
  # the guest itself has no way to know this (its own instance id is
  # only assigned by the API after apply, long after this string is
  # rendered into its cloud-init), so it's computed here instead.
  coordinator_deployment_name = "${var.project}-${var.environment}-llm-chat-coord${local.sfx}-01"

  # Stage 2 — the same CodeMirror 5.65.16 build already vendored for the
  # Dashboard's own Editor page (ui/vendor/, a sibling of this example
  # directory — ../../ui/vendor from here), read fresh and re-embedded
  # into THIS guest's own cloud-init: the dashboard's /vendor/ route
  # only ever serves the admin host, never a student-facing coordinator.
  codemirror_core_js         = file("${path.module}/../../ui/vendor/codemirror.min.js")
  codemirror_core_css        = file("${path.module}/../../ui/vendor/codemirror.min.css")
  codemirror_theme_css       = file("${path.module}/../../ui/vendor/codemirror-theme-dracula.min.css")
  codemirror_matchbrackets_js = file("${path.module}/../../ui/vendor/codemirror-addon-matchbrackets.min.js")
  codemirror_python_mode_js  = file("${path.module}/../../ui/vendor/codemirror-mode-python.min.js")

  # Stage 5B — already vendored for the Dashboard's own admin Terminal
  # feature (ui/src/js/11-terminal.js) — reused as-is, same re-embedding
  # reasoning as the CodeMirror assets just above.
  xterm_core_js      = file("${path.module}/../../ui/vendor/xterm.min.js")
  xterm_core_css     = file("${path.module}/../../ui/vendor/xterm.min.css")
  xterm_fit_addon_js = file("${path.module}/../../ui/vendor/xterm-addon-fit.min.js")

  coordinator_user_data = templatefile("${path.module}/files/coordinator-cloud-init.yaml.tftpl", {
    llama_archive_name = var.llama_archive_name
    llama_sha256       = var.llama_sha256
    model_filename      = var.model_filename
    model_sha256        = var.model_sha256
    http_port           = var.http_port
    context_size        = var.context_size
    threads              = var.threads
    llama_rpc_flags      = local.llama_rpc_flags
    promtail_config      = local.promtail_config
    webui_config_json    = local.webui_config_json
    verify_proxy_source     = local.verify_proxy_source
    enable_verification     = var.enable_verification
    verify_timeout_seconds  = var.verify_timeout_seconds
    verify_max_memory_mb    = var.verify_max_memory_mb
    verify_max_fix_rounds   = var.verify_max_fix_rounds
    max_continuation_rounds = var.max_continuation_rounds
    generation_stall_timeout_seconds = var.generation_stall_timeout_seconds
    examples_api_base        = local.examples_api_base
    examples_ingestion_token = var.examples_ingestion_token
    deployment_name           = local.coordinator_deployment_name
    sandbox_system_message   = var.sandbox_system_message
    linux_system_message     = var.linux_system_message
    rate_limit_run_per_minute = var.rate_limit_run_per_minute
    rate_limit_ask_per_10min  = var.rate_limit_ask_per_10min
    firecracker_archive_name    = var.firecracker_archive_name
    firecracker_sha256          = var.firecracker_sha256
    firecracker_kernel_name     = var.firecracker_kernel_name
    firecracker_kernel_sha256   = var.firecracker_kernel_sha256
    firecracker_rootfs_name     = var.firecracker_rootfs_name
    firecracker_rootfs_sha256   = var.firecracker_rootfs_sha256
    sandbox_subnet_cidr         = var.sandbox_subnet_cidr
    sandbox_terminal_source     = local.sandbox_terminal_source
    terminal_port                    = var.terminal_port
    terminal_idle_timeout_minutes    = var.terminal_idle_timeout_minutes
    terminal_max_session_minutes     = var.terminal_max_session_minutes
    terminal_max_concurrent_sessions = var.terminal_max_concurrent_sessions
    terminal_boot_timeout_seconds    = var.terminal_boot_timeout_seconds
    terminal_unresponsive_seconds    = var.terminal_unresponsive_seconds
    preview_ports                    = join(",", var.preview_ports)
    websockets_wheel_name    = var.websockets_wheel_name
    websockets_wheel_sha256  = var.websockets_wheel_sha256
    codemirror_core_js         = local.codemirror_core_js
    codemirror_core_css        = local.codemirror_core_css
    codemirror_theme_css       = local.codemirror_theme_css
    codemirror_matchbrackets_js = local.codemirror_matchbrackets_js
    codemirror_python_mode_js  = local.codemirror_python_mode_js
    xterm_core_js      = local.xterm_core_js
    xterm_core_css     = local.xterm_core_css
    xterm_fit_addon_js = local.xterm_fit_addon_js
  })
}
