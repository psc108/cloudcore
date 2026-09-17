# Distributed LLM inference — a llama.cpp coordinator (loads the GGUF
# model, runs llama-server, serves an OpenAI-compatible HTTP API) plus
# one or more RPC worker instances, each pinned to a different paired
# peer via var.worker_peers. The coordinator splits the model's layers
# across itself and every worker (llama.cpp's own --rpc mechanism) and
# streams each worker its own share of tensor data over the network at
# load time — workers never need a copy of the model file on disk,
# only a compatible llama.cpp build (see module.workers' own comment).
#
# Built and proven at Mistral-7B-Instruct-v0.3 (Q4_K_M, ~4.37GB weights)
# deliberately, per direct request — and not just as a capability
# proof: api/compute.py's own biggest instance flavor (standard.large)
# is 4096MB RAM, which does not fit this model's weights alone on ANY
# single CloudCore instance that exists today. Splitting across the
# coordinator and one worker (roughly half the layers each) is
# currently the only way this specific model runs on this platform at
# all, not a nicety. The same template scales to a model that doesn't
# fit even split across today's paired hosts (30B+) the same way —
# bigger model_filename/model_sha256, more worker_peers entries — once
# more machines join CloudCore.
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
    "llm${local.sfx}" = { newbits = 8, netnum = 1, public = true, zone = "a" }
  }
}

module "security_groups" {
  source = "../../modules/security-groups"

  project     = var.project
  environment = var.environment
  owner       = var.owner
  vpc_id      = module.vpc.vpc_ids_by_key[local.vpc_key]

  security_groups = {
    # Coordinator only — SSH + the inference HTTP API. Deliberately no
    # RPC port here: RPC is inbound to WORKERS, never to the coordinator
    # itself (the coordinator only ever makes outbound RPC connections).
    "coordinator${local.sfx}" = {
      description = "Distributed LLM coordinator — SSH + inference HTTP API, scoped to admin_cidr"
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

  name               = "llm-coord${local.sfx}"
  image_id           = "ubuntu-22.04"
  flavor             = var.coordinator_flavor
  count_instances    = 1
  vpc_id             = module.vpc.vpc_ids_by_key[local.vpc_key]
  subnet_id          = module.subnets.subnet_ids_by_key["llm${local.sfx}"]
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

  name                = "llm-worker${local.sfx}"
  image_id            = "ubuntu-22.04"
  flavor              = var.worker_flavor
  count_instances     = local.worker_count
  vpc_id              = module.vpc.vpc_ids_by_key[local.vpc_key]
  subnet_id           = module.subnets.subnet_ids_by_key["llm${local.sfx}"]
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
    "llm${local.sfx}" = {
      type       = "application"
      vpc_id     = module.vpc.vpc_ids_by_key[local.vpc_key]
      subnet_ids = values(module.subnets.public_subnet_ids)
      internal   = false
    }
  }
}

resource "cloudcore_lb_target_group" "coordinator" {
  lb_id    = module.lb.lb_ids_by_key["llm${local.sfx}"]
  name     = "${var.project}-${var.environment}-llm-tg${local.sfx}"
  port     = var.http_port
  protocol = "http"

  targets = [
    {
      instance_id = module.coordinator.instance_ids_by_key["01"]
      port        = var.http_port
    }
  ]
}

resource "cloudcore_lb_listener" "coordinator" {
  lb_id           = module.lb.lb_ids_by_key["llm${local.sfx}"]
  port            = var.http_port
  protocol        = "http"
  target_group_id = cloudcore_lb_target_group.coordinator.id
}
