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
  default     = "10.90.0.0/16"
}

# standard.large (4 vCPU / 4096MB / 40GB — api/compute.py's own biggest
# flavor today) for both roles, deliberately: Mistral-7B-Instruct-v0.3's
# Q4_K_M weights alone are ~4.37GB, which does not fit in ANY single
# CloudCore instance flavor that exists right now. Splitting the model's
# layers across the coordinator and every worker (roughly half each,
# with exactly one worker) is not a proof-of-concept nicety here — it's
# currently the only way this specific model runs on this platform at
# all. See main.tf's own header comment for the fuller story.
variable "coordinator_flavor" {
  description = "Compute flavor for the coordinator instance."
  type        = string
  default     = "standard.large"
}

variable "worker_flavor" {
  description = "Compute flavor for each RPC worker instance."
  type        = string
  default     = "standard.large"
}

variable "admin_cidr" {
  description = "CIDR allowed to reach SSH (22) and the inference HTTP API (http_port) on the coordinator's own security group."
  type        = string
  default     = "0.0.0.0/0"
}

variable "http_port" {
  description = "Host port the load balancer (and llama-server itself) listens on for the OpenAI-compatible HTTP API."
  type        = number
  default     = 8610
}

variable "rpc_port" {
  description = "Port each RPC worker's ggml-rpc-server listens on. Deliberately NOT exposed beyond the coordinator's own subnet — see the SECURITY note on worker_peers below."
  type        = number
  default     = 50052
}

# Conservative default, not llama-server's own "load from model" (0):
# KV-cache memory scales with context_size, and each participant here
# only has ~4096MB total for its own share of weights + KV cache + OS
# overhead — leaving that uncapped risked an OOM discovered only at
# request time, not at boot.
variable "context_size" {
  description = "Inference context window size (tokens), passed to llama-server's own -c flag. Kept modest given each node's own tight RAM ceiling (see coordinator_flavor's own comment)."
  type        = number
  default     = 2048
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
# it with the coordinator's own local CPU -- found live: it tried to
# allocate a ~4.3GB buffer on a worker with only 4096MB RAM and failed
# outright. Mistral-7B-Instruct-v0.3 has 32 transformer layers; 16 here
# gives a roughly even coordinator/worker split. Changing model_filename
# to a model with a different layer count (or adding more worker_peers
# entries) means re-tuning this by hand -- there's no automatic
# even-split behavior to rely on.
variable "rpc_offload_layers" {
  description = "Number of model layers to offload to the RPC worker(s) via -ngl. Tune this alongside model_filename/worker_peers — see the comment above for why 99 (the usual GPU-offload convention) is wrong here."
  type        = number
  default     = 16
}

# --- Pinned artifacts (api/build-package-repo.sh) — exact values kept in
# sync by hand with that script's own ARTIFACT_URLS, same convention
# every other example's own pinned-artifact variables already use (see
# e.g. ghidra-workstation/variables.tf's ghidra_sha256).
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
  description = "GGUF model file the coordinator loads. Workers never need a copy of this — llama.cpp's RPC backend streams each worker its own share of tensor data over the network at load time, not a whole model file on disk (see module.workers' own comment in main.tf)."
  type        = string
  default     = "Mistral-7B-Instruct-v0.3-Q4_K_M.gguf"
}

variable "model_sha256" {
  description = "SHA-256 of model_filename — Hugging Face's own X-Linked-ETag header for the LFS-backed file (its authoritative server-side content hash for this exact object, not self-computed from a partial download)."
  type        = string
  default     = "1270d22c0fbb3d092fb725d4d96c457b7b687a5f5a715abe1e818da303e562b6"
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
