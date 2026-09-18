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
| 1 | Sandboxed execution + honest display (Python-only, single turn) | Not started |
| 2 | Grounded fix loop | Not started |
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
