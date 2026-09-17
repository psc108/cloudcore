variable "project" {
  description = "Project name used in resource naming and tags."
  type        = string
  default     = "example"
}

variable "environment" {
  description = "Environment name used in resource naming and tags."
  type        = string
  default     = "dev"
}

variable "owner" {
  description = "Owner tag value applied to all resources."
  type        = string
  default     = "platform-team"
}

variable "suffix" {
  description = "Optional suffix appended to resource names to keep them unique across runs."
  type        = string
  default     = ""
}

variable "cidr_block" {
  description = "CIDR block for the coordinator's local VPC."
  type        = string
  default     = "10.91.0.0/16"
}

# standard.xlarge (6 vCPU / 8192MB / 60GB) by default for both roles:
# Qwen2.5-Coder-14B-Instruct's Q4_K_M weights alone are ~8.37GB, which
# does not fit in a single CloudCore instance flavor. Splitting the
# model's layers across the coordinator and every worker is not a
# proof-of-concept nicety here — it's the only way this default model
# runs on this platform at all. See main.tf's own header comment for
# the fuller story, and examples/distributed-llm (same underlying
# mechanism, built first, for automated Sentinel-log-intelligence
# ingestion rather than a human chat session).
#
# Stepped up from the 7B default to 14B, and standard.large to
# standard.xlarge, after real testing found the 7B model still
# hallucinated on non-trivial code tasks even with the anti-
# hallucination system prompt genuinely reaching it — per direct
# request ("is there anything less likely to hallucinate... code is
# all I really care about"). Every worker peer's own real available
# RAM is still checked against the chosen worker_flavor BEFORE the
# build is even submitted (api/capacity_gate.py, reusing the same
# peers_routes.peer_stats() the traffic-light system already uses) —
# rejected with a clear message rather than letting a worker OOM
# partway through model load, regardless of which flavor is chosen.
variable "coordinator_flavor" {
  description = "Compute flavor for the coordinator instance."
  type        = string
  default     = "standard.xlarge"
}

variable "worker_flavor" {
  description = "Compute flavor for each RPC worker instance."
  type        = string
  default     = "standard.xlarge"
}

variable "admin_cidr" {
  description = "CIDR allowed to reach SSH (22) and the chat HTTP UI/API (http_port) on the coordinator's own security group."
  type        = string
  default     = "0.0.0.0/0"
}

# Deliberately different from examples/distributed-llm's own default
# (8610) — both examples can be built on the same host at once without
# a port collision on the load balancer's own listener.
variable "http_port" {
  description = "Host port the load balancer (and llama-server itself) listens on — this is both the chat Web UI (GET /, llama-server's own built-in frontend) and the OpenAI-compatible HTTP API."
  type        = number
  default     = 8620
}

variable "rpc_port" {
  description = "Port each RPC worker's ggml-rpc-server listens on. Deliberately NOT exposed beyond the coordinator's own subnet — see the SECURITY note on worker_peers below."
  type        = number
  default     = 50052
}

# Larger than distributed-llm's own default (2048) — an interactive
# chat session benefits from more room for conversation history than a
# single-shot log-ingestion prompt needs, within each participant's own
# tight RAM ceiling (see coordinator_flavor's own comment).
variable "context_size" {
  description = "Inference context window size (tokens), passed to llama-server's own -c flag."
  type        = number
  default     = 4096
}

variable "threads" {
  description = "CPU threads llama-server/ggml-rpc-server each use for their own local share of the compute."
  type        = number
  default     = 4
}

# llama.cpp's own -ngl flag means "how many of the model's layers to
# offload to non-CPU backends" (GPU normally, but --rpc's own workers
# count the same way) -- NOT "how many layers per worker" and NOT a
# percentage. With exactly one RPC worker configured, every offloaded
# layer goes to that ONE worker; a high value like the GPU-offload
# convention's usual 99 ("offload everything possible") tries to push
# nearly the ENTIRE model onto that single worker instead of splitting
# it with the coordinator's own local CPU -- found live building
# examples/distributed-llm (same mechanism): it tried to allocate a
# ~4.3GB buffer on a worker with only 4096MB RAM and failed outright.
#
# This default (24, half of Qwen2.5-Coder-14B-Instruct's own real 48
# transformer layers) is only ever a FALLBACK for building this
# template directly (`tofu apply` from the CLI, bypassing the CloudCore
# API entirely). Submitting a build through the API instead
# (POST /v1/tofu/builds, which is what the Dashboard's own Build
# Manager and Scheduler both do) computes this fresh for every single
# run instead: api/layer_split.py reads the chosen model_filename's own
# real layer count straight out of its GGUF header (api/gguf_meta.py —
# no more hand-maintained "model X has Y layers" comment to keep in
# sync, a real bug class this project hit three separate times tuning
# this exact variable across model swaps), weighs it against the
# coordinator's and every worker peer's *current* CPU cores and load
# (the same host_stats.py numbers the Peers/Capacity traffic-light
# already shows), and skews the split toward whichever side has more
# real spare capacity right now — per direct request: "make it a
# dynamic calculation... so we constantly adjust resource allocation
# for wherever it might do the best." An explicit value passed in the
# build request (this variable's own override) is always left alone —
# the dynamic calculation only ever fills in a value nobody asked for.
variable "rpc_offload_layers" {
  description = "Number of model layers to offload to the RPC worker(s) via -ngl. Only a static fallback for a direct `tofu apply` — see the comment above for how a real API-submitted build computes this dynamically instead, and for why 99 (the usual GPU-offload convention) is wrong here regardless."
  type        = number
  default     = 24
}

# --- Pinned artifacts (api/build-package-repo.sh) — exact values kept in
# sync by hand with that script's own ARTIFACT_URLS, same convention
# every other example's own pinned-artifact variables already use (see
# e.g. ghidra-workstation/variables.tf's ghidra_sha256), and identical
# to examples/distributed-llm's own — same underlying binaries, this
# example just serves the interactive Web UI they already come with
# instead of only exposing the raw API.
variable "llama_release_tag" {
  description = "llama.cpp release tag the CPU build binaries below were pulled from."
  type        = string
  default     = "b11025"
}

variable "llama_archive_name" {
  description = "Filename of the cached llama.cpp CPU build archive (api/build-package-repo.sh's own ARTIFACT_URLS)."
  type        = string
  default     = "llama-b11025-bin-ubuntu-x64.tar.gz"
}

variable "llama_sha256" {
  description = "SHA-256 of llama_archive_name — computed directly against the real downloaded artifact (llama.cpp's own GitHub releases publish no checksums file for this asset), verified again on every node before the archive is unpacked."
  type        = string
  default     = "bfdb3743f689bfe312f6c529908a09fb5ed1249c817b448e6a138ebb9dceb35d"
}

variable "model_filename" {
  description = "GGUF model file the coordinator loads. Workers never need a copy of this — llama.cpp's RPC backend streams each worker its own share of tensor data over the network at load time, not a whole model file on disk (see module.workers' own comment in main.tf). Defaults to Qwen2.5-Coder-14B-Instruct rather than a general chat model or the smaller 7B — per direct request: \"code/code production/correction/assistance is all i really care about\" and, after the 7B still hallucinated in real testing, \"is there anything less likely to hallucinate.\" Its ChatML template supports a real system role (confirmed live), unlike Mistral-7B-Instruct-v0.3's own template (confirmed live via GET /props' chat_template_caps.supports_system_role: false), so webui_system_message actually reaches the model."
  type        = string
  default     = "Qwen2.5-Coder-14B-Instruct-Q4_K_M.gguf"
}

variable "model_sha256" {
  description = "SHA-256 of model_filename — Hugging Face's own X-Linked-ETag header for the LFS-backed file (its authoritative server-side content hash for this exact object, not self-computed from a partial download)."
  type        = string
  default     = "2946d28c9e1bb2bcae6d42e8678863a31775df6f740315c7d7e6d6b6411f5937"
}

# --- WebUI defaults ---------------------------------------------------
# llama-server's own built-in Web UI reads its default sampling/system-
# prompt settings from a JSON file passed via --webui-config-file —
# confirmed live against a real local instance (GET /props' own
# ui_settings key echoes this file's contents verbatim back to the
# frontend at page load). Defaults below deliberately lower temperature
# from llama-server's own built-in default (0.8) and add a system
# prompt discouraging invented claims about code — per direct request:
# "what can we do to help prevent hallucination and prevent claims
# about code that doesn't actually exist in it's answer" followed by
# "configure the web ui defaults to a more technical configuration (per
# sampling temperature)." A real stress-test response at these defaults
# is what actually surfaced the hallucination problem this is fixing
# (wrong percentile math + a described-but-never-called sorted() call)
# — this doesn't make the model incapable of being wrong, only less
# likely to wander at the sampling level and more explicitly told not
# to invent functionality. Still fully user-editable per-session in the
# browser's own Settings panel — this only changes what a fresh session
# starts from.
variable "webui_temperature" {
  description = "Default sampling temperature the coordinator's Web UI starts each new session with (llama-server's own built-in default is 0.8). Lower is more deterministic/less prone to invented detail — chosen for a 'technical' default per direct request, not because the model itself is unable to produce imprecise text at 0.2."
  type        = number
  default     = 0.2
}

variable "webui_system_message" {
  description = "Default system prompt the coordinator's Web UI starts each new session with — sets ground rules the model doesn't always follow but is measurably steered by, aimed at the specific hallucination pattern found in a real stress-test response (claiming code does something the actual code shown doesn't do)."
  type        = string
  default     = "You are a technical assistant. Only describe what code actually does — never claim a function, sort, or check exists unless it is genuinely present in the code you just wrote or were shown. If you are not certain something is correct, say so explicitly rather than stating it as fact. Prefer precise, verifiable statements over confident-sounding guesses."
}

# One entry per RPC worker instance — the whole point of this template.
# Each worker is pinned to a specific paired peer (see the Peers
# section, or the cloudcore_peers data source, for available hosts) and
# that peer's own vpc_id/subnet_id — a peer has its own separate
# catalogue, not this build's local one (see haFullStack-LLD.md §13,
# and load-balanced-web's own peer_vpc_id/peer_subnet_id for the same
# requirement explained in full). Must have at least one entry — an
# empty list would defeat the reason this template exists. Add more
# entries as more machines join CloudCore; nothing here is hardcoded to
# exactly two hosts.
#
# SECURITY: llama.cpp's own RPC backend documentation is explicit that
# it is a "proof-of-concept" that is "fragile and insecure," and warns
# "Never run the RPC server on an open network." This template takes
# that seriously — but the worker's security group is an EXISTING one
# on the peer (peer_security_group_id below), created outside this
# apply, so it cannot be enforced here. When creating it, restrict the
# RPC port (var.rpc_port) to var.cidr_block (this build's own local
# subnet, where the coordinator lives) — never 0.0.0.0/0. Reusing a
# broader existing security group defeats this protection.
variable "worker_peers" {
  type = list(object({
    peer_id                = string
    peer_vpc_id            = string
    peer_subnet_id         = string
    peer_security_group_id = string
  }))

  validation {
    condition     = length(var.worker_peers) > 0
    error_message = "At least one worker_peers entry is required — this template's whole purpose is cross-host distributed inference."
  }
}
