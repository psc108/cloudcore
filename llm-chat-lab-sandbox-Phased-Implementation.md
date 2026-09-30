# llm-chat — A Real Lab: Run the Advice, Keep What Works

Paul Scott | Direction agreed 2026-09-30

---

## Context

Linux Help answers are advice that nobody runs. F-174 and F-175 showed what
that costs:
- the model recommends packages that the lab can't install, or that may not exist;
- it has the student edit files that aren't there;
- it gives config that weakens security.

Today the only protection is a warning under each answer. The student's
Terminal can't test most advice either: it only sees `jammy main`, its
kernel lacks the netfilter/TUN/FUSE features that common tools need, and it
is a 1 vCPU / 256MB / 2GB VM.

Direct request: "we need to be able to install anything the llm might offer
as advice to install and then configure it. that way the student can safely
test the llm response and as a bonus we'll start collecting a decent corpus
of our own that is verified working response or not and what did/didn't
work in advice".

**Decisions (2026-09-30):**
- **Sizing is never tied to a machine.** Resources come from what the
  approved peers report at build time and from what the coordinator actually
  has at run time. No host names in templates or defaults; a single small host
  still works, with a smaller sandbox. ("someone coming behind us using this
  code will wonder why they can't run")
- **Every Linux Help answer is run automatically, step by step**, in a
  disposable microVM. It never runs in the student's own Terminal, which still
  runs only what the student clicks.
- **Answers verified in the lab are reused automatically** for grounding,
  labelled as lab-verified, not human-approved.

## What limits the lab today (measured 2026-09-30)

| Limit | Effect | Fix (stage) |
|---|---|---|
| Runtime apt sources: `jammy main` only | universe/multiverse packages (fail2ban, libpam-google-authenticator, docker.io, nodejs…) can't be installed; no `-updates`/`-security` | L2 |
| Guest kernel = Firecracker CI 6.1 config: no `NF_TABLES`, no xt `limit`/`recent`/`multiport`/`state`/`LOG`, no `TUN`, `WIREGUARD`, `FUSE`, `NFSD`, AppArmor; `MODULES` off | jammy's iptables (nft backend), ufw, fail2ban's ban actions, VPNs and FUSE tools fail even once installed | L1 |
| Terminal VM 1 vCPU / 256MB / 2GB scratch; per-run VM 256–512MB | databases, docker, JVMs and larger installs don't fit | L3 |
| Guest sshd is also the control channel (Terminal bridge, per-run executor) | advice that edits `sshd_config`/PAM and restarts sshd cuts off the session running it | L2 |

## Stages

| # | Stage | Status |
|---|---|---|
| L1 | **Guest kernel.** Build our own 6.1 guest kernel from Firecracker's CI config plus a fragment (nf_tables + expressions, the common xt matches/targets, NAT/masquerade, TUN, WireGuard, FUSE, NFSD, loop/dm), with a new `api/build-firecracker-kernel.sh` in a throwaway builder instance like the rootfs. Pinned artifact + sha256, TF + Ansible. Prove ufw, fail2ban (iptables and nft actions), nft, WireGuard and a FUSE mount in a guest | Not started |
| L2 | **Rootfs.** Full runtime apt sources (main, restricted, universe, multiverse; `-updates`, `-security`). A separate **control sshd**: its own port, config and host key, key-only, `UsePAM no`, used only by the Terminal bridge and the executor, so advice that reconfigures the normal sshd/PAM can't cut off the session. Rebuild the rootfs (F-171 procedure), re-pin the SHA | Not started |
| L3 | **Sizing from real resources.** Build time: the coordinator's flavor candidates run up to the largest flavor, and `/v1/peers/recommend` picks the best any approved peer can afford now (the mechanism already exists). Run time: Terminal and advice-run VMs size themselves from the coordinator's actual memory, cores and disk (reserve for its own services, divide by the allowed concurrent sessions, never claim every core), with floors below which a session is refused with a clear message rather than started half-sized | Not started |
| L4 | **Advice runner.** Turn an answer into steps: `bash` blocks are *run*; `text`/config blocks tied to a named path are *write/append file*; editor invocations (`nano`, `vim`) are replaced by those writes; interactive commands run with stdin closed and a timeout, and are recorded as *interactive*. Run the steps in a fresh microVM (internet-only egress, as today). After each step, check what the answer relied on: packages exist and installed, referenced files/commands/units exist, services `is-active`, and config validators where one exists (`sshd -t`, `nginx -t`, `visudo -c`, `apachectl configtest`, `named-checkconf`, `fail2ban-client -t`, `systemd-analyze verify`…). Classify every step | Not started |
| L5 | **Page.** Show the run under the answer as it progresses ("Lab run: step 3/7…"), then per-step results in plain words ("fail2ban: installs ✓", "/etc/fail2ban/jail.local: didn't exist — the answer edits it but never creates it"). F-175's notices become facts from the run (e.g. "package X does not exist in Ubuntu 22.04") instead of guesses | Not started |
| L6 | **Corpus.** Sentinel stores each run: question, answer, the extracted steps, per-step command/exit/output tail/classification, verdict. Student-initiated Terminal runs from Linux Help are recorded the same way (source `student`). A Sentinel view of runs, filterable by classification | Not started |
| L7 | **Reuse.** Answers verified in the lab (definition below) become `lab_verified` in `grounding_log`. `find_match()` returns them after human-approved answers, labelled "Verified in the lab (date)". Failures become *known-bad facts* ("libfoo does not exist in Ubuntu 22.04"; "X ships no /etc/x/y.conf") added to the prompt when a question matches, so the model stops repeating them. Verify live with repeat questions | Not started |

## Definitions

**Step classifications:** `ok`, `package_not_found`, `command_not_found`,
`file_missing` (the answer uses a path that isn't there and never creates
it), `service_failed`, `config_invalid` (a validator rejected it),
`step_failed` (other non-zero exit), `interactive` (needed input),
`timeout`, `not_runnable` (no step could be extracted).

**Lab-verified** (and eligible for automatic reuse): every extracted step
is `ok`, at least one step changed the system, nothing was skipped as
`interactive`, and every service the answer starts is active at the end.

**The limit, stated plainly:** "it ran" is not "it's good advice". F-174's
`pam_unix.so … md5` line would run cleanly. So a reused answer keeps any
F-175 risky-change notice it carries, and a later human rejection in Sentinel
always overrides `lab_verified`.

## Risks and constraints

- **Time.** The model generates at ~1 token/s, so the run starts only after
  the answer is complete and runs alongside nothing heavier than the next
  question. Most runs are apt plus config, seconds to a few minutes.
- **Egress.** Advice-run VMs reach only the internet, the same policy as
  today's per-run and Terminal VMs. No lab or host network.
- **Security of the runner.** It executes model output as root, but only
  inside a disposable, network-fenced microVM (the same boundary the coding
  panel's verification already relies on). It never touches the student's
  own Terminal.
- **Kernel build time.** It's a one-off per kernel version, done in a builder
  instance, never on a live path.

Methodology unchanged: build and verify live, log findings as F-NNN, tear
down after.
