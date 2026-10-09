# llm-chat: full VMs for proving answers and for the student — Phased Implementation

> **Roadmap (2026-10-09):** open items are now tracked in `cloudcore-Roadmap.md` (CC-52, CC-64, CC-65, CC-69, CC-98). This document is kept as the record of how the work was done.

**Status:** F1–F5 done (F5 2026-10-03: llm-chat runs proofs on full VMs). F8 done as L20 (2026-10-04). Open: F6–F7 (cloudcore-Roadmap.md CC-64, CC-65). **Owner:** Paul Scott.

## Context

The held-out evaluation (F-200) proved 3 of 40 unseen Linux Help answers. Most failures were the lab's, not the model's. Part of that is the Firecracker microVM itself. To make it pass for an Ubuntu server, the lab carries a layer of workarounds, each a special case fitted to answers already seen:
- a fake `modprobe`;
- `/dev/sdb` aliases;
- a device-mapper snapshot root;
- boot-time service masking.

On top of the workarounds, some kinds of answer can't be tried at all: GRUB, reboot-sensitive changes, loadable modules, grown disks, snaps.

Direct requests (2026-10-02):
- "would we be better, during the proving, that we use a full vm not a micro vm? … we'd still want it as a throwaway"
- "having a full vm running for the student. on entering the linux chat start a full vm in the background … perhaps we can keep using a microvm for small question but let the student know we need to use a full vm for a specific question"

## What changes, and what doesn't

- **Proving** (the lab run behind every answer) moves to throwaway **full VMs**: CloudCore instances from the Ubuntu 22.04 cloud image, a fresh target and prober per run, destroyed afterwards. The lab logic (steps, repairs, checks, another way) is unchanged: it already asks for "a machine" through one interface.
- **The student** gets a personal **full VM**, started in the background when they open Linux Help. The microVM Terminal stays as the instant default for small questions. When a question needs a real machine, the page says so and offers the full VM (ready, or "starting — about 2 minutes").
- **Proving and experimenting stay separate.** The student's machine is for trying things; it accumulates state. Proofs always run on fresh throwaways, so a verdict never depends on what a student did earlier.
- **Kept as they are:** internet-only egress for every lab machine, a control channel the answer can't break, sizing from free resources across peers (never a named host), and the microVM for the Terminal's instant start.

## Not fixed by this (needs the setup stage and other F-200 work regardless)

- Most held-out failures: presumed state, wrong goal checks, placeholder styles, harness bugs.
- Real hardware (SMART, GPU, fan sensors) can't be tested on any VM.
- Let's Encrypt needs a public domain. A local ACME test CA on the prober could make certificate answers testable (separate stage).

## Stages

| # | Stage | Status |
|---|---|---|
| F1 | **Lab-VM broker on CloudCore (security first).** The coordinator is itself a lab VM that runs model-written commands, so it must never hold CloudCore's master API token. A narrow endpoint set on the hub (`/v1/lab-vms`: create from a fixed template, list its own, destroy its own), with its own scoped token stored in SSM-style settings, not in files. Fixed template: the Ubuntu 22.04 image, a lab-only VPC/subnet, a security group, cloud-init with the run's control key and harness prerequisites. Quotas: max concurrent lab VMs, a hard TTL, and an idle reaper on the hub, so a crashed coordinator can't leak VMs. Placement: `recommend-placement` from free resources across peers. | Done — `/v1/lab-vms` broker with its own token; proof VM boot to SSH in 18 s; quotas, ownership and idle reaping verified (F-202). **No model-written commands on these VMs until F2 isolates them.** |
| F2 | **Lab network (isolated bridge).** Measured 2026-10-02: security groups can't isolate lab VMs. The kernel's bridge-netfilter isn't loaded, so VM-to-VM traffic on `ccbr0` never reaches iptables; SGs only filter routed egress; traffic to the host's own services goes through INPUT, which SGs don't touch; VPCs don't separate anything (F-201). So lab VMs get their own bridge on each host, set up by a root script (as `setup-network.sh` sets up `ccbr0`): **`cclab0`**, 10.250.0.0/24, with its own dnsmasq (DHCP, and DNS forwarded to public resolvers) and NAT to the internet. nftables, scoped to that bridge only (no global bridge-netfilter): (1) lab→internet allowed, lab→any private, link-local, CGNAT or loopback range dropped; (2) lab→host: only DHCP and DNS (the API, peer and examples ports, the package repo, Loki and Sentinel all closed); (3) into the lab only SSH from the configured lab controllers (the coordinator), plus replies; (4) lab VM↔lab VM dropped at L2 except for a run's registered target↔prober pair. Pairs are added and removed by the broker through a narrow root helper (`/usr/local/sbin/cloudcore-labnet`, a sudoers entry for that script only). CloudCore attaches instances in the `lab-vms` VPC to `cclab0` and reads their address from the lab lease file. Verified from inside a lab VM before any model-written command runs: internet works; the LAN, the hub's ports, the coordinator and unpaired lab VMs are unreachable; the coordinator can SSH in; a paired prober reaches its target. Needs root on each host (the setup script) and the peer on current code. | Done on the hub (2026-10-02, F-203): isolation verified from inside lab VMs. Lab VMs are placed on the caller's own host (lab traffic never crosses WireGuard; cross-host would need WG routes for the lab subnets, not done). **The peer needs current code and the setup script before proof runs can use it.** |
| F3 | **Disks.** CloudCore can't attach extra disks today. Add optional blank data disks to the instance template on a **virtio-scsi** bus, so a guest sees `/dev/sdb` and `/dev/sdc` natively (no aliases), plus a resize call so "grow the filesystem after enlarging the disk" becomes testable. | Done on the hub (2026-10-02, F-204): lab VMs have root `/dev/sda` and blank `/dev/sdb`/`/dev/sdc` on virtio-scsi; live disk growth verified end to end (growpart + resize2fs). The runner must rescan a grown data disk (F4). |
| F4 | **`FullVM` backend for the advice runner.** Same interface as `MicroVM`: boot, `ssh_client(user)`, the address the prober uses, reboot that keeps state, teardown. The control channel is a separate key-only sshd on its own port, installed by cloud-init (as on the microVM, which used vsock). Sized from the peer's free resources. The microVM-only workarounds don't apply on this backend. | Done (2026-10-02, F-205): `fullvm.py`; control sshd on 1022; per-run target address; in-place reboots; prober boots in parallel. Six stored answers match their microVM verdicts on the hub. Deployed with `lab_backend = "microvm"` until the peer is updated. |
| F5 | **Prove on full VMs.** Advice runs, repairs and another-way attempts use `FullVM`. Re-run a sample of the held-out answers on both backends to see what the backend alone changes. | Done 2026-10-03 (results below) |
| F6 | **The student's full VM.** Requested from the broker when the student opens Linux Help, one per session. Lifetime: an idle timeout plus a maximum. The page's Terminal shows "your full machine is starting (~2 min)" and attaches when it's ready; the microVM Terminal works meanwhile. It is reconnected on page reload via the session token and destroyed on timeout or by a "Destroy" button. | Not started |
| F7 | **Which questions need a full machine.** From the question and the answer: kernel modules, GRUB/boot/kernel parameters, reboots, disks and partitions, snaps, multi-machine setups, long-running services. The page says "this one needs a full machine" and points the Terminal there. Small questions keep the microVM. | Not started |
| F8 | **Measure.** After the setup stage (F-200 class A) and the goal-check fixes, run a **fresh** held-out set; the F-200 set is no longer held out. | Done 2026-10-04 as the lab plan's L20 (corrected 2026-10-09: this row said "Not started") |

## Decisions (user, 2026-10-02)

1. **A scoped broker,** not the master token. The coordinator gets only a `/v1/lab-vms` token: create from a fixed template, destroy its own, with quotas and a TTL reaper on the hub.
2. **Student VM lifetime:** destroyed after a 30-minute idle timeout or after 4 hours at most; one per session, at most 2 at once.
3. **Proofs always on full VMs.** The microVM remains only for the student's instant Terminal.

## Risks

- **Boot time:** two full VMs per proof run. Mitigation: a small pool of pre-booted, never-used VMs, if measured boot times hurt.
- **Leaks:** VMs outliving their runs. Mitigation: the broker's TTL and reaper on the hub, independent of the coordinator.
- **Egress fencing:** needs to match today's per-run iptables policy. Verified in F2 before any model-written command runs.

Methodology unchanged: build and verify live, log findings as F-NNN, tear down after.

## F5 results (2026-10-03)

llm-chat was rebuilt with `lab_backend = full`, with the coordinator on Llwyn-y-Groes using its own host's broker (two-host S5). All 40 held-out answers (F-200) were re-run as recorded, with no new model answers, on both backends, including repairs (`reports/llm-chat-lab/run_backend_compare.py`, results in `f5-backend-compare.jsonl`).

| Final verdict | MicroVM | Full VM |
|---|---|---|
| goal_verified | 4 | 4 |
| ran_clean | 3 | 5 |
| partial (nothing changed to verify) | 4 | 6 |
| failed | 26 | 23 |
| not_testable / not_runnable | 1 / 2 | 0 / 2 |
| Median time per answer | 37 s | 83 s |

**Full VMs fix three microVM failures and one "can't test":**
- #16, automatic security updates: the `unattended-upgrades` service works on a full VM.
- #18, growing an ext4 filesystem: the broker can grow a disk.
- #29, disabling IPv6.
- #31, apt vs apt-get.

They prove nothing new, so the backend alone moves the result little. The 23 full-VM failures fall into four groups, and none is caused by the lab backend:

| Cause | Answers | Count |
|---|---|---|
| The answer is wrong, or leaves placeholders in its commands (`your-uuid-here`, `ZOMBIE_PID`, `server_ip_address`, `ufw ... port 22/tcp`, `systemd-resolve`) | 6, 12, 20, 22, 26, 33, 36, 39 | 8 |
| The question assumes something the lab lacks (no `/srv/reports`, nginx or MariaDB not installed, `labuser` not in sudo, no Dockerfile, an unreachable gateway): the setup stage, F-200 class A | 8, 9, 10, 13, 23, 27, 38, 40 | 8 |
| Can't be tested here, but not recognised as such (certbot needs a public domain; SMART, `sensors` and the GPU need physical hardware) | 14, 21, 30, 35 | 4 |
| The goal check or a lab limit (no swap on the VM, the Docker goal, a step time limit) | 11, 17, 28 | 3 |

**What follows:**
- **Proofs stay on full VMs:** fewer special cases, and reboots, disks and services behave as on a server.
- **Next for the score:** the setup stage (8 answers), recognising more hardware and external-service questions as untestable (4), and the answers themselves (8): placeholders should be caught and filled before running.

