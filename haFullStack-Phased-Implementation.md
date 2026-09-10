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

**Build order across phases:** every phase's `.A` (Lab/OpenTofu) sub-path
gets built and verified, as one growing stack in
`examples/ha-frontend-lb/` (not independent per-phase templates), before
the next phase's `.A` starts. Only once every phase is proven on
Lab/OpenTofu does the whole set move to Ansible (`.B`), then On-Prem
(`.C`/`.D`), then AWS (`.E`/`.F`) — not phase-by-phase across every
environment/tool combination as each phase completes.

| # | Phase | LLD Ref | Status |
|---|---|---|---|
| 1 | Load Balancer Tier — Client to Frontend | LLD §1 | Phase 1.A (Lab/OpenTofu) done — see [findings](haFullStack-Findings-Log.md#phase-1a--lab-opentofu); 1.B–1.F pending |
| 2 | Database Tier — MySQL High Availability | LLD §2 | Phase 2.A complete (2A-01–2A-17) — see [findings](haFullStack-Findings-Log.md#phase-2a--lab-opentofu-database-tier); On-Prem/AWS and Ansible still pending |
| 3 | Identity Tier — Keystone | LLD §3 | Phase 3.A complete (3A-01–3A-15) — see [findings](haFullStack-Findings-Log.md#phase-3a--lab-opentofu-identity-tier); On-Prem/AWS and Ansible still pending |

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

## Phase 2 — Database Tier: MySQL High Availability

Extends `examples/ha-frontend-lb/` in place — one growing stack, not a
new template. Per the build order above, only Phase 2.A is worked now;
2.B–2.F wait until every phase's `.A` is done.

### Phase 2.A — Lab, OpenTofu

| ID | Task | Description | Status |
|---|---|---|---|
| 2A-01 | Security groups | `mysql` (3306 + 33061 GR peer traffic), `proxysql` (6033 from `nginx` SG only), per LLD §2.3.1 | Done |
| 2A-02 | Resolve the seed-address self-reference | LLD §2.7's open item — `modules/compute`'s per-key `user_data` can't reference that same call's own `private_ips_by_key`; needs a real answer before the MySQL module call can be written for real | Done — split into two module calls (bootstrap node, then joiners referencing its known IP), matching the frontend→nginx pattern from §1 |
| 2A-03 | MySQL cloud-init | 3-node Group Replication, per-node `server-id`/bootstrap `user_data`, plus `group_replication_autorejoin_tries` (LLD §2.3.1b — not in `haFullStack.md` §5.1's original config), per LLD §2.3.2 | Done — hit 4 real Group Replication bootstrapping bugs along the way, [F-016](haFullStack-Findings-Log.md#f-016--group-replication-config-variables-rejected-with-unknown-variable-on-a-fresh-install)–[F-019](haFullStack-Findings-Log.md#f-019--caching_sha2_password-refuses-authentication-without-a-secure-connection) |
| 2A-04 | Verify GR bootstrap sequencing | LLD §2.3.1's flagged risk — confirm empirically whether nodes 2/3 need an explicit wait/retry against node 1's readiness | Done — confirmed via a clean `tofu destroy` + `tofu apply` with all 5 fixes (F-015–F-019) in place from the start: the wait-loop in mysql-b/c's own `runcmd` handled the real race with zero manual intervention, all 3 nodes reached `ONLINE` with exactly one `PRIMARY` fully unattended |
| 2A-05 | ProxySQL cloud-init | Read/write hostgroup config against the 3 MySQL nodes, per `haFullStack.md` §5.2 | Done — used ProxySQL's `mysql_group_replication_hostgroups` (real GR-aware primary auto-detection) instead of §5.2's static write/read split, which would have been wrong the moment a failover happened |
| 2A-06 | Extend NGINX cloud-init | Add the `stream{}` block (§1's NGINX nodes, not new ones) listening on `:3306`, proxying to ProxySQL | Done |
| 2A-07 | Extend frontend cloud-init — status script | Not a raw query dump: heartbeat-table write+read, membership query, an OK/DEGRADED/CRITICAL verdict, and — in CRITICAL — a real `group_replication_force_members` value built from the survivor's own view of the group, per LLD §2.3.1b. Likely a small Python script, not a shell one-liner (flagged in LLD §2.7). Also writes the same status as structured JSON to a local file, not HTML-only | Done — OK/DEGRADED/CRITICAL verdict logic and JSON output built; CRITICAL's real `force_members` value not yet exercised against an actual below-quorum failure (that's 2A-13) |
| 2A-08 | `tofu apply` | Stand up for real against this CloudCore instance | Done — a second, clean `tofu destroy` + `tofu apply` with all 5 fixes in the source template from the start reached the full working state (3-node cluster ONLINE, ProxySQL auto-routing, frontend page OK with an advancing heartbeat) with zero manual SSH intervention |
| 2A-09 | Verify cluster membership | All 3 MySQL nodes show `ONLINE` in `performance_schema.replication_group_members`; exactly one `PRIMARY` | Done |
| 2A-10 | Verify through the full path | Frontend page (via the VIP) shows live, correct cluster status and an advancing heartbeat counter — not just a direct MySQL connection | Done — verdict `OK`, heartbeat counter genuinely advancing, through VIP → NGINX `stream{}` → ProxySQL → MySQL |
| 2A-11 | Failure test 1 — stop a secondary | LLD §2.3.1a test 1: zero write impact, dead node drops from the read hostgroup, page keeps advancing without interruption; status verdict stays OK or briefly DEGRADED | Done — heartbeat counter advanced with zero gap (37→50, no reset) across the whole test; ProxySQL auto-moved the stopped node to hostgroup 9999 (SHUNNED) with no manual re-tagging; frontend page correctly showed DEGRADED (2/3) throughout, captured in the history table |
| 2A-12 | Failure test 2 — stop the primary | LLD §2.3.1a test 2: GR elects a new primary, ProxySQL re-tags the write hostgroup; run a write-loop against the heartbeat table through the failover window and measure the real RTO from actual consecutive-failure count, not the ~5-10s from `haFullStack.md` §5.3 taken on faith | Done — measured RTO ~3.5s (0.5s-interval write-loop: last OK 14:48:05.999Z, single FAIL 14:48:06.600Z, first recovered OK 14:48:10.127Z — only 1 failed write in 120 attempts), beating the assumed ~5-10s. mysql-c auto-elected PRIMARY, ProxySQL re-tagged hostgroup 10 to it and shunned mysql-a into 9999, entirely automatically. Frontend's own independent status check corroborated the same event (brief CRITICAL blip, self-corrected) |
| 2A-13 | Failure test 3 — stop 2 of 3 nodes | LLD §2.3.1a test 3 (the negative test): confirm the survivor drops out of `ONLINE` and refuses writes rather than silently continuing — the actual evidence for why 2 nodes isn't enough (F-001). Also verify the diagnostic: status flips to CRITICAL, the missing nodes are named, and the displayed `force_members` value is the real, runnable command for the actual surviving member — not a placeholder | Done — **and the expected outcome was wrong.** The survivor does *not* refuse writes or drop out of `ONLINE` — it expels the unreachable peers and keeps accepting writes as a legitimate 1-node group ([F-021](haFullStack-Findings-Log.md#f-021--the-whole-point-of-test-3-didnt-hold-the-survivor-doesnt-refuse-writes-after-losing-quorum), `haFullStack.md` §5.3 corrected to v1.3). The frontend's own diagnostic *did* correctly flag CRITICAL throughout (validating §2.3.1b's design), even though MySQL itself didn't act on it — real split-brain protection is a new, undecided open item |
| 2A-14 | Failure test 4 — restart a stopped node | LLD §2.3.1a test 4: confirm it does not auto-rejoin (`group_replication_start_on_boot = OFF`), needs an explicit `START GROUP_REPLICATION`, then catches up via distributed recovery | Done — `systemctl restart mysql` came back up with GR genuinely not running (empty `replication_group_members`/`group_replication_primary_member`); explicit `START GROUP_REPLICATION` rejoined and reached `ONLINE` in ~8s via distributed recovery; frontend page confirmed back to `OK` with all 3 members |
| 2A-15 | Failure test 5 — transient network blip | LLD §2.3.1a test 5: briefly `iptables DROP` one node's GR port without stopping `mysqld`; confirm it's expelled and then **automatically rejoins** once connectivity returns, with no manual step — proves `group_replication_autorejoin_tries` (2A-03) actually works, not just that it's configured | Done — expelled at 15:03:16.937Z ("Member was expelled from the group due to network failures"), auto-rejoin attempt 1 of 3 started the same second, succeeded by 15:03:21.121Z (~4s), zero manual SQL run. Frontend page confirmed back to OK/3-3 |
| 2A-16 | Fix F-021 — quorum watchdog | Not in the original task list — added after 2A-13 overturned the expected outcome. `quorum-watchdog.py`: a local, per-node systemd timer (3s) enforcing `super_read_only` on a node that's `ONLINE`/`PRIMARY` of a group below the original cluster's majority | Done — re-ran the full 2A-13 scenario twice. First run found a second bug (`read_only` vs `super_read_only` — see [F-021](haFullStack-Findings-Log.md#f-021--the-whole-point-of-test-3-didnt-hold-the-survivor-doesnt-refuse-writes-after-losing-quorum)); second run, after fixing it, self-healed completely with zero manual `SET GLOBAL`/`LOAD MYSQL SERVERS` commands — write correctly refused below quorum, ProxySQL independently pulled the node from the writer hostgroup, both automatically cleared and writer status automatically restored once membership returned |
| 2A-17 | Teardown | `tofu destroy`; confirm no orphaned resources | Done — clean destroy, 15 resources, confirmed no orphaned instances/VPCs |

**Verification for this phase (Lab/OpenTofu):** 2A-10 through 2A-16 are
the real test — proof reachable through the actual request path, every
failure mode in LLD §2.3.1a's test matrix demonstrated for real (not
just a healthy-topology `tofu apply` that completes without error), and
the manual-intervention diagnostics in LLD §2.3.1b actually accurate
under a real below-quorum failure, not just present. 2A-13 is not
optional — it's the actual evidence this design does what a 2-node
cluster (F-001) can't, and it disproved an assumption the whole slice had
been resting on. 2A-15 is one counterpart — the evidence that not
everything needs a human. 2A-16 is the other — closing the gap 2A-13
found rather than just documenting it, verified with the same rigor (the
fix's own first attempt had a real bug, caught by re-running the test
against it rather than trusting it worked).

### Phase 2.B — Lab, Ansible

Same shape as Phase 1.B, extending `ansible/examples/12-ha-frontend-lb.yml`
(once it exists) rather than a new playbook — not started; waits for
Phase 2.A plus every other phase's `.A` to be done first.

### Phase 2.C — On-Prem, OpenTofu

Not started — waits for the Lab/OpenTofu pass across every phase, per the
build order above.

### Phase 2.D — On-Prem, Ansible

Not started — same as 2.C.

### Phase 2.E — AWS, OpenTofu

Not started — RDS Multi-AZ + RDS Proxy, per LLD §2.5.

### Phase 2.F — AWS, Ansible

Not started — same as 2.E, via `amazon.aws`.

---

## Phase 3 — Identity Tier: Keystone

Extends `examples/ha-frontend-lb/` further in place — same growing
stack. Per the build order above, only Phase 3.A is worked now.

### Phase 3.A — Lab, OpenTofu

| ID | Task | Description | Status |
|---|---|---|---|
| 3A-01 | Security groups | `keystone` (5000 from `nginx` SG only, plus SSH), `memcached` (11211 from `keystone` SG only, plus SSH), per LLD §3.3.1 | Done |
| 3A-02 | Fernet key generation | `random_id` (32 bytes, base64url) ×2 in Terraform, new `hashicorp/random` provider dependency — same identical key material injected into both Keystone nodes' `user_data`, no runtime coordination needed | Done — key file format (raw base64url, no wrapper) confirmed directly against a real `keystone-manage fernet_setup` run, exactly matching `random_id`'s own `.b64_url` output |
| 3A-03 | Keystone `keystone` DB + user | New database and dedicated `keystone` user on the existing §2 MySQL cluster (through ProxySQL, not a new DB tier), per LLD §3.3.1 | Done — added to `mysql-cloud-init.yaml.tftpl`'s bootstrap-node block and `proxysql-cloud-init.yaml.tftpl`'s `mysql_users` |
| 3A-04 | memcached cloud-init | 2 identical nodes via `modules/instance-group`, per LLD §3.3.2 | Done |
| 3A-05 | Keystone cloud-init | 2 identical nodes via `modules/instance-group` (no bootstrap/joiner split needed — genuinely active-active); `keystone-manage bootstrap --bootstrap-password admin` on one node only, per LLD §3.3.1 | Done — design revised after direct verification: `db_sync`/`bootstrap` confirmed idempotent and safe to run unconditionally on **every** node (re-running each against an already-initialized DB is a clean no-op), so genuinely identical `user_data` runs both, rather than needing a per-node split. Package availability, port 5000 pre-wired by the `keystone` apt package itself, and the full db_sync→bootstrap→token-issuance sequence all confirmed directly against a real Ubuntu 22.04 instance before writing the template |
| 3A-06 | Extend NGINX cloud-init | Add Keystone routing — dedicated port 5000, matching how the real backend application addresses Keystone (confirmed directly, not a Lab DNS workaround — see LLD §3.3.1) | Done — new `nginx-keystone.conf.tftpl` (own `server{ listen 5000; }` block), concatenated with the existing frontend conf into the same `sites-available/default`, per LLD §3.3.1's framing |
| 3A-07 | Extend frontend cloud-init — `keystone-status.py` | Same systemd-timer/rolling-history pattern as `mysql-status.py`; issues a token via the VIP, then deliberately re-validates it against **the other** Keystone node specifically, per LLD §3.3.1 | Done — validates directly against **every** Keystone node's own IP (not just "the other" one), since that proves cross-node consistency without needing to first determine which node the VIP happened to route the issuance request to |
| 3A-08 | `tofu apply` | Stand up for real against this CloudCore instance | Done — hit and fixed 3 real bugs along the way (missing Fernet-key base64 padding, cloud-init `write_files` module running before packages install, missing `mysql-client` silently breaking the readiness wait-loop) — see Findings Log |
| 3A-09 | Verify token issuance + cross-node validation | `keystone-status.html` shows `OK` through the full real path (VIP → NGINX → Keystone → MySQL/memcached) | Done — both nodes confirmed sharing the same MySQL-backed identity data (matching user/project IDs), and a token issued directly against node 1 validated successfully directly against node 2 with zero memcached involvement, confirming this LLD's §3.1 counter-claim to `haFullStack.md` §7 |
| 3A-10 | Failure test 1 — stop one Keystone node | LLD §3.3.1a test 1: zero impact, no election/promotion delay (unlike MySQL's primary failover in §2) | Done — **outcome differs from the LLD's expectation.** VIP-routed issuance fails over quickly (a brief CRITICAL blip at the exact moment, matching NGINX's default failure-detection window), but the status page then shows `DEGRADED`, not `OK`, for the entire time the node stays down — by design, not a bug: `keystone-status.py` deliberately validates directly against every node's own IP (§3A-07), so it correctly and honestly reports one node unreachable rather than papering over it. A restarted node also takes ~30s longer than SSH/ping suggest before it actually answers — `mod_wsgi`'s own worker-startup time, not a CloudCore issue (F-028) |
| 3A-11 | Failure test 2 — the memcached question | LLD §3.3.1a test 2: get a token from node A, stop node A, validate it against node B — resolves whether shared Fernet keys (this LLD's claim) or memcached (`haFullStack.md` §7's claim) is what actually enables cross-node validation. Not optional — same standing as 2A-13 was for the DB tier | Done — **confirmed this LLD's claim, not `haFullStack.md` §7's.** Token issued by node A, then node A stopped entirely, validated successfully (`HTTP 200`) directly against node B with zero memcached involvement. `haFullStack.md` §7 needs correcting — see F-026 |
| 3A-12 | Failure test 3 — stop one memcached node | LLD §3.3.1a test 3: token issuance/validation keep working | Done — issuance succeeded (`HTTP 201`), just measurably slower (~3s vs. sub-second) while the dead server's connection attempt was still in the retry path |
| 3A-13 | Failure test 4 — stop both memcached nodes | LLD §3.3.1a test 4: pushes test 3 further — is memcached ever a hard dependency for basic auth | Done — issuance still succeeded with both memcached nodes down (first attempt after the second stop hit a 15s client timeout with no response at all; a retry with a 60s timeout succeeded in 4.7s — consistent with a per-worker-process retry/backoff penalty on the first request after a server newly becomes unreachable, not a hard dependency) |
| 3A-14 | Failure test 5 — stop both Keystone nodes | LLD §3.3.1a test 5: genuine outage, `keystone-status.html` correctly shows `CRITICAL` | Done — correctly showed `CRITICAL` throughout, recovered cleanly to `OK` once both nodes were restarted (subject to the same `mod_wsgi` worker-startup delay as test 1 — F-028) |
| 3A-15 | Teardown | `tofu destroy`; confirm no orphaned resources | Done — clean destroy, 23 resources, confirmed no orphaned instances/VPCs |

**Verification for this phase (Lab/OpenTofu):** 3A-09 through 3A-14 are
the real test. 3A-11 carries the same weight 2A-13 did for the DB tier —
it's the test that actually resolves whether `haFullStack.md` §7 is
correct about memcached, rather than carrying an unverified claim
forward a second time in the same project.

### Phase 3.B — Lab, Ansible

Not started — waits for every phase's `.A` to be done first, per the
build order above.

### Phase 3.C — On-Prem, OpenTofu

Not started — real vhost routing (`identity.example.com`) instead of
Lab's dedicated-port substitution, per LLD §3.4.

### Phase 3.D — On-Prem, Ansible

Not started — same as 3.C, via Ansible.

### Phase 3.E — AWS, OpenTofu

Not started — genuinely undecided per LLD §3.5/§3.7 (Keystone-on-EC2 vs.
a deliberate IAM/Cognito redesign), not just unconfirmed detail. Don't
start speculatively.

### Phase 3.F — AWS, Ansible

Not started — same as 3.E.

---

## Cross-Cutting Notes

- Every phase's `.A` (Lab) sub-path is the only one that can be built and
  verified for real in this working session. `.B`–`.F` are written to the
  same level of design detail but their task statuses can only move past
  "Pending" once actually run — against a real Ansible pass, real on-prem
  infrastructure, or a real AWS account, none of which are this session's
  Lab/OpenTofu-only reach.
- Do not start any phase's On-Prem/AWS "Confirm" tasks (e.g. 1C-01, 1E-01)
  speculatively — they're blocking questions for whoever owns those
  environments, not something to guess and build against.
- Don't start a new phase's `.A` before the previous phase's `.A` is
  proven — each one extends the same growing stack, so an unresolved
  problem in an earlier phase (e.g. Phase 2.A's open seed-address
  question) blocks the next one for real, not just on paper.

---

## Document History

| Version | Date | Author | Change Summary |
|---|---|---|---|
| v0.1 | 2026-09-10 | Paul Scott | First slice — Phase 1, Load Balancer Tier client-to-frontend, all six environment/tool paths. |
| v0.2 | 2026-09-10 | Paul Scott | Phase 1.A (Lab/OpenTofu) marked Done, all tasks verified against real infrastructure; linked to new `haFullStack-Findings-Log.md`. |
| v0.3 | 2026-09-10 | Paul Scott | Second phase — Phase 2, Database Tier (MySQL High Availability), all six environment/tool paths; made the build-order sequencing (all phases' `.A` first, then `.B`–`.F` across the whole set) explicit. Expanded 2A's verification into LLD §2.3.1a's 5-test failure matrix (secondary/primary/2-node loss, rejoin, transient-expulsion autorejoin) plus a write-path heartbeat and LLD §2.3.1b's OK/DEGRADED/CRITICAL diagnostics with a real `force_members` remediation value. Draft, not yet built. |
| v0.4 | 2026-09-10 | Paul Scott | First real build of Phase 2.A: 2A-01–2A-10 done and verified (3-node cluster ONLINE, ProxySQL auto-routing, frontend page showing a live advancing heartbeat through the real path) after fixing 5 real bugs found along the way (F-015–F-019). 2A-04/2A-08 marked only partially verified — reached working state via manual recovery, not yet proven via a clean unattended re-apply with the fixes in place from the start. 2A-11–2A-16 (failure-mode tests, teardown) still pending. |
| v0.5 | 2026-09-10 | Paul Scott | Clean `tofu destroy` + `tofu apply` with all 5 fixes in place from the start, zero manual SSH intervention — 2A-04/2A-08 upgraded from partially verified to Done. A transient "unreachable" reading on the frontend page's very first check (NGINX's `stream{}` not up yet at that exact moment) self-corrected on the next 5s timer tick, exactly as §2.3.1's "don't block frontend's own boot on the DB tier" design intended — not a bug, confirmation the design choice was right. 2A-11–2A-16 (failure-mode tests, teardown) still pending. |
| v0.6 | 2026-09-10 | Paul Scott | Failure-mode tests 2A-11, 2A-12, 2A-14 done and passed as expected. 2A-13 done but overturned the expected outcome (F-021) — the survivor doesn't refuse writes below quorum by default, correcting `haFullStack.md` §5.3. 2A-15 and teardown (2A-16) still pending. |
| v0.7 | 2026-09-10 | Paul Scott | 2A-15 done — automatic rejoin confirmed in ~4s with zero manual intervention after a simulated network partition. All 5 failure-mode tests complete; only teardown (2A-16) remains. |
| v0.8 | 2026-09-10 | Paul Scott | New task 2A-16 — fixed F-021 for real with `quorum-watchdog.py`, verified by re-running 2A-13's scenario twice (catching a second bug, `read_only` vs `super_read_only`, in the fix's own first attempt). Teardown renumbered to 2A-17. |
| v0.9 | 2026-09-10 | Paul Scott | Third phase — Phase 3, Identity Tier (Keystone), all six environment/tool paths. Test 3A-11 carries the same weight 2A-13 did — it's what actually resolves whether `haFullStack.md` §7's memcached claim is correct. Draft, not yet built. |
| v0.10 | 2026-09-10 | Paul Scott | Phase 3.A built and failure-tested for real (3A-01–3A-14 done). Two real deploy bugs found and fixed (F-025, F-026). Test 3A-11 resolved the memcached question — shared Fernet keys, not memcached, enable cross-node validation (F-027), correcting `haFullStack.md` §7. Test 3A-10's outcome differs from the original "zero impact" expectation — the status page correctly shows `DEGRADED` while one node is down, by design, and instance restarts carry a real TCP-reachability settling window distinct from ICMP/SSH readiness (F-028). |
| v0.11 | 2026-09-10 | Paul Scott | Teardown (3A-15) done — clean `tofu destroy`, 23 resources, no orphaned instances/VPCs. Phase 3.A fully complete. |
| v0.12 | 2026-09-10 | Paul Scott | F-028 corrected — not a CloudCore platform gap. Direct follow-up investigation (a clean isolated service showing zero restart gap, then real Keystone/Apache reproducing the exact symptom with precise timing) traced it to `mod_wsgi`'s own ~30s worker-startup latency. Nothing to change in CloudCore. |
