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

# Qwen2.5-Coder-14B-Instruct's Q4_K_M weights alone are ~8.37GB, which
# does not fit in a single CloudCore instance flavor. Splitting the
# model's layers across the coordinator and every worker is not a
# proof-of-concept nicety here — it's the only way this default model
# runs on this platform at all. See main.tf's own header comment for
# the fuller story, and examples/distributed-llm (same underlying
# mechanism, built first, for automated Sentinel-log-intelligence
# ingestion rather than a human chat session).
#
# Stepped up from the 7B default to 14B after real testing found the
# 7B model still hallucinated on non-trivial code tasks even with the
# anti-hallucination system prompt genuinely reaching it — per direct
# request ("is there anything less likely to hallucinate... code is
# all I really care about").
#
# coordinator_flavor and worker_flavor deliberately do NOT share one
# default any more — found live building the first real
# verify-proxy.service deployment: the coordinator is *always* local
# (module.coordinator in main.tf has no placement override, unlike
# workers), so its flavor's vCPU count is really a claim against
# *this specific host's* real core count, not a generic budget. A
# 6-vCPU standard.xlarge coordinator on a real 4-core host hit KVM's
# own "-accel kvm: warning: Number of SMP cpus requested (6) exceeds
# the recommended cpus supported by KVM (4)" and took 5-6x longer than
# normal to come up — standard.large (4 vCPU) is coordinator_flavor's
# own default specifically because it matches a real 4-core host
# exactly, not oversubscribed. worker_flavor stays standard.xlarge
# since a worker peer's own core count is checked for real (see
# api/capacity_gate.py) and this project's own paired peer genuinely
# has 8 cores. Making the coordinator itself peer-placement-aware
# (choosing whichever host — including a peer — has the most real
# spare capacity, the same way api/peers_routes.recommend_placement()
# already does for other resources) is a real, tracked follow-up, not
# solved by this flavor split alone — see
# llm-chat-verification-Phased-Implementation.md's own priority note
# for why it isn't done yet.
#
# Every worker peer's own real available RAM is still checked against
# the chosen worker_flavor BEFORE the build is even submitted
# (api/capacity_gate.py, reusing the same peers_routes.peer_stats()
# the traffic-light system already uses) — rejected with a clear
# message rather than letting a worker OOM partway through model load,
# regardless of which flavor is chosen.
variable "coordinator_flavor" {
  description = "Compute flavor for the coordinator instance. The coordinator is always local (never peer-placed today), so this is a real claim on THIS host's own core count, not a generic budget — see the comment above for a real KVM-oversubscription case this default was sized to avoid."
  type        = string
  default     = "standard.large"
}


# Direct live incident: a worker running at standard.xlarge (8GB)
# crashed (ggml_abort(), "Remote RPC server crashed or returned
# malformed response") holding 34 offloaded layers of the 14B model --
# `free -h` on the worker at the time showed only ~1.7GB free out of
# 7.8GB total, with the rpc-server process itself already using ~5GB
# just for its own share of the model + KV cache. The comment above
# sized this flavor by matching the peer's real CORE count (8) when
# worker_flavor was first split from coordinator_flavor -- but that
# was before the model default stepped up from 7B to 14B (see this
# file's own comment on model_filename), and the RAM figure was never
# revisited after that jump. standard.2xlarge (16GB) restores a real,
# comfortable margin -- confirmed live via GET /v1/peers/<id>/stats
# that llwyn-y-groes genuinely has 17GB+ available right now, so this
# isn't oversubscribing the host, just correcting a stale default.
variable "worker_flavor" {
  description = "Compute flavor for each RPC worker instance. Workers are peer-placed, and api/capacity_gate.py checks the target peer's own real capacity before the build is submitted, so this can safely be sized larger than coordinator_flavor when a peer genuinely has the cores/RAM for it."
  type        = string
  default     = "standard.2xlarge"
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
  description = "Default system prompt the coordinator's Web UI starts each new session with — sets ground rules the model doesn't always follow but is measurably steered by, aimed at the specific hallucination pattern found in a real stress-test response (claiming code does something the actual code shown doesn't do). Vestigial as of Phase 4: llama-server's own webui is no longer reachable through this deployment (see sandbox_system_message below), so this only still shapes --webui-config-file, which nothing reaches anymore. Left in place — the flag itself is harmless to keep passing."
  type        = string
  default     = "You are a technical assistant. Only describe what code actually does — never claim a function, sort, or check exists unless it is genuinely present in the code you just wrote or were shown. If you are not certain something is correct, say so explicitly rather than stating it as fact. Prefer precise, verifiable statements over confident-sounding guesses."
}

# Phase 4 — the interactive sandbox's own system prompt (verify_proxy.py's
# POST /sandbox/ask), genuinely distinct from webui_system_message above:
# that one only ever shaped llama-server's own general-purpose webui,
# which this phase makes unreachable. This one directly operationalizes
# "we're not aiming for general chat" as a real instruction to the
# model — a mitigation, not a guarantee (a system prompt can still be
# talked around); the actual safety net stays run_sandboxed()'s own
# real-execution grounding, same as Phases 1-2.
variable "sandbox_system_message" {
  description = "System prompt for the interactive sandbox's Ask panel — should keep the model on the student's own submitted code and decline off-topic requests, since nothing else in this deployment grounds a free-form answer. Stage 3: also explains the ```stdin fenced-block convention the model uses to supply input() values to a real, live, mid-execution-paused sandboxed process (verify_proxy.py's own run_sandboxed_interactive()/extract_stdin_value()) — superseded the earlier \"never use input()\" wording once the sandbox grew real interactive support. Stage 5 update: absorbed two lessons the general webui's own webui_system_message had already learned but this prompt never did — that one is vestigial (nothing reaches llama-server's own webui any more), so its wording just sat unused instead of actually helping. First, its explicit anti-hallucination instruction (born from a real stress-test failure this project found — a response with wrong percentile math and a described-but-never-called sorted()). Second, this prompt previously had zero awareness the Terminal panel (a real shell with real internet access, plus browser-reachable preview ports) exists at all — asked whether it can install a package or serve a web page, the model had no grounding for the true answer either way. Also added a short concision nudge, since this platform's own measured generation speed (~1 token/sec on modest hardware) makes an unnecessarily long answer a real, felt cost, not just a style preference."
  type        = string
  default     = "You are a lab coding assistant. Only discuss the Python code the student has provided in this conversation. If asked something unrelated to that code or to this lab exercise, politely decline and redirect the student back to their code. Only describe what code actually does -- never claim a function, sort, or check exists unless it is genuinely present in the code you just wrote or were shown; if you are not certain something is correct, say so explicitly rather than stating it as fact. When suggesting a fix, provide the complete corrected script in a single fenced python code block, and keep your own explanation concise -- this hardware generates slowly, so prefer a short, precise answer over a long one where both would be equally correct. Code you write runs in a real sandbox that supports interactive input() calls -- if a script you wrote is waiting for input, you will be shown exactly what it has printed so far and asked what to provide; reply with ONLY a fenced ```stdin block containing exactly the one line to send. This can happen a few times per script, not unlimited, so keep prompts short and avoid scripts that would need a long back-and-forth. This code sandbox is Python-only, one-shot, and has no network access. Separately, the page's own Terminal panel gives a real persistent Linux shell with genuine internet access (pip install, curl, cloning a repo) that is otherwise fully isolated, plus ports __PREVIEW_PORTS_LIST__ reachable from the browser for previewing a web app run there -- if asked about installing packages, running something long-lived, or viewing a web app's own output, say to use the Terminal (whose own panel lists the exact ports), not this code sandbox."
}

# Stage 8 — a second, genuinely separate system prompt for the new
# Linux Help panel (verify_proxy.py's POST /sandbox/linux-ask).
# Deliberately not scoped to "the student's own code" the way
# sandbox_system_message above is, and deliberately doesn't describe
# an auto-execution/fix loop the way that one does either -- a shell
# command suggested here isn't run automatically; it only runs if the
# student clicks the page's own "Run in Terminal" button against their
# already-live Terminal session. See verify_proxy.py's own
# LINUX_SYSTEM_MESSAGE comment for the full reasoning.
variable "linux_system_message" {
  description = "System prompt for the Linux Help panel — general Linux Q&A (\"admin to use\", per direct request) kept genuinely separate from the coding Ask panel's own prompt above: not scoped to the student's submitted code, and explicit that a suggested command is never run without the student clicking 'Run in Terminal' themselves, since auto-executing a model-suggested shell command against a student's live Terminal session was explicitly ruled out as a safety boundary. Also asks the model to build its own install-if-missing check into a suggested command (a `command -v X || sudo apt-get install -y X; X` idiom, combined into the SAME fenced block, not a separate naive attempt plus a second install-only block) rather than this side pre-flight-checking what's installed — after F-113's fdisk gap, deliberately chosen over a real pre-check mechanism, which would have to guess at a binary from arbitrary, possibly multi-command shell text with no reliable way to do so. Wording tightened twice from a first pass live-tested this session — an initial version got the check right but still emitted a redundant plain attempt alongside it; the final wording (explicit 'give ONLY ONE block' instruction) produces exactly one correct combined command, confirmed against three different real questions (fdisk, traceroute, htop). Stage 9 update: once a suggested command IS run, its real result is now automatically checked (verify_proxy.py's own runCommandInTerminal()/_captureAndDiagnose()) and, on a real failure, relayed back to the model as a new turn asking for a fix — this prompt is told that explicitly so it doesn't contradict what the student is actually seeing when that automatic turn arrives."
  type        = string
  default     = "You are a Linux help assistant for a lab environment. Answer any Linux question, from everyday usage (files, permissions, searching, editors) through real system administration (systemd, networking, package management, users and groups, disk and filesystem, cron, log inspection) -- the student may be a complete beginner or already comfortable at the command line, so don't assume either. Only describe what a command actually does -- never claim a flag or behavior exists unless you are genuinely sure of it; if you are not certain something is correct, say so explicitly rather than stating it as fact. When you suggest a command, put it in its own fenced ```bash code block so the student can run it with one click -- a command you suggest is NEVER run without the student clicking 'Run in Terminal' themselves, but once they do, the real result IS automatically checked: if it fails, you will be shown the real terminal transcript and exit code and asked to diagnose it and suggest a fix, same as this turn. This hardware generates slowly, so keep answers short and precise rather than long where both would be equally correct. The student's own Terminal panel is a real, minimal Ubuntu 22.04 shell with genuine internet access, but: its package index only covers the 'main' archive component (a 'universe' package needs another route), it has no persistent storage across sessions, and it cannot reach anything on the local network except the real internet. Ports __PREVIEW_PORTS_LIST__ are reachable from the student's browser for previewing anything they serve there. This is a genuinely minimal image -- ordinary tools you might expect (e.g. fdisk) are often not preinstalled. If a command you suggest might need one, give ONLY ONE fenced ```bash block for it, combining the install check and the real command with `||` in that single block -- for example exactly `command -v fdisk >/dev/null || sudo apt-get install -y fdisk; fdisk -l /dev/vda` (adjust the tool/package name and real command). Do NOT also show a plain, naive version of the command on its own first -- that copy would just fail with 'command not found' if the tool is missing, defeating the whole point. The student should only ever need to click 'Run in Terminal' once, on the one block you give them. Politely decline anything clearly unrelated to Linux or this lab and redirect back to that."
}

# Stage 4 — per-client rate limiting for the sandbox's own /sandbox/run
# and /sandbox/ask endpoints, rolled up from the Phase 4 doc's own
# "Explicitly out of scope" list. Keyed by the real client IP via
# X-Forwarded-For, which examples/llm-chat's own LB sets (option
# forwardfor, HTTP mode) — see verify_proxy.py's own _client_ip().
variable "rate_limit_run_per_minute" {
  description = "Maximum POST /sandbox/run requests a single client IP may make per rolling 60-second window before getting a 429. Guards against one student's script loop or a runaway client hammering the sandbox."
  type        = number
  default     = 10
}

variable "rate_limit_ask_per_10min" {
  description = "Maximum POST /sandbox/ask requests a single client IP may make per rolling 10-minute window before getting a 429. Ask is far more expensive than Run (a real model generation plus sandboxed execution, possibly several fix rounds), so its window and limit are both wider than run's."
  type        = number
  default     = 10
}

# --- Grounded code verification ----------------------------------------
# Per direct request: prompting alone couldn't be trusted to prevent
# hallucination ("the lab students can't be allowed to walk away with
# false education or confidence... even if the answers are wrong then
# we can show why and how it might be put right") — so instead of only
# asking the model to be accurate, any Python code block in a response
# is actually run in a sandbox on the coordinator (see files/
# verify_proxy.py) and the real result is appended into the same chat
# turn, clearly labeled as executed rather than model output. See
# llm-chat-verification-Phased-Implementation.md for the full design.
variable "enable_verification" {
  description = "Whether the coordinator actually executes Python code blocks and appends the real result to each response. Off falls back to today's behaviour (the model's own narrative, unverified)."
  type        = bool
  default     = true
}

variable "verify_timeout_seconds" {
  description = "Wall-clock ceiling (seconds) for a single sandboxed execution — both a hard subprocess timeout and the sandbox's own RLIMIT_CPU. A runaway loop is killed and shown as killed, not silently retried."
  type        = number
  default     = 15
}

variable "verify_max_memory_mb" {
  description = "RLIMIT_AS ceiling (MB) applied to a single sandboxed execution via the unprivileged sandboxrunner user — a script that exceeds it is killed by the kernel, and that real failure is shown like any other."
  type        = number
  default     = 256
}

# Phase 2 — grounded fix loop. Widened from an initial default of 1 to
# 3 per direct follow-up ("close the gap [on being able to] iterate
# until acceptable/correct performance/output is met") — a single
# automatic attempt was judged too thin for that to feel real. Each
# round is grounded in the REAL traceback from the attempt before it
# (never another unverified guess), and only the truly final block in
# the whole chain ever tells the student to ask again themselves.
variable "verify_max_fix_rounds" {
  description = "How many automatic grounded fix-and-reverify rounds follow a real execution failure, each one a fresh completion given the actual traceback from the attempt before it. Bounds compute cost against a stubbornly-wrong model — this is a ceiling, not a guarantee every round runs (it stops as soon as one actually passes)."
  type        = number
  default     = 3
}

# Direct report: real Linux Help/Ask answers were regularly cut off
# mid-word ("often find the llm running out of steam or trailing
# off"), and manually asking again ("push for it to finish") reliably
# completed it. Root cause: llama-server's own -c context_size ceiling
# (not the model choosing to stop) -- the OpenAI-compatible endpoint's
# final streamed chunk carries finish_reason: "length" whenever that
# happens, which verify_proxy.py never used to even read. Same
# ceiling-not-guarantee shape as verify_max_fix_rounds above, just
# triggered by a truncated response instead of a failed execution --
# bounds compute cost against a reply that keeps hitting the ceiling
# every round (a long enough conversation history eventually will,
# regardless of how large context_size is) rather than looping forever.
variable "max_continuation_rounds" {
  description = "How many automatic 'continue exactly where you left off' rounds follow a real llama-server response that was cut off by the context-window ceiling (finish_reason: \"length\"), each one relayed seamlessly into the same answer with no student action needed. A ceiling, not a guarantee every round runs — it stops as soon as one round actually finishes naturally (finish_reason: \"stop\")."
  type        = number
  default     = 3
}

# F-129 correction: earlier /proc/<pid>/task/*/stack snapshots (F-118,
# F-128) that looked like a permanent llama-server/ggml-rpc deadlock
# were, as far as this deployment has been able to confirm, genuinely
# slow, in-progress work, not a hang -- a direct request to
# llama-server, bypassing this whole watchdog, completed successfully
# with real content after 297s; llama-server's own `timings` object
# put prefill alone at 142.6s (4.03 tok/s) for the real production
# system-message length, already past the old 120s default before a
# single token could exist. Raw network throughput (20MB in 0.9s)
# rules out link bandwidth -- this is compute-bound (a large model,
# RPC-split compute, and a coordinator host under real contention),
# not a protocol bug. Raised well above the measured 297s worst case.
# See haFullStack-Findings-Log.md F-129/F-130 -- this alone does not
# guarantee a working end-to-end answer (F-130 is a separate, still-
# open issue in verify_proxy.py's own relay), only that a genuinely
# slow-but-successful response isn't killed before it can finish.
variable "generation_stall_timeout_seconds" {
  description = "How long a response can go with zero real content (not just any byte -- llama-server's own SSE keep-alive pings keep the raw connection alive even during a long, legitimate prefill/generation) before verify_proxy.py treats it as stalled and automatically restarts llama-server.service in the background. Set with real margin above this platform's own measured worst-case total latency (prefill + generation) for a realistic prompt, not just per-token latency -- see F-129's own direct timing measurement."
  type        = number
  default     = 420
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
# F-126: empty is now a real, deliberately supported configuration —
# direct request to keep this whole worker_peers/RPC-offload mechanism
# fully intact for later (better hardware) while letting the
# coordinator run the entire model itself in the meantime, on
# whichever single host (including a peer, via coordinator_peer_id)
# can actually carry it alone. The old validation rule below required
# at least one entry, on the reasoning that cross-host splitting was
# this template's whole purpose — true when it was written, but no
# longer the only supported shape once a real, current-hardware
# constraint (this build's own coordinator host being a shared dev
# desktop, not a dedicated inference box) made "run it all on one
# other, more capable peer instead" the right call right now.
# llama_rpc_flags (locals.tf) already degrades correctly to "no
# offload, run every layer locally" when this is empty — nothing else
# assumes a worker exists.
variable "worker_peers" {
  type = list(object({
    peer_id                = string
    peer_vpc_id            = string
    peer_subnet_id         = string
    peer_security_group_id = string
  }))
  default = []
}

# --- Coordinator placement -----------------------------------------------
# The coordinator is local by default (all four below empty) — exactly
# today's only behaviour. Per direct follow-up ("should we not place
# the co-ordinator on the peer with the best available resource?"):
# this host's own core count is a real, fixed ceiling regardless of
# which flavor is chosen (see coordinator_flavor's own comment for a
# real KVM-oversubscription case this caused), so sometimes the right
# answer is putting the coordinator itself on a peer instead. Same
# shape as worker_peers' own four fields, applied to a single instance
# instead of a list — `api/capacity_gate.py` checks a peer-placed
# coordinator's real available RAM the same way it already does for
# workers, and both Build Managers' own submit routes reject a request
# where coordinator_peer_id matches a worker's own peer_id (same
# machine hosting both roles defeats the entire reason this template
# splits across hosts via RPC).
#
# Deliberately NOT auto-selected server-side the way
# api/layer_split.py auto-fills rpc_offload_layers — a worker's own
# security group is an existing one on that peer, scoped narrowly to
# the RPC port; it was never designed for a coordinator's own needs
# (SSH + the chat HTTP UI). Blindly reusing "the peer's first SG" the
# low-risk way the dashboard auto-picks a first VPC/subnet would be a
# real, silent security decision. Leaving this to a human choosing
# from the Dashboard's own live cascading dropdown (which already
# shows the real SG options to review before submitting — the exact
# same `_peer_id`-suffix convention worker_peers already gets picked
# up by, `ui/src/js/16-build-manager.js`'s own `_BM_PEER_ID_RE`, no
# new frontend code needed) keeps that a deliberate choice, not an
# automated one.
variable "coordinator_peer_id" {
  description = "Approved peer to place the coordinator instance on instead of this host. Empty (default) keeps today's behaviour — coordinator always local. Must not match any worker_peers[].peer_id — rejected server-side before the build is even submitted if it does."
  type        = string
  default     = ""
}

variable "coordinator_peer_vpc_id" {
  description = "The chosen coordinator_peer_id's own VPC to place the coordinator in — a peer has its own separate catalogue, not this build's local one. Ignored when coordinator_peer_id is empty."
  type        = string
  default     = ""
}

variable "coordinator_peer_subnet_id" {
  description = "The chosen coordinator_peer_id's own subnet. Ignored when coordinator_peer_id is empty."
  type        = string
  default     = ""
}

variable "coordinator_peer_security_group_id" {
  description = "The chosen coordinator_peer_id's own existing security group — must actually allow SSH (22) and http_port from admin_cidr; this is not verified automatically (see the SECURITY note above for why). Ignored when coordinator_peer_id is empty."
  type        = string
  default     = ""
}

# Deliberately NOT cloudcore_api_token — that name is reserved for the
# OpenTofu *provider's* own auth (tofu_engine.py's _build_env()
# explicitly excludes cloudcore_api_token/cloudcore_api_url from ever
# becoming a real Terraform variable, so a variable with that name
# here would silently never receive the value a caller actually
# submits). This is a genuinely separate credential: what the
# coordinator's own verify-proxy.service uses to authenticate its
# POST back to the CloudCore API's examples-capture endpoint
# (api/examples_listener.py, Phase 3). Defaults to the same
# "dev-token" every other unauthenticated-by-default piece of this lab
# stack uses; override to match a real deployment's CLOUDCORE_API_TOKEN.
variable "examples_ingestion_token" {
  description = "Bearer token verify-proxy.service uses to POST captured grounded-verification examples back to the CloudCore API (api/llm_examples_routes.py's ingest_example). Must match the API host's own CLOUDCORE_API_TOKEN."
  type        = string
  default     = "dev-token"
}

# --- Stage 5: Firecracker sandbox shell ---------------------------------
# Per direct request: the model-driven sandbox should also give students a
# real interactive shell with genuine internet access, isolated so it
# "can't jailbreak the sandbox and have access to anything else other than
# the sandbox and network." Built on Firecracker microVMs (a real, separate
# guest kernel under KVM — confirmed live that the coordinator has nested
# KVM available), not a container sandbox, since a persistent network-
# connected shell has far more opportunity to probe a shared-kernel
# boundary than the existing bounded, network-less Python sandbox ever
# did. See llm-chat-interactive-sandbox-Phased-Implementation.md's own
# Stage 5 section for the full design and its live isolation verification.
#
# Firecracker + jailer — same pinned-download-plus-checksum convention as
# llama_release_tag/llama_archive_name/llama_sha256 above, kept in sync by
# hand with api/build-package-repo.sh's own ARTIFACT_URLS. Verified
# directly against the real downloaded archive (both the whole-archive
# hash here and firecracker/jailer's own per-binary hashes, checked again
# on the guest via the archive's own bundled SHA256SUMS).
variable "firecracker_release_tag" {
  description = "Firecracker release tag the firecracker/jailer binaries below were pulled from."
  type        = string
  default     = "v1.17.0"
}

variable "firecracker_archive_name" {
  description = "Filename of the cached Firecracker release archive (api/build-package-repo.sh's own ARTIFACT_URLS) — contains both firecracker and jailer."
  type        = string
  default     = "firecracker-v1.17.0-x86_64.tgz"
}

variable "firecracker_sha256" {
  description = "SHA-256 of firecracker_archive_name, verified directly against the real downloaded artifact (matches the archive's own published .sha256.txt release asset)."
  type        = string
  default     = "06094a1108ae9e82aa4c23a775aa92758f53f1175d422270d9d6162cb9ade558"
}

# A pinned kernel build from Firecracker's own public CI artifact bucket —
# the documented, official source for exactly this (see
# firecracker-microvm/firecracker's own docs/getting-started.md), not
# built from source here. The kernel's own version need not track the
# firecracker_release_tag above 1:1 — Firecracker maintains compatibility
# across CI kernel builds and release versions independently.
variable "firecracker_kernel_name" {
  description = "Filename of the cached Firecracker-compatible guest kernel (an uncompressed ELF vmlinux, not bzImage) — api/build-package-repo.sh's own ARTIFACT_URLS."
  type        = string
  default     = "firecracker-vmlinux-6.1.155"
}

variable "firecracker_kernel_sha256" {
  description = "SHA-256 of firecracker_kernel_name, verified directly against the real downloaded artifact (Firecracker's CI bucket publishes no separate checksums file for this asset)."
  type        = string
  default     = "e20e46d0c36c55c0d1014eb20576171b3f3d922260d9f792017aeff53af3d4f2"
}

# The golden guest rootfs — deliberately NOT Firecracker's own quickstart
# demo image (a shared squashfs + a shared public demo SSH key, fine for a
# single-user tutorial, wrong for a multi-tenant lab). Built fresh by
# api/build-firecracker-rootfs.sh (debootstrap, minimal Ubuntu 22.04,
# sshd + a "student" account, a one-shot boot unit that fetches THIS
# session's own SSH public key from Firecracker's MMDS — no key ever
# baked into the image itself) — run that script by hand to rebuild and
# update these two values, same cadence as build-package-repo.sh itself.
variable "firecracker_rootfs_name" {
  description = "Filename of the golden Firecracker guest rootfs image (api/build-firecracker-rootfs.sh's own output, served via api/build-package-repo.sh's ARTIFACT_URLS path)."
  type        = string
  default     = "firecracker-rootfs-jammy.ext4.gz"
}

variable "firecracker_rootfs_sha256" {
  description = "SHA-256 of firecracker_rootfs_name — printed by api/build-firecracker-rootfs.sh itself after each build; update this value by hand whenever that script is re-run."
  type        = string
  default     = "d4662e397860a133ce7a2b8d803faee55ade15ccd297a39c7e48f76dded28fb4"
}

# Deliberately outside both the platform's own real bridge range
# (192.168.x.0/24 — see coordinator-cloud-init.yaml.tftpl's own comment on
# fcbr0 for why that's the thing this whole subnet must never be able to
# reach) and this example's own fictional declared VPC CIDR (var.cidr_block,
# 10.91.0.0/16 by default) — no real or apparent overlap with either.
variable "sandbox_subnet_cidr" {
  description = "Private /24 the coordinator's own fcbr0 bridge uses for per-session Firecracker microVMs. Each microVM gets one address on this subnet via the coordinator's own dnsmasq; the coordinator's iptables rules NAT it out to the real internet while dropping every RFC1918 destination (see the cloud-init template's own runcmd for the exact ruleset) — this is the actual enforcement point for 'nothing but the sandbox and the network.'"
  type        = string
  default     = "10.200.0.0/24"
}

# --- Stage 5B: the sandbox terminal's own WebSocket service ------------
# sandbox_terminal.py — a new, separate systemd service from verify-
# proxy.service (deliberately: it needs websockets + paramiko, which
# don't belong bolted onto verify_proxy.py's own zero-dependency
# stdlib-only posture). Reachable through the same LB listener as the
# rest of the sandbox via a new path routing rule (main.tf), not a new
# LB port.
variable "terminal_port" {
  description = "Loopback port sandbox_terminal.py's own WebSocket server binds to — distinct from http_port (verify-proxy's own port), reached via a new LB path routing rule instead of a new LB listener."
  type        = number
  default     = 8622
}

# Defaults sized against this repo's own existing conventions
# (RATE_LIMIT_RUN_PER_MINUTE=10, verify_timeout_seconds=15,
# idle_watcher.py's 60-120min *deployment*-level idle default — this is
# a *per-session* idle timeout, tracking one active shell, not "is
# anyone using this deployment at all," hence the much shorter default).
variable "terminal_idle_timeout_minutes" {
  description = "A sandbox terminal session with no WebSocket activity for this long is closed automatically."
  type        = number
  default     = 15
}

variable "terminal_max_session_minutes" {
  description = "Hard wall-clock cap on a single sandbox terminal session, regardless of activity — forces periodic re-provisioning rather than one microVM running indefinitely."
  type        = number
  default     = 60
}

variable "terminal_max_concurrent_sessions" {
  description = "Maximum sandbox terminal sessions a single client IP may have open at once. Each microVM needs real dedicated host memory, so this bounds RAM/CPU exposure — a request past this limit gets a clear 'capacity full' message, never silent overcommit."
  type        = number
  default     = 4
}

variable "terminal_boot_timeout_seconds" {
  description = "Ceiling on how long sandbox_terminal.py waits for a freshly-booted microVM to become SSH-reachable before failing the WebSocket connection with a clear error."
  type        = number
  default     = 20
}

# Direct follow-up after discussing whether the platform is prepared
# for a student running something destructive (e.g. partitioning the
# running root fs) from a Terminal session started off a Linux Help
# suggestion. The architecture already recovers cleanly (every session
# is a disposable microVM, and Disconnect's client-side ws.close() plus
# the server's SIGKILL-backed teardown() don't depend on the guest
# responding at all) — the real gap was a guest that's unusable but
# whose SSH channel doesn't actually error (kernel/sshd still resident
# in RAM), which the existing idle-timeout can't detect since retyping
# into a dead shell still counts as activity. This tracks a genuinely
# different signal instead: real input sent with no real output
# following it for this long.
variable "terminal_unresponsive_seconds" {
  description = "If a sandbox terminal session has sent real input but received no shell output for this long, the student is shown a 'may be stuck — start a fresh session?' hint (with a one-click restart) rather than staring at a silently dead shell. Deliberately generous — a legitimately slow command (a big apt install, a large download) shouldn't false-positive — and phrased as a suggestion, not an assertion, since this signal alone can't distinguish 'stuck' from 'just slow'."
  type        = number
  default     = 30
}

# Per direct request: a student's own program very often needs more
# than one port at once (a frontend + an API, a websocket alongside an
# HTTP port, etc.), so this is a fixed pool decided once at this
# example's own deploy time ("llm-chat inception time"), not something
# picked per-session or left to the student to request — same
# fixed-at-deploy reasoning terminal_port itself already uses. Four high
# ports, deliberately out of any well-known/commonly-used range, so a
# student's own choice of port inside their program is very unlikely to
# collide with anything already meaningful on this host. sandbox_
# terminal.py opens one small reverse-proxy listener per port (same
# process that already owns each session's own microVM IP) and routes
# each incoming connection to whichever student's own currently-open
# terminal session it came from — see that file's own PREVIEW_PORTS
# handling for the full mechanism.
variable "preview_ports" {
  description = "Fixed pool of high ports reachable from the browser, reverse-proxied into whichever microVM a student's own terminal session is currently using — lets a student run and view a web app (Flask, a static file server, etc.) they wrote in the sandbox terminal."
  type        = list(number)
  default     = [41001, 41002, 41003, 41004]
}

# jammy's own python3-websockets (9.1-1) is confirmed BROKEN on jammy's
# own current Python 3.10.12 — it calls asyncio.Lock(loop=...), a
# parameter Python 3.10 removed outright, so every single WS connection
# crashed with a real TypeError before this was caught live. A newer
# version is vendored instead — 16.1.1 is the newest release that still
# supports Python 3.10 (17.x requires 3.11+, confirmed via PyPI's own
# metadata), a real manylinux wheel pinned via PyPI's own JSON API, not
# guessed — same pinned-artifact convention as every other third-party
# dependency in this deployment.
variable "websockets_wheel_name" {
  description = "Filename of the cached websockets wheel (api/build-package-repo.sh's own ARTIFACT_URLS) — extracted into /opt/llama.cpp/vendor-py/ and referenced via sandbox-terminal.service's own PYTHONPATH=, since jammy's apt-archive websockets package is broken on jammy's own Python version."
  type        = string
  default     = "websockets-16.1.1-cp310-manylinux.whl"
}

variable "websockets_wheel_sha256" {
  description = "SHA-256 of websockets_wheel_name, confirmed directly against PyPI's own JSON API (pypi.org/pypi/websockets/16.1.1/json) for this exact wheel file."
  type        = string
  default     = "1214e673c404684b9bf7154f5cf43b45025b1a6160fac3a9e438e9c1a97e22cb"
}

# --- Debug access (F-127 follow-up) ---------------------------------------
# Direct request, short-term: give Claude Code its own real SSH access to
# every instance this template creates, since diagnosing F-127's own
# still-open deadlock (and anything like it in the future) needs a live
# /proc/<pid>/task/*/stack capture -- something impossible to get from the
# outside, and impossible for Claude Code itself when the instance lands
# on a peer host it has no other access to (see haFullStack-Findings-Log.md
# F-127's own "requires the user's own real-time SSH access" note). A
# dedicated keypair (api/keys/claude_debug_ed25519*, gitignored, generated
# once locally -- never committed) rather than reusing CloudCore's own
# inter-instance keypair, so this access is distinguishable in auth logs
# and independently revocable without touching inter-instance SSH at all.
# Explicitly toggleable per direct request ("allow it to be turned off via
# the template should it be required") -- flip to false and rebuild to
# drop it from every instance this template creates.
variable "enable_claude_debug_access" {
  description = "Add a sudo-capable 'claude-debug' user (with the dedicated key in claude_debug_ssh_public_key) to every instance this template creates, for live diagnosis of stalls/deadlocks. Set false to omit it entirely."
  type        = bool
  default     = true
}

variable "claude_debug_ssh_public_key" {
  description = "Public half of the dedicated claude-debug keypair (api/keys/claude_debug_ed25519.pub) -- a public key, not a secret, safe to bake into this default. Only used when enable_claude_debug_access is true."
  type        = string
  default     = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIBsvg7yGQHL+ezs9craT31EBXuZb9PzBLs3CX4/7tIii claude-debug@cloudcore"
}
