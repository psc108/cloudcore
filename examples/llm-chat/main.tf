# Interactive LLM chat — a llama.cpp coordinator (loads the GGUF model,
# runs llama-server, serves its own built-in browser chat Web UI at
# GET / alongside the OpenAI-compatible HTTP API) plus one or more RPC
# worker instances, each pinned to a different paired peer via
# var.worker_peers. Same underlying distributed-inference mechanism as
# examples/distributed-llm (built first, for automated Sentinel-log
# ingestion) — this example exists purely to give a person a browser
# tab to actually talk to the model, per direct request: "lets create
# a template thats built solely to provide a llm chat interface to 7b
# or whichever llm we provide in the future... a browser frontend for
# the moment (cli can wait)."
#
# The chat UI itself is NOT custom-built here — llama-server ships its
# own compiled single-page Web UI, enabled by default (--webui,
# confirmed via `llama-server --help` and a real local test: `curl -v`
# without gzip support gets a 415 — the same "415 on GET /" behavior
# examples/distributed-llm's own F-097 found and worked around for its
# health check — but any real browser sends Accept-Encoding: gzip
# automatically and gets the genuine chat page). This matches the
# convention every other browser-facing example in this project
# already follows (ghidra-workstation, kiwix-library): serve the
# upstream tool's own web interface through a load balancer, don't
# write a new frontend.
#
# Built and proven at Mistral-7B-Instruct-v0.3, then switched to
# Qwen2.5-Coder — code generation/correction is this deployment's
# actual job, not general chat, and Qwen2.5-Coder's own ChatML template
# supports a real system role (confirmed live) where Mistral-7B-
# Instruct-v0.3's own template does not (confirmed live via GET /props'
# chat_template_caps.supports_system_role: false). Stepped up again
# from the 7B to the 14B variant (Q4_K_M, ~8.37GB weights, default as
# of this revision) after real testing found the 7B still hallucinated
# on non-trivial code tasks even with the system prompt genuinely
# reaching it — parameter count matters more than prompting for this
# specific failure mode. For the same reason examples/distributed-llm
# was built this way: no single CloudCore instance flavor has enough
# RAM for this model's weights alone, so splitting across the
# coordinator and at least one worker is currently the only way it runs
# here at all — not a nicety. The coordinator/worker split itself is
# computed fresh for every real build submitted through the CloudCore
# API (api/layer_split.py, weighing each participant's own current CPU
# headroom — see rpc_offload_layers' own comment in variables.tf),
# rather than fixed at "roughly even." Scales to more/bigger models the
# same way as more machines join CloudCore.
#
# SECURITY: llama.cpp's own RPC backend is explicitly documented as a
# "proof-of-concept" that is "fragile and insecure," warning "Never run
# the RPC server on an open network." See worker_peers' own variable
# comment in variables.tf for what this means for the worker's security
# group specifically.

module "vpc" {
  source = "../../modules/vpc"

  project     = var.project
  environment = var.environment
  owner       = var.owner

  vpcs = {
    (local.vpc_key) = { cidr_block = var.cidr_block }
  }
}

module "subnets" {
  source = "../../modules/subnets"

  project        = var.project
  environment    = var.environment
  owner          = var.owner
  vpc_id         = module.vpc.vpc_ids_by_key[local.vpc_key]
  vpc_cidr_block = var.cidr_block

  subnets = {
    "chat${local.sfx}" = { newbits = 8, netnum = 1, public = true, zone = "a" }
  }
}

module "security_groups" {
  source = "../../modules/security-groups"

  project     = var.project
  environment = var.environment
  owner       = var.owner
  vpc_id      = module.vpc.vpc_ids_by_key[local.vpc_key]

  security_groups = {
    # Coordinator only — SSH + the chat HTTP UI/API. Deliberately no
    # RPC port here: RPC is inbound to WORKERS, never to the coordinator
    # itself (the coordinator only ever makes outbound RPC connections).
    "coordinator${local.sfx}" = {
      description = "LLM chat coordinator — SSH + chat Web UI/API, scoped to admin_cidr"
      ingress_rules = {
        ssh  = { ip_protocol = "tcp", from_port = 22, to_port = 22, cidr = var.admin_cidr }
        http = { ip_protocol = "tcp", from_port = var.http_port, to_port = var.http_port, cidr = var.admin_cidr }
      }
      egress_rules = {
        all = { ip_protocol = "-1", cidr = "0.0.0.0/0" }
      }
    }
  }
}

module "coordinator" {
  source = "../../modules/instance-group"

  project     = var.project
  environment = var.environment
  owner       = var.owner

  name               = "llm-chat-coord${local.sfx}"
  image_id           = "ubuntu-22.04"
  flavor             = var.coordinator_flavor
  count_instances    = 1
  # Fallback values only — used as-is when coordinator_peer_id is empty
  # (today's default: coordinator stays local). When it's set,
  # coordinator_placement_overrides' own "01" entry takes over instead,
  # same mechanism module.workers already relies on below.
  vpc_id              = module.vpc.vpc_ids_by_key[local.vpc_key]
  subnet_id           = module.subnets.subnet_ids_by_key["chat${local.sfx}"]
  security_group_ids  = module.security_groups.security_group_ids_list
  placement_overrides = local.coordinator_placement_overrides
  user_data          = local.coordinator_user_data
}

# Every worker is peer-placed — there's no "local anchor" instance here
# the way other templates have one, since the entire point of this
# template is spreading across machines. vpc_id/subnet_id/
# security_group_ids below are never actually applied in practice
# (placement_overrides covers every single key, all worker_count of
# them) — passed only because the module requires non-null fallback
# values for any key an override entry doesn't cover.
module "workers" {
  source = "../../modules/instance-group"

  project     = var.project
  environment = var.environment
  owner       = var.owner

  name                = "llm-chat-worker${local.sfx}"
  image_id            = "ubuntu-22.04"
  flavor              = var.worker_flavor
  count_instances     = local.worker_count
  vpc_id              = module.vpc.vpc_ids_by_key[local.vpc_key]
  subnet_id           = module.subnets.subnet_ids_by_key["chat${local.sfx}"]
  security_group_ids  = module.security_groups.security_group_ids_list
  user_data           = local.worker_user_data
  placement_overrides = local.worker_placement_overrides
}

module "lb" {
  source = "../../modules/load-balancer"

  project     = var.project
  environment = var.environment
  owner       = var.owner

  load_balancers = {
    "chat${local.sfx}" = {
      type       = "application"
      vpc_id     = module.vpc.vpc_ids_by_key[local.vpc_key]
      subnet_ids = values(module.subnets.public_subnet_ids)
      internal   = false
    }
  }
}

resource "cloudcore_lb_target_group" "coordinator" {
  lb_id    = module.lb.lb_ids_by_key["chat${local.sfx}"]
  name     = "${var.project}-${var.environment}-llm-chat-tg${local.sfx}"
  port     = var.http_port
  protocol = "http"

  # llama-server's own root path ("/") requires gzip-encoded responses
  # (its embedded Web UI assets are pre-compressed) — a plain `GET /`
  # health check without Accept-Encoding: gzip gets a 415, exactly the
  # same finding examples/distributed-llm's own F-097 made for the same
  # binary. /health is llama-server's real liveness endpoint and needs
  # no special headers.
  health_check = {
    path = "/health"
  }

  targets = [
    {
      instance_id = module.coordinator.instance_ids_by_key["01"]
      port        = var.http_port
    }
  ]
}

resource "cloudcore_lb_listener" "coordinator" {
  lb_id           = module.lb.lb_ids_by_key["chat${local.sfx}"]
  port            = var.http_port
  protocol        = "http"
  target_group_id = cloudcore_lb_target_group.coordinator.id

  # Stage 5B — the sandbox terminal's own WebSocket service reuses this
  # SAME listener/port via a path routing rule rather than a new LB
  # listener, matching how verify_proxy.py itself already sits in front
  # of llama-server on another loopback port. HAProxy (this listener's
  # own real implementation, api/lb.py) passes a WebSocket upgrade
  # through transparently once path-matched, same as any other HTTP
  # request — no separate LB-level WS configuration needed.
  routing_rules = [
    {
      priority = 10
      conditions = {
        path_pattern = "/terminal*"
      }
      target_group_id = cloudcore_lb_target_group.terminal.id
    }
  ]
}

resource "cloudcore_lb_target_group" "terminal" {
  lb_id    = module.lb.lb_ids_by_key["chat${local.sfx}"]
  name     = "${var.project}-${var.environment}-llm-chat-term-tg${local.sfx}"
  port     = var.terminal_port
  protocol = "http"

  # sandbox_terminal.py is a WebSocket-only service -- a plain GET / (the
  # target group's own default health-check path) never completes the WS
  # handshake, so the websockets library correctly answers it with a 426
  # Upgrade Required. HAProxy's httpchk reads that as unhealthy and marks
  # this whole backend down, 503-ing every real /terminal request even
  # though the service itself is fine. sandbox_terminal.py's own
  # process_request hook special-cases exactly this path to answer a
  # plain 200 instead, but only for /health specifically.
  health_check = {
    path = "/health"
  }

  targets = [
    {
      instance_id = module.coordinator.instance_ids_by_key["01"]
      port        = var.terminal_port
    }
  ]
}

# Per direct request: a browser-reachable way to see the output of a web
# app a student wrote and ran in the sandbox terminal. One target group +
# listener per fixed preview port (var.preview_ports), each a plain 1:1
# forward straight to sandbox_terminal.py's own port-matching reverse-
# proxy listener on the coordinator -- that process (not this LB) is what
# actually resolves "which student's microVM" a given connection belongs
# to (by the same X-Forwarded-For source IP its WebSocket terminal
# handler already keys concurrency on) and proxies into it. `for_each`,
# not `count`, over the fixed port list -- this project's own Terraform
# convention (CLAUDE.md).
resource "cloudcore_lb_target_group" "preview" {
  for_each = toset([for p in var.preview_ports : tostring(p)])
  lb_id    = module.lb.lb_ids_by_key["chat${local.sfx}"]
  name     = "${var.project}-${var.environment}-llm-chat-preview-${each.key}-tg${local.sfx}"
  port     = tonumber(each.key)
  protocol = "http"

  # sandbox_terminal.py's own preview listener answers a plain GET
  # /health itself (bypassing the per-connection proxy logic entirely),
  # same fix already applied to the terminal target group just above --
  # without it, HAProxy's own health probe would hit the real proxy
  # path with no matching session and get read as unhealthy.
  health_check = {
    path = "/health"
  }

  targets = [
    {
      instance_id = module.coordinator.instance_ids_by_key["01"]
      port        = tonumber(each.key)
    }
  ]
}

resource "cloudcore_lb_listener" "preview" {
  for_each        = cloudcore_lb_target_group.preview
  lb_id           = module.lb.lb_ids_by_key["chat${local.sfx}"]
  port            = each.value.port
  protocol        = "http"
  target_group_id = each.value.id
}
