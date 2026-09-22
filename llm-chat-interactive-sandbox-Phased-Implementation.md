# llm-chat — Interactive Sandbox: Phased Implementation

Paul Scott | Plan approved 2026-09-18

---

## Context

This is Phase 4 of `llm-chat-verification-Phased-Implementation.md`
(Phases 1-3 and the coordinator-placement-awareness priority are all
complete and committed). That document's own Phase 4 placeholder
required a new, separate phased document before any sandbox code is
written — this is that document.

**Why this exists.** Phases 1-3 pair every code-bearing chat response
with real, sandboxed execution evidence, and capture the whole journey
into a shared learning corpus. But the only way a student could
iterate was re-prompting the same free-form chat and hoping — no real
hands-on workspace. Per direct request: *"a real hands-on workspace
where a student can edit code themselves, re-run it on demand, and
iterate directly with the model."*

**A real scope pivot happened while planning this, not assumed going
in.** The obvious reading of "add a sandbox" is: keep llama-server's
own general-purpose chat webui, and add a second, separate editor
surface alongside it. Direct follow-up reframed it into something
stronger: *"i'm hoping extending it would mean that the llm would be
available in the sandbox as it currently is in the chat. that would
mean then in likely hood that the chat itself would no longer be
needed... since we're not really aiming for general chat i can't see
why we'd risk having hallucinogenic chat agents available to provide
possibly incorrect and none lab related answers to questions
themselves not related to the lab."* Confirmed further that the old
chat webui should become genuinely unreachable, not merely
unadvertised: *"i can't see how a chatbox would help debug to be
honest."*

This is a materially stronger design than "sandbox next to chat":
every interaction in the new interface stays anchored to actual
student code — the one interaction shape Phase 1/2's grounding
mechanism (execute and show truth) actually applies to. A free-form
chat box invites exactly the ungrounded, off-topic question Phase 1/2
has no way to verify at all. Removing it closes a real gap in the
whole safety argument this project has been built around, not just a
UI simplification.

**Scope**: `examples/llm-chat`'s coordinator only — no new
example/VPC/LB/instance stack. `examples/distributed-llm` stays
untouched, as in every prior phase.

| # | Stage | Status |
|---|---|---|
| 1 | Core Run/Ask loop, plain textarea, chat webui removed | Done — verified live 2026-09-18 |
| 2 | CodeMirror upgrade | Done — verified live 2026-09-18 |
| 3 | True interactive execution (`input()` support) | Done — verified live 2026-09-18 |

---

## Research this plan is grounded on

- **CodeMirror 5.65.16 + xterm.js 5.3.0 are already vendored** under
  `ui/vendor/`, loaded by `ui/src/html/head.html`/`body.html`, driven
  by `ui/src/js/18-editor.js` (`_edInitCm()` renders straight into a
  div, not `CodeMirror.fromTextArea`). These are the CloudCore
  **dashboard's** own vendored assets, served by the API host's Flask
  app (`server.py`'s `GET /vendor/<path>` route) — **not present on
  any guest VM**. No Python CodeMirror mode is vendored today (only
  yaml/javascript/markdown); `.tf` files currently reuse the
  `javascript` mode as a stand-in, with an inline comment noting a
  real HCL mode should replace it "when added."
- **`api/editor_routes.py`** (the dashboard's existing "Editor" page)
  is a plain file CRUD API scoped to two fixed server-side template
  roots (the Ansible/OpenTofu example directories) with no execution
  route at all — not reusable as-is for arbitrary student code.
- **`api/terminal.py`** is the only WebSocket usage anywhere in this
  codebase (`websockets` library, its own systemd unit
  `PartOf=cloudcore-api.service`, JSON frame protocol
  `{"type":"input"/"resize"/"output"/"error"/"connected"}`). It runs
  **host-side** (the API process SSHing into an instance via
  paramiko) — the `websockets` library has never run on a **guest** VM
  in this codebase. `verify_proxy.py` (the only guest-side
  long-running server) is stdlib `http.server.BaseHTTPRequestHandler`,
  which has no WebSocket-upgrade support at all, and the guest image
  provisions no `websockets` dependency today.
- **`examples/llm-chat/files/verify_proxy.py`**'s `run_sandboxed()`
  (`unshare --net --pid --fork --mount-proc` + an unprivileged
  `sandboxrunner` user + `resource.setrlimit` + a wall-clock timeout)
  is the only "run untrusted code safely" primitive anywhere in the
  repo. It is Python-only, synchronous/blocking
  (`subprocess.Popen(...).communicate()`), one-shot per call — not a
  persistent REPL/kernel. Already proven live across Phases 1-2.
- **LB WebSocket passthrough is a solved, proven problem in this
  codebase**, should a future phase need it —
  `examples/ghidra-workstation` fronts noVNC (also a persistent
  bidirectional connection) via `type = "network"` on `module.lb`,
  which makes `api/lb.py` generate HAProxy `mode tcp` (`lb.py:71`)
  instead of `mode http` — pure passthrough, no
  `option http-server-close` (only added in http mode, `lb.py:202`),
  which is what breaks a WebSocket Upgrade handshake. Not needed for
  this design — see the Run/Ask channel decision below.
- **`db.py`**'s `llm_verification_examples` table (Phase 3, already
  shipped) carries a `source` column specifically designed to be
  extensible: *"a future capture client... could feed the same
  central store without any schema change."* Directly reusable here —
  see Architecture below — rather than a new table.
- Phase 1-3's own "Explicitly out of scope" list (non-Python code
  blocks, stronger-than-`unshare`/`setrlimit` isolation, a portable
  local-capture client) is required to be rolled up into this
  document rather than silently dropped again — addressed explicitly
  near the end.

---

## Design decisions

**1. Same coordinator VM, not a new deployment.** `verify-proxy.service`
grows new routes rather than a separate example/VPC/LB/instance stack.
Reuses the model already loaded and warmed up; no new infrastructure.

**2. The sandbox fully replaces the general chat webui — not
alongside it.** `GET /` on verify-proxy currently passes through to
llama-server's own webui. It will instead serve the sandbox page.
llama-server's own webui becomes genuinely unreachable through this
deployment — confirmed directly: not kept as a hidden or debug path
either, since a chat box doesn't actually help debugging.

**3. Run-output channel: Server-Sent Events, not WebSocket.**
`/sandbox/ask` reuses the exact SSE mechanism the chat already proved
(`_relay_and_verify_stream`) — no new guest-side dependency, no LB
reconfiguration, HAProxy's existing `http` mode is fine. A hard
mid-generation interrupt isn't built for chat today either and stays
explicit future work, not a v1 requirement.

**4. Execution model: one-shot re-run, not a persistent kernel.**
Every Run re-executes the submitted code from scratch through the
*existing* `run_sandboxed()` — no new process-lifecycle or session
management. A notebook-style persistent kernel (variables surviving
between runs) is real future work, not proven anywhere in this
codebase today, and not requested.

---

## Architecture

### Two new actions on the coordinator's existing `verify-proxy.service`

**`POST /sandbox/run`** — body `{"code": "..."}`. Calls the *existing*
`run_sandboxed()` directly, under the same `VERIFY_TIMEOUT_SECONDS`/
`VERIFY_MAX_MEMORY_MB` limits already in production use for inline
chat verification. Bounded to a few seconds by design, so this is a
**plain synchronous JSON response** — `{"stdout", "stderr",
"exit_code", "timed_out"}` — no streaming needed for this action at
all. A real simplification: nothing here requires SSE, WebSockets, or
any new guest-side library.

**`POST /sandbox/ask`** — body `{"code": "...", "question": "...",
"history": [...]}`. Builds a messages list — the new, tightly scoped
system prompt (below) plus the running conversation the browser
already holds plus this turn's code and question — and streams the
model's response back via SSE using the *same* mechanics
`_relay_and_verify_stream` already implements. Extracts any code block
from the response, runs it through `run_sandboxed()`, appends the real
result via the *existing* `format_verification_block()` — i.e. Phase
1/2's own grounding mechanism, now triggered on demand by the student
instead of automatically once per chat turn. Captured via the
*existing* `capture_example()`, `source = "llm-chat-sandbox"`.

### A new, more tightly scoped system prompt

Today's `webui_system_message` variable configures llama-server's own
`--webui-config-file`, which only shapes *its own* webui — moot once
that webui is unreachable. A new `sandbox_system_message` variable (own
sensible default: stay on the submitted code, decline off-topic
requests, redirect back to the lab task) directly operationalizes "we're
not aiming for general chat" as an actual instruction to the model — a
mitigation, not a guarantee, since a system prompt can still be talked
around. Phase 1/2's real-execution grounding remains the actual safety
net; this is a second, smaller layer on top of it, not a replacement
for it. `webui_temperature`/`webui_system_message` themselves become
vestigial (still passed to `--webui-config-file`, harmless, just
without effect now that nothing reaches that webui) — noted here
rather than left unexplained; not worth removing since the underlying
llama-server flag is harmless to keep passing.

### Capture reuses Phase 3's existing table and pipeline — no new DB table

Only `/sandbox/ask` transactions get captured — a model claim worth
grounding and exporting. Plain `/sandbox/run` clicks are not — they're
the student's own code, not a model claim, so there's nothing to
ground. `source = "llm-chat-sandbox"` distinguishes these rows from
`"llm-chat-coordinator"` chat-originated ones. The Dashboard's existing
`ui/src/js/30-llm-examples.js` needs no changes at all — it already
renders whatever `source` a row carries generically. The student
review page (`GET /examples`, unaffected by this phase) will naturally
start showing sandbox-originated examples alongside chat-originated
ones once an instructor publishes them.

### No server-side identity, matching Phase 3's own privacy stance

The code buffer and the ask-conversation history live client-side
(`localStorage`) — matching how llama-server's own webui already kept
chat history client-side (IndexedDB), and Phase 3's explicit "no
student/session identity is ever recorded" commitment. The full
conversation history is resent with each `/sandbox/ask` call, mirroring
exactly how the old chat's own multi-turn mechanism already worked —
no server-side session state needed for multi-turn Q&A about the same
code.

### Editor widget — plain `<textarea>` for Stage 1, CodeMirror in Stage 2

Embedding CodeMirror requires shipping its own JS/CSS to the **guest**
VM — the dashboard's `/vendor/` route serves the *admin* host, not the
student-facing coordinator, so CodeMirror assets would need a
`file()`-read, inlined into `coordinator-cloud-init.yaml.tftpl`'s own
`write_files`, the same pattern `verify_proxy_source` already uses. A
real, provable mechanism, just added complexity not needed to prove
the core loop. Stage 1 starts with a plain, monospace-styled
`<textarea>` — the same "prove the concept first" discipline Phase 1
itself followed — and Stage 2 upgrades it once Run/Ask are
live-verified.

### Guest-side route hygiene

`verify_proxy.py`'s passthrough today forwards anything not explicitly
intercepted straight to llama-server. Once its own webui is no longer
the point of this deployment, `/` serves the sandbox page instead of
passthrough, and passthrough itself narrows to an explicit allowlist
(`/health`, still needed for the LB's own health check) rather than
forwarding everything else by default — matching this project's
existing least-exposure convention (the same endpoint-allowlist
reasoning `api/examples_listener.py`'s own gate already uses).

### Abuse/rate consideration

Run/Ask become directly student-triggerable rather than
automatic-once-per-chat-turn. Stage 1 relies on the simplest real
mitigation — the UI disables both buttons while a request is in
flight — rather than new server-side rate-limiting state. True per-IP
throttling is real future hardening, not a Stage 1 blocker.

---

## Stage 1 — Core Run/Ask loop, plain textarea, chat webui removed

**Modified**: `examples/llm-chat/files/verify_proxy.py` /
`ansible/examples/templates/llm-chat/verify_proxy.py` (kept
byte-identical, as always) —
- `do_GET`'s dispatch: `/` now serves a new, self-rendered sandbox page
  (same stdlib-only convention `/examples` already established) instead
  of `_proxy_passthrough()`.
- `_proxy_passthrough()` (or a new wrapper around it) narrows to an
  explicit allowlist — just `/health` — for anything not otherwise
  intercepted.
- New `do_POST` branches: `/sandbox/run` (calls `run_sandboxed()`
  directly, returns plain JSON) and `/sandbox/ask` (builds the message
  list, streams via the existing SSE relay mechanics, runs/appends
  verification, calls `capture_example(..., source="llm-chat-sandbox")`).

**New Terraform/Ansible variable**: `sandbox_system_message` — same
pattern `examples_api_base`/`examples_ingestion_token` already used
this session (`examples/llm-chat/variables.tf` +
`ansible/examples/14-llm-chat.yml`'s `vars:` block), threaded into
`coordinator-cloud-init`'s own `write_files`/template call.

**No changes needed**: `api/db.py`, `api/llm_examples_store.py`,
`api/llm_examples_routes.py`, `api/examples_listener.py`,
`ui/src/js/30-llm-examples.js` — all reused exactly as Phase 3 already
built them.

**Verification** (live, matching this project's own established
discipline):
1. Real redeploy of `llm-chat` with the new capture wiring.
2. Write/paste real Python code into the textarea, click Run, confirm
   the actual stdout/stderr/exit code shown match a real local run of
   the same code.
3. Ask the model a real question about that code (e.g. "why does this
   fail on an empty list"), confirm the SSE-streamed response plus an
   appended real verification block, confirm the transaction lands in
   `llm_verification_examples` with `source = "llm-chat-sandbox"`.
4. Confirm `GET /` serves the sandbox, not llama-server's own webui —
   `curl --compressed` directly against the deployment to confirm no
   path reaches the old webui at all.
5. Confirm the existing student review page (`GET /examples`) and the
   Dashboard's LLM Examples page both show sandbox-originated rows
   correctly, with no code changes needed on either.

### Verified live, 2026-09-18

Real redeploy (`llm-chat`, coordinator local + worker on
Llywyn-Y-Groes, carrying the new capture wiring). All five steps
above confirmed against the real running deployment, not just unit
tests:

1. `GET /` → 200, the sandbox page (confirmed by content, not just
   status). `GET /v1/chat/completions` (and everything else not
   explicitly routed) → 404 — genuinely closed, not just unlinked.
   `GET /health` still passes through for the LB.
2. `POST /sandbox/run` with a real 1-5 summing loop → real stdout
   `"15\n"`, `exit_code: 0` — the actual sandboxed interpreter, not a
   simulated result.
3. `POST /sandbox/ask` asked the real 14B model why
   `divide(10, 0)` crashes — took 97s at this hardware's real
   generation speed (consistent with prior findings), returned a
   correct explanation plus a fix, and the SSE stream carried a real
   appended `ACTUALLY EXECUTED` block showing the *actual* re-run
   result (`"Error: Division by zero is not allowed.\n"`, `exit_code: 0`)
   — not the model's own unverified claim about what the fix does.
4. Confirmed via the admin API: the transaction landed in
   `llm_verification_examples` with `source = "llm-chat-sandbox"`,
   correctly distinguishing it from chat-originated rows.
5. Published that row and confirmed it rendered correctly, unmodified,
   on both `GET /examples` (fetched live through the LB) and the
   Dashboard's existing LLM Examples page — zero code changes needed
   on either, exactly as designed.

One extension made beyond the original design during implementation:
`POST /v1/chat/completions` itself (not just `GET /`) is now also
unreachable from outside — a raw, un-gated chat endpoint would have
left exactly the loophole this whole phase exists to close (a caller
could still get an ungrounded, off-topic answer by POSTing directly to
it, bypassing `sandbox_system_message` entirely). `_relay_and_verify_stream`
is reused internally by `/sandbox/ask`; the dead
`_handle_chat_completions`/`_relay_raw`/`_relay_and_verify_json` code
paths were removed rather than left unreachable.

---

## Stage 2 — CodeMirror upgrade

**New**: vendor a Python CodeMirror mode (not present in
`ui/vendor/` today). Embed CodeMirror core + theme + matchbrackets
addon + the new Python mode into `coordinator-cloud-init`'s own
`write_files`, the same mechanism `verify_proxy_source` already
proves works for shipping real content to the guest.

**Modified**: the sandbox page's own JS (self-rendered inside
`verify_proxy.py`, same as Stage 1) swaps the plain textarea for a
CodeMirror instance — same editor UX the Dashboard's own Editor page
already has (`ui/src/js/18-editor.js`'s `_edInitCm()` as the pattern
to mirror), applied here to a guest-served page instead of the
dashboard.

**Verification**: real syntax highlighting and line numbers render in
a real browser against a real deployment; Run/Ask still work
unchanged underneath — this stage only touches presentation.

### A real bug found and fixed during implementation

The first real redeploy failed outright — `Error: Instance entered
error state` from the CloudCore provider, no further detail. Isolated
methodically rather than guessed at: a synthetic ~234KB `user_data`
blob created a real instance successfully (ruling out raw size), so
the actual coordinator cloud-init was rendered locally byte-for-byte
and fed to `yaml.safe_load` directly, which failed with `unacceptable
character #x0080`. Traced to `ui/vendor/codemirror.min.js` itself — a
literal `U+0080` character in its own minified source (part of a
word-character range check), completely valid JavaScript and valid
UTF-8, but forbidden by strict YAML 1.1 inside a plain scalar. This
was never a problem before Stage 2: the Dashboard only ever serves
this exact file as a raw static asset (`GET /vendor/<path>`), never
through a YAML document — Stage 2 is the first place in this codebase
this file has ever been embedded inside one.

**Fixed** by switching all five `write_files` entries for the
CodeMirror assets from plain `content: |` to `encoding: b64` +
`base64encode(...)` (Terraform) / `| b64encode` (Ansible) — a
standard, well-supported cloud-init feature. This sidesteps the whole
class of problem for any vendored asset, not just this one character
in this one file, and was confirmed round-trip-exact locally
(`base64.b64decode(...) == original bytes`) before redeploying.

### Verified live, 2026-09-18

Redeployed successfully after the fix. All five vendor assets fetched
from the live coordinator via `GET /vendor/<name>` matched the local
`ui/vendor/` files byte-for-byte (confirmed by SHA-256, not just size)
— `codemirror.min.js` and `codemirror-mode-python.min.js` both
checksum-identical to their source files, confirming the base64
round-trip through real cloud-init delivered them uncorrupted.
`POST /sandbox/run` re-verified working unchanged underneath. The
sandbox page's own HTML/JS was confirmed to reference and initialize
CodeMirror correctly (`mode: 'python'`, the `#codeHost` div, `cm.getValue()`
wired into Run/Ask) — genuine visual rendering in an actual browser
was **not** confirmed in this environment (no browser available),
flagged rather than assumed, same limitation noted for the Phase 3
Dashboard page.

---

## Stage 3 — True interactive execution (`input()` support)

Stage 1's own `input()`-avoidance fix (steering the model away from it
via the system prompt) was the right first move, but per direct
follow-up: *"how can [we] provide a sandbox that can accept
input/output but keep it within the sandbox? i'd like the llm to be
able to fully use the sandboxes o/s as far as it's practical or safe
to do."* Two decisions confirmed before building: **the model drives
interactive sessions, not the student** (extends the existing Ask
panel rather than handing out a raw terminal — every real command and
result still shown, staying transparent rather than becoming a black
box), and **true mid-execution pause/resume against a real live
process**, not just pre-supplied stdin values guessed upfront.

**Research this stage is grounded on**: `llama-server`'s own
OpenAI-style tool-calling looked like the right mechanism to build
this on — `GET /props` reports `chat_template_caps.supports_tools:
true` / `supports_tool_calls: true`. A real test request with a
`tools` array against the live model proved this doesn't actually
work with this exact model/template combination — it echoed the tool
schema back as plain text (`<response>{...}</response>`, `finish_reason:
"stop"`) rather than a genuine `tool_calls` array. Built on the same
proven, prompt-based fenced-block pattern used throughout this project
instead (a new ` ```stdin ` block convention, alongside the existing
`python` one) — not genuine function-calling. Also confirmed live the
sandbox has zero network access at all, not even loopback (`unshare
--net` creates a namespace with no interfaces configured) — a real,
strong, already-existing safety property, left untouched by this
stage.

**New**: `run_sandboxed_interactive()` — same isolation exactly as
`run_sandboxed()` (same `unshare` + unprivileged user + the same
`resource.setrlimit` CPU/memory/proc/fsize limits, which being
CPU-time-based rather than wall-clock still correctly bound a
longer-lived session), but stdin stays open and a non-blocking
`select()` loop detects when the process goes quiet — likely waiting
for input — instead of a single blocking `.communicate()`. On a
detected pause, the model is consulted (the same internal
`_call_llama_direct()` mechanism the fix loop already uses), grounded
in the real transcript so far, for what to supply; its reply is fed to
the *same live process*, not a restart. Two hard caps are enforced
regardless of what the model decides: `INTERACTIVE_MAX_EXCHANGES`
(3) and `INTERACTIVE_MAX_WALL_S` (1800s) — either one trips, the
process is killed and an honest "exceeded its budget" message is
shown, never a silent hang.

**Deliberately entirely server-side — no new client-facing protocol
at all.** Because the model drives the session rather than the
student, the whole "detect pause → ask the model → resume" loop runs
inside `_handle_sandbox_ask`'s own existing SSE response, the same way
the Phase 2 fix loop already makes multiple internal model round-trips
within one streamed turn. No WebSocket, no LB mode change, no new
guest-side dependency — a real infrastructure expansion this stage
turned out not to need.

**Modified**: `verify_and_maybe_fix()` now runs every execution
(initial and any fix-round re-run) through the interactive primitive
rather than the one-shot one. `sandbox_system_message` flipped from
"never use `input()`" to explaining the new `stdin` block convention —
the old wording became actively wrong the moment this shipped.
**Deliberately unchanged**: the plain **Run** button
(`_handle_sandbox_run`) — per the "model drives it" decision,
interactive stdin only ever arrives through the model-driven Ask flow.

### Verified live, 2026-09-18

Real redeploy (dynamic split computed 33, correct placement). Asked
the exact "search a directory for a file the user specifies" prompt
that originally surfaced this need. Confirmed genuinely live: the
model wrote a script with two `input()` calls; the sandbox correctly
paused at the first, the model was consulted grounded in the real
prompt text printed so far and replied `/home/user`; the transcript
shows that value fed to the *same live process* (not a restart), which
paused again at the second `input()`, was consulted again, and
completed for real — `os.walk()` genuinely ran, found nothing, printed
`File not found.`, exit code 0, 2 real exchanges. The interleaved
transcript shown to the student (prompt → input provided → prompt →
input provided → real result) confirms
`format_interactive_verification_block()`'s ordering is correct, not
just the underlying mechanism.

Confirmed unaffected: the plain Run button still uses one-shot
`run_sandboxed()` — a script with `input()` submitted via Run still
gets a real, immediate `EOFError`, exactly as before this stage.
Confirmed the captured transaction renders correctly on both the
student review page and the Dashboard's LLM Examples page with zero
code changes needed on either, as designed.

Not re-tested live: the hard exchange/wall-clock caps — `unshare`
itself needs root, unavailable on this dev host, so the core
select/quiet-period/exchange-cap algorithm was validated instead
against a real plain subprocess (single and multiple genuine `input()`
exchanges each correctly grounded in the growing real transcript,
`provide_input()` returning `None` stops cleanly, the exchange cap
kills a runaway script at exactly the configured limit). Forcing a
genuine 3+-exchange runaway against the live model would cost many
real minutes per exchange for marginal additional confidence beyond
that, since the cap logic is byte-identical, just wrapped in the real
sandbox rather than a plain subprocess — a reasoned choice to skip,
not an oversight.

---

## Stage 4 — Per-client rate limiting + a hard interrupt

Picks up two items straight off the "Explicitly out of scope" list
below, per direct request to work through the remaining items now
("the only item i have no interest in at the moment is non python
code blocks"), sequenced small-first ("Small ones first") ahead of the
bigger items that each need their own design discussion.

**Rate limiting**: sliding-window per-IP counters on `POST
/sandbox/run` (10/minute) and `POST /sandbox/ask` (10/10 minutes —
wider since Ask is far more expensive: a real generation plus
sandboxed execution, possibly several fix rounds). Keyed by the real
client IP via `X-Forwarded-For`, confirmed in `api/lb.py` that this
example's own LB runs in HTTP mode with `option forwardfor`
specifically so this works — without it every request would appear to
come from the LB itself, one shared IP for every student. A second
concurrency cap rejects a second `/sandbox/ask` from the same IP while
one is already in flight; this doubles as the interrupt mechanism's
own key, since a real student only ever has one live question and IP
alone is therefore enough to identify which in-flight request a Stop
request targets.

**Hard interrupt**: a `threading.Event` created per in-flight ask,
checked at every real wait point in the call chain — the SSE relay's
own read loop, the interactive sandbox's `select()` poll loop, and the
internal model-call heartbeat thread — so `POST /sandbox/interrupt`
(wired to a new Stop button, enabled only while an Ask is in flight)
stops a request promptly regardless of which phase of a potentially
long request is currently running. Best-effort by design: llama-server
has no cancellation endpoint of its own, so the model's own generation
keeps computing server-side regardless — this only stops relaying or
waiting on it further, same as an ordinary dropped connection already
does today, and the student is told that honestly ("Stopped at your
request") rather than shown a misleading failure message.

### Verified live, 2026-09-21

Real redeploy. Basic Run and Ask both confirmed still working
end-to-end (grounded real execution unaffected by this stage). Run
rate limiting genuinely 429s after the 10th request in a minute and is
correctly IP-scoped (a different `X-Forwarded-For` IP is unaffected).
Two genuinely concurrent Ask requests: the second 429s with the real
reason ("you already have a question in progress") while the first
completes normally. A real in-flight Ask — mid-generation, actually
computing a slow prime-search script — was interrupted via `POST
/sandbox/interrupt` and stopped cleanly within ~5 seconds, reporting
"Stopped at your request" rather than a misleading failure. Per-IP
state is correctly cleared afterward: an immediate follow-up Ask
succeeds rather than staying wrongly blocked, and interrupting with
nothing in flight correctly reports `interrupted: false`.

Also verified locally before the live pass: sliding-window limiter
behaviour including window eviction and per-IP/per-bucket
independence, `_client_ip()`'s XFF-vs-TCP-peer fallback, and the
interrupt-checking poll loop against a real subprocess (both a pre-set
interrupt and one set from another thread mid-run). Full existing test
suite re-run clean throughout.

---

## Stage 5 — Firecracker sandbox shell + network access

The user collapsed three remaining out-of-scope items into one
combined ask: *"i think all of those but i'd particularly like to see
the student get a shell as well"*, then *"i'd like the sandbox to have
network access yes but i don't want them to be able to jailbreak the
sandbox and have access to anything else other than the sandbox and
network"* — and explicitly deferred the isolation architecture:
*"security isn't my strength... asking you for the best way to
provide the closest to my requirements."*

**Decision**: Firecracker microVMs, not a container sandbox — a real,
separate guest kernel under KVM (confirmed live that nested KVM is
available on the coordinator), not a shared-kernel boundary a
persistent, network-connected shell has far more opportunity to probe
than the existing bounded, network-less Python sandbox.

Split into two sub-stages, matching this project's own build-verify-
next-layer discipline. **This is Stage A only**: infra + the network
isolation proof, live-verified, no WebSocket service or browser UI
yet.

**New**: `api/build-firecracker-rootfs.sh` — builds the golden guest
rootfs (debootstrap, minimal Ubuntu 22.04, `sshd` + a passwordless-
sudo `student` account, a one-shot boot unit fetching the session's own
SSH key from Firecracker's MMDS — never baked into the image),
following `build-package-repo.sh`'s own "throwaway CloudCore instance,
build there, pull back" architecture. Deliberately not Firecracker's
own quickstart demo image (a shared squashfs + a shared public demo
key — wrong for a multi-tenant lab). Firecracker v1.17.0 + jailer and
a pinned CI kernel, both downloaded and verified for real (the
release's own `SHA256SUMS` checked against the actual bytes, a real
`firecracker --version` run against the extracted binary).

**Network isolation** (the actual security mechanism): a new
coordinator-local bridge `fcbr0`, subnet `10.200.0.0/24`. `iptables`
`MASQUERADE`s it out to the real internet; the `FORWARD` chain drops
every RFC1918 destination before the general `ACCEPT` — deliberately
never enumerating specific CloudCore ports/IPs, since that's brittle.
`dnsmasq` serves DHCP+DNS for the subnet.

### Verified live, 2026-09-21 — and two real bugs found by doing so

Hand-booted one real microVM against the actual coordinator, SSH'd in
with a throwaway per-session key (same delivery shape MMDS will use in
Stage B), confirmed a real `student` account with working passwordless
sudo. Positive: real HTTPS egress to two external sites, DNS via the
coordinator's own `dnsmasq`. Negative (the actual bar): the microVM
cannot reach the coordinator's own `verify-proxy` port, cannot reach
the coordinator's own SSH on its real bridge address, cannot reach the
CloudCore control-plane's examples-listener (`:8083`) or package
mirror (`:8090`) — neither via HTTP nor a raw TCP connect. IPv6:
confirmed the guest has no global IPv6 route at all, so there's
nothing for the IPv4-only iptables model to miss as currently built.

Live testing caught what static review didn't:

1. The first INPUT-chain rule only blocked traffic to the
   coordinator's `fcbr0` gateway address specifically. Live testing
   found the coordinator's *other* address (its real bridge IP, a
   different interface) was still fully reachable from the sandbox —
   SSH included — because the kernel delivers INPUT-chain traffic
   locally based on *any* locally-owned destination address, not just
   the one the rule happened to name. Fixed by dropping the
   destination filter entirely (INPUT-chain traffic is already
   guaranteed local-destined by definition) plus an
   `ESTABLISHED,RELATED` exception — which is also what makes the
   coordinator's own outbound SSH into a microVM (Stage B's whole
   terminal mechanism) work at all, since the original rule silently
   dropped its own replies too.
2. The MMDS-key-fetch script's `curl` call had no
   `--connect-timeout`/`--max-time`, so a stalled fetch could hang for
   tens of seconds per attempt across 20 retries — confirmed live this
   stalls boot far past the intended ~5s budget. Also added an
   explicit `exit 0`: a false `if` with no `else` was becoming the
   script's own exit status, making systemd report a hard failure for
   what's actually a correct, graceful "no key available" outcome.

Both fixes verified live after applying them, not just reasoned about.

### Stage B — the WebSocket terminal service + browser UI

`examples/llm-chat/files/sandbox_terminal.py` — a new, separate
systemd service (`sandbox-terminal.service`), modeled directly on
`api/terminal.py`'s proven WS↔SSH bridge shape, but owning the full
per-session Firecracker lifecycle instead of connecting to an existing
CloudCore instance: on each WS connection, checks a per-IP concurrency
cap, boots a fresh microVM (a private rootfs copy, a fresh ephemeral
ed25519 keypair delivered via MMDS — never baked into any image),
bridges the shell, and tears everything down completely when the
connection ends. Deliberately **not** run through `jailer` in this
first pass — a host-side hardening layer on top of the VMM process
itself, not what makes the actual guest-to-host isolation boundary
work (that's the KVM guest kernel + Stage A's own iptables policy) —
named explicitly as a follow-up, not a silent gap. LB wiring reuses the
same loopback listener via a new path routing rule
(`/terminal*` → a new target group on `terminal_port`), confirmed
against the real provider source, not assumed. Browser side: a new
Terminal panel in the sandbox page using `xterm.js` (already vendored
for the Dashboard's own admin Terminal feature, reused as-is).

### Verified live, 2026-09-21 (Stage B) — two more real bugs found by doing so

Full end-to-end, multiple independent sessions, against the actual
coordinator: real per-session microVM boot (~9-13s), real MMDS-
delivered SSH key, real shell login as `student`, real command
execution through the actual WebSocket bridge. Real internet egress
and internal-address blocking both re-confirmed from *inside* a live
interactive session (not just Stage A's one-shot smoke test) — a
blocked-address `curl` correctly timed out (`rc=28`) from within a
real bridged shell. Concurrency cap: the 5th/6th of 6 simultaneous
connections correctly rejected with a clear capacity message while the
first 4 booted under real contention. Teardown confirmed complete
every time — zero leftover processes/TAP devices/session directories
across many test sessions, including ones that disconnected mid-boot
(bounded by the existing timeouts, not instant, but self-cleaning — a
known minor latency gap, not a resource leak). Full existing local
test suite re-run clean — zero regression to the untouched Run/Ask
flow.

Live testing again caught what review didn't:

1. **jammy's own `python3-websockets` (9.1-1) is fundamentally broken
   on jammy's own current Python 3.10.12** — it calls the removed
   `asyncio.Lock(loop=...)` parameter, crashing every single WS
   connection with a real `TypeError`. `api/terminal.py` never hit
   this because it runs on a completely different host/Python (3.12,
   pip-installed 17.0.1) — confirming the package *exists* in the
   archive was never the same as confirming it *works* on the actual
   target runtime. Fixed by vendoring `websockets` 16.1.1 (the newest
   release still supporting Python 3.10, per PyPI's own metadata) as a
   pinned `ARTIFACT_URLS` wheel, extracted via `python3 -m zipfile`
   (no new apt dependency) into a `PYTHONPATH` the systemd unit points
   at — same pinned-artifact convention as every other third-party
   binary in this project.
2. **Firecracker's MMDS returns a leaf string value JSON-quoted**
   (literal surrounding double quotes) when the request sends
   `Accept: application/json` — confirmed live via direct MMDS API
   inspection and a real guest-side debug capture that the delivered
   SSH key was landing in `authorized_keys` wrapped in quotes,
   breaking every login. Fixed by omitting that header entirely
   (MMDS's own IMDS-compatible plain-text format is exactly the raw
   key line SSH expects). Also confirmed live and fixed: MMDS's
   link-local address needs an explicit host-scope route inside the
   guest (`ip route add 169.254.169.254 dev eth0`) — not reachable via
   the normal default route alone.

**At the time**: not yet done was an actual `tofu apply` exercising the
new LB `routing_rules` end-to-end through the real load balancer
(blocked on not having this deployment's own `worker_peers` value
outside the Dashboard's own Build Manager) — the WS service itself was
instead verified directly against its loopback port. See Stage 5C
immediately below for what that real apply actually found.

**Known follow-ups, not silently deferred**: `jailer`-based host-side
hardening for the VMM process (named above); pre-warming/pooling for
faster boots; a slightly tighter mid-boot-disconnect cleanup path.

### Stage 5C — the real LB verification, and four real bugs found closing it

The gap named above closed itself the first time the user actually
built this through the Dashboard's own Build Manager (the first real
`tofu apply` with a genuine `worker_peers` value) — and immediately hit
a live 503 outage, reported directly: *"tried building llm-chat and we
get 503 unvailable after quite some time of waiting."*

Four real, compounding bugs found and fixed chasing it down, each one
unmasking the next:

1. **`api/server.py`'s every LB mutation endpoint did an unlocked
   read-modify-write.** Terraform applies independent resources against
   the same LB concurrently by default (no `depends_on` between two
   unrelated `cloudcore_lb_target_group` blocks); the two target-group
   creates in this apply — the coordinator's own, and Stage 5's new
   terminal one — genuinely raced, and whichever `put_lb()` committed
   second silently overwrote the first's write. The coordinator's own
   target group vanished from the stored LB entirely, its listener's
   `target_group_id` left dangling, haproxy falling back to a bogus
   port-80 guess for the coordinator itself. Fixed with a new
   per-`lb_id` lock (`store.lb_lock`) wrapping every such handler's full
   read-modify-write span — a structural fix, not scoped to this one
   example, since any LB that picks up more than one target group in
   the same apply was equally exposed.
2. **HAProxy's classic health-check directive sends a bare HTTP/1.0
   request**, and `websockets` 16's own parser rejects HTTP/1.0 outright
   before it ever reaches `sandbox_terminal.py`'s own health hook.
   Fixed with a `GET /health` short-circuit plus switching
   `api/lb.py`'s own health-check generation to HAProxy 2.x's explicit
   `http-check send ... ver HTTP/1.1` form, for every target group, not
   just this one.
3. **`sandbox_terminal.py` bound its WebSocket server to `127.0.0.1`**,
   but HAProxy reaches a bridge-mode target group's server via the
   instance's own real `private_ip`, never loopback — every LB-routed
   request got a genuine connection-refused. Bound `0.0.0.0` instead,
   matching `verify_proxy.py`'s own already-correct convention (Stage
   5B's own oversight — that file was added after `verify_proxy.py`'s
   binding convention was already established, and never matched it).
4. **A hard JS `SyntaxError` broke Run/Ask/Terminal simultaneously.**
   `SANDBOX_PAGE_HTML` is a plain (non-raw) Python string, so the two
   ANSI-color `term.write()` lines added for the Terminal panel's own
   error styling had their escape sequences collapsed to real control
   bytes by Python before the page was ever served — a literal,
   unescaped newline landing inside a single-quoted JS string in the
   served page, a hard `SyntaxError` that broke the *entire* inline
   `<script>` tag at once (confirmed directly: extracting the
   previously-shipped page's own script and running Node's `--check`
   against it reproduces the exact error; the fixed version passes).

All four deployed live and verified end-to-end through the real load
balancer: `/`, `/health`, and `/terminal` all healthy, a real terminal
session usable, the served page's script confirmed syntactically valid.

## Stage 6 — Browser preview for anything the student runs in the Terminal

Direct follow-up, once the Terminal's own real internet access made
"run a web server and see it" something a student would actually reach
for: *"we need to provide the student a way to run their code and see
the results/output... if creating a browser based output program...
there's no way to connect a browser to see the output."* Clarified via
direct follow-up into a fixed pool of ports decided once at deploy
time, not per-session, since a real program often needs more than one
port at once: *"a student's own program... may be more than one port
required... create 4 ports with high numbers out of the way of
anything obviously known... when the student opens the terminal give
them a reminder."*

**`preview_ports`** (default `41001`–`41004`): `sandbox_terminal.py`
opens one small reverse-proxy listener per port alongside the existing
WebSocket terminal, routing each incoming connection to the right
student's own currently-connected microVM by reading
`X-Forwarded-For` off its first request — the same IP-keyed session
model the WS handler already uses for concurrency — then splicing raw
bytes for the rest of the TCP connection, so whatever the student's
program returns (HTML, JSON, images, even its own WebSocket upgrade)
passes through completely unmodified. A plain `GET /health`
short-circuits before any of that, same fix already applied to the
terminal target group's own check. `main.tf` gets one target group +
listener per preview port (`for_each`, not `count`, matching this
project's own Terraform convention). The Terminal panel's own
description and the shell's "connected" message both remind the
student which ports are live the moment a session starts.

Then, per direct follow-up idea — *"we should probably look at
providing a lab web browser... one that can be used to browse to any
of the four ports we allow"* — a new **Preview** panel on the sandbox
page itself, next to Terminal: a port selector (one button per
configured port, the active one highlighted) driving a plain
`<iframe>`, a **Refresh** button, and an always-visible **Open in new
tab** link for anything that refuses to be framed. No new capability
or attack surface — purely a browser-side convenience over the exact
same per-session proxy above; everything shown was already reachable
by opening the same URL in a new tab, the panel just saves the round
trip. The port list itself is substituted server-side from the real
configured `preview_ports` at page-load time, never hardcoded.

### Verified live, 2026-09-22

Full round-trip through the real load balancer, twice over. First:
a real terminal WebSocket session, a real `python3 -m http.server`
started inside the isolated microVM with real content, fetched from a
second, independent connection through the LB on the same port — the
student program's own real content came back, 200 OK; a request with
no active session got a clean 502 instead of hanging; the microVM
tore down with zero leftover processes once the session ended. Second,
with a real headless-Chrome screenshot of the actual rendered sandbox
page (not just `curl`): the Preview panel's iframe showing the
student's own live page for real, port buttons rendering correctly
with the active one highlighted, Refresh/Open-in-new-tab both present.

## The golden rootfs: `vi`/`vim`, and `apt install` actually working

Direct report once students started really using the Terminal for
real: *"if you start a terminal and try to vi a file (existing or new)
vi (vim) is not found, if you try to apt install vi (vim) it's not
found."* `vim` was simply missing from the golden image's own base
package list (fixed, alongside the existing `nano`); `apt install
<anything>` failing was a real, general gap, not specific to vim — the
golden image's own build-time cleanup strips the apt package index to
keep the shipped artifact small (correct for a frozen image reused
across many future sessions), but nothing ever refreshed it at boot.

Rebuilding the golden rootfs to fix that surfaced three more real,
unrelated bugs, each one masking the next until fixed:

1. The build script's own `chroot ... /bin/bash -c '...'` form — three
   quote-levels deep inside an `ssh "..."` argument — silently
   corrupted mid-parse on every single real rebuild attempt. Reproduced
   identically on the completely unmodified, pre-existing script,
   conclusively ruling out anything specific to this change. Fixed by
   writing the same provisioning script to a real file and running it
   with `bash <file>` instead of the fragile inline form.
2. `network-online.target` is satisfied trivially on this minimal
   debootstrap image (no NetworkManager/systemd-networkd wait-online
   unit installed), so the first fix attempt (a one-shot `apt-get
   update`) raced real network readiness at boot.
3. Far more persistent: `/etc/resolv.conf` as set at image-build time
   gets silently replaced at boot by systemd's own resolved-stub
   symlink, which is never actually running on this minimal image —
   every DNS query failed permanently, not just transiently at boot,
   confirmed live with `getent hosts` returning nothing at all
   indefinitely. Fixed by force-writing a real, static resolver on
   every retry attempt, not just once at image-build time.

### Verified live, 2026-09-22

End to end, through a real terminal session on the redeployed golden
image: `which vi vim` resolves to real binaries, `vim --version` runs,
and `sudo apt-get install -y bc` genuinely downloads, installs, and
the installed binary computes correctly (`echo 2+2 | bc` → `4`) —
through the real DNS/network path, not a stub. Known, flagged
limitation, not silently papered over: debootstrap's own default
`sources.list` only enables the `main` component, so a package living
in `universe` (`tree`, the first one tried) still reports not-found
even with a now-fully-working index.

## The sandbox's own system prompt: closing two real gaps

Asked directly to reflect on what had actually been learned about the
prompt recently. Two real gaps folded into `sandbox_system_message`'s
own default:

- **An orphaned anti-hallucination instruction.** `webui_system_message`
  already carried real wording ("only describe what code actually
  does... say so explicitly if you're not certain"), written for a real
  stress-test failure this project found — but that variable went
  vestigial once Phase 4 closed off llama-server's own webui, so the
  lesson it encoded just sat unused. Carried across into the prompt
  that's actually live today.
- **Zero awareness the Terminal/preview ports exist at all.** The
  prompt only ever described the Run sandbox, so the model had no
  grounding to correctly answer whether it could install a package or
  serve a web page — it could only guess or hallucinate either way.

Live-testing the fix caught a real gap in the fix itself: without a
concrete port number, the model confidently filled in Flask's own
conventional default (port 5000, then 8080) instead of a real,
actually-proxied one — worse than not mentioning ports at all. Fixed
by substituting the real configured `preview_ports` into the prompt at
`verify_proxy.py`'s own load time (the same mechanism the Terminal
panel's own UI reminder already uses), never a hardcoded guess.

---

## Stage 7 — Five student-experience improvements, and a Regenerate bug caught only by real browser testing

Exploratory ask — "can you think of anything more for the sandbox
that would improve the student experience?" — followed by direct
approval to build all five proposed ideas together:

1. **"Use this code" button.** Every fenced code block in the Ask
   panel's own replies now gets its own button, wiring `cm.setValue()`
   straight from the model's answer into the Run editor — no more
   manual copy/paste out of the transcript.
2. **"Send to Terminal."** Sends the editor's own contents into a live
   terminal session as a real file, via the same base64+quoted-heredoc
   pattern this project settled on for the browser→terminal input path
   after F-107's own hard-won lesson about shell-escaping fragility —
   chosen specifically because base64's alphabet contains no
   shell-special characters, so arbitrary code (quotes, `$vars`,
   backticks, anything) survives byte-perfect regardless of content.
3. **Terminal session warnings.** `sandbox_terminal.py`'s per-session
   loop now sends a new `{"type": "warning"}` WS frame once, 300
   seconds before the hard max-session cutoff and 120 seconds before
   an idle timeout would fire — giving a student real notice instead
   of a session just vanishing.
4. **Preview panel auto-refresh.** A plain 5-second `setInterval`, not
   genuine "detect when something starts listening" — a browser's
   CORS-opaque `fetch`/`<img>`/`<iframe>` responses cannot actually
   distinguish a real 200 from a listening student program vs. this
   project's own 502 "no active session yet" response, so a real
   detection mechanism isn't buildable here. Documented as an honest
   tradeoff in the code itself, not oversold.
5. **"Regenerate."** Re-asks the model's last question unchanged,
   appending a fresh reply alongside the original rather than
   replacing it, so a student can compare answers.

Three of the five were verified directly against the real running
coordinator through WebSocket/curl test harnesses: send-to-terminal
round-tripped shell-special-character content byte-perfect and ran
correctly; the idle-timeout warning fired at the right threshold
against a temporarily-lowered timeout; the preview panel's own
screenshot confirmed correct rendering. The remaining two are
client-side-only DOM/JS behaviour, which needed a real browser rather
than a WS/curl harness — driven via Chrome DevTools Protocol against a
real headless Chrome instance pointed at the live page, chosen over
waiting on this host's own slow (~1 token/sec) real model round trips
just to get a code block to click.

### F-112 — Regenerate was broken in the exact state it was enabled in

That CDP testing caught a real bug that no static check (`py_compile`,
`node --check`) could have: `regenerateAsk()` required history's
*last* entry to be the student's own question before proceeding — but
by the time the button is actually enabled (inside the ask-completion
handler, after a full round trip), history's last entry is always the
model's own just-added reply. Clicking Regenerate immediately after
any normal Ask — the exact moment the button is designed for —
failed with "Ask a question first." A second, related bug: the
button's enabled/disabled state was only ever set inside that same
completion handler, never synced from saved history itself, so a
returning student (page reload restoring history from `localStorage`)
or a student who'd just hit Clear both saw whatever state was left
over from the last request, not one reflecting what history actually
held.

Fixed by scanning backward for the most recent question rather than
requiring it to be the very last entry, and by deriving the button's
enabled state from saved history on every `renderTranscript()` call —
page load, post-Ask, post-Regenerate, and post-Clear all now derive it
from the same single source of truth.

### Verified live, 2026-09-22

Via CDP against the real deployed page, including one real model round
trip: fresh page → `regenBtn` disabled; `localStorage`-restored
history (simulating a reload) → correctly enabled with no live ask
needed; a real `regenerateAsk()` call succeeded with no "Ask a
question first." bailout; history grew `user, assistant, assistant` —
the regenerated reply appended alongside the original, not replacing
it; the button stayed enabled after; Clear correctly reset it to
disabled. All changes deployed to the redeployed, restarted
`verify-proxy`/`sandbox-terminal` services and confirmed healthy
through the real LB.

---

## Stage 8 — a separate Linux Help panel, general Q&A kept apart from coding

Direct request: "it's time now to allow the llm to provide answers to
general linux based questions (all questions from admin to use)."
Asked whether to keep that separate from the existing coding Ask
panel; agreed, then approved a concrete design: a new panel with its
own system prompt, grounded not through an automatic re-execution loop
(the coding panel's own mechanism, which only works because
`run_sandboxed()` is a disposable, stateless, always-safe sandbox) but
through the **Terminal panel already built in Stages 5/7** — every
suggested command gets a **Run in Terminal** button, sent to the
student's own already-live session only if they click it. A shell
command suggested for a general question (`rm`, `apt install`,
`systemctl restart`, `sed -i`) isn't safe to auto-run against a
student's live, stateful shell the way disposable Python code is, and
this reads directly off the user's own earlier framing of the Terminal
feature itself ("don't want them to be able to jailbreak the
sandbox").

### Backend

`_handle_sandbox_ask`/`_do_handle_sandbox_ask` generalized into a
shared `_handle_ask`/`_do_handle_ask(system_message, capture_source,
verify, include_code, endpoint_label)`, called by both the existing
coding endpoint and a new `POST /sandbox/linux-ask` — same per-IP
rate-limit and one-in-flight concurrency gate for both (a student only
ever has one live question regardless of which panel asked it, and
both hit the same slow shared backend), so `/sandbox/interrupt`'s Stop
button needed no changes.

`_relay_and_verify_stream` gained a `verify: bool` parameter. When
`False` (the Linux panel's own path), the whole
extract-code/auto-run/capture block is skipped entirely — one guard
realizing both "don't auto-execute a suggested command" and "don't
capture into the Phase 3 learning corpus" at once, since that corpus
hard-requires real code/exec grounding data (`generated_code` is
`NOT NULL`) this panel deliberately never produces.

A new `LINUX_SYSTEM_MESSAGE` (own file-based load mechanism,
`linux-system-message.txt`, same convention as the coding panel's own
prompt) covers everyday usage through real sysadmin tasks, is grounded
in the Terminal's own real constraints (Ubuntu 22.04, `main`-component-
only apt index, no persistent storage, real preview ports), asks for
suggested commands in fenced ```bash blocks, and is explicit that a
suggested command is not automatically run or checked here — unlike
the coding panel's prompt.

### Frontend

Rather than duplicate the coding Ask panel's ~150 lines of JS a second
time, `renderTranscript`/`_renderAssistantContent`/`getHistory`/
`saveHistory`/`askModel`/`regenerateAsk`/`_runAsk`/`stopAsk` were
refactored into one `makeAskPanel(cfg)` factory, instantiated twice
(`codeAsk`, `linuxAsk`) with only the real differences — endpoint,
request body, history key, and what a code block's own button does —
passed in as config. The Linux panel's code blocks get a **Run in
Terminal** button instead of **Use this code**, wired to a new
`runCommandInTerminal(code, statusEl)`: a quoted heredoc piped into a
fresh `bash` (not the base64-into-a-file mechanism `sendCodeToTerminal()`
already uses for the editor's own Send to Terminal button — that one
exists so `python3` can run the result afterward and deliberately
prevents shell interpretation; this one's whole point IS for the shell
to interpret `$vars`/backticks normally, which is what "running a
command" means). The heredoc delimiter carries a random suffix rather
than a fixed literal, since this panel's own answers can plausibly
include a heredoc example of their own using a plain "EOF"-style name.
Clicking the button with no active Terminal session gives real
feedback ("Start a terminal below first.") in the panel's own status
line rather than a silent no-op.

### Verified live, 2026-09-22

Full real round trip via Chrome DevTools Protocol against the real
deployed page: a genuine `/sandbox/linux-ask` question ("how do I
check disk usage") came back with a real `df -h` suggestion in a
fenced bash block; a synthetic conversation with a `touch ... && ls
-lh` command was injected to avoid waiting a second time on this
host's slow (~1 token/sec) real generation; the **Run in Terminal**
button was clicked against a real, already-booted Firecracker microVM
session, and the exact command executed for real — the terminal's own
buffer showed the heredoc arrive, run, and the real `ls -lh` output
(the actual created file, actual timestamp) come back. Clicking with
no Terminal session open showed the real "Start a terminal below
first." message. Regression-checked the existing coding Ask panel
end to end (real question, real code, real re-execution, "Use this
code" button) to confirm the shared-factory refactor changed nothing
about its behavior. Confirmed the per-IP concurrency gate is correctly
shared: a Code-ask attempted while a Linux-ask was in flight from the
same IP got the existing "already have a question in progress" 429.
Confirmed directly against the examples corpus (`GET
/v1/llm-chat/examples`) that a Linux-ask turn adds no new row, while a
coding-ask turn still does, both before and after this change.

One real methodology lesson from this round, not a product bug: the
coordinator's own port 8620 serves `/sandbox/*` directly (so testing
against it works for those routes), but `/terminal` only exists via
the load balancer's own path-based routing to the separate terminal
service — a test hitting the coordinator's address directly for
`/terminal` times out with nothing useful logged, not a clean error;
the fix was testing against the LB's own real listening address
instead. Worth remembering for the next round of live verification,
even though nothing in the shipped code was wrong.

### Follow-up: a bigger question box

Direct report: "the linux text input box (ask) needs to be much
larger than it is. perhaps at least capable of showing 4 lines of
typed text." `linuxQuestion` changed from a single-line `<input>` to
a 4-row `<textarea>` (vertically resizable), with Shift+Enter now
inserting a newline and a plain Enter still submitting, same
submit-on-Enter convenience the single-line box had — `askModel()`
itself needed no change, since `.value`/`.focus()` work identically on
both element types. Verified live: the element renders as a real
4-row textarea (confirmed via screenshot and its own computed height);
a plain Enter correctly still submits (`askBtn` disables, the box
clears); Shift+Enter correctly does not submit (`askBtn` stays
enabled) — verified via Chrome DevTools Protocol dispatching real
keyboard events, distinguishing the two cases directly rather than
assuming the conditional works.

### Follow-up: a recovery path for a genuinely stuck Terminal session

Direct question, prompted by the Linux Help panel's own "Run in
Terminal" button now making it easy to run something destructive (e.g.
a suggested disk-partitioning command) against a real live shell: is
the platform prepared if that leaves the session unresponsive or
destroyed beyond use? The architecture already recovers cleanly on its
own -- every session is a disposable microVM, and Disconnect's
client-side `ws.close()` plus the server's `SIGTERM`-then-`SIGKILL`
`teardown()` don't depend on the guest responding at all. The one real
gap: a guest left unusable (e.g. a trashed root fs) but whose SSH
channel doesn't itself error, since the kernel/sshd can still be
resident in RAM -- the existing idle-timeout can't catch this, because
retyping into a dead shell still counts as activity.

New signal in `sandbox_terminal.py`: `TERMINAL_UNRESPONSIVE_SECONDS`
(default 30) tracks real input sent with no real shell output
following it for that long, distinct from the existing idle/max-session
timers. `ssh_reader()` resets the clock on every real output byte
relayed; the main loop's periodic check (same cadence as the existing
idle/max-session warnings) fires a new `{"type": "unresponsive"}` frame
once per approach, resetting so it can fire again later in the same
session. The browser shows this as a `.warn`-styled status line plus a
new **Start a fresh session** button (`termRestartBtn`, hidden except
when this fires) -- `restartTerminal()` is just `stopTerminal();
startTerminal();`, one click instead of the student needing to know
Disconnect-then-Start-terminal is what actually recovers a stuck
session.

A real discovery from live-testing this, not assumed from reading the
code: a naive "how long since the last output" signal alone would
false-positive on any merely slow-but-fine command (a big `apt
install`, a large download), which is exactly the false alarm the
design set out to avoid by phrasing the hint as a question rather than
an assertion. Testing it live with a real, safe `sleep 15` command
showed the mechanism is actually more precise than that: bash's own
readline echoes typed input back immediately (proof the shell is alive
and has acknowledged it) *before* a slow command's own silence begins,
so the "last input vs last output" comparison never trips for an
ordinary running command at all -- it only fires when the shell never
even acknowledges receiving the input in the first place. Confirmed
directly live with a real, safe simulation of true unresponsiveness
(`SIGSTOP` on the session's own real Firecracker process on the
coordinator, not just a slow command): the hint correctly appeared
after the configured threshold in both a raw WebSocket test and
through the real browser UI (Chrome DevTools Protocol), the **Start a
fresh session** button correctly appeared and, clicked, tore down the
frozen VM and booted a genuinely new one end to end (confirmed via the
new session's own real "Booting... Connected... Welcome to
Ubuntu..." transcript); `SIGCONT`-ing the original frozen VM afterward
confirmed it would otherwise have resumed cleanly with zero data loss
(the buffered `echo hello` finally completed and printed real output)
-- proving the freeze itself, not the recovery mechanism, was the only
thing standing between the student and a working shell. New Terraform/
Ansible variable `terminal_unresponsive_seconds`, same threading
convention as the other `terminal_*` tunables.

### Follow-up: F-113 -- a leaked builder hostname, a missing `/etc/hosts`, and no `fdisk`

Direct report from actually using the new features together: running
`fdisk` from a Terminal session after a Linux Help suggestion showed
`sudo: unable to resolve host cloudcore-fcrootfs-builder-1790072530:
Name or service not known` then `sudo: fdisk: command not found`.
Full detail in `haFullStack-Findings-Log.md`'s own F-113 -- in short,
`/etc/hostname` had silently leaked the throwaway build instance's own
real hostname into the golden image (shared by every session since),
`/etc/hosts` didn't exist in the minbase chroot at all, and `fdisk`
was genuinely never in the base package list. Rebuilt the rootfs for
real rather than hand-reasoning about the nested chroot-heredoc
quoting locally -- a local dry-run reproduction attempt gave
misleading, contradictory results and was abandoned, directly the
same F-107 lesson this script's own header comment already documents.
New pinned artifact deployed live; verified via a real fresh Terminal
session that the hostname now reads `sandbox` everywhere (including
the shell prompt itself), `/etc/hosts` has real entries, and
`sudo fdisk -l`/`lsblk` both run with zero resolution warning.

### Follow-up: teaching the model to self-check before suggesting a command

Direct follow-up prompted by the fdisk gap above: should "Run in
Terminal" pre-check whether a suggested command's tool needs
installing first? Recommended against a real pre-check mechanism --
reliably detecting which binary an arbitrary, possibly multi-command
shell block depends on isn't solvable in general -- in favor of
teaching the model to build its own install-if-missing check into the
one command it suggests, since the shell already has real internet
and working `apt`.

Iterated live rather than shipped on a first guess. The first wording
("build a check into the SAME command") got real, partial compliance:
asked about `ifconfig`, the model correctly recognized it might be
missing and added an install step -- but as a second, separate fenced
block, leaving the naive first block (which would still fail) sitting
right alongside it. Tightened to an explicit "give ONLY ONE block, do
NOT also show a plain/naive version" instruction and re-tested against
two more real, different questions (`traceroute`, `htop`): both came
back as exactly one correctly-combined `command -v X >/dev/null ||
sudo apt-get install -y X; X` block, confirming the fix generalizes
rather than only working for fdisk, the one example spelled out in the
prompt itself. `linux_system_message` and the live coordinator's
`linux-system-message.txt` were redeployed after each wording change,
each round checked against a real `/sandbox/linux-ask` round trip
rather than assumed correct from the text alone.

---

## Stage 9 — closing the loop: run, fault, offer a fix, run again

Direct request: "is there a way we can tie the run in terminal back to
the llm and check for it's success. if no success offer a fix and
allow another run in terminal and so on until fixed." Explicitly *not*
autonomous re-execution -- confirmed directly: "i wasn't wanting
autonomous, run, fault, offer alternative, offer run in terminal again
was my idea." The whole reason "Run in Terminal" is student-triggered
rather than auto-executed (Stage 8's own design) is that a shell
command can be destructive/stateful, unlike the disposable Python
sandbox the coding panel auto-re-runs -- so this stays fault
*detection* and *diagnosis* automatic, every actual execution still a
real click.

### Design

Every command sent via "Run in Terminal" now carries a second,
randomized completion sentinel appended after the existing heredoc:
`echo "MARKER:$?"`, run by the *outer* interactive shell immediately
after the heredoc-fed `bash` invocation finishes, so `$?` is genuinely
that invocation's real exit status. The browser polls the real,
already-rendered `xterm.js` buffer (`term.buffer.active`,
`translateToString(true)` -- clean, ANSI-stripped text, not the raw WS
byte stream which still carries cursor/color/bracketed-paste codes)
for up to 45 seconds. On a genuine non-zero exit, the real command,
the real captured transcript, and the real exit code are automatically
relayed back to the model as a new turn, plainly labeled `[Automatic
-- result of your last suggested command, sent via Run in Terminal]`
so it's never mistaken for something the student typed themselves
(same transparency standard as the coding panel's own `ACTUALLY
EXECUTED` blocks). A genuine success is reported quietly (no model
call at all -- no need to spend a slow round trip confirming something
that already visibly worked). A timeout says so honestly ("may be
long-running") rather than claiming success or failure, since a
genuinely long-lived or interactive command (a server, `htop`, `tail
-f`) never prints the sentinel at all until the student stops it
themselves.

A new `askWithText(question)` on the shared `makeAskPanel` factory
(`askModel()` itself now a thin wrapper reading the textarea and
calling it) is what lets this system-constructed turn enter the Linux
Help conversation exactly like a real question would -- no new backend
route needed at all; the "diagnosis" is just another turn through the
existing `/sandbox/linux-ask`, sharing its rate limit and concurrency
gate. The model's reply comes back through the exact same
`renderAssistantContent()` path as any other answer, so any new
command it suggests automatically gets its own "Run in Terminal"
button -- this is what makes "and so on" work for free, no special-
casing needed for a second or third round.

### F-115 — two real, self-introduced bugs, both caught by live testing before shipping

Full detail in `haFullStack-Findings-Log.md`'s own F-115. In short:
the sentinel-matching logic originally misdiagnosed *every* command,
success included, as a failure -- the shell's own echo of the typed
`echo "MARKER:$?"` command (shown back before it even runs) also
contains the literal text `MARKER:`, and a plain `indexOf`+`slice`
matched that line first, parsed `NaN` from the literal `$?"`, and
`NaN !== 0` is always `true` in JavaScript. Caught not by the test that
was designed to find it, but by noticing a stray in-flight model
generation a *successful* command shouldn't have triggered. The fix
(require real digits after the colon via a regex) then broke the
entire live page with a real `SyntaxError` on its first attempt -- a
defensive `marker`-escaping step had its own backslash-doubling bug in
this file's own recurring non-raw-Python-string trap (F-105, F-112).
Root-caused, this time, by importing the actual Python module and
reading `SANDBOX_PAGE_HTML`'s real runtime string directly -- an
earlier attempt to verify through a `bash -e`/heredoc reconstruction
gave misleading results from yet another, uncontrolled layer of shell
escaping, and was abandoned once that became clear. Fixed by dropping
the defensive escaping entirely rather than re-fixing it: `marker` is
always plain alphanumeric/underscore by construction
(`CLOUDCORE_RC_` + `Math.random().toString(36)`), never a single
regex-special character, so escaping it was unnecessary complexity in
the first place -- and the exact thing that caused the second bug.

### Verified live, 2026-09-22

Two independent, real verification methods, neither of which is a
browser. First: the exact deployed JS was confirmed syntactically
valid by importing the real Python module and reading
`SANDBOX_PAGE_HTML`'s actual runtime value through `node --check` --
not a hand-traced or shell-escaped reconstruction, which had already
proven unreliable earlier in the same investigation. Second: the exact
command-construction and matching logic was validated against the
real, running terminal service via a direct raw-WebSocket test
(bypassing any browser entirely) covering three cases -- `true`
(rc=0), `false` (rc=1), `exit 42` (rc=42) -- all resolved correctly
within roughly 0.1s of the real server's own response, proving both
the regex fix and the underlying relay pipeline are fast and correct.

The overall mechanism (fault -> automatic diagnosis -> real model fix
-> a second "Run in Terminal" click -> success reported quietly, loop
naturally ending) was separately confirmed end to end through the real
browser earlier in the same investigation, before the regex bug was
found -- a real `gparted_cli_xyz_does_not_exist --list` failure (rc
127) triggered a real automatic diagnosis, the model correctly
suggested `lsblk` as a real alternative, and clicking that fix's own
"Run in Terminal" button ran it for real (rc 0, correctly quiet, no
further diagnosis).

Two things found live, worth recording honestly rather than glossing
over. `llama-server`'s own 14B model was found consuming 87% of the
coordinator's 3.8GB RAM after this session's own sustained heavy
use -- a legitimate, expected footprint (not a leak), but severe
enough at times to degrade real-time responsiveness across the whole
host; restarting it (standing permission for lab-infra resets between
phases) restored healthy headroom. Separately, headless Chrome under
CDP automation showed consistently delayed `xterm.js` buffer updates
(multiple seconds behind the real, sub-second server relay, confirmed
directly by the same raw-WebSocket test run in parallel) even on a
freshly restarted, otherwise-idle coordinator -- concluded, after
directly ruling out coordinator load, memory, and orphaned sessions as
causes, to be a rendering-pipeline artifact specific to headless,
display-less CDP test automation (`xterm.js`'s own buffer sync is
plausibly tied to a rendering loop a `--disable-gpu` headless instance
doesn't drive promptly), not a product defect, and not something a
real student's own visible browser tab would experience.

---

## Explicitly out of scope — rolled up from Phases 1-4, not silently dropped again

- **Non-Python code blocks.** The sandbox stays Python-only, for the
  same reason `run_sandboxed()` itself is Python-only today. Future
  work: per-language sandbox runners plus a language picker in the UI.
- **Stronger isolation than same-VM `unshare`/`setrlimit`.** ~~Still
  deferred~~ — **superseded by Stage 5**: Firecracker microVMs are the
  stronger isolation this item asked for, chosen specifically because
  the sandbox becoming the *primary* interface (not just occasional
  inline chat verification) raised the bar past what same-VM
  `unshare`/`setrlimit` can honestly promise for a persistent,
  network-connected shell.
- **A portable local-capture client** (a student running a model on
  their own laptop, feeding the same central corpus). Still not
  built — the `source` field Phase 3 already designed for exactly this
  needs no further change here.
- ~~A hard mid-generation interrupt for `/sandbox/ask`~~ — **done,
  Stage 4** (above).
- ~~Server-side rate limiting~~ — **done, Stage 4** (above).
- ~~**Network access inside the sandbox.**~~ — **done, Stage 5**: the
  *existing* Python sandbox (Run/Ask) stays exactly as network-less as
  before; a real, isolated internet path now exists for the new
  Firecracker shell specifically, live-verified end-to-end (Stage A
  infra + isolation, Stage B the actual student-facing terminal).
- ~~**Giving the student their own raw interactive terminal.**~~ —
  **done, Stage 5**: built additively — the model-driven Ask flow is
  untouched, the terminal is a new, separate capability sitting
  alongside it, not a replacement.
- **Perfect "is it actually waiting for input" detection** (Stage 3).
  The quiet-period heuristic is real but imperfect — a script merely
  computing something slowly looks identical to one genuinely waiting
  on stdin. Documented as a known limitation, not solved by this
  stage.
- **`jailer`-based host-side hardening for the Firecracker VMM process**
  (Stage 5B). Firecracker currently runs directly as root — genuine
  guest-to-host isolation is unaffected (that's the KVM guest kernel
  boundary + Stage A's own iptables policy, both live-verified), but
  `jailer`'s own chroot/uid-drop/cgroups would add a further layer of
  protection against a hypothetical VMM-process-level compromise.
  Deliberately deferred rather than debugged blind alongside the rest
  of Stage 5B's own real, novel bugs — flagged as the next likely
  hardening step, not silently dropped.
