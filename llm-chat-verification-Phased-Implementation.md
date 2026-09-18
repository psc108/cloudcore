# llm-chat — Grounded Code Verification: Phased Implementation

Paul Scott | Plan approved 2026-09-18

---

## Context

Per direct request, following a real pattern found testing
`examples/llm-chat` against progressively larger models (Mistral-7B →
Qwen2.5-Coder-7B → Qwen2.5-Coder-14B): every single test response,
regardless of model size, contained code that looked plausible but was
subtly or badly wrong (a no-op "rolling window" dedup, an `IndexError`
crashing on ordinary input, fabricated function names) — and in every
case the model's own prose *confidently asserted the code was
correct*. The user's own words: "the learning experience being
accurate and if not accurate then explainable as to it's lack of
accuracy is most important. the lab students can't be allowed to walk
away with false education or confidence... even if the answers are
wrong then we can show why and how it might be put right."

This is deliberately not another "pick a better model" attempt — that
was already explored and hit real, diminishing returns (see
`haFullStack-Findings-Log.md` v0.64/v0.65). The fix here is
architectural: stop presenting the model's own narrative about its
code as trustworthy on its own. Pair every code-bearing response with
real, sandboxed execution evidence, shown to the student as clearly
separate ground truth — then, once that's proven solid, close the loop
by feeding real failures back to the model so its *fix* is grounded in
an actual traceback instead of another unverified guess.

**Scope**: `examples/llm-chat` only (the human-facing chat
deployment). `examples/distributed-llm` (automated Sentinel ingestion)
is a different, non-interactive consumer and stays untouched.

| # | Phase | Status |
|---|---|---|
| 1 | Sandboxed execution + honest display (Python-only, single turn) | Done — verified live (real `numpy` `ModuleNotFoundError` shown honestly, single `[DONE]`) |
| 2 | Grounded fix loop | Done — verified live (real 3-round fix loop against a genuine `numpy` failure, correct round-limiting, single "ask again" invite only on the final block) |
| PRIORITY | Coordinator placement-awareness | Code complete, verified as far as topology allows — 2026-09-18 |
| 3 | Central learning corpus + student review page | Not started |
| 4 | Interactive sandbox (placeholder — needs its own document) | Not started |

---

## Research findings this plan is grounded on

- No sandboxed/restricted-execution utility exists anywhere in this
  codebase today (checked `api/*.py` for `setrlimit`/`prlimit`/
  `seccomp`/`chroot`/`nsjail`/`firejail` — none found). This is new,
  not a rewire of something existing.
- `examples/ghidra-workstation`'s LB target group points straight at
  noVNC's own port with zero CloudCore-authored software in the
  request path (`main.tf:102-124`, explicit comment: "No module wraps
  target groups/listeners yet... TCP passthrough straight to the
  instance's noVNC port"). Same is true for `llm-chat` today
  (`main.tf:162-190` — `cloudcore_lb_target_group.coordinator` targets
  the coordinator instance's `var.http_port` directly, i.e. llama-
  server itself). This is the first CloudCore-authored proxy sitting
  in front of an example's own tool — a new pattern, not reuse of an
  existing one.
- Real, directly relevant precedent found: `examples/ha-frontend-lb`
  runs a CloudCore-authored Python HTTP service as its own guest-VM
  systemd unit — `serve-ca-certs.py`
  (`files/ca-cloud-init.yaml.tftpl:130-153`, mirrored in
  `ansible/examples/templates/ha-frontend-lb/ca-cloud-init.yml.j2`),
  built on stdlib `http.server.HTTPServer`, **not Flask** — no `Flask`
  app exists anywhere under any `examples/*/files/*.tftpl` or
  `ansible/examples/templates/*/*.j2`. This settles the implementation
  choice below: stdlib only, matching the one real precedent for this
  exact kind of guest-side service, and avoiding adding a new pip
  dependency to the coordinator image for what stdlib already covers.

---

## Architecture

**Where it runs**: on the coordinator VM itself (same instance as
llama-server) — not a separate throwaway VM. The coordinator is
already disposable/rebuilt-per-run, and a same-VM subprocess sandbox
avoids the real added latency of provisioning a fresh VM per code
block on an already CPU-slow (~1 tok/s at 14B) deployment.

**How it intercepts traffic without touching llama-server's own
webui**: llama-server's chat UI is a compiled SvelteKit bundle we
don't own the source of and deliberately don't patch (matches this
project's own "serve the upstream tool's UI as-is" convention). Two
hard constraints follow from that:
1. We cannot add a "Verify" button or any new UI element.
2. Conversation/turn history is stored **client-side**, in the
   browser's own IndexedDB (confirmed by inspecting the real webui
   bundle) — a server-side component cannot inject a new, separate
   turn into a student's visible thread.

Both are satisfied by one move: a new stdlib-`http.server` reverse
proxy — `verify-proxy.service` — takes over the port the load balancer
already points at (`var.http_port`); llama-server itself moves to an
internal-only port (`127.0.0.1:8721`, a fixed local constant, not a
new user-facing variable — nothing needs to override it). The proxy
passes every request through unchanged (including `/health`, so the
LB's existing health check needs no changes) **except** it duplicates
the SSE stream for `POST /v1/chat/completions`: forward chunks to the
browser in real time exactly as llama-server sends them (so the
existing chat UI renders normally, with no added latency perceived
until generation actually finishes), while also accumulating the full
assistant text server-side. Once the model's own stream ends, if the
accumulated text contains a fenced Python code block, run the sandbox,
then **append** the result as more streamed content — same SSE
connection, same turn, same chat bubble — clearly delimited (e.g.
`\n\n---\n### ✅ Actually executed (not model output)\n`) before
finally sending `[DONE]`. No frontend changes, no new "turn" needed,
and the distinction between "what the model said" and "what really
happened" is visually unambiguous to the student because it's a
clearly labeled block within the same message, not a separate claim.

**Sandbox** (Python-only for v1 — matches every test prompt used so
far): stdlib only, no new packages on the guest image:
- `unshare --net --pid --fork --mount-proc` (stock `util-linux`,
  already on the Ubuntu 22.04 image) — no network namespace, no
  visibility into other host processes.
- Dedicated unprivileged system user (`sandboxrunner`, no login, no
  home dir) — never root, never the `ubuntu` user.
- `resource.setrlimit` (Python stdlib) for CPU time, address space,
  process count (no fork bombs), file size.
- A wall-clock `timeout` wrapper on top of the above.
- Fresh `tempfile.mkdtemp()` per run as the execution cwd, deleted
  immediately after.
- **No synthetic test fixtures are auto-generated.** If the model's
  code expects a CSV file that doesn't exist, the real
  `FileNotFoundError` is itself honest, valid signal — trying to
  guess/fabricate a fixture the model never specified would
  reintroduce exactly the kind of guessing this feature exists to
  eliminate.
- Real stdout, stderr, exit code, and wall-clock time are all captured
  verbatim — never summarized or reworded before display.

**Keeping the LB connection alive during the longer round-trip**:
HAProxy's `timeout client`/`timeout server` (`api/lb.py`, 300s) is an
*inactivity* timer, confirmed empirically this session (a real 7m28s
response completed because tokens kept streaming). The sandbox run
itself is sub-second; Phase 2's second LLM call is not — the proxy
sends periodic SSE comment heartbeat lines (`: verifying...`) during
that gap so the connection is never silent long enough to trip the
existing timeout, reusing a mechanism already proven this session
rather than inventing a new one.

---

## Phase 1 — Sandboxed execution + honest display

Python-only, single turn, no auto-fix loop. This is the provable
milestone: "even when the model is wrong, the student is shown the
truth, not left trusting a wrong answer."

**New files** (written inline via cloud-init `write_files`, matching
how `llama-server.service`'s own unit is already inlined — no new
pinned artifact needed, plain-text Python needs no build step):
- `/opt/llama.cpp/verify_proxy.py` — the proxy + code-block extraction
  (`re`, fenced ```python blocks) + sandbox runner
  (`subprocess`+`resource`+`tempfile`, as above).
- `/etc/systemd/system/verify-proxy.service` — `Restart=on-failure`,
  same convention as `llama-server.service`.

**Modified**:
- `llama-server.service`'s `ExecStart` — `--host 127.0.0.1 --port
  8721` instead of `--host 0.0.0.0 --port ${http_port}`.
- `examples/llm-chat/files/coordinator-cloud-init.yaml.tftpl` /
  `ansible/examples/templates/llm-chat/coordinator-cloud-init.yml.j2`
  — the two additions above.
- `examples/llm-chat/variables.tf` / the `vars:` block in
  `ansible/examples/14-llm-chat.yml` — new: `enable_verification`
  (bool, default `true`), `verify_timeout_seconds` (default `15`),
  `verify_max_memory_mb` (default `256`) — real, documented tunables,
  not hardcoded magic numbers.
- `examples/llm-chat/locals.tf` — pass the new vars into the
  coordinator's `templatefile()` call; same pattern already used for
  `webui_config_json`.
- `examples/llm-chat/main.tf` — **no changes**: the LB/target
  group/listener already point at `var.http_port`, which is exactly
  where `verify-proxy.service` will now listen instead of llama-server
  directly.

**Verification** (live, not just code review):
1. Rebuild `llm-chat` for real.
2. From the terminal, `curl --compressed` the coordinator's `/health`
   through the LB to confirm the proxy's pass-through doesn't break
   the existing health check.
3. Send the exact percentile/CSV prompt already used all session
   (known to reliably reproduce the `IndexError`) via a direct
   streamed `POST /v1/chat/completions`, and confirm the raw SSE
   response itself — not just the rendered UI — contains both the
   model's original text and the appended, clearly-labeled real
   traceback. This proves it's genuine server-side ground truth, not
   a UI trick.
4. Open the real browser UI, send the same prompt, confirm the
   execution result renders correctly in the same chat bubble.

---

## Phase 2 — Grounded fix loop

Only after Phase 1 is live-verified. On a real execution failure, the
proxy makes one additional internal `/v1/chat/completions` call to
llama-server's own internal port (not looping back through itself),
with a new user-role message containing the verbatim traceback and
asking for an explanation quoting the failing line plus a fix. The new
code block is extracted and re-executed the same way, and that whole
narrative (original code → real failure → grounded explanation → fix →
real re-verification result) is appended to the same single streamed
turn.

**New**: `verify_max_fix_rounds` variable (default `3`, not `1` —
widened per direct follow-up: a single automatic attempt was judged
too thin for "iterate until acceptable/correct" to feel real) — bounds
the loop so a stubbornly-wrong model can't run away with the
deployment's compute budget. Each round still ends with a real
execution result, never glossed over.

**Closing the "can a student actually iterate" gap without a new UI**:
per direct follow-up question — no dedicated interactive sandbox
exists in this plan (that's Phase 4, below); the only lever a student
has today is the chat itself. Made explicit rather than left implicit:
the appended verification block's final line always tells the student
they can just ask for another attempt in the same conversation if the
result still isn't right — since every new turn already gets verified
the same way (Phase 1's own mechanism), this is a real, working
iteration path using infrastructure that already exists, not a new
feature, just no longer a silent/undiscoverable one.

**Verification**: same percentile/CSV prompt (a known, reliable
`IndexError` repro). Confirm the appended fix references the *actual*
captured traceback (not a generic explanation), that the re-executed
fix's real result is shown whatever it is — a fix that still fails
should be shown as still failing, not glossed over — and that the
"ask again" invite line is present in a failing result.

**Verified live** (2026-09-18): a complete, real 3-round fix loop
against a genuine `numpy` `ModuleNotFoundError` (the sandbox is
deliberately stdlib-only). Confirmed: exactly one `[DONE]`, exactly
`VERIFY_MAX_FIX_ROUNDS` (3) fix attempts, each one's real re-execution
result shown honestly, and the "ask again" invite appearing exactly
once — only on the truly final block, never on an intermediate
failure. The model's own fix attempts each round only suggested
`pip install numpy` rather than rewriting the code to avoid the
dependency — a real, honest limitation of what a 14B model can fix on
its own, not a flaw in the mechanism; the student sees that plainly
instead of a false "should work now."

**Two real bugs found and fixed during this same live verification,
both now committed**:
- `layer_split.py`'s dynamic CPU-only split pushed 47 of 48 layers
  (~8.2GB of an 8.37GB model) onto a worker's own 8192MB flavor,
  leaving almost no room for KV cache/compute buffers — its real
  RPC server crash-looped under genuine memory pressure on every
  restart (confirmed via the guest's own `journalctl`:
  `ggml-rpc.cpp:569: Remote RPC server crashed or returned malformed
  response`). Fixed: the split is now also clamped so neither side's
  real share of the model's weight bytes exceeds 75% of its own
  flavor's RAM, falling back to the template's static default
  (rather than asserting an unsafe value) if no split fits either
  side safely.
- `_call_llama_direct()` (the fix loop's own internal completion
  call) had a 900s timeout and no `max_tokens` cap — too short for a
  real non-streaming generation on this hardware, confirmed live when
  a genuinely-still-working fix round was aborted with "failed to
  generate (timed out)". Fixed: timeout raised to 3600s, and
  `max_tokens` now threads through from the original request instead
  of being left effectively unbounded.

**Small future refinement, not urgent**: the fix prompt could
explicitly state that only the Python standard library is available
in the sandbox, which would likely help the model reach for a real
fix (e.g. `statistics.quantiles`) instead of repeatedly suggesting an
unusable `pip install`.

**Also found live, unrelated to this template's own code**: `api/lb.py`
generates a second, unused, stale HAProxy backend block
(`example-dev-chat-back`, wrong port, always `DOWN`) alongside the
real target-group-driven one — harmless (the frontend correctly
routes to the real backend) but worth a cleanup pass separately.

---

## PRIORITY — coordinator placement-awareness

Found live deploying the first real verify-proxy.service build:
`module.coordinator` in `main.tf` has no placement override at all —
it's always forced onto whichever host submits the build, unlike
workers (which already go through `placement_overrides` driven by
`worker_peers`). This host (stourport) has 4 real CPU cores;
`coordinator_flavor` briefly defaulted to `standard.xlarge` (6 vCPU)
during Phase 2 testing and hit KVM's own "-accel kvm: warning: Number
of SMP cpus requested (6) exceeds the recommended cpus supported by
KVM (4)", taking 5-6x longer than normal to come up. Immediately
unblocked by giving `coordinator_flavor` its own smaller default
(`standard.large`, 4 vCPU) separate from `worker_flavor` — but per
direct follow-up, that flavor split is a stopgap, not the real fix:
"should we not place it on the co-ordinator on the peer with the best
available resource?" Agreed, prioritized before Phase 3.

### Research confirmed, both real open questions from the earlier note

1. **LB routing to a peer-placed instance — confirmed working, not
   assumed.** Real TCP connect from this host directly to a peer-
   placed instance's own port (`/dev/tcp/192.168.101.x/50052`)
   succeeded — the WireGuard tunnel bridges the whole guest network,
   the same way worker RPC traffic already proves, so an LB created
   locally can reach a peer-placed coordinator's real IP:port exactly
   like it reaches today's local one. No LB/`module.lb` changes
   needed — it already resolves an instance's real IP server-side
   regardless of which host it's actually on (same mechanism already
   proven for `module.workers.private_ips_list`).
2. **`modules/instance-group`'s `placement_overrides` already supports
   a single-instance group — confirmed, no rework needed.**
   `variables.tf:117-123`: `map(object({peer_id, vpc_id, subnet_id,
   security_group_ids}))`, keyed by the same two-digit index workers
   already use ("01", "02", ...). `main.tf`'s own per-key
   `effective_peer_id`/`effective_vpc_id`/etc. lookups (lines 25-39)
   have no count-based branching at all — `count_instances = 1` with
   a single `"01"` key works identically to the worker case.

### A real security consideration that shapes scope

Unlike vpc_id/subnet_id (pure network topology, no real risk), a
peer's security group genuinely governs what's reachable. A worker's
own security group is deliberately "an EXISTING one on the peer,"
scoped narrowly to the RPC port (see `worker_peers`' own SECURITY
comment in `variables.tf`) — it was never designed for a coordinator's
own needs (SSH + the chat HTTP UI, `admin_cidr`-scoped, today wide
open at `0.0.0.0/0` by default). Auto-selecting "the peer's own first
SG" the same low-risk way the dashboard already auto-picks "the first
VPC/subnet" would be a real, silent security decision, not just a
placement one. That pushes this feature toward **human-confirmed
placement**, not `layer_split.py`-style blind server-side automation —
matching how every other peer-placed field in this project already
works (a human picks from a live, real dropdown before submitting),
rather than being the exception.

### Design

**New Terraform variables** (`examples/llm-chat/variables.tf`):
`coordinator_peer_id`, `coordinator_peer_vpc_id`,
`coordinator_peer_subnet_id`, `coordinator_peer_security_group_id` —
all `string`, default `""` (empty = stay local, today's exact default
behaviour, fully backward compatible).

**`locals.tf`**: new `coordinator_placement_overrides`, built only
when `var.coordinator_peer_id != ""` — `{"01" = {peer_id = ...,
vpc_id = ..., subnet_id = ..., security_group_ids = [...]}}`, same
shape `worker_placement_overrides` already uses.

**`main.tf`**: `module.coordinator` gains
`placement_overrides = local.coordinator_placement_overrides`
(currently has none at all).

**Free dashboard support, zero new frontend code**: confirmed live in
`ui/src/js/16-build-manager.js` — `_BM_PEER_ID_RE = /(^|_)peer_id$/i`
already matches any variable ending in `peer_id`, and
`_bmPeerFamilies()` already derives `_peer_vpc_id`/`_peer_subnet_id`/
`_peer_security_group_id` siblings by name and wires up the same live
cascading dropdowns worker fields already get — `coordinator_peer_id`
will be picked up automatically the moment these variables exist, no
JS changes needed (same convention `20-tofu-manager.js` mirrors).

**Ansible parity** (`ansible/examples/14-llm-chat.yml`): same four new
vars; the existing "Create coordinator instance" task's hardcoded
`vpc_id`/`subnet_id: subnet-local-01`/`security_group_ids` become
conditional on `coordinator_peer_id` being set, plus a `peer_id:`
arg — same per-item override shape the worker loop already proves
works, applied to a single task instead of a loop.

**`api/capacity_gate.py`**: extend to also check a peer-placed
coordinator's real available RAM against `coordinator_flavor` when
`coordinator_peer_id` is set — the same check already exists for
workers; a peer-placed coordinator had no equivalent safety net at
all before this feature made it possible.

**`api/layer_split.py`**: `maybe_apply()` currently always calls
`host_stats.collect()` for the coordinator's own stats, assuming
"coordinator = this host" — now calls
`peers_routes.peer_stats(coordinator_peer_id)` instead whenever one is
actually chosen.

**New validation, both submit routes**: reject with a clear 400 if
`coordinator_peer_id` equals any `worker_peers[].peer_id` — landing
both roles on the same machine silently defeats the entire reason this
template splits across hosts via RPC. Cheap to add, matches
`capacity_gate.py`'s own existing "reject with a clear message before
the build is even submitted" convention.

**Explicitly not in this pass**: automatic server-side placement
selection for the coordinator (the `layer_split.py` pattern) — the SG
consideration above makes that a real, separate, security-relevant
decision worth its own explicit sign-off later, not something to fold
in silently while making placement merely *possible* for the first
time. A human choosing via the dashboard (which already shows the
real SG options to review before submitting) is this pass's actual
mechanism, consistent with how every other peer-placed field already
works in this project.

**Verification**: build `llm-chat` once with `coordinator_peer_id`
set to the real paired peer and `worker_peers` empty/local (the
inverse of today's only configuration) — confirm the coordinator
instance really lands on the peer (`host_hostname` in
`GET /v1/instances/<id>`), the LB's own health check against its real
peer IP succeeds, and a real chat completion works end to end through
that path. Separately, confirm the new same-peer validation actually
rejects a request where `coordinator_peer_id` matches a
`worker_peers[].peer_id`.

### Verified 2026-09-18

**API-side code** (`api/capacity_gate.py`'s new `check_coordinator_peer()`
and `check_no_coordinator_worker_overlap()`, `api/layer_split.py`'s
`maybe_apply()` now calling `peers_routes.peer_stats(coordinator_peer_id)`
instead of `host_stats.collect()` when one is set, and both submit
routes wiring them in) — unit-verified directly against real data
(the real approved peer's own live stats via `peers_routes.peer_stats()`,
a real GGUF file, `db.init()` against the real `cloudcore.db`), then
re-verified through the real running HTTP API after a required server
restart (see incident note below): the same-peer overlap request
correctly returns `400 Invalid peer placement`, an unreachable
`coordinator_peer_id` correctly returns `400 Insufficient peer capacity`
naming the real RAM shortfall, and `rpc_offload_layers` computes a
real, different value depending on whether the coordinator's stats
come from this host or the peer.

**Incident during testing, disclosed and resolved**: an early live
test was sent to the *already-running* dev API process rather than
one that had picked up these code changes — its in-memory code was
stale, so the same-peer-overlap request fell through to the
pre-existing "destroy existing state before applying" build path
instead of being rejected up front, using the test's own placeholder
`vpc_id`/`security_group_id` values. This destroyed the real, working
`llm-chat` deployment that existed at the time (coordinator
`37801a0a-...`, worker `d4b7c78f-...`), and the attempted recreation
correctly failed against the peer's own security-group validation
("security group 'z' not found"), which CloudCore's own failed-build
auto-destroy then cleanly tore down — leaving an empty, non-broken
state rather than a half-provisioned one. Root cause: testing against
a live process without restarting it to load the edited code, not a
flaw in the new logic itself (confirmed once retested properly). The
dev server was restarted on stourport (where the CloudCore control
plane runs — Llywyn-Y-Groes is a placement target only, never where
`server.py` itself runs) and the same request then correctly rejected
with `400` before touching any real infrastructure. `llm-chat` was
then rebuilt fresh in its known-good configuration (coordinator local,
one worker on Llywyn-Y-Groes) to restore working state — confirmed via
a real `tofu apply` (`Apply complete! Resources: 8 added`) and real
instance IDs (`c11d781c-...` coordinator, `fcb9e44a-...` worker on
`Llywyn-Y-Groes`).

**Full live end-to-end test of "coordinator actually placed on the
peer" — genuinely blocked, not skipped.** This plan's own verification
step above assumed `worker_peers: []` was a viable standalone
config to isolate coordinator placement. It isn't: the coordinator's
`llama-server` command line always passes both `-ngl
${rpc_offload_layers}` and `--rpc ${rpc_servers}`
(`coordinator-cloud-init.yaml.tftpl:69`), and with zero workers
`rpc_servers` renders as an empty string — an RPC-offload flag with no
RPC backends. This template was never designed to run coordinator-only.
Combined with the same-peer exclusion this same pass just added
(intentionally — landing coordinator and worker on the same machine
defeats the reason for RPC splitting), a *working* "coordinator on a
peer, with a real functioning worker" configuration needs the
coordinator on one peer and the worker on a **different** one — and
this fleet currently has exactly one approved peer (Llywyn-Y-Groes).
Structurally impossible to fully exercise until a second peer joins.

**What this leaves genuinely proven vs. not**: the Terraform mechanism
itself (`placement_overrides` on a single-instance group) is not new
code risk — it's the exact same `modules/instance-group` code path
already proven live, repeatedly, for `module.workers`, including in
this very redeploy. A real `tofu plan` dry-run earlier in this pass
independently confirmed `coordinator_peer_id` correctly forces
`module.coordinator`'s instance to resolve `peer_id`/`vpc_id`/
`subnet_id`/`security_group_ids` from the peer's real values. What
remains genuinely unverified is only the *combination* — coordinator
on a peer at the same time as a live, chat-serving worker — which
needs a second peer host to even attempt safely. Recorded here rather
than silently assumed; revisit when a second peer is available.

---

## Phase 3 — Central learning corpus + student review page

Per direct follow-up request: capture must not be lost or scattered —
"i don't want lots of different data all over the place with no
ability to keep a copy of it/track of it and it's value" — and must
benefit the whole cohort, not any one student's session, while also
being usable later as real fine-tuning material once better hardware
exists ("when we obtain the correct h/w we can get the learning
process underway with some useful examples").

**Design principle — one table, two views, not two pipelines.** Every
verification transaction (Phase 1/2's real execution result, pass or
fail, plus the fix round if one ran) is captured unconditionally —
nothing is filtered out at capture time, so the full table *is* the
training corpus, exportable as JSONL at any time. A `status` field
(`pending`/`published`/`hidden`) controls only what's shown on the
separate student review page — an instructor curates by publishing
good teaching examples on the Dashboard; that never affects what's
captured or exportable. No student/session identity is ever recorded
— this is a shared cohort resource by design, matching how the chat
itself already has no login.

**Durability — retention lives in CloudCore's own persistent
control-plane DB, never the coordinator VM.** The coordinator is
ephemeral (rebuilt/destroyed routinely) — it is only ever a
capture/relay point, POSTing each transaction back to the CloudCore
API host's own SQLite DB (`api/cloudcore.db`, via `api/db.py`'s single
`init()` convention, same shape as `failed_build_logs` —
`db.py:437-471`). This is already true regardless of where a
transaction originates, which is what makes the schema below
deliberately origin-agnostic rather than coordinator-specific.

**Forward-looking, not scope creep**: a `source` field on every row
(default `"llm-chat-coordinator"`) means a *future* capture client —
e.g. a student running a model locally on their own laptop instead of
through this deployment — could feed the exact same central store
without any schema change, and the JSONL export format is plain,
self-contained, and migration-friendly toward the bigger AWS/on-prem
infrastructure `haFullStack-Phased-Implementation.md` already
anticipates. Actually building and distributing that local-capture
client (student install story, offline queuing, its own credential
model) is real, separate future work and is explicitly **not** built
in this phase — only the schema is shaped so it won't need to be
redesigned when that work starts.

**New DB table** (`api/db.py`, added to the single `init()` alongside
every other table, same `CREATE TABLE IF NOT EXISTS` convention):
```sql
CREATE TABLE IF NOT EXISTS llm_verification_examples (
    id               TEXT PRIMARY KEY,
    source           TEXT NOT NULL DEFAULT 'llm-chat-coordinator',
    build_id         TEXT NOT NULL DEFAULT '',
    model_filename   TEXT NOT NULL,
    prompt           TEXT NOT NULL,
    generated_code   TEXT NOT NULL,
    exec_stdout      TEXT NOT NULL DEFAULT '',
    exec_stderr      TEXT NOT NULL DEFAULT '',
    exec_exit_code   INTEGER,
    passed           INTEGER NOT NULL DEFAULT 0,
    fix_explanation  TEXT NOT NULL DEFAULT '',
    fixed_code       TEXT NOT NULL DEFAULT '',
    fix_exec_stdout  TEXT NOT NULL DEFAULT '',
    fix_exec_stderr  TEXT NOT NULL DEFAULT '',
    fix_passed       INTEGER,
    status           TEXT NOT NULL DEFAULT 'pending',
    created_at       TEXT NOT NULL
)
```

**New store** `api/llm_examples_store.py`, mirroring
`api/failure_queue.py`'s own shape (`queue_failure`/`list_pending`/
`delete`):
- `record_example(source, build_id, model_filename, prompt,
  generated_code, exec_result, fix_result=None) -> id`
- `list_examples(status=None, limit=...) -> list[dict]`
- `set_status(example_id, status) -> None`
- `export_jsonl() -> iterator` — full corpus, any status; the actual
  training-data path.

**New blueprint** `api/llm_examples_routes.py`, registered in
`api/server.py` (exact same `from X_routes import X_bp` /
`app.register_blueprint(X_bp)` pattern already used for every other
blueprint, e.g. `scheduler_bp` at lines 42/58):
- `POST /v1/llm-chat/examples` — ingestion, gated by the same
  `cloudcore_api_token` every template's guest already has via
  `tofu_engine.py`'s `_connection_vars()` (lines 224-225) — no new
  plumbing needed for the coordinator path.
- `GET /v1/llm-chat/examples` — admin-token gated, all statuses (the
  Dashboard moderation list).
- `PUT /v1/llm-chat/examples/<id>` — admin-token gated, set status.
- `GET /v1/llm-chat/examples/export` — admin-token gated, full JSONL
  export.
- `GET /v1/llm-chat/examples/published` — deliberately the one
  **unauthenticated** route in this codebase, published-only,
  read-only, never exposes pending/hidden rows or the full corpus.
  This is the one thing students' side ever calls.

**Dashboard (instructor side)**: new `ui/src/js/30-llm-examples.js` +
nav entry, following the LLM Performance page precedent exactly
(`ui/src/html/body.html` nav button + section, `ui/build.sh` re-run
after) — list all examples, publish/hide toggle, export button.

**Student review page**: served by `verify-proxy.service` itself
(Phase 1's own component, already sitting in the chat's own LB path)
at a new path on the *same* URL/port students already use for chat —
e.g. `GET /examples` — a small self-rendered HTML page (stdlib
`http.server`, no new frontend framework) calling the new public
`/v1/llm-chat/examples/published` endpoint and showing each
transaction's *full* journey: prompt → wrong code → real failure →
grounded explanation → fix → real re-verification pass. No new
credentials, no new URL to distribute — matches "for the benefit of
all students," not gated per-person.

**Verification**: submit a real prompt through Phase 1/2, confirm the
transaction lands in `llm_verification_examples` with the correct
`source`/`model_filename`; publish it via the Dashboard; confirm it
appears on `GET /examples` on the coordinator's own URL with the full
journey intact; confirm `GET /v1/llm-chat/examples/export` returns
valid JSONL covering *all* captured rows regardless of status, not
just published ones.

---

## Phase 4 — Interactive sandbox (placeholder — needs its own document)

Per direct follow-up: a real hands-on workspace where a student can
edit code themselves, re-run it on demand, and iterate directly with
the model — not just re-prompt in the chat and hope — is a genuinely
different, larger feature than Phases 1-3. It almost certainly means
finally building a real UI surface of our own (a code editor + run
history panel), which breaks the "use llama-server's own webui as-is,
never build a new frontend" principle this whole plan has held to —
that's a deliberate scope boundary, not an oversight, and it's why
this stays a placeholder here rather than a real design.

**This section exists as a marker, not a plan**: once Phases 1-3 are
built and verified, before writing any code for this phase, stop and
write a **new, separate phased-implementation document** for the
interactive sandbox specifically — mirroring how this document itself
started (a plan-mode session, real codebase research, explicit
architecture decisions, user sign-off before code). That new document
should also **roll up every item in "Explicitly out of scope" below**
(non-Python languages, stronger sandbox isolation, the portable local-
capture client) — not because they necessarily belong *in* the
sandbox feature, but so nothing raised and deliberately deferred
across Phases 1-3 gets silently lost when attention moves to Phase 4.

## Explicitly out of scope for now (future work, not silently assumed
— to be rolled into Phase 4's own document, not dropped)

- Non-Python code blocks.
- Stronger isolation than same-VM `unshare`/`setrlimit` (e.g. a
  dedicated throwaway execution VM/container) — real hardening, but
  added latency and complexity not justified until Phase 1/2 prove the
  concept out.
- A portable local-capture client for students running models on their
  own laptops (see Phase 3's own "forward-looking, not scope creep"
  note) — the schema is shaped for it, nothing more, for now.
