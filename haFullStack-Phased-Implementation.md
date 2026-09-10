# NGINX High-Availability Load Balancing Architecture — Phased Implementation

**Multi-Service Platform — Frontend, Backend, MySQL, Keystone, RabbitMQ**

v0.1 (in progress — built section by section) | Paul Scott

---

## How This Document Is Built

Built and reviewed one slice at a time alongside the LLD
(`haFullStack-LLD.md`) — each LLD section gets its matching phase(s) here
before the next slice starts. A "phase" here is scoped to one slice of
functionality across however many environment/tool combinations apply to
it, not to an entire tier end-to-end. Every non-obvious issue hit while
building a phase is logged in `haFullStack-Findings-Log.md`, linked from
that phase's row/section below rather than repeated inline.

| # | Phase | LLD Ref | Status |
|---|---|---|---|
| 1 | Load Balancer Tier — Client to Frontend | LLD §1 | Phase 1.A (Lab/OpenTofu) done — see [findings](haFullStack-Findings-Log.md#phase-1a--lab-opentofu); 1.B–1.F pending |

---

## Phase 1 — Load Balancer Tier: Client to Frontend

Six independent build paths exist for this phase (3 environments × 2
tools). **Build and verify exactly one before touching the others** — Lab
via OpenTofu is the recommended first path, since it's the only one that
can be verified against real, running infrastructure in this working
session; the rest can then be built from a proven pattern rather than in
parallel with it.

### Phase 1.A — Lab, OpenTofu (recommended starting path) — Done

| ID | Task | Description | Status |
|---|---|---|---|
| 1A-01 | Confirm bridged networking | `ip link show ccbr0`; if `DOWN`/`NO-CARRIER` persists once instances are attached, run `sudo bash api/setup-network.sh` | Done — 4 real platform gaps found and fixed, [F-003–F-006](haFullStack-Findings-Log.md#f-003--bridged-instances-silently-fall-back-to-slirp-without-etcqemubridgeconf) |
| 1A-02 | Confirm per-instance user_data support | Resolve LLD §1.7's open item: does `modules/instance-group` support per-instance `user_data`, or are two explicit `cloudcore_instance` resources needed for the NGINX pair | Done — `modules/compute` takes per-key `user_data` natively; used instead of either option |
| 1A-03 | New example template | `examples/ha-frontend-lb/` — vpc, subnets, security_groups (nginx + frontend), instance-group (frontend), compute (nginx, per-key user_data) | Done |
| 1A-04 | NGINX cloud-init | `http{}` frontend upstream, matching `haFullStack.md` §3.1 | Done — hit a YAML-templating bug along the way, [F-009](haFullStack-Findings-Log.md#f-009--templatefilecloud-init-yaml-broke-on-indent-and-inline-runcmd-quoting) |
| 1A-05 | Keepalived cloud-init | One MASTER + one BACKUP config (resolves 1A-02) | Done — **multicast** VRRP, not unicast as originally planned; validated directly over `ccbr0` |
| 1A-06 | `tofu apply` | Stand up for real against this CloudCore instance | Done — required two platform fixes to get frontend IPs into NGINX's config correctly, [F-007](haFullStack-Findings-Log.md#f-007--private_ip-permanently-stuck-on-the-slirp-placeholder-for-bridged-instances)/[F-008](haFullStack-Findings-Log.md#f-008--opentofu-providers-create-didnt-wait-for-private_ip-before-considering-an-instance-ready) |
| 1A-07 | Verify VIP reachable | `curl` the VIP from the CloudCore host; confirm a frontend response | Done — round-robins across both frontend instances |
| 1A-08 | Verify failover | Stop the MASTER NGINX instance; confirm the VIP migrates and traffic keeps flowing within the RTO target (HLD §6, ~3 s) | Done — VIP moved to BACKUP with zero dropped requests across 60s of continuous polling |
| 1A-09 | Teardown | `tofu destroy`; confirm no orphaned resources | Done — clean destroy, confirmed no orphaned instances/VPCs |

**Verification for this phase (Lab/OpenTofu):** confirmed via 1A-07/1A-08
above against real infrastructure — a working reverse-proxied response
through the VIP, and a failover with zero dropped requests, not just a
plan/apply that completed without error. Full detail on every issue hit
getting here: [Findings Log — Phase
1.A](haFullStack-Findings-Log.md#phase-1a--lab-opentofu).

### Phase 1.B — Lab, Ansible

| ID | Task | Description | Status |
|---|---|---|---|
| 1B-01 | New playbook | `ansible/examples/12-ha-frontend-lb.yml`, mirroring `09-ghidra-workstation.yml`'s structure | Pending |
| 1B-02 | Security groups + instances | `security_group` ×2, `instance` ×2 (frontend) + ×2 (nginx), per LLD §1.3.3 | Pending |
| 1B-03 | Per-node Keepalived state | Two separate `instance` tasks (or a templated loop) so MASTER/BACKUP `user_data` differ correctly | Pending |
| 1B-04 | `ansible-playbook` run | Stand up for real | Pending |
| 1B-05 | Verify VIP + failover | Same checks as 1A-07/1A-08 | Pending |
| 1B-06 | Teardown via `07-teardown.yml` | Confirm the security-group cleanup fix (already applied this session) still covers this template's SGs | Pending |

### Phase 1.C — On-Prem, OpenTofu

| ID | Task | Description | Status |
|---|---|---|---|
| 1C-01 | Confirm target hypervisor | libvirt vs. vSphere vs. other — resolves LLD §1.7's open item before writing real provider config | Pending |
| 1C-02 | Confirm VRRP permitted on-network | IP protocol 112 (or unicast UDP) not blocked between the two NGINX hosts' VLAN/segment | Pending |
| 1C-03 | Provider config | Per 1C-01's answer | Pending |
| 1C-04 | Compute + network resources | 2× NGINX, 2× frontend, equivalent security policy to LLD §1.3.2 | Pending |
| 1C-05 | Reuse cloud-init content | Same NGINX/Keepalived cloud-init as 1A-04/1A-05 — guest-level config doesn't change with the provider | Pending |
| 1C-06 | Apply, verify, document actual on-prem specifics | Real hostnames/IPs/VLAN this was built against, for the next person who reruns this | Pending |

### Phase 1.D — On-Prem, Ansible

| ID | Task | Description | Status |
|---|---|---|---|
| 1D-01 | Inventory | Real host group `nginx_lb`, `frontend` — however hosts are provisioned (manually, or by Phase 1C) | Pending |
| 1D-02 | NGINX/Keepalived role or tasks | Per LLD §1.4.3 — classic configuration-management style, not cloud-init | Pending |
| 1D-03 | `vrrp_state`/`vrrp_priority` derivation | Confirm the `inventory_hostname`/`groups` approach in LLD §1.4.3 against the real inventory shape in use | Pending |
| 1D-04 | Run, verify, document | Same verification intent as 1A-07/1A-08, adapted to real hosts | Pending |

### Phase 1.E — AWS, OpenTofu

| ID | Task | Description | Status |
|---|---|---|---|
| 1E-01 | Confirm ASG vs. plain instances | Resolves LLD §1.7's open item | Pending |
| 1E-02 | VPC/subnet data sources or resources | Depending on whether this reuses existing AWS network or creates new | Pending |
| 1E-03 | Security groups | ALB SG (internet-facing), frontend SG (ALB-SG-sourced only) | Pending |
| 1E-04 | ALB + target group + listener | Per LLD §1.5.2 | Pending |
| 1E-05 | Launch template + ASG (or instances) | Per 1E-01's answer | Pending |
| 1E-06 | Apply, verify | ALB DNS name resolves and returns a frontend response; kill one instance, confirm ASG replaces it and ALB health check drains/restores correctly | Pending |

### Phase 1.F — AWS, Ansible

| ID | Task | Description | Status |
|---|---|---|---|
| 1F-01 | `amazon.aws` collection installed | Confirm available/pinned version | Pending |
| 1F-02 | Target group + ALB | Per LLD §1.5.3 | Pending |
| 1F-03 | Instances/launch template | Via `amazon.aws.ec2_instance` or equivalent — confirm against 1E-01's ASG decision, since Ansible's ASG support differs in maturity from Terraform's | Pending |
| 1F-04 | Apply, verify | Same intent as 1E-06 | Pending |

---

## Cross-Cutting Notes for This Phase

- Phases 1.A/1.B (Lab) are the only ones that can be built and verified for
  real in this working session. Phases 1.C–1.F are written to the same
  level of design detail but their task statuses can only move past
  "Pending" once run against real on-prem/AWS infrastructure, which is
  outside what this session has access to.
- Do not start Phase 1.C–1.F's "Confirm" tasks (1C-01, 1E-01) speculatively
  — they're blocking questions for whoever owns those environments, not
  something to guess and build against.

---

## Document History

| Version | Date | Author | Change Summary |
|---|---|---|---|
| v0.1 | 2026-09-10 | Paul Scott | First slice — Phase 1, Load Balancer Tier client-to-frontend, all six environment/tool paths. |
| v0.2 | 2026-09-10 | Paul Scott | Phase 1.A (Lab/OpenTofu) marked Done, all tasks verified against real infrastructure; linked to new `haFullStack-Findings-Log.md`. |
