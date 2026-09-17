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
# Built and proven at Mistral-7B-Instruct-v0.3 (Q4_K_M, ~4.37GB
# weights) for the same reason examples/distributed-llm was: this
# platform's own biggest instance flavor (standard.large) is 4096MB
# RAM, less than this model's weights alone, so splitting across the
# coordinator and at least one worker is currently the only way this
# model runs here at all — not a nicety. Scales to more/bigger models
# the same way as more machines join CloudCore.
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
  vpc_id             = module.vpc.vpc_ids_by_key[local.vpc_key]
  subnet_id          = module.subnets.subnet_ids_by_key["chat${local.sfx}"]
  security_group_ids = module.security_groups.security_group_ids_list
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
}
