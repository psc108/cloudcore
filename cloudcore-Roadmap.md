# CloudCore — Roadmap (all open work)

**Status:** 2026-10-09, after the review; items in question are in `roadmap-Verify-Items.md` (V-numbers). Consolidated from the 14 plan documents by reading each in full. **Owner:** Paul Scott.

## How this works

- **One list of everything still to do on CloudCore,** including its examples (llm-chat, haFullStack). Sentinel's open work is in `sentinel-Roadmap.md`.
- **Each entry links to where it came from.** Detail stays in the source plan; nothing there is deleted.
- **A new idea goes here as a short entry,** not into a new plan. A plan document is written only when an entry is about to be built, and is linked from it.
- **Active projects keep their own plan while they run:** the LFS OS build (`lfs-os-Phased-Implementation.md`). Its later phases are listed here as pointers.
- **Statuses:** **open** · **deferred** (a condition or a later time) · **decision** (needs Paul) · **verify** (see `roadmap-Verify-Items.md`).

Sources, abbreviated:

| Short | Document (archived ones are in `docs/archive/`) |
|---|---|
| AUTH | docs/archive/cloudcore-auth-Phased-Implementation.md |
| 2HOST | docs/archive/cloudcore-two-host-Phased-Implementation.md |
| DEF | docs/archive/DEFICIENCIES.md (Terraform provider review, 2026-09-01) |
| HA | haFullStack-Phased-Implementation.md |
| LFS | lfs-os-Phased-Implementation.md |
| SKB | sentinel-kb-browsing-Phased-Implementation.md |
| NEXT | docs/archive/llm-chat-Next-Steps.md |
| LAB | docs/archive/llm-chat-lab-sandbox-Phased-Implementation.md |
| FVM | docs/archive/llm-chat-full-vm-Phased-Implementation.md |
| PLC | docs/archive/llm-chat-placement-Phased-Implementation.md |
| VER | docs/archive/llm-chat-verification-Phased-Implementation.md |
| SBX | docs/archive/llm-chat-interactive-sandbox-Phased-Implementation.md |
| EXT | docs/archive/llm-chat-sandbox-extensions-Phased-Implementation.md |
| KIW | docs/archive/llm-chat-kiwix-expansion-Phased-Implementation.md |

## 0. Now

| # | Item | Status | Source |
|---|---|---|---|
| CC-00 | **LFS OS build, run 1** (chapter 8 under way). Everything below waits for it unless Paul pulls an item forward. | active | LFS |

## 1. Data safety and operations

| # | Item | Status | Source |
|---|---|---|---|
| CC-01 | **Backups to the USB device** on Llwyn-y-Groes: a third copy, on a daily schedule, with longer retention. | open | SKB K5.1 |
| CC-02 | **The USB device mounted for good and encrypted** (LUKS + ext4). Root steps for Paul. | open | SKB K5.2 |
| CC-03 | **What the backups don't cover yet:** Loki logs, keys and tokens (encrypted), the LFS build disks, the coordinator's state, Grafana, tutor sessions; off-site (S3). One decision each. | decision | SKB K5.3 |
| CC-04 | **Monthly tested restores** of the peer and USB copies. | open | SKB K5.4 |
| CC-05 | **A data-access layer in CloudCore,** built as code is touched, so a later PostgreSQL move is contained. | deferred | SKB K5.6 |

## 2. Auth and access

| # | Item | Status | Source |
|---|---|---|---|
| CC-10 | **Audit log page** in the dashboard (filter by identity, route, status). | open | AUTH follow-up 1 (A3); NEXT 4 |
| CC-11 | **AUDIT lines to Loki,** so Sentinel can watch them. | open | AUTH follow-up 2; NEXT 4; see SN-30 |
| CC-12 | **Token management page:** list, create (shown once), revoke, rotate, expiry warnings. The API exists. | open | AUTH follow-up 3 (A4) |
| CC-13 | **Rotate the shared guest tokens** (capture, lab-VM broker) on both hosts as a dashboard or CLI action. Done by hand on 2026-10-04. | open | AUTH follow-up 4; NEXT 4 |
| CC-14 | **Retire the per-blueprint `_auth()` checks,** after checking peers in both directions. | open | AUTH A1 (partial) |
| CC-15 | **Peer and student tokens in `api_tokens`:** deliberately not migrated; `api.env`'s admin token kept as break-glass. Confirm that stays the design. | decision (V-20) | AUTH A2 |
| CC-16 | **An identity component** for the dashboard and llm-chat beyond localhost: Authelia, or oauth2-proxy with an IdP. | decision | AUTH H1 |
| CC-17 | **The identity proxy in front of the dashboard,** with roles admin / instructor / student. | open (after CC-16) | AUTH H2 |
| CC-18 | **Student logins for llm-chat:** VMs, quotas and corpus attributed to a person. | open (after CC-17) | AUTH H3 |
| CC-19 | **Seeing the build machine yourself:** a read-only log viewer in the dashboard, a lab-VM terminal as `viewer`, an optional console. | open | LFS B5 |

## 3. Network and host security

| # | Item | Status | Source |
|---|---|---|---|
| CC-20 | **Template ingress source lists:** replace the single `admin_cidr` (default 0.0.0.0/0) with source lists defaulting to private ranges, in all Terraform and Ansible templates; rebuild and check. | open | NEXT 6 |
| CC-22 | **TLS for the capture listener** (plain HTTP today; LAN only). | deferred | EXT 13.4, out of scope |
| CC-23 | **A stale HAProxy backend** that `api/lb.py` generates (`example-dev-chat-back`, wrong port, always down). Harmless; remove. | open | VER Phase 2 |

## 4. Terraform provider (from the 2026-09-01 review)

In the review's priority order.

| # | Item | Status | Source |
|---|---|---|---|
| CC-30 | **Update methods write the plan, not the API's result** (vpc, instance, load_balancer). | open (P1) | DEF §1.1 |
| CC-31 | **SecurityGroup Update loses fields** (name, vpc_id, created_at). | open (P1) | DEF §1.2 |
| CC-32 | **NFS share add/delete loops return early,** leaving partial state. | open (P2) | DEF §1.3 |
| CC-33 | **Provider tests:** unit, acceptance (VPC/instance CRUD and import), data source by id or name. | open (P3) | DEF §2 |
| CC-34 | **Refuse a non-https `api_url`,** or warn loudly. | open (P4) | DEF §6 |
| CC-35 | **Schema descriptions** on every attribute. | open (P5) | DEF §3 |
| CC-36 | **Retries with backoff** for 5xx and 429, plus a configurable timeout. | open (P6–P7, P10) | DEF §4, §5, §9 row 1 |
| CC-37 | **Pagination** in the data sources. | open (P8) | DEF §7 |
| CC-38 | **De-duplication** of the model structs, data source reads and nested mappers. | open (P9) | DEF §8.1–8.3 |
| CC-39 | **Smaller gaps:** LB target groups, DNS record import by zone+name+type, per-resource timeouts blocks, required tags, `dns_zone` id. | open (P10) | DEF §9, §10 |

## 5. Dashboard

| # | Item | Status | Source |
|---|---|---|---|
| CC-40 | **A real HCL editor mode** for `.tf` files (JavaScript mode stands in today). | open | SBX research |
| CC-41 | **Browser checks** of pages only tested through the API: the llm-chat examples page and the Capture Tokens card (and source filter). | open | VER Phase 3; EXT Stage 13 |

## 6. Placement and multi-host

| # | Item | Status | Source |
|---|---|---|---|
| CC-50 | **Prove placement moves work to a faster host** (a stand-in faster peer; roles move with no template change). | open | PLC C6; NEXT 3 |
| CC-51 | **Per-request routing of students' answers** across answer-capable hosts. | deferred (a second answer-capable host) | PLC C5; NEXT 3 |
| CC-52 | **Cross-host lab networking** (WireGuard routes for lab subnets), so lab VMs can be placed on either host. | deferred | FVM F2 |
| CC-53 | **End-to-end test of a coordinator on one peer with a worker on another.** | deferred (a third host) | VER PRIORITY |
| CC-54 | **Automatic coordinator placement:** needs its own security sign-off (security group choice). | decision | VER PRIORITY |
| CC-55 | **A GPU build of llama.cpp.** | deferred (a GPU host) | PLC risks |

## 7. llm-chat (a CloudCore example)

| # | Item | Status | Source |
|---|---|---|---|
| CC-60 | **Resume the lab-quality loop** after the LFS build; its stop rule isn't met yet (after L29: 2 false passes, lab faults 10 vs 4). | deferred (after LFS) | NEXT decision |
| CC-61 | **The lab backlog,** worked as one item: L29 failures #5, #9, #12, #15, #18, #24, #26, #29 and #32; weak checks #25 and #30; L28 #3 (a foreground server counted, and its port probed); L27 #3 and #21 (setup: the named program, a stub app). Plus the known limit: the lab can't judge what a setting means (L22 #19). | open | NEXT backlog = LAB L23, L27–L29 |
| CC-62 | **Older lab gaps,** to check whether they're still real before working them: L14 goal checks by kind; L20's smaller gaps (a config block replacing a default file; file-content lines in bash blocks; `systemctl set-default` as a change; setup for presumed LVM or remote machines; the harness recording a run's error); preparing an SSH/rsync target on the prober (#25, #39); the L28 #28 channel timeout. | verify (V-10) | LAB L14, L16–L20, L28 |
| CC-63 | **Use what the lab proves:** a matching question gets the lab-verified answer at once, marked tested. | open | NEXT 2 |
| CC-64 | **A student's own full VM** per Linux Help session (idle 30 min, 4 h max, at most 2, reconnect, Destroy). | open | FVM F6 |
| CC-65 | **Send full-machine questions to it** (modules, GRUB, reboots, disks, multi-machine). | open (after CC-64) | FVM F7 |
| CC-66 | **Sandbox follow-ups:**<br>• microVM pre-warming or pooling (boots take 6–16 s);<br>• faster clean-up after a mid-boot disconnect;<br>• the apt `universe` component in the golden rootfs;<br>• the Python runner into Firecracker;<br>• newer Node/Go than jammy's;<br>• re-test stuck-terminal recovery under the jailer;<br>• a real-model compile-fail fix round;<br>• the fix prompt to say "standard library only". | open | SBX Stage 5B; EXT Stages 10 and 12; VER Phase 2 |
| CC-67 | **The capture client submitting from a second LAN machine** (never tried). | open | EXT 13.4–13.5 |
| CC-68 | **Kiwix content gaps:** assembler references (x86/ARM, NASM, OSDev) and the Rust Book, as ZIMs of our own. | deferred | KIW gaps |
| CC-69 | **A local ACME test CA on the prober,** so Let's Encrypt answers can be tested. | deferred | FVM "not fixed by this" |
| CC-70 | **Smaller llm-chat limits, recorded and accepted:**<br>• an interrupt doesn't cancel llama-server's generation;<br>• input detection falls back to a heuristic for event-loop runtimes;<br>• a persistent notebook kernel;<br>• languages beyond four;<br>• a vsock guest agent;<br>• the 14B's RAM pressure on the coordinator;<br>• IPv4-only isolation.<br>Revisit only on need. | deferred | SBX, EXT |

## 8. haFullStack (a CloudCore example project)

`haFullStack-Phased-Implementation.md` stays the working plan for this project: its tier-by-tier, path-by-path order is the plan. This entry summarises what's open.

| # | Item | Status | Source |
|---|---|---|---|
| CC-80 | **Back to haFullStack** after LFS: the build, test, document and destroy cycle. | deferred (after LFS) | NEXT 5 |
| CC-81 | **Lab, Ansible path (.B)** for all six tiers: not started (1B-01–06, 2.B–6.B). | open | HA |
| CC-82 | **Phase 5.A remainder:** the CA-down test, the auto-renewal test, teardown (5A-14–16). | open | HA 5.A |
| CC-83 | **On-prem (.C/.D) and AWS (.E/.F) paths** for every tier. Their decisions first:<br>• **On-prem:** the hypervisor (1C-01); VRRP allowed (1C-02); an enterprise CA (5.C); mirror tooling (6.C).<br>• **AWS:** ASG or instances (1E-01); VPC reuse (1E-02); Keystone on EC2 or IAM/Cognito (3.E); ACM Private CA (5.E); an S3 mirror (6.E); Amazon MQ compatibility (4.E). | decision | HA 1.C–6.F |
| CC-84 | **To check:** 6A-12's teardown after the v0.19 retrofit, and Phase 7's missing .B–.F rows. (§6.3's quorum fix is confirmed done: V-02.) | verify (V-18) | HA |

## 9. LFS OS build, later phases (tracked in LFS)

| # | Item | Status | Source |
|---|---|---|---|
| CC-90 | **Run 2:** the 14B and Sentinel alone. | after run 1 | LFS F |
| CC-91 | **Supply chain:** signatures, tarball against git, CVEs, build behaviour, targeted review, update diffs. | after run 1 | LFS G |
| CC-92 | **Hardening,** as run 3. | after F | LFS H |
| CC-93 | **MFA.** | after stage 1 | LFS I |
| CC-94 | **Stage 2, Wayland.** | after D6 | LFS E |
| CC-95 | **The worker resumes a task from its failed step,** instead of re-unpacking (each glibc retry cost ~45 min; GCC's 4.6 h tests ran three times). **Built 2026-10-09:** a progress marker on the build machine (rewound with the tree by a checkpoint restore); a retry resumes only if the tree is still there and every step that already ran is unchanged in the new plan. Offline simulation `tests/lfs_resume_check.py` (8 cases). **Live since 2026-10-09 19:00; first real resume on 8.32 GCC (steps 1–6 reused, including 4.4 h of tests).** | done | LFS-Findings-Log LFS-033, LFS-036 |

## 10. Housekeeping

| # | Item | Status | Source |
|---|---|---|---|
| CC-98 | **Unrecorded results to check,** each when its area is next worked on: a coordinator-only config (V-11), `coordinator_flavor` (V-12), the full VM's disk rescan (V-13), the peer for full-VM runs (V-14), a Run during generation (V-15), the review page's filter (V-16), CodeMirror in a browser (V-17). Settled already: V-01 to V-07. | verify | `roadmap-Verify-Items.md` |
