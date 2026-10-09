# llm-chat — Sandbox Extensions: Phased Implementation

> **Roadmap (2026-10-09):** open items are now tracked in `cloudcore-Roadmap.md` (CC-22, CC-41, CC-66, CC-67, CC-70, CC-98) and `sentinel-Roadmap.md` (SN-33). This document is kept as the record of how the work was done.

Paul Scott | Plan approved 2026-09-28

---

## Context

`llm-chat-interactive-sandbox-Phased-Implementation.md` (Phase 4,
Stages 1-9, all verified live 2026-09-18 to 2026-09-22) ends with an
"Explicitly out of scope" list. Four items on it are still open. Per
direct request on 2026-09-28, all four are now in scope:

1. `jailer`-based host-side hardening for the Firecracker VMM process.
2. Exact "is it waiting for input" detection, replacing Stage 3's
   quiet-period heuristic where possible.
3. Non-Python languages in Run/Ask.
4. A portable local-capture client feeding the central corpus.

Stage numbering continues from the Phase 4 document (Stage 10
onwards) so the sandbox has one continuous stage history.

**Scope**: `examples/llm-chat` coordinator, its Ansible mirror
(`ansible/examples/templates/llm-chat/`), `api/build-firecracker-rootfs.sh`,
and — for Stage 13 only — `api/llm_examples_*` plus a new standalone
client under `scripts/`. `examples/distributed-llm` stays untouched,
as in every prior phase.

| # | Stage | Status |
|---|---|---|
| 10 | Firecracker under `jailer` + a shared microVM launcher module | Done — verified live 2026-09-28 |
| 11 | Exact input-wait detection via `/proc/<pid>/syscall` | Done — verified live 2026-09-28 (11.3 guest helper moves into Stage 12's rootfs rebuild) |
| 12 | Bash, JavaScript (Node), C/C++, Go in Run/Ask — Firecracker per run | Done — verified live 2026-09-28 |
| 13 | Local-capture client with per-student tokens and server-side re-verification | Done — verified live 2026-09-28; submit from a second LAN machine not yet tried (see 13.4 note) |

---

## Decisions taken 2026-09-28 (direct answers, not assumed)

- **Languages**: Bash, JavaScript (Node), C/C++, Go — all four.
- **Where non-Python code runs**: a fresh Firecracker microVM per Run,
  not the coordinator's own `unshare`/`setrlimit` runner. Stronger
  isolation was chosen over speed/RAM; the design below is shaped
  around making that affordable on this coordinator, not around
  revisiting it.
- **Local-client trust**: the client submits prompt + code only; the
  coordinator re-executes it in its own sandbox and records *its own*
  result. Client-reported execution output is never stored as
  evidence. Per-student, revocable tokens.

Python's existing coordinator-side runner (`run_sandboxed` /
`run_sandboxed_interactive`) is **kept as-is**: it is proven across
Stages 1-9, is fast, and costs no extra RAM. Moving Python into
Firecracker too is a possible later unification, not part of this
phase.

---

## Stage ordering — why this order

Stage 10 comes first because Stage 12 launches microVMs from a second
caller (`verify_proxy.py`, not just `sandbox_terminal.py`). Extracting
one jailed launcher module first means Stage 12 is built on the
hardened path from day one, instead of adding a second unjailed
launcher and hardening it afterwards. Stage 11 is independent and
small, and its guest-side helper is reused by Stage 12's interactive
support. Stage 13 depends on Stage 12, because re-verifying a
client's non-Python code needs the multi-language runner to exist.

---

## Stage 10 — Firecracker under `jailer`, one shared launcher

### Current state (verified by reading the code, 2026-09-28)

- `sandbox_terminal.py:228` starts `/opt/firecracker/firecracker`
  directly as root.
- Cloud-init already downloads and checksum-verifies `jailer`
  (`/opt/firecracker/jailer`), creates the `fcrunner` system user,
  and creates `/srv/jailer`. None of these are used yet.
- Every session copies the full 768MB golden rootfs
  (`shutil.copyfile`) before boot.

### Design

- **New module `files/microvm.py`** (mirrored to Ansible), holding the
  `MicroVM` class moved out of `sandbox_terminal.py`. Both
  `sandbox_terminal.py` and (Stage 12) `verify_proxy.py` import it.
  Deployed next to both services under `/opt/llama.cpp/`.
- **Launch via jailer**:
  `jailer --id <session> --exec-file /opt/firecracker/firecracker
  --uid <fcrunner> --gid <fcrunner> --chroot-base-dir /srv/jailer
  --cgroup-version 2 --cgroup memory.max=<n> --cgroup cpu.max=<n>
  -- --api-sock /run/api.sock`. jailer execs into firecracker, so the
  `Popen` PID stays the VMM PID and teardown logic is unchanged.
- **Files inside the chroot** (`/srv/jailer/firecracker/<id>/root/`):
  the kernel is hard-linked (`/opt/firecracker` and `/srv/jailer`
  must share a filesystem — checked at boot, falling back to a copy
  with a stderr warning if not). The API socket path becomes
  `<chroot>/run/api.sock` on the host side.
- **Read-only shared rootfs + per-session scratch drive**, replacing
  the 768MB copy. The golden rootfs is attached `is_read_only: true`
  (hard-linked into each chroot). A small sparse ext4 scratch image
  (default 512MB, created with `truncate` + `mkfs.ext4 -q`) is the
  second drive. The guest mounts an overlayfs of the two at boot. This
  cuts per-session disk I/O from ~768MB to a few MB, and is what makes
  Stage 12's per-run VMs viable. **This changes the golden rootfs**
  (an overlay-root init step in `build-firecracker-rootfs.sh`), so
  the rootfs SHA in `variables.tf` changes.
- **TAP device**: still created on the host by the root-owned
  service before jailer starts. jailer runs in the host netns (no
  `--netns`), so the existing `fcbr0` + iptables egress policy is
  unchanged.
- **Teardown** additionally removes the chroot tree and the
  `/sys/fs/cgroup/firecracker/<id>` cgroup.
- The service itself still runs as root (it has to create TAPs and
  run jailer). jailer is what drops the VMM to `fcrunner`.

### Tasks

| # | Task | Verify |
|---|---|---|
| 10.1 | Extract `MicroVM` to `files/microvm.py`; `sandbox_terminal.py` imports it; no behaviour change | Terminal session boots and works exactly as before |
| 10.2 | Launch through jailer with uid/gid `fcrunner` and cgroup v2 limits | `ps -o user= -p <vmm pid>` shows `fcrunner`; `/proc/<pid>/root` is the chroot; cgroup `memory.max` set |
| 10.3 | Read-only golden rootfs + scratch drive + overlay-root in the guest; rebuild the rootfs, update its SHA | Two concurrent sessions: each sees its own writes, the golden image hash is unchanged afterwards |
| 10.4 | Teardown removes chroot + cgroup | After 5 open/close cycles, `/srv/jailer/firecracker/` and the cgroup dir are empty |
| 10.5 | Regression: Stage 5C LB path, Stage 6 preview ports, Stage 8 stuck-terminal recovery, `apt install` in guest | All still work through the LB, live |
| 10.6 | Mirror everything to Ansible; update module docstrings and cloud-init comments that say "not run through jailer" | `diff -q` on both mirrors is clean |

### Failure-mode tests

| Test | Expected |
|---|---|
| Guest runs a memory hog past its 256MB | Guest OOM-kills inside the VM; the VMM stays within its cgroup; the host is unaffected |
| `kill -9` the VMM mid-session | The WS client gets an error frame; teardown still removes TAP, chroot and cgroup |
| Restart `sandbox-terminal.service` with sessions open | No orphaned chroots/cgroups/TAPs remain after restart (add a startup sweep if they do) |

### Verified live, 2026-09-28

Against a fresh llm-chat build (coordinator `standard.large`, one RPC
worker on the paired peer), running the rebuilt rootfs
(`93d4bd9d…`), using real Terminal sessions opened through
`sandbox_terminal.py`'s own WebSocket port and inspected from the host.

| Check | Result |
|---|---|
| Golden rootfs + kernel ownership | `root:root 0644` |
| VMM user | `fcrunner` for both concurrent sessions |
| VMM root | Sees only its jail: `dev firecracker firecracker.pid rootfs.ext4 run scratch.ext4 vmlinux`; own mount namespace |
| cgroup | `/fcsandbox/term-<id>`, `memory.max` = 402653184 (256MB guest + 128MB overhead) |
| Overlay isolation | Session A's write to `/etc` invisible to session B; `/` is overlay, `/rom` is `ro` |
| Golden image | Unwritable from the guest; SHA-256 identical before and after |
| `apt-get install` in guest | Works (onto the scratch drive) |
| Teardown | No VMM, chroot, cgroup, TAP or key dir left after close |
| 10.5: Terminal + preview through the real LB | Connected via `:8620/terminal`; a `python3 -m http.server 41001` in the guest served through the LB's `:41001` |

Failure-mode matrix:

| Test | Expected | Actual |
|---|---|---|
| Guest allocates 600MB (256MB guest) | Guest-side OOM only | VMM survived, session still usable, host `MemAvailable` 1267MB → 1255MB |
| `kill -9` the VMM mid-session | Error frame + full cleanup | **Failed first time**: session hung (see below). After the fix: error frame within one poll, nothing left behind |
| Restart `sandbox-terminal.service` with 2 sessions open | No orphans | `sweep_orphans()` logged "Removed 3 microVM(s) orphaned by a previous run"; nothing left; new session works |

**Found live, fixed (F-157):**
- **A dead VMM hung the session instead of ending it.** SSH over TCP
  to a guest whose VMM has died never errors, because nothing is left
  to send a RST. The session would have sat there until the 15-minute
  idle timeout. This predates Stage 10 (an unjailed VMM crash behaved
  the same way); the new failure matrix is what exposed it.
  `MicroVM.alive()` is now checked on every pass of the session loop.
- **Orphans survive a service restart.** jailer moves each VMM into
  its own cgroup, outside the service's, so systemd no longer kills
  them on stop/restart. This was expected in the design, and
  `sweep_orphans()` handles it.
- **The golden rootfs and kernel were `fcrunner`-owned** (from the
  Stage 5 cloud-init). Once the VMM runs as `fcrunner` behind a hard
  link, that would have let a compromised VMM rewrite the shared
  golden image for every later session. Found by reading the code
  before building; now `root:root 0644`.

Not covered: the Stage 8 "stuck terminal" recovery path was not
re-exercised separately; its code is unchanged. Its original trigger,
a student partitioning or formatting the running root, no longer
reaches the root disk: `/sys/block/vda/ro` is `1` in the guest, and
`sudo dd of=/dev/vda` copies 0 bytes (checked live). A student can
still wreck their own session by formatting `/dev/vdb`, the scratch
drive under their overlay, so the recovery path still matters.

---

## Stage 11 — Exact input-wait detection

### Current state

`run_sandboxed_interactive()` treats 3s of silence
(`INTERACTIVE_QUIET_S`) while the process is alive as "waiting for
input". A slow computation looks the same as a blocked `input()`.
That is the known limitation recorded in the Phase 4 doc.

### Design

`verify-proxy` runs as root, so it can read `/proc/<pid>/syscall` for
every process in the sandboxed tree. The field format is
`<nr> <arg0> ...`. On x86_64, `0 0x0 ...` means *blocked in `read()`
on fd 0*. Confirming that fd 0 is the stdin pipe we hold (compare the
`/proc/<pid>/fd/0` link target to our pipe's inode) closes the "fd 0
redirected elsewhere" gap.

- **The PID tree**: `unshare --pid --fork` puts the real interpreter
  one or two levels below `proc.pid`. Walk `/proc/*/stat` PPIDs from
  `proc.pid` to find all descendants on each poll.
- **Decision rule**, checked every 0.5s poll:
  - Any descendant is blocked in `read(0)` on our pipe → waiting for
    input, **immediately** (no 3s wait). Tagged `detection: "exact"`.
  - Otherwise, if the process is still in a running state (`R` in
    `/proc/<pid>/stat`) → computing. **Never** treated as waiting,
    however long the silence.
  - Otherwise, if it is sleeping but in something other than
    `read(0)` (epoll, futex, nanosleep) → fall back to the existing
    quiet-period heuristic. Tagged `detection: "heuristic"`. This
    covers runtimes that read stdin through an event loop (Node's
    libuv), where syscall inspection cannot tell stdin apart from
    other waits.
- The `detection` tag goes into the transcript marker and the Phase 3
  capture, so the corpus records which cases were exact.
- **A small guest-side helper**, `/usr/local/bin/stdin-wait-check
  <pid>`, is baked into the golden rootfs. It applies the same rule
  inside a microVM, so Stage 12's interactive runs use it over SSH.

### Tasks

| # | Task | Verify |
|---|---|---|
| 11.1 | `_stdin_wait_state(root_pid, pipe_inode)` → `exact` / `computing` / `unknown` | Unit tests against real child processes: `input()`, a busy loop, `time.sleep`, stdin redirected from `/dev/null` |
| 11.2 | Wire it into `run_sandboxed_interactive()`; keep `INTERACTIVE_QUIET_S` only for the `unknown` case | A script that computes silently for 10s and then calls `input()` is **not** handed input during the 10s, and is handed input within ~0.5s once it blocks |
| 11.3 | Guest helper `stdin-wait-check` in the rootfs build | Same four cases, run inside a microVM |
| 11.4 | Update the Phase 4 doc's out-of-scope entry: resolved for blocking-read runtimes, heuristic remains for event-loop runtimes | — |

### Verified live, 2026-09-28

`run_sandboxed_interactive()` against the real `unshare` sandbox on the
coordinator (task 11.2):

| Case | Old heuristic | Actual now |
|---|---|---|
| Plain `input()` | ~3s | Handed over at 0.57s, `exact` |
| 8s silent busy-loop, then `input()` | Wrong input at 3s | Handed over at 8.23s, `exact` |
| `time.sleep(8)`, then `input()` | Wrong input at 3s | **Failed first time** (3.06s, `heuristic`); after the fix 8.13s, `exact` |
| Two sequential `input()`s | 2 × ~3s | Both `exact`, correct sum |

End to end, through the LB and the real model: an Ask of "write a
script that reads a number with `input()` and prints its square"
produced a script. The sandbox recorded `>>> INPUT PROVIDED (exact): '4'`
and printed `The square of 4 is 16.` (exit 0).

**Found live, fixed (F-158):** jammy's Python 3.10 implements
`time.sleep()` as `pselect6(0, …)`, not `clock_nanosleep` (the dev host's
3.12 uses the latter, so it passed locally). Zero-fd
`select`/`pselect6`/`poll`/`ppoll` now count as timer sleeps.

**Deferred to Stage 12 (11.3):** the guest-side `stdin-wait-check`
helper only matters for per-run microVMs. It ships in Stage 12's rootfs
rebuild instead of forcing a separate rebuild now.

---

## Stage 12 — Bash, Node, C/C++, Go in Run/Ask, Firecracker per run

### Design

- **Toolchains in the golden rootfs**: `bash` (already present),
  `nodejs`, `gcc`/`g++`/`libc6-dev`, `golang-go` — all from the apt
  mirror in `build-firecracker-rootfs.sh`. Go roughly doubles the
  image. With Stage 10's read-only shared rootfs that costs disk once,
  not per session. The rootfs SHA is updated again.
- **Per-run microVM**, via Stage 10's `microvm.py`:
  boot → SSH (existing ephemeral-key-via-MMDS mechanism) → copy source
  → compile if needed → run → collect stdout/stderr/exit code →
  teardown. **No outbound network**: per-run VMs never join `fcbr0`
  (the Terminal's internet-capable bridge), so model-generated code
  keeps the same network-less guarantee the Python runner has today.
  - *Transport without a NIC*: Firecracker's vsock device plus a tiny
    guest agent is the clean answer, but it is new code in the guest.
    **Proposed instead**: a TAP on a separate, isolated bridge
    (`fcrun0`) with **no** forward/NAT rules at all. SSH from the host
    works; the guest can reach nothing else. This reuses the proven
    SSH path. The iptables policy for `fcrun0` is `-P DROP` on FORWARD
    for that bridge, verified live the same way Stage A verified
    `fcbr0`.
- **Language detection**: `CODE_BLOCK_RE` is generalised to capture
  the fence tag (`bash`/`sh`, `js`/`javascript`/`node`, `c`, `cpp`/
  `c++`, `go`). Untagged blocks keep today's Python heuristic. The UI
  gets a language picker for Run; Ask infers it from the student's
  selection and passes it to the system prompt.
- **Compile step**: C/C++/Go compile inside the VM with a separate
  compile timeout. A compile failure is a real, first-class result
  (`phase: "compile"`, compiler stderr verbatim), and it feeds the
  existing fix loop exactly like a runtime error.
- **Resource budget — the real constraint.** The coordinator is 4 vCPU
  / ~3.8GB, and llama-server already uses ~87% of RAM under load
  (recorded in Stage 9). Controls:
  - `run_vm_mem_mib` (default 256) and `run_vm_max_concurrent`
    (default **1**), a separate cap from the Terminal's own.
    Additional Runs queue with a visible position (the same pattern
    as F-143), rather than being rejected.
  - Go compiles are the heaviest case. `run_vm_mem_mib_go` (default
    512) applies to Go only.
  - A pre-flight check reads `MemAvailable` and refuses to boot below
    a floor (default 400MB), with an honest "coordinator is under
    memory pressure" message instead of an OOM.
  - **Measured, not assumed**: boot-to-result latency and peak host
    RAM for each language are recorded live in 12.6. If they are
    unacceptable, that is a finding to bring back, not something to
    quietly tune around.
- **Interactive (`stdin`) support** for the new languages uses the
  same model-driven exchange loop as Stage 3, over the SSH channel,
  with Stage 11's guest helper for detection.
- **System prompts**: the sandbox prompt stops saying "Python only".
  It gains per-language guidance (C/C++: a complete `main`; Go:
  `package main`; Node: no browser APIs).
- **Capture**: Phase 3 rows gain a `language` column (migration via
  `_add_column_if_missing`), and the review page shows it.

### Tasks

| # | Task | Verify |
|---|---|---|
| 12.1 | Rootfs: add toolchains + Stage 11 helper; rebuild; update SHA | Inside a VM: `node -v`, `gcc --version`, `g++ --version`, `go version` |
| 12.2 | Isolated `fcrun0` bridge with no forwarding; per-run TAP | From a per-run VM: `curl` to the internet, the coordinator's other ports, and `fcbr0` guests all fail; host→guest SSH works |
| 12.3 | `run_in_microvm(code, language)` in verify_proxy using `microvm.py` | Hello-world + a deliberate runtime error for each of the 4 languages; exit codes and stderr verbatim |
| 12.4 | Compile phase for C/C++/Go, and compile errors fed into the fix loop | A deliberate compile error is fixed by the model and re-verified, for each compiled language |
| 12.5 | Language-aware extraction, UI picker, system-prompt changes, capture `language` column | Ask in each language produces a verified block with the right language label; the corpus row shows `language` |
| 12.6 | Resource budget: concurrency cap + queue, memory floor, per-language measurements | Two simultaneous Runs: the second queues with a visible position; recorded latency and peak RAM table for each language |
| 12.7 | Interactive stdin for the new languages | A C `scanf` program and a Bash `read` program are both driven by the model through a real exchange |
| 12.8 | Ansible mirror + `diff -q` clean | — |

### Failure-mode tests

| Test | Expected |
|---|---|
| Infinite loop in C | Killed at the run timeout; VM torn down; the next Run works |
| Fork bomb in Bash | Contained inside the VM (the guest's own limits plus the VMM cgroup); host unaffected |
| Go program allocating 2GB | Guest OOM; honest error returned; host `MemAvailable` never drops below the floor |
| Run requested while llama-server is mid-generation | Either runs within budget or queues/refuses honestly. Never OOM-kills llama-server |

### Verified live, 2026-09-28

On a clean rebuild of llm-chat, with the toolchain rootfs (`4396aea0…`,
2048MB image, about 1GB used). It carries Node 12.22 (jammy's own
nodejs, universe), gcc/g++ 11, Go 1.18, and the Stage 11 guest helper,
and its runtime `sources.list` is back to main-only.

| Check | Result |
|---|---|
| 12.1 Toolchains in guest | `node`, `gcc`, `g++`, `go`, `stdin-wait-check` all present |
| 12.2 `fcrun0` isolation, from inside a per-run VM | Internet, the coordinator's own address, the `fcrun0` gateway, `fcbr0` and the CloudCore host all unreachable |
| 12.3 Hello world × 5 languages | All exit 0 with the right output |
| 12.3 Runtime errors | Reported verbatim: Bash exit 3; Node stack trace; C `SIGSEGV` (139); C++ `std::runtime_error` → `SIGABRT` (134); Go panic (2) |
| 12.4 Compile errors | C, C++ and Go come back as `phase=compile` with the compiler's own text; the result block says "compilation failed" |
| 12.5 Through the real LB | Page has the language picker; all four new editor modes served; C Run, C compile error, unknown language (400), `/sandbox/run-status`, and an unchanged Python Run all correct |
| 12.6 Queue | Second concurrent Run shows `queued, position 1`, waits 10.75s, then runs |
| 12.7 Interactive | C `scanf` × 2 → `exact, exact`, `sum=10`; Bash `read -p` → `exact`, `hi 5` |
| Stage 10 regression on this build | 24/24 checks + 8/8 failure matrix |
| 12.4 Real model, through the LB | Ask with language C on code missing a `;`: the model's fix was compiled and run **as C** in a per-run VM (`15`, exit 0). It was right first time, so the "model's own code fails to compile → fix round" path was exercised with scripted compile failures (above), not by a real model answer |
| `verify-proxy` restarted mid-Run | Startup sweep logged "removed 1 per-run microVM(s) orphaned"; no jail, VMM, TAP or cgroup left |
| Corpus `language` column | Migration + route verified on a copy of the real DB (27 existing rows → `python`; `c`/`go` stored; unknown → `other`), then live after the API restart: a real model Ask in Go was captured as `language = go`. The one C row captured *before* the restart had been stored as `python` and was corrected by hand (see F-160) |

Measured cost per non-Python Run (task 12.6), idle coordinator:

| Language | Boot | Compile | Run |
|---|---|---|---|
| Bash | 5.98s | — | 0.05s |
| JavaScript (Node) | 6.95s | — | 0.81s |
| C | 7.79s | 0.36s | 0.05s |
| C++ | 6.17s | 0.36s | 0.05s |
| Go | 6.33s | 1.56s | 0.05s |

Boot is the whole cost. From the guest journal: the kernel reaches
`overlay-init` at 2.1s, sshd is up at 4.2s, and the rest is host-side
jail/scratch setup plus the SSH handshake. With the coordinator busy
(llama-server loading its model right after cloud-init), the same
boots took 11.7–16.2s. That is the price of the per-run microVM choice
on this 4-vCPU coordinator, recorded here rather than tuned away.

Failure-mode matrix:

| Test | Expected | Actual |
|---|---|---|
| Infinite loop in C | Killed at the run limit, next Run works | `SIGXCPU` at 15.3s, named in the output ("exceeded the 15s CPU-time limit") |
| Fork bomb in Bash | Contained in the VM | `ulimit -u 128` held it; the script's own `echo` after it still ran; host memory unchanged |
| Go allocating 2GB (512MB guest) | Guest-side failure | Go runtime out-of-memory panic, exit 2; host `MemAvailable` 2726 → 2730MB |
| Everything above, with llama-server running | Never OOM-kills llama-server | llama-server still active; nothing left behind |

**Found live or locally, fixed (F-159):**
- **A signal death showed as `exit code -1` with empty stderr** (C
  segfault). sshd reports it as a signal, which paramiko surfaces only
  as -1. The program now runs as a child of a small shell that names
  the signal.
- **`ulimit -t` sets soft = hard,** so a CPU overrun was an anonymous
  `SIGKILL`. The soft limit is now 1s under the hard one, so it's
  `SIGXCPU` and the message can say which limit was hit.
- **The wrapper failed open.** A failed `ulimit` chain followed by `;`
  still ran the program, with no CPU limit (found locally, before
  shipping, when a stray test process spun forever). The limits now
  have to apply or the program doesn't run (exit 125, said so).

**Known limitations, not fixed:** Node is jammy's 12.22 (end-of-life;
no `??=` and friends); Go is 1.18. Both are what the pinned Ubuntu base
ships. Newer versions would mean pulling from outside the apt mirror.

---

## Stage 13 — Local-capture client

### Design

- **Per-student tokens.** A new `llm_client_tokens` table (id,
  label/student name, SHA-256 of the token, created_at, revoked_at,
  last_used_at). The token is shown once at creation; only its hash is
  stored. Admin-only routes to create, list and revoke, plus a small
  Sentinel/Dashboard panel. The shared `CLOUDCORE_API_TOKEN` is
  never given to a client.
- **New route `POST /v1/llm-chat/client-submissions`**, on the
  existing `examples_listener` bind (port 8083), added to
  `EXAMPLES_REACHABLE_ENDPOINTS`. It accepts `prompt`, `code`,
  `language`, `model_filename` and an optional free-text `client_note`.
  **Execution fields sent by the client are rejected, not ignored**,
  so a client can't believe they were stored.
- **Server-side re-verification.** The API host has no sandbox, so it
  forwards the submission to the coordinator's `verify-proxy` as a new
  internal `/sandbox/reverify` action, authenticated with the existing
  shared token over the private network. The coordinator runs the
  code with Stage 12's runner and captures the result through the
  **existing** `capture_example()` path, with `source="local-client"`
  and `client_token_id` set. Stored evidence is therefore always the
  coordinator's own execution.
  - If no llm-chat deployment is registered/reachable, the submission
    is stored as `status="pending_verification"` and retried by a
    scheduled job. It is never stored as verified.
- **Rate limiting** per token (default 20 submissions/hour), on the
  same pattern as Stage 4.
- **Reachability — a real constraint to confirm, not assume.** Port
  8083 on `192.168.100.1` is reachable from lab guests and peers over
  WireGuard, but **not** from an arbitrary student laptop. Initial
  scope: the client works from any machine that can reach the
  CloudCore host's LAN address on 8083. The listener binds `0.0.0.0`
  today, so this needs a host firewall rule scoped to the lab/LAN
  CIDR, recorded explicitly. Anything wider (internet exposure, TLS
  termination) is out of scope for this stage, and the client refuses
  plain HTTP to non-private addresses.
- **The client** (`scripts/llm-capture-client/`): a single-file Python
  CLI (`httpx`, `argparse`, Python 3.9+). Two modes:
  - `submit --prompt ... --file code.c --language c`, for manual use.
  - `watch --ollama|--llama-server <url>`, which wraps a local model
    endpoint as a thin proxy, extracts fenced code from each response
    and submits it. Local model runtimes are the student's own; the
    client does not ship one.
  - Token taken from `LLM_CAPTURE_TOKEN` or `~/.config/llm-capture/token`
    (mode 0600). Never accepted as a CLI argument, so it stays out of
    shell history.
  - README with Purpose, Prerequisites, Usage and a Configuration
    table.

### Tasks

| # | Task | Verify |
|---|---|---|
| 13.1 | Token table, admin create/list/revoke routes, UI panel | Create shows the token once; revoke → the next submission gets 401 |
| 13.2 | `client-submissions` route: validation, rejecting execution fields, rate limit | A payload with `exec_stdout` gets 400; the 21st submission in an hour gets 429 |
| 13.3 | `/sandbox/reverify` on verify-proxy + forwarding from the API; the pending path when the coordinator is absent | A submitted C file appears in the corpus with `source=local-client` and the coordinator's own stdout; with llm-chat destroyed it goes pending, then is verified after a rebuild |
| 13.4 | Host firewall rule for 8083 scoped to the LAN CIDR | Reachable from the LAN; still reachable from guests/peers |
| 13.5 | Client CLI (`submit` + `watch`) + README | End to end from a second machine on the LAN against a real local model |
| 13.6 | Review page: `local-client` filter, token label shown | — |

---

### Built as designed, with one change

The API does the re-verification call and writes the corpus row itself,
rather than the coordinator POSTing its own capture as in the plan. The
coordinator's `/sandbox/reverify` only returns the result. That keeps the
link between a submission and its corpus row in one process, and the
result still only ever comes from the coordinator.

### Verified live, 2026-09-28

Server side, on a copy of the real DB (21 checks, all pass):
- Tokens: only the SHA-256 is stored, the list never returns secrets,
  and create/revoke are admin-only.
- The admin token can't submit.
- Validation: execution fields are rejected by name (`exec_stdout`,
  `passed`), missing fields or an unknown language → 400, oversize → 413.
- The 20/hour limit returns 429.
- Isolation: one student's token can't read another's submission, and a
  revoked token → 401.
- Worker: no coordinator → stays `pending` with the reason; coordinator
  re-run → `verified` with the coordinator's result, called with the
  shared token; coordinator 4xx → `rejected`; older than 24h → `expired`.

Live, against the running coordinator and the restarted API:

| Check | Result |
|---|---|
| `/sandbox/reverify` without, or with a wrong, token, direct and through the LB | 401 / 401 |
| Admin token route on the capture port 8083 | 403 (port gate), even with the admin token |
| `submit --wait`, C file | `verified`; re-run in a per-run microVM, the coordinator's own `row 1..3`, exit 0 |
| `submit --wait`, Python file | `verified`; `45` |
| `--dry-run` | Shows the payload, sends nothing |
| Plain HTTP to a public address | Refused, exit 2 |
| Bad token | Refused, exit 3 |
| `watch` against the real local Ollama (`qwen2.5-coder:1.5b`), non-streaming and streaming | Both chat responses passed through intact (JSON; 27 SSE chunks to `[DONE]`); both answers captured and `verified` |
| Dashboard | Capture Tokens card and source filter served by the restarted API. **Not checked visually in a browser** |

The two test tokens were revoked afterwards and their four test rows set
to `hidden`.

**13.4 — corrected after running it: ufw is not enabled on this host.**
I had read `systemctl is-active ufw` (active) as "the firewall is on". It
only means the unit ran at boot. `ufw status` says `inactive`, so
`api/setup-capture-firewall.sh --lan-cidr 192.168.1.0/24` (run
2026-09-28) stored its rule but enforces nothing. Port 8083 was, and
is, reachable from every network the host is on, not just the lab LAN.
So LAN laptops need no change to use the client. The listener's own
protection is the port gate (only the capture/registration routes;
everything else 403) plus per-route token auth. Its remaining weakness
is plain HTTP. Enabling ufw would default-deny *all* incoming traffic
(peer listener, WireGuard, LB ports, possibly SSH), so it is a separate,
deliberate host-hardening change and is not done here. The script now
warns when ufw is inactive. A submit from a second LAN machine is still
untested.

## Methodology — unchanged from Phases 1-4

Build each stage for real on a rebuilt llm-chat environment. Run its
own tests and failure matrix live, and record the actual results next
to the expected ones. Log every non-obvious finding as F-NNN in
`haFullStack-Findings-Log.md` (re-running `sentinel ingest-kb`).
Commit code and docs separately. Re-run `sentinel index-codebase`
after source commits. Tear down when done.

## Explicitly out of scope for this phase

- Moving Python's runner into Firecracker (a possible later
  unification; see Decisions).
- Exposing the capture listener beyond the LAN, or adding TLS to it.
- Languages beyond the four chosen.
- A vsock guest agent. It is only revisited if 12.2's isolated-bridge
  approach shows a real problem.
