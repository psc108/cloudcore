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

## Explicitly out of scope — rolled up from Phases 1-3, not silently dropped again

- **Non-Python code blocks.** The sandbox stays Python-only, for the
  same reason `run_sandboxed()` itself is Python-only today. Future
  work: per-language sandbox runners plus a language picker in the UI.
- **Stronger isolation than same-VM `unshare`/`setrlimit`.** The
  sandbox becoming the *primary*, higher-traffic interface — rather
  than occasional inline chat verification — makes the case for
  stronger isolation (a dedicated throwaway VM/container per run,
  gVisor/nsjail, a tighter seccomp profile) somewhat stronger than it
  was in Phase 1. Still deferred here, since the existing mechanism is
  already live-proven and low-latency — flagged as the most likely
  next real hardening step once this ships, not silently re-deferred
  without comment.
- **A portable local-capture client** (a student running a model on
  their own laptop, feeding the same central corpus). Still not
  built — the `source` field Phase 3 already designed for exactly this
  needs no further change here.
- **A hard mid-generation interrupt** for `/sandbox/ask`. Not built for
  the chat's own streaming today either; real future work once Stage 1
  proves the core loop.
- **Server-side rate limiting.** Stage 1's mitigation (disable the
  button while a request is in flight) is real but thin; per-IP
  throttling is future hardening if actual abuse is observed.
- **Network access inside the sandbox.** Confirmed live (Stage 3) that
  `unshare --net` gives zero connectivity at all, not even loopback —
  a real, strong existing safety property, deliberately left untouched
  rather than reconsidered as part of broadening interactivity.
- **Giving the student their own raw interactive terminal.** The other
  branch of Stage 3's own "who drives it" decision — not chosen; the
  model remains the one operating the sandbox as a tool, every real
  command and result still shown to the student, not a black box.
- **Perfect "is it actually waiting for input" detection** (Stage 3).
  The quiet-period heuristic is real but imperfect — a script merely
  computing something slowly looks identical to one genuinely waiting
  on stdin. Documented as a known limitation, not solved by this
  stage.
