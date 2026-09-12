# NGINX High-Availability Load Balancing Architecture — Findings Log

**Multi-Service Platform — Frontend, Backend, MySQL, Keystone, RabbitMQ**

v0.16 | Paul Scott

---

## Purpose

A running record of every non-obvious issue this project has uncovered —
in the architecture itself or in the underlying platform — and how each
was resolved. Updated at the completion of each slice in
`haFullStack-Phased-Implementation.md`, alongside that slice's section in
`haFullStack-LLD.md`. The point is to stop the same platform quirk or
design gap getting rediscovered the next time a slice touches the same
ground — On-Prem and AWS builds of the Load Balancer Tier will hit some
of this Lab-specific plumbing differently, but the design-level findings
(§ARCH) apply everywhere.

Each finding is numbered `F-NNN` (sequential, never reused) and tagged
with the slice it surfaced in. Findings are **not** re-litigated once
fixed — if a later slice hits a variant of an old problem, it gets a new
`F-NNN` that cross-references the original, rather than editing history.

| Slice | Findings |
|---|---|
| [Architecture Review (pre-Phase 1)](#architecture-review-pre-phase-1) | [F-001](#f-001--mysql-group-replication-two-nodes-is-not-fault-tolerant), [F-002](#f-002--rabbitmq-quorum-queues-the-same-two-node-gap) |
| [Phase 1.A — Lab, OpenTofu](#phase-1a--lab-opentofu) | [F-003](#f-003--bridged-instances-silently-fall-back-to-slirp-without-etcqemubridgeconf) – [F-009](#f-009--templatefilecloud-init-yaml-broke-on-indent-and-inline-runcmd-quoting) |
| [Platform Hardening (post-Phase 1.A review)](#platform-hardening-post-phase-1a-review) | [F-010](#f-010--bridge-mode-security-group-enforcement-silently-fails-100-of-the-time-not-just-under-concurrency) – [F-014](#f-014--dns-a-records-had-the-same-launch-time-stale-value-bug-private_ip-had-f-007) |
| [Phase 2.A — Lab, OpenTofu (Database Tier)](#phase-2a--lab-opentofu-database-tier) | [F-015](#f-015--bridge-mode-security-groups-silently-block-all-general-internet-egress) – [F-021](#f-021--the-whole-point-of-test-3-didnt-hold-the-survivor-doesnt-refuse-writes-after-losing-quorum) |
| [Platform Hardening (post-Phase 2.A review)](#platform-hardening-post-phase-2a-review) | [F-022](#f-022--cloudcores-dns-was-confirmed-working-but-guests-couldnt-reach-it) – [F-024](#f-024--help_articlesslug-uniqueness-didnt-account-for-soft-deletes) |
| [Phase 3.A — Lab, OpenTofu (Identity Tier)](#phase-3a--lab-opentofu-identity-tier) | [F-025](#f-025--random_idb64_url-needs-padding-added-before-keystone-can-use-it-as-a-fernet-key) – [F-028](#f-028--apachemod_wsgis-own-worker-startup-takes-30s-after-the-process-itself-is-active--not-a-cloudcore-platform-gap) |
| [Phase 4.A — Lab, OpenTofu (Message Broker Tier)](#phase-4a--lab-opentofu-message-broker-tier) | [F-029](#f-029--rabbitmqctl-set_policy-cant-set-queue-type-on-rabbitmq-39) – [F-031](#f-031--rabbitmqs-below-quorum-recovery-is-automatic-hafullstackmd-10s-requires-manual-intervention-claim-is-wrong-for-a-transient-outage) |

---

## Architecture Review (pre-Phase 1)

Found while drafting `haFullStack.md` / `haFullStack-HLD.md`, before any
implementation slice started — these are design-correctness issues, not
platform quirks, so they apply identically to every environment
(Lab/On-Prem/AWS) and every tool (OpenTofu/Ansible).

### F-001 — MySQL Group Replication: two nodes is not fault-tolerant

**Where:** `haFullStack.md` §5 (MySQL High Availability), §5.1 (Group
Replication — Single-Primary Mode); `haFullStack-HLD.md` §4.2 (Database
Redundancy).

**Symptom:** The original draft specced 2 MySQL nodes in a Group
Replication cluster.

**Root cause:** Group Replication is Paxos-based and requires a strict
majority of the configured member set to make progress. With 2 nodes,
majority = 2 — losing *either* node drops the survivor below quorum and
the whole cluster stops accepting writes. A 2-node "HA" cluster has
**zero** fault tolerance: it's strictly worse than a single unclustered
instance for availability, while adding all of clustering's operational
complexity. General rule: `2F+1` nodes are needed to tolerate `F`
failures — minimum 3 for single-failure tolerance. Unlike Galera, MySQL
Group Replication has no lightweight witness/arbitrator-only node option
(`garbd`-equivalent) that would let 2 full nodes + 1 witness satisfy
quorum more cheaply.

**Fix:** Bumped the design to 3 MySQL nodes throughout `haFullStack.md`
§5 and `haFullStack-HLD.md` §4.2.

**Decided by:** Paul Scott, confirming from direct prior experience with
Galera cluster join/leave behaviour that the 3-node design was the right
call over alternatives (e.g. a lighter arbitrator node), which Group
Replication doesn't support anyway.

---

### F-002 — RabbitMQ quorum queues: the same two-node gap

**Where:** `haFullStack.md` §6 (RabbitMQ High Availability), §6.3
(Quorum Queues — Primary Recommendation); `haFullStack-HLD.md` §4.3
(Messaging Redundancy).

**Symptom:** Same shape as F-001 — the original draft specced 2 RabbitMQ
nodes with quorum queues.

**Root cause:** Quorum queues use Raft, which has the identical
majority-quorum requirement as Group Replication's Paxos — a 2-node
quorum-queue cluster tolerates zero node failures for the same reason as
F-001. Flagged proactively (by symmetry with F-001) rather than found
independently.

**Fix:** Bumped to 3 RabbitMQ nodes throughout `haFullStack.md` §6 and
`haFullStack-HLD.md` §4.3, mirroring the MySQL fix.

---

## Phase 1.A — Lab, OpenTofu

Found while building and empirically testing `examples/ha-frontend-lb/`
against this CloudCore instance — see
`haFullStack-Phased-Implementation.md` Phase 1.A and `haFullStack-LLD.md`
§1.3 for the slice these belong to. F-003–F-006 are CloudCore platform
gaps (fixed in `api/setup-network.sh`, apply to *every* bridged-networking
template on this platform, not just this one); F-007–F-009 are specific
to this template's own design.

### F-003 — Bridged instances silently fall back to SLIRP without `/etc/qemu/bridge.conf`

**Where:** `api/compute.py`'s `_bridge_usable()`.

**Symptom:** Instances created against a VPC/subnet expected to be
bridged (`ccbr0`) came up with SLIRP addressing (`10.0.2.15`) instead,
with no error.

**Root cause:** `_bridge_usable()` requires `/etc/qemu/bridge.conf` to
contain `allow ccbr0` (or `allow all`) before libvirt's QEMU driver will
attach a guest NIC to the bridge. Without it, every instance silently
falls back to SLIRP — no error surfaced anywhere in the stack.

**Fix:** `api/setup-network.sh` now creates `/etc/qemu/bridge.conf` with
`allow ccbr0` if missing (idempotent — checked with `grep -qxF` first).

**Verified by:** Real instance creation against a bridged subnet,
confirmed `private_ip` was a real `192.168.100.x` address, not
`10.0.2.15`.

---

### F-004 — `qemu-bridge-helper` needs `CAP_NET_ADMIN` under session-mode libvirt

**Where:** libvirt session-mode QEMU driver.

**Symptom:** Bridged instance creation failed outright: "failed to
create tun device: Operation not permitted"; instance ended up
`stopped`.

**Root cause:** This platform's libvirt runs QEMU in session mode (as
the invoking user, not root), so there's no ambient `CAP_NET_ADMIN` for
`qemu-bridge-helper` to create the guest's tun device even with
`bridge.conf` correctly configured (F-003).

**Fix:** `api/setup-network.sh` now runs
`setcap cap_net_admin+ep <qemu-bridge-helper path>` if the capability
isn't already set (checked via `getcap` first, so idempotent).

**Verified by:** Real bridged instance creation succeeding end to end
after the fix, where it had failed with the exact error above beforehand.

---

### F-005 — Docker's iptables FORWARD policy silently drops all `ccbr0` traffic

**Where:** Host iptables `FORWARD` chain.

**Symptom:** A bridged instance got a DHCP IP and appeared to configure
correctly, but had **zero** connectivity — not even ICMP to the gateway
address it was itself configured with. `ip_forward=1` was set and the
NAT/MASQUERADE rule for the bridge subnet was correctly in place.

**Root cause:** This host runs Docker, which manages the `FORWARD`
chain's default policy as `DROP` and only allowlists its own bridges —
confirmed via `iptables -L FORWARD -n -v` showing `policy DROP`. NAT only
rewrites source IP in `POSTROUTING`, which runs *after* the `FORWARD`
chain's accept/drop decision, so the MASQUERADE rule never even gets a
chance to apply — traffic is dropped before it gets there. This is
entirely independent of F-003/F-004: those two block bridged instance
*creation*; this one blocks bridged instance *networking* even once
creation succeeds.

**Fix:** `api/setup-network.sh` now inserts (`-I`, not `-A` — has to win
over Docker's earlier DROP-oriented rules already in the chain) explicit
`iptables -I FORWARD -i ccbr0 -j ACCEPT` and the `-o ccbr0` equivalent.

**Verified by:** Direct ICMP test isolating the failure to raw
connectivity before DNS was ever in the picture (`ping 8.8.8.8` 100%
loss vs. `ping 192.168.100.1` 0% loss), then confirmed fixed after the
two `iptables -I FORWARD` rules were added.

---

### F-006 — Bridged guests got no DNS resolver at all

**Where:** `api/setup-network.sh`'s `dnsmasq` invocation (DHCP only).

**Symptom:** Once F-003/F-004/F-005 were fixed, bridged instances had
real IP connectivity but all name resolution failed — `apt-get install`,
`curl` against a hostname, anything needing DNS.

**Root cause:** The dnsmasq instance serving DHCP on `ccbr0` was
DHCP-only (`--no-resolv`, no DNS proxying) and handed out no DNS server
option at all, so guests had an IP and a route but nothing to resolve
names with. Two red herrings ruled out before finding this: cloud-init's
`manage_resolv_conf`/`resolv_conf` module (silently no-ops on Ubuntu
22.04's systemd-resolved) and the ordering of `packages:` vs. `runcmd:`
in cloud-init (`packages:` runs first — a DNS fix placed in `runcmd:`
can't help a `packages:`-declared install regardless of whether DNS
itself is correctly fixed).

**Fix:** Added `--dhcp-option="option:dns-server,8.8.8.8,1.1.1.1"` to the
dnsmasq invocation in `api/setup-network.sh`.

**Verified by:** A clean throwaway bridged instance completing an
`apt-get install` via a plain `packages:` cloud-config block with zero
manual workarounds — the same mechanism `examples/ha-frontend-lb`'s
cloud-init now relies on directly.

---

### F-007 — `private_ip` permanently stuck on the SLIRP placeholder for bridged instances

**Where:** `api/compute.py`'s `get_instance_ip()`; `api/server.py`'s
`get_instance()`.

**Symptom:** A bridged instance's `private_ip`, read back via
`GET /v1/instances/{id}` minutes after boot (DHCP long since complete),
stayed at the literal string `"10.0.2.15"` — the SLIRP fallback address
— rather than its real bridged address.

**Root cause:** `_launch()` (`api/server.py`) calls `get_instance_ip()`
immediately after the domain starts, before DHCP can possibly have
completed. For a bridged instance at that moment, `get_instance_ip()`
found the MAC in the domain XML but no matching lease yet, and fell
through to `return "10.0.2.15"` — a *truthy* value. That got stored as
`instance.private_ip`, and `get_instance()`'s refresh-on-GET logic only
re-attempts the lookup `if not instance.private_ip` — a non-empty string
is never falsy, so the stale placeholder was never retried, permanently.

**Fix:** `get_instance_ip()` now distinguishes "this domain has no
bridge interface at all" (genuinely SLIRP → `"10.0.2.15"` is correct)
from "this domain has a bridge interface but no lease yet" (→ returns
`""`, matching the "unknown yet, please retry" convention the rest of
the codebase already expects).

**Verified by:** Empirical poll of a real bridged instance every 10s:
`private_ip` was `""` at t=10s (status already `running`), then
`192.168.100.224` at t=20s and stable for the following 90s.

---

### F-008 — OpenTofu provider's `Create()` didn't wait for `private_ip` before considering an instance ready

**Where:** `provider/internal/resources/instance.go`, `InstanceResource.Create()`.

**Symptom:** Even with F-007 fixed at the API level, a bridged
instance's `private_ip` computed attribute in Terraform state could
still end up empty — meaning a *downstream* resource referencing it
(e.g. another instance's `user_data` interpolating
`module.frontend.private_ips_by_key`) would render with a blank IP.

**Root cause:** `Create()`'s poll loop returned as soon as
`poll.Status == "running"`. Empirically, status flips to `running`
*before* DHCP completes on this platform — so the very first "running"
observation could (and did) still have an empty `private_ip`, and
`Create()` stopped polling right there, baking that empty value into
state permanently (`Read()` never gets called again for an attribute
Terraform already considers final within the same apply).

**Fix:** The poll condition is now
`poll.Status == "running" && poll.PrivateIP != ""` — bounded by the same
overall `createTimeout` as before, so a genuinely stuck instance still
fails the same way it did previously, it just also waits out DHCP for a
healthy one.

**Verified by:** This is the mechanism that makes
`examples/ha-frontend-lb`'s design work at all — `module.nginx`'s
`user_data` interpolates `module.frontend.private_ips_by_key` directly,
with no lease-file reads, `local-exec`, or extra API surface needed.
Confirmed via a real `tofu apply`: both frontend instances' `private_ip`
were real, non-empty addresses by the time `module.frontend` finished
applying, and the NGINX nodes' rendered `upstream frontend {}` block
(checked via `tofu console`) had both real IPs baked in.

---

### F-009 — `templatefile()`/cloud-init YAML broke on `indent()` and inline `runcmd` quoting

**Where:** `examples/ha-frontend-lb/files/nginx-cloud-init.yaml.tftpl`.

**Symptom:** First real `tofu apply` of the finished template failed
both NGINX instances with "Instance entered error state." Server log
showed a YAML parse error: `while scanning a simple key ... could not
find expected ':'` pointing at the `upstream frontend {` line.

**Root cause:** Two independent mistakes in the same file:
1. `${indent(6, nginx_conf)}` was used directly after a `content: |`
   line with no leading whitespace of its own. OpenTofu's `indent()`
   function indents *only* lines after the first — by design, meant for
   embedding right after some prefix text on the same source line — so
   the first line of `nginx_conf` (`upstream frontend {`) landed at
   column 0, breaking the YAML block scalar's indentation.
2. A `runcmd:` entry was a raw, unquoted shell one-liner containing
   `awk -F': ' '...{print $2; exit}'` — the colon-quote and brace
   characters aren't safe inside an unquoted YAML plain scalar.

**Fix:** (1) Added the 6 literal leading spaces before `${indent(6,
...)}` so the first line is indented the same as `indent()` indents the
rest. (2) Moved the interface-detection/`sed` logic out of `runcmd`
entirely into its own script file
(`/usr/local/sbin/render-keepalived-conf.sh`, written via `write_files`
as a plain literal block — which isn't subject to plain-scalar quoting
rules), with `runcmd` reduced to just calling it.

**Verified by:** Rendered the actual `user_data` via `tofu console`,
parsed it with `yaml.safe_load()` directly (catching the same class of
error before spending another real `tofu apply` cycle on it), then
re-ran `tofu apply` for real — both NGINX instances built cleanly,
`cloud-init status` returned `done`, and `nginx`/`keepalived` were both
`active` on both nodes.

---

## Platform Hardening (post-Phase 1.A review)

A deliberate pause before starting Phase 1.B to ask "what would CloudCore
itself benefit from before we build on top of it again" — not tied to a
specific LLD section, since these are all platform-level gaps rather than
this slice's own design. Two were suspected from re-reading Phase 1.A's
own server logs; digging into the DNS one in particular (flagged as worth
taking seriously rather than deferring) surfaced three more, underneath
the one originally suspected.

### F-010 — Bridge-mode security-group enforcement silently fails 100% of the time, not just under concurrency

**Where:** `api/sg.py`'s `_run()`.

**Symptom:** Re-reading Phase 1.A's own apply log turned up
`iptables -N CC-SG-... returned non-zero exit status 4` for both frontend
instances, logged as `"Failed to create instance: ..."` even though both
instances reached `running` normally. Initially assumed (and reported as)
an `xtables` lock-contention race from the two instances being created
concurrently.

**Root cause:** Reproducing it directly showed otherwise — `iptables -N`
run exactly as the API server runs it fails **every single time**. exit
code 4, `"Permission denied (you must be root)"`. The API server process
runs unprivileged; nothing in `api/sg.py`'s host-level iptables calls
ever had a path to root. This isn't a concurrency bug at all — bridge-mode
security-group enforcement has never once actually applied on this host,
for any bridged instance, concurrent creation or not. It only stayed
hidden this long because every earlier bridged-mode template that used
security groups was actually still running SLIRP at the time (bridging
wasn't reliable until F-003–F-006), and SLIRP's enforcement path
(`apply_slirp`) runs entirely inside the guest over SSH — a different
mechanism, unaffected by this.

**Fix:** `_run()` now invokes every host-level iptables/ip6tables call as
`sudo -n <cmd>` (`-n` fails fast with a clear error instead of hanging a
background thread on a password prompt that can never arrive). A
tightly-scoped `NOPASSWD` sudoers rule for exactly `iptables`/`ip6tables`
(never blanket root) is provisioned by `api/setup-network.sh`, matching
its existing idempotent check-then-fix pattern.

**Verified by:** Real instance with a security group attached: confirmed
the `CC-SG-<id>` chain exists on the host with the correct MAC-matched
rule, referenced from `FORWARD` position 1, matching the instance's real
domain MAC exactly (`sudo -n iptables -L -n`).

---

### F-011 — Instance error states carried no reason via the API

**Where:** `api/server.py`'s `_launch()`; `api/models.py`'s `Instance`.

**Symptom:** When F-009's malformed `user_data` broke instance creation,
the only way to find out *why* was `grep`-ing the server's log file
directly — `GET /v1/instances/{id}` just reported `status: "error"` with
no further detail.

**Root cause:** Never captured anywhere. `_launch()`'s catch-all `except`
logged the exception and set `status = ERROR`, but nothing persisted the
message onto the instance itself.

**Fix:** Added `error_message` to the `Instance` model (migrated via the
existing `_migrate_columns()` pattern in `api/db.py`) and split
`_launch()`'s single try/except into two: a hard failure before the VM
exists (`compute.create_instance()` itself) sets `status = ERROR` and
`error_message`; a failure in a step *after* the VM is already up (DNS
registration, SG enforcement) only sets `error_message` — deliberately
not `status`, since `get_instance()`'s live libvirt status check would
silently overwrite an `ERROR` status back to the true state on the very
next poll anyway (it trusts libvirt over the stored value), making that
status flip pure noise; `error_message` is what actually survives and is
worth surfacing.

**Verified by:** A real instance with no failures showed
`"error_message": ""` in its API response; the field round-trips through
the DB migration correctly (confirmed via `PRAGMA table_info(instances)`
on the live DB).

---

### F-012 — CloudCore's own internal DNS server has never actually started

**Where:** `api/dns_server.py`'s `start()`.

**Symptom:** Repeated `dns_server: dnsmasq failed to start: dnsmasq:
junk found in command line` in the server log during every instance
creation, throughout this entire session — present but not investigated
until this review.

**Root cause:** `start()` invoked
`subprocess.run(["dnsmasq", "--conf-file", str(_CONF_PATH)], ...)` — two
separate argv tokens. dnsmasq's `-C`/`--conf-file` option does not accept
the space-separated form (confirmed directly: identical invocation with
`--conf-file=<path>` succeeds, `--conf-file <path>` fails with exactly
this error, every time). This means CloudCore's own DNS resolution
(`127.0.0.1:5353`, `*.instances.cloudcore.local`) has never actually been
up — a `dnsmasq: syntax check OK` on the generated config file (`--test`)
never caught it, since the config was never the problem.

**Fix:** Changed to the single-token `f"--conf-file={_CONF_PATH}"` form.

**Verified by:** Direct reproduction of the failure and the fix in
isolation, then confirmed dnsmasq starts cleanly under the real API
server and a PTR query resolves correctly.

---

### F-013 — A long-lived dnsmasq process eventually refuses queries after enough SIGHUP reloads

**Where:** `api/dns_server.py`'s `reload()`.

**Symptom:** Found immediately after fixing F-012: once dnsmasq was
actually starting, DNS queries for a real instance's own A-record came
back `REFUSED` (EDNS extended error 14, "Not Ready") — with the WARNING
about `.local` being mDNS-reserved space initially looking like the
likely cause.

**Root cause:** Ruled out `.local` entirely — an identical, byte-for-byte
copy of the exact same config file, loaded by a **freshly started**
dnsmasq process, answered the same query correctly every time, including
after being manually SIGHUP'd 8 times in a row. The only reproducible
difference was the process's own history: the real, long-running
`dns_server`-managed dnsmasq — reloaded many times over the session via
SIGHUP on every DNS record change — refused a query that a fresh process
given the identical config answered instantly. Killing and restarting
that exact process against its own on-disk config immediately fixed it.
The precise internal dnsmasq state behind this wasn't pinned down (it
isn't config-related, and isn't tied to `.local` specifically), but the
practical trigger is unambiguous: process longevity + repeated SIGHUP
reloads, not any particular record or query.

**Fix:** `reload()` now does a full `stop()` + `start()` on every DNS
record change instead of `SIGHUP`. dnsmasq starts in well under a second,
so the resolution gap this trades away is negligible against the
alternative — silently going permanently unresponsive after enough
routine reloads, which is a much worse failure for something whose whole
job is being reliably queryable. This is exactly the kind of issue that
would stay invisible in short testing and only surface in a longer-lived
real deployment.

**Verified by:** Reproduced the failure, confirmed a full restart against
the identical config immediately fixed it, applied the code fix, restarted
the real server, and confirmed DNS resolution kept working correctly
across further real record changes with no manual intervention.

---

### F-014 — DNS A-records had the same launch-time stale-value bug `private_ip` had (F-007)

**Where:** `api/server.py`'s `_launch()` and `get_instance()`.

**Symptom:** Once F-012/F-013 were fixed and DNS was actually
resolvable, a real instance's own A-record still resolved to `127.0.0.1`
instead of its real bridged IP — indefinitely, for the instance's entire
lifetime, unless the whole API server happened to restart (its startup
reconciliation loop re-registers every instance's DNS record with its
current known IP as a side effect, which is how this stayed hidden during
earlier ad-hoc testing).

**Root cause:** The exact same pattern as F-007, one layer up:
`_launch()` registers the DNS A-record with
`ip or "127.0.0.1"` at the same moment it fetches `private_ip` — before
DHCP has necessarily completed for a bridged instance. Unlike
`private_ip`, which self-corrects because `get_instance()` retries the
lookup on every GET while it's still falsy, nothing ever re-registered
the DNS record once the real IP became known.

**Fix:** `get_instance()`'s existing self-correcting `private_ip` refresh
now also re-upserts the DNS A-record with the real IP, once, the moment
it successfully resolves one — piggybacking on a mechanism that already
existed rather than adding a new poll/background job.

**Verified by:** Created a real instance, confirmed its DNS record read
`127.0.0.1` immediately after creation, called `GET /v1/instances/{id}`
once `private_ip` had a real value, confirmed the DNS record was
rewritten to the real IP in the same call, and confirmed `dig` resolved
it correctly — all without restarting the server.

---

## Phase 2.A — Lab, OpenTofu (Database Tier)

Found building and empirically fixing the real 3-node MySQL Group
Replication cluster + ProxySQL + NGINX `stream{}` extension — see
`haFullStack-Phased-Implementation.md` Phase 2.A and `haFullStack-LLD.md`
§2 for the slice these belong to. F-015 is a platform-level bug (affects
*any* bridged instance with a restrictive security group, not just this
slice); F-016–F-019 are MySQL-Group-Replication-specific bootstrapping
gotchas, found in the order a real deploy hits them.

### F-015 — Bridge-mode security groups silently block all general internet egress

**Where:** `api/sg.py`'s `apply_bridge()`.

**Symptom:** Every instance in this slice with a security group attached
(mysql, proxysql, frontend — anything without a `-1`-protocol rule
scoped broadly enough to incidentally cover it) lost internet
connectivity partway through boot: `ping 8.8.8.8` 100% loss while
`ping 192.168.100.1` (gateway) stayed fine — the same signature as
F-005, but F-005 was already fixed and this was a different cause.
`apt-get`/`curl` inside cloud-init's `packages:`/`runcmd:` failed with
`Temporary failure resolving '...'` or outright connection failures.

**Root cause:** iptables' `xt_mac` module only supports `--mac-source`
matching (`man iptables-extensions` — there is no destination-MAC
match), so the single MAC-matched `FORWARD`-chain jump `apply_bridge()`
installs can only ever intercept traffic *leaving* the instance, never
traffic arriving at it. The function built that chain from
`ingress_rules` and never referenced `egress_rules` at all — meaning an
explicit, permissive `egress_rules` entry (`-1` protocol, `0.0.0.0/0`)
had **zero effect**, and general outbound traffic (DNS, HTTPS — anything
not matching one of the SG's narrowly port/protocol-scoped ingress
rules) hit the chain's default `DROP`. This had stayed invisible through
every earlier build this session by pure luck: Phase 1's `nginx` SG has
a `-1`-protocol VRRP rule scoped to the bridge subnet, which incidentally
allows all egress too (protocol `-1` matches everything, and the
instance's own source IP is inside that CIDR) — nothing before this
slice had a security group *without* an accidental wildcard-protocol
rule.

**Fix:** `apply_bridge()` now builds its chain from `egress_rules`
(direction `"egress"`, so `_iptables_rule_args` correctly emits `-d
<cidr>` — matching packet destination, not source, which is what
"where can this instance send traffic to" actually means). `ingress_rules`
is accepted for API-shape symmetry but has no bridge-mode enforcement
path — true ingress filtering isn't achievable with a MAC-based match at
all (no destination-MAC match exists) and would need destination-IP
matching instead, which needs the instance's `private_ip` already known
(not guaranteed the first time this runs — see F-007) plus
re-application once it is. Flagged, not fixed here.

**Verified by:** Re-applied the fixed logic to all 7 already-running
instances (`sg.apply()` via each instance's already-stored
`security_group_ids`, no recreation needed) and confirmed `ping 8.8.8.8`
immediately succeeded on every previously-blocked instance.

---

### F-016 — Group Replication config variables rejected with "unknown variable" on a fresh install

**Where:** `examples/ha-frontend-lb/files/mysql-cloud-init.yaml.tftpl`.

**Symptom:** `systemctl restart mysql` failed on every MySQL node,
restart-looping. `/var/log/mysql/error.log`: `[ERROR] unknown variable
'group_replication_group_name=...'`.

**Root cause:** `group_replication_*` are plugin-defined system
variables, not core server variables — mysqld rejects them outright if
the `group_replication` plugin isn't already loaded. `INSTALL PLUGIN`
(the SQL command that registers a plugin for future restarts) can't run
before mysqld has successfully started even once — a genuine
chicken-and-egg on a completely fresh install, where the plugin has
never been installed before.

**Fix:** Added `plugin_load_add = group_replication.so` to the config —
this loads the plugin directly from its `.so` file at mysqld startup,
independent of the `mysql.plugin` table `INSTALL PLUGIN` writes to.
Removed the now-redundant (and, once the plugin auto-loads, actively
erroring) `INSTALL PLUGIN group_replication SONAME '...'` SQL statement.

**Verified by:** `systemctl restart mysql` succeeded cleanly with the
fix in place, on all 3 nodes.

---

### F-017 — Distributed recovery connects to the donor by hostname, which guests can't resolve

**Where:** `examples/ha-frontend-lb/files/mysql-cloud-init.yaml.tftpl`.

**Symptom:** Joining nodes reached `RECOVERING` and stuck there
indefinitely. Error log: `Unknown MySQL server host 'example-dev-mysql-a'
(-3)`.

**Root cause:** Group Replication's low-level gossip (over
`group_replication_local_address`, IP:port, configured directly) worked
correctly, but the separate distributed-recovery channel connects to the
donor using the OS hostname reported in `MEMBER_HOST`
(`performance_schema.replication_group_members`) — and these guest VMs
have no way to resolve each other's hostnames: no `/etc/hosts` entries,
and CloudCore's own DNS server isn't reachable from inside a guest (it
binds host-loopback-only, see F-012's fix — that only helps host-side
resolution).

**Fix:** Added `report_host = "$LOCAL_IP"` (the same runtime-detected IP
already used for `group_replication_local_address`) — this makes every
member report and connect to others via a real IP instead of its OS
hostname, for both `MEMBER_HOST` and the recovery connection target.

**Verified by:** `MEMBER_HOST` changed from hostnames to real IPs after
the fix; the specific "Unknown MySQL server host" error stopped
recurring in the error log on the next join attempt.

---

### F-018 — `bind-address` silently overridden by Ubuntu's own shipped config, by file-load order

**Where:** `examples/ha-frontend-lb/files/mysql-cloud-init.yaml.tftpl`.

**Symptom:** After fixing F-017, joins still failed — but differently:
`Can't connect to MySQL server on '192.168.100.81:3306' (111)`
(connection refused, not a resolution failure). `ss -tlnp` on the
donor showed `mysqld` listening on `127.0.0.1:3306` only, despite the
cloud-init explicitly setting `bind-address = 0.0.0.0`.

**Root cause:** `/etc/mysql/mysql.conf.d/*.cnf` files load in
lexicographic order, later files' values winning for the same variable.
Ubuntu's own `mysql-server` package ships `mysqld.cnf` with
`bind-address = 127.0.0.1` as a security-conscious default. This
cloud-init's own file was named `group_replication.cnf` — alphabetically
*before* `mysqld.cnf` — so Ubuntu's own loopback-only default silently
won, regardless of what this file said.

**Fix:** Renamed the written file to `zz-group-replication.cnf`, so it
sorts and loads after `mysqld.cnf` and its `bind-address = 0.0.0.0`
actually takes effect.

**Verified by:** `ss -tlnp` showed `mysqld` listening on `0.0.0.0:3306`
on all 3 nodes after the rename + restart; the connection-refused error
stopped recurring.

---

### F-019 — `caching_sha2_password` refuses authentication without a secure connection

**Where:** `examples/ha-frontend-lb/files/mysql-cloud-init.yaml.tftpl`.

**Symptom:** After fixing F-018, joins reached the actual authentication
step and failed there: `Authentication plugin 'caching_sha2_password'
reported error: Authentication requires secure connection.`

**Root cause:** MySQL 8.0's default authentication plugin,
`caching_sha2_password`, refuses to authenticate a non-TLS connection
outright (rather than degrading gracefully) unless the client already
has the password cached from a prior secure session. No TLS/cert
distribution is set up between nodes in this Lab prototype, so the
recovery channel's very first connection attempt always hits this.
ProxySQL has also historically had limited `caching_sha2_password`
support for its own backend connections, so the same issue would likely
have resurfaced for `appuser`/`proxysql_monitor` even if the recovery
channel itself hadn't hit it first.

**Fix:** `repl`, `proxysql_monitor`, and `appuser` are all created with
`IDENTIFIED WITH mysql_native_password` instead of the 8.0 default — an
explicit, documented Lab-only simplification (no TLS set up), not
appropriate for a production deployment, which should set up proper TLS
between nodes and keep `caching_sha2_password`.

**Verified by:** All 3 MySQL nodes reached `ONLINE` with exactly one
`PRIMARY` after the fix; ProxySQL's own `runtime_mysql_servers` correctly
auto-detected the primary via `mysql_group_replication_hostgroups`; the
frontend status page showed a live, advancing heartbeat counter through
the full real path (VIP → NGINX `stream{}` → ProxySQL → MySQL) with
verdict `OK`.

---

### F-020 — The bootstrap node can't rejoin the group on its own after a restart

**Where:** `examples/ha-frontend-lb/files/mysql-cloud-init.yaml.tftpl`;
found during failure-mode test 2A-12 (stop the primary), when restoring
mysql-a afterward.

**Symptom:** mysql-b (a joiner node) restarted and rejoined cleanly with
just `START GROUP_REPLICATION` (test 2A-14). mysql-a (the original
bootstrap node) restarted after being stopped in test 2A-12, and the same
`START GROUP_REPLICATION` failed outright: `[GCS] Unable to join the
group: peers not configured.`

**Root cause:** This was already flagged as a known simplification in
§2.3.2/§2.7 (the seed-address self-reference problem: node "a" is created
before nodes "b"/"c" exist, so it can never know their IPs at its own
`cloud-init` time) — but its real-world consequence wasn't obvious until
hit empirically: node "a"'s `group_replication_group_seeds` stays `""`
for its *entire lifetime*, not just its first boot. Nodes "b"/"c" always
carry a real, permanent seed (node "a"'s IP, baked in once and always
valid, since "a" — being the original bootstrap node — is stable
infrastructure in this topology), so *their* rejoin-after-restart path
works unconditionally. Node "a" has no such permanent anchor: with an
empty seed list, it can only ever *bootstrap a brand-new group*, never
*find and rejoin an existing one* — a real, asymmetric gap between the
bootstrap node and the joiners, not a transient issue.

**Fix applied for now (manual, not templated):** `SET GLOBAL
group_replication_group_seeds = '<b-ip>:33061,<c-ip>:33061';` before
`START GROUP_REPLICATION` — a runtime-settable global variable, no
config file edit or restart needed. This is **not** fixed in the source
template — doing so properly needs either a remotely-accessible,
sufficiently-privileged account (current `repl` user only has
`REPLICATION SLAVE`/`BACKUP_ADMIN`/`SELECT on performance_schema`, not
`SYSTEM_VARIABLES_ADMIN`) so joiners can update the bootstrap node's seed
list as they join, or an equivalent mechanism — real added complexity
and its own security tradeoff (a remote account with `SUPER`-adjacent
privilege), deliberately not taken on without a decision to do so.
Flagged as an open item (§2.7), not silently worked around.

**Verified by:** mysql-a reached `ONLINE`/`SECONDARY` (correctly not
re-claiming `PRIMARY` — single-primary mode doesn't reassign it just for
rejoining) within 8s of the manual seed-list fix; full 3-node membership
restored.

---

### F-021 — The whole point of test 3 didn't hold: the survivor doesn't refuse writes after losing quorum

**Where:** `haFullStack.md` §5.3's failure-mode table (now corrected);
found running failure-mode test 3 (2A-13, stop 2 of 3 nodes).

**Why this one matters more than the others:** every other finding in
this log is a platform quirk or a bootstrapping gotcha — this one is a
correction to the architecture document's own core safety claim, the
thing the entire 3-node design (F-001) exists to provide. It was wrong
as originally written and stayed wrong through two rounds of review
before being caught here, empirically, by actually running the test
rather than trusting the documented failure table.

**Symptom:** Stopped `mysqld` on 2 of 3 nodes simultaneously (mysql-a
and mysql-b), leaving mysql-c (the then-current primary) alone.
Expected, per `haFullStack.md` §5.3 as originally written: "the
surviving node cannot certify writes and drops out of `ONLINE` state."
Actual: mysql-c stayed `ONLINE`/`PRIMARY` and kept accepting writes
indefinitely — a direct `INSERT` against it, run repeatedly over several
minutes, always returned in well under a second, exit code 0, no
blocking, no error.

**Root cause:** Group Replication's failure detector doesn't just mark
unreachable peers as unavailable — after `group_replication_member_expel_timeout`
(default 5s, already elapsed by the time this was checked) it formally
**expels** them from its own membership view and reconfigures to a
smaller group. Once reconfigured, majority is evaluated against the
*new, smaller* group — a group of 1 trivially has majority of itself.
`group_replication_unreachable_majority_timeout` (the setting that
sounds like it should prevent exactly this) only governs the *window
before* expulsion completes, confirmed by setting it live (from its
default of `0`/infinite to `10`) mid-test and observing writes kept
succeeding instantly regardless — the reconfiguration had already
happened before the setting was ever touched, and the setting doesn't
retroactively un-shrink an already-reconfigured group.

**Consequence — why this is a real risk, not just a documentation bug:**
for a genuine node-*death* scenario (what was actually tested here — the
other two `mysqld` processes were stopped, not partitioned), there's no
data-divergence risk, since the "lost" nodes aren't writing anywhere.
But the identical mechanism removes the safety property for a **network
partition**: if the 2 "unreachable" nodes are still alive and can still
reach each other on the other side of a split, they also hold a majority
of the *original* group of 3 and could independently elect their own
primary and keep accepting writes — two primaries, genuinely diverging
data, with nothing in Group Replication's default configuration
preventing it. This is precisely the split-brain scenario a 3-node
majority-quorum design is supposed to rule out (F-001) — and by default,
it doesn't.

**What this validated, as a genuine design win:** the frontend status
page's own diagnostic logic (§2.3.1b) correctly flagged this as
`CRITICAL` the entire time — it counts *rows returned by the membership
query* against the *expected* 3, not whatever Group Replication itself
currently considers "the group," so it correctly showed `1 of 3 ONLINE`
throughout. The monitoring correctly detected the dangerous condition;
MySQL itself just didn't act on it.

**Fix — implemented:** `quorum-watchdog.py`, a systemd-timer-driven
script (every 3s) running locally on **every** MySQL node, root via the
Unix socket — no new remote-accessible privileged account, so no
meaningful increase in attack surface. If the local node is `ONLINE`
`PRIMARY` of a group whose `ONLINE` member count has dropped below the
*original* cluster's majority (2 of 3), it sets `SET GLOBAL
super_read_only = ON`; once original membership is restored, it clears
it again. Deliberately local-only and per-node — it directly targets the
exact minority-side gap F-021 describes, without needing a remote
watchdog, a new credential, or STONITH-style fencing.

**A second bug found building the fix itself:** the first version only
cleared `super_read_only`, which does **not** automatically clear the
separate `read_only` flag (`super_read_only=ON` implies `read_only=ON`,
but the reverse doesn't hold). With `read_only` left `ON`, ProxySQL's own
GR monitor never re-added the node to the writer hostgroup even though
`super_read_only` correctly showed `OFF` and Group Replication itself
showed full `ONLINE`/`PRIMARY` — even a full `LOAD MYSQL SERVERS TO
RUNTIME` and a ProxySQL service restart didn't fix it, confirming this
wasn't a caching issue but the watchdog leaving a genuinely different
flag still set. Fixed by clearing `super_read_only` before `read_only`
(MySQL won't allow `read_only=OFF` while `super_read_only` is still
`ON`) — both, in that order, every cycle.

**Verified by:** Ran the full below-quorum cycle twice. First run
(before the `read_only` fix): `super_read_only`/`read_only` both
correctly flipped `ON` within ~15s of the 2-node loss, a direct write
correctly failed with the `super-read-only` error, and — independently —
ProxySQL's own GR monitor also pulled the node out of the writer
hostgroup entirely (defense in depth: two independent layers both
refusing writes). Restoring membership did *not* self-heal ProxySQL's
routing (the `read_only` bug). Second run (after the fix), same below-
quorum sequence, then full recovery watched passively — zero manual `SET
GLOBAL`/`LOAD MYSQL SERVERS` commands — and both `super_read_only` and
`read_only` correctly cleared automatically, ProxySQL automatically
re-added the node to the writer hostgroup, and the frontend page returned
to `OK` with the heartbeat counter advancing again, entirely on its own.

**Verified by:** Repeated direct writes against the lone survivor,
timestamped before/after, consistently succeeding in under a second with
no blocking, both before and after setting
`group_replication_unreachable_majority_timeout` live. Cross-checked
against `performance_schema.replication_group_members` (showing only 1
row — the survivor itself, already reconfigured) and
`replication_group_member_stats` (`COUNT_CONFLICTS_DETECTED: 0`,
transaction count climbing normally, consistent with the survivor
operating as a fully healthy single-node group rather than a
blocked/degraded 3-node one).

---

## Platform Hardening (post-Phase 2.A review)

Same pattern as the post-Phase 1.A review — a pause before starting
Phase 3 to fix platform-level gaps rather than carry them forward.
Motivated directly by lessons from the Database Tier build (F-017's
hostname-resolution gap in particular); the SQLite and help-articles
findings were surfaced incidentally while sanity-checking the DNS work
against the full test suite, not sought out deliberately.

### F-022 — CloudCore's DNS was confirmed working but guests couldn't reach it

**Where:** `api/setup-network.sh`, `api/dns.py`, `api/dns_server.py`,
`api/server.py`.

**Symptom:** CloudCore's own DNS server was confirmed working by
F-012–F-014, but only via `127.0.0.1:5353` on the host — nothing inside
a guest VM could resolve a CloudCore-managed hostname at all. This is
the same class of gap F-017 hit directly (MySQL's distributed recovery
connecting to a donor by hostname, unresolvable from inside a guest);
fixing it at the platform level means the next slice that needs
guest-to-guest hostname resolution doesn't have to work around it again.

**Fix:** The bridge's own DHCP dnsmasq (`setup-network.sh`) now also
serves DNS to guests — handing out itself as the DNS server and
forwarding CloudCore's own zones to the existing API dnsmasq
(`127.0.0.1:5353`), with public DNS kept as the default catch-all so
normal internet resolution (`apt`, `curl`, ...) is unaffected.

**A second issue found building the fix:** guest-side resolution worked
via a direct `dig` against the bridge dnsmasq, but failed (`SERVFAIL`)
through the guest's own default resolver (`systemd-resolved`'s stub at
`127.0.0.53`). Root cause: CloudCore's zones used a `.local` suffix,
which RFC 6762 reserves for mDNS — `systemd-resolved` refuses to forward
genuine unicast queries for it regardless of configured DNS server, by
design, not a bug in this fix. Fixed by renaming CloudCore's zone suffix
from `.local` to `.internal` — the suffix RFC 9476 actually reserves for
this purpose — throughout the codebase (source, tests, UI, live DB),
rather than working around the resolver's correct behavior.

**Verified by:** `getent hosts` and `dig` through the guest's own default
resolver correctly resolving an instance's real private IP end-to-end;
external DNS (`google.com`) still resolving correctly through the same
path; the host-side `dns_server.py` path unaffected.

---

### F-023 — Concurrent writes could hang or 500 with "database is locked" — and the first fix attempt could have hung worse

**Where:** `api/db.py`.

**Symptom:** `tests/run_tests.py --skip-vm` occasionally stalled
indefinitely under write-heavy load (confirmed genuine — zero CPU-time
progress across repeated checks, not just slow) or threw uncaught 500s
with `sqlite3.OperationalError: database is locked`.

**Root cause:** SQLite (WAL mode included) only ever allows one writer
at a time. Flask's dev server runs multi-threaded by default — `app.run()`
sets `threaded=True` itself; checking only Werkzeug's own lower-level
default (`False`) gives the wrong answer. CloudCore also spawns
background daemon threads for instance launch/destroy and SG
apply/remove (`server.py`'s `threading.Thread(..., daemon=True)` call
sites). Each thread gets its own SQLite connection via `get_db()`'s
existing thread-local pattern, and a `threading.Lock()` already defined
in `db.py` for exactly this coordination was dead code — never
referenced anywhere else in the file.

**Fix:** A `_SerializedConnection(sqlite3.Connection)` subclass,
installed via `sqlite3.connect(..., factory=...)`, that makes the
existing lock actually serialize writers. The first attempt — acquiring
and releasing the lock around each individual `execute()`/`commit()`
call — was insufficient: an INSERT opens an implicit transaction and
holds the real file-level write lock from `execute()` until the
*separate*, later `commit()` call, so releasing the Python lock in
between let another thread's connection slip in and hit the same error.
Fixed by tracking `Connection.in_transaction`: the lock is held across a
connection's calls once a transaction opens, released only on
`commit()`/`rollback()` (or immediately, for a plain read).

**A second bug found building the fix, before it shipped:** a failed
statement (see F-024) leaves `in_transaction` `True` with no automatic
rollback. Releasing the lock only when idle meant a connection left in
that state would hold the process-wide lock **indefinitely** if its
caller never explicitly rolled back — strictly worse than the original
bug, since `Lock.acquire()` has no timeout at all. Caught by
deliberately reproducing the exact scenario (a write that raises with no
`except`/rollback around it) before considering the fix complete, not by
accident. Fixed by releasing the lock unconditionally on any exception,
which degrades gracefully to SQLite's own native 30s busy-wait for that
one case instead of hanging forever.

**Verified by:** An 80-way concurrent-write stress test directly against
the running server (0 errors, ~1s total — down from repeated 30s+
stalls/500s beforehand); a direct repro of the exception-path bug,
confirming it now fails after SQLite's native 30s timeout rather than
hanging indefinitely; a full `tests/run_tests.py --skip-vm` run to
genuine completion with 0 `database is locked` errors anywhere in the
server log (first two attempts at this looked complete but had actually
only captured a background launcher process exiting immediately, not the
real test run — caught by checking the process was still alive rather
than trusting an early "completed" signal).

---

### F-024 — `help_articles.slug` uniqueness didn't account for soft-deletes

**Where:** `api/db.py` schema, `api/help_store.py`, `api/help_routes.py`.

**Symptom:** Found while investigating F-023 — repeated `POST
/v1/help/articles` calls threw an uncaught 500
(`sqlite3.IntegrityError: UNIQUE constraint failed: help_articles.slug`)
during test runs. Each one left a dangling, never-committed-or-rolled-
back transaction (`help_store.put()` had no exception handling around
the insert), which — combined with F-023's bug above — is what cascaded
into the ~30s-per-test stalls seen through most of the rest of that test
run.

**Root cause:** `help_store.find_by_slug()`'s own duplicate-check
already excludes soft-deleted articles (`WHERE status != 'deleted'`),
but the schema's `slug TEXT NOT NULL UNIQUE` constraint applied to
*every* row regardless of status. A slug reused after its article was
soft-deleted passed the app-level pre-check (which assumed reuse was
fine) but still collided with the raw DB constraint (which didn't know
about soft-deletes at all).

**Fix:** Replaced the column-level `UNIQUE` with a partial unique index
scoped to `status != 'deleted'`, matching what `find_by_slug()` already
assumed — via a table-rebuild migration for existing databases (SQLite
can't drop a column-level `UNIQUE` via `ALTER TABLE`), forcing an FTS5
index rebuild afterward since rowids aren't guaranteed preserved across
the rebuild. `help_store.put()` now also catches the residual
race-condition case (two concurrent creates racing for the same free
slug) explicitly, rolls back, and raises a catchable error instead of
leaving a dangling transaction — routes return a clean 409.

**Verified by:** Migration run against the live DB (all 39 existing
articles, 21 of them soft-deleted, preserved exactly; FTS search still
functional); a manual create → soft-delete → recreate-with-same-slug
cycle through the real API (now `201`, previously an uncaught `500`);
confirmed a genuine still-*active* slug conflict still correctly returns
`409`; the full Help suite (25 tests) passing in the same complete test
run that verified F-023.

---

## Phase 3.A — Lab, OpenTofu (Identity Tier)

### F-025 — `random_id.b64_url` needs padding added before Keystone can use it as a Fernet key

**Where:** `examples/ha-frontend-lb/main.tf`'s `random_id.fernet_key0`/`fernet_key1`, `locals.tf`'s `keystone_user_data`.

**Symptom:** Every token-issuance request returned an uncaught 500. Apache's `keystone.log` traced it to `binascii.Error: Incorrect padding` inside `cryptography.fernet.Fernet.__init__`, called from Keystone's own Fernet token provider while loading the key material from `/etc/keystone/fernet-keys/0`.

**Root cause:** A 32-byte value base64-encodes to 43 characters — one short of the next multiple of 4 that standard base64 (including base64url) needs for valid padding. `random_id`'s `.b64_url` attribute outputs the unpadded 43-character form; Python's `base64.urlsafe_b64decode()` (used internally by the `cryptography` library) requires the padding and rejects unpadded input outright. `keystone-manage fernet_setup`'s own generated keys always carry the trailing `=`, which is what made the mismatch obvious once compared side by side against a real one from the earlier probe VM.

**Fix:** Append the padding directly where the key values are passed into the template: `fernet_key0 = "${random_id.fernet_key0.b64_url}="` (byte_length=32 always needs exactly one `=`, not a general-purpose padding calculation).

**Verified by:** Direct token issuance against both Keystone nodes after the fix, both returning `HTTP 201` with a real Fernet token.

---

### F-026 — cloud-init's `write_files` module runs *before* packages install

**Where:** `examples/ha-frontend-lb/files/keystone-cloud-init.yaml.tftpl`.

**Symptom:** `cloud-init status` reported `error`; `/usr/local/sbin/setup-keystone.sh` was never written to disk at all (`runcmd` failed with "not found"), despite appearing earlier in the same `write_files` list as the Fernet key files.

**Root cause:** The Fernet key file entries specified `owner: keystone:keystone`. cloud-init's `write_files` module runs before the `packages` stage, so the `keystone` system user (created by the `keystone` package's own postinst) doesn't exist yet at that point — confirmed directly via `cloud-init status --long`: `OSError('Unknown user or group: "getpwnam(): name not found: \'keystone\'"')`. A single failing entry aborts the **entire** `write_files` module, silently skipping every other file in the same list — including the setup script that was never actually broken itself.

**A related, secondary gap found while fixing this:** the Keystone nodes' `packages` list never included `mysql-client`, so `setup-keystone.sh`'s own MySQL-readiness wait-loop (`mysql -h ... -e "SELECT 1"`) silently failed every iteration (command not found) without ever confirming real readiness, exhausting its full 5-minute timeout on every boot regardless of whether MySQL was actually ready. Not a correctness bug — `db_sync` itself still connects independently via `pymysql` and succeeds on its own merits once actually invoked — but a real, wasteful gap, fixed by adding `mysql-client` to the package list.

**Fix:** Write the Fernet keys to a plain root-owned staging path (`/root/fernet-key-0`/`-1`, no `owner:`) in `write_files`, then `mkdir -p`, `mv`, `chown keystone:keystone`, and `chmod 600` them explicitly as the first steps of `setup-keystone.sh` in `runcmd` — which runs after packages are installed. Added `mysql-client` to the packages list for the wait-loop fix.

**Verified by:** `cloud-init status` reporting `done` (not `error`) on a fresh apply of both Keystone nodes; direct confirmation the Fernet keys landed at `/etc/keystone/fernet-keys/0`/`1` with correct `keystone:keystone` ownership and `0600` permissions.

---

### F-027 — Shared Fernet keys, not memcached, enable cross-node token validation

**Where:** `haFullStack.md` §7 (now corrected); resolved by failure-mode test 2 (3A-11).

**Symptom/question:** `haFullStack.md` §7 claimed memcached "is what allows either Keystone instance to validate a token issued by the other." `haFullStack-LLD.md` §3.1 flagged this as likely imprecise before building anything, arguing Fernet's self-describing bearer tokens need only shared key material — the same discipline that caught F-021 for the DB tier. Given its own standing (equivalent to 2A-13), this got a dedicated test rather than being assumed either way.

**Result:** A token issued directly against Keystone node A, with node A then **stopped entirely** (not just also-reachable), validated successfully (`HTTP 200`) directly against node B. Separately, stopping one and then both memcached nodes (tests 3/4) left basic token issuance and validation working throughout — slower while a dead memcached connection attempt was still being retried, but never a hard failure. This confirms the LLD's claim and `haFullStack.md` §7's original text is wrong.

**Fix:** `haFullStack.md` §7 corrected — memcached's actual role is caching validation results and propagating revocation state, not enabling cross-node validation at all; that's shared Fernet key material, which §7 now documents needing to be distributed identically to every node.

**Verified by:** Direct `HTTP 200`/`HTTP 201` responses for all four scenarios above, run against real instances with real stop/start cycles, not simulated.

---

### F-028 — Apache/mod_wsgi's own worker startup takes ~30s after the process itself is "active" — not a CloudCore platform gap

**Where:** Observed during failure-mode tests 1 and 5 (3A-10, 3A-14); investigated further and corrected afterward — application-level (Apache/mod_wsgi), not CloudCore's bridged networking as first suspected.

**Symptom:** After restarting a stopped instance, `ping` and `ssh` succeeded almost immediately, and `systemctl is-active apache2` (and `ss -tlnp`) confirmed the service was already listening — but the Keystone API (port 5000) stayed unreachable, refused rather than timing out, for roughly 1-2 minutes before starting to respond. Originally logged as a suspected platform-level TCP-reachability gap distinct from ICMP/SSH readiness, deliberately left un-root-caused pending further investigation.

**Root cause, found on follow-up:** Not a CloudCore or bridge networking issue at all. Reproduced with a clean, isolated instance: a plain `python3 -m http.server` under systemd showed **zero gap** — ping, SSH, and the custom HTTP port all recovered together within ~15s of restart, consistently, across repeated runs. Reinstalling the real `keystone` package on that same instance and precisely timing the restart reproduced the original symptom exactly: SSH reachable at t=16s, `apache2` reporting `active` from t=16s onward, but port 5000 outright refused until **t=47s** — then immediately started responding once past that point. The gap is Apache's `WSGIDaemonProcess` (`processes=5`) forking and initializing its Python worker processes — importing Keystone's substantial dependency chain (`oslo.*`, SQLAlchemy, the memcached client, etc.) — which takes real wall-clock time *after* the Apache master process itself is already listening and systemd already considers the unit active.

**Consequence:** Not a CloudCore bug, nothing to fix at the platform level. A real, application-specific characteristic worth knowing for any future restart-based failure test involving Apache/mod_wsgi services specifically (Keystone here; would apply to any other mod_wsgi-fronted service added later) — `systemctl is-active` and even a listening socket are not sufficient signals that such a service is actually ready to serve; something has to actually probe the port with a real request.

**Not fixed** — nothing to fix; documented as an application-deployment characteristic to account for in RTO expectations, not a defect.

---

## Phase 4.A — Lab, OpenTofu (Message Broker Tier)

### F-029 — `rabbitmqctl set_policy` can't set queue type on RabbitMQ 3.9

**Where:** `haFullStack.md` §6.3 (Quorum Queues — Primary Recommendation); found building 4A-04.

**Symptom:** Running the exact command §6.3 documents —
`rabbitmqctl set_policy quorum-default "^" '{"queue-type":"quorum"}' --apply-to queues` —
against a real install failed outright: `[{<<"queue-type">>,<<"quorum">>}]
are not recognised policy settings`.

**Root cause:** `queue-type` as a *policy* key (letting an operator
retroactively force existing/future queues matching a pattern to a given
type) was added in RabbitMQ 3.11. Ubuntu 22.04's `rabbitmq-server`
package is 3.9.27 — the version actually available on this platform
without adding a third-party repository. On 3.9.x, a queue's type can
only be set at **declaration time**, via the `x-queue-type` argument the
client passes when creating the queue — there is no cluster-wide policy
mechanism to force it after the fact.

**Fix:** No policy step in the deploy at all. `rabbitmq-status.py`
declares its own status-check queue directly with
`{"durable": true, "arguments": {"x-queue-type": "quorum"}}` via the
management HTTP API's `PUT /api/queues/%2F/<name>` — confirmed creating
a genuine quorum-type queue, verified via `GET` showing `"type":
"quorum"` in the response.

**Consequence for `haFullStack.md`:** §6.3's documented command needs
correcting — not as "the wrong syntax" but as version-dependent guidance
presented without qualification. A real deployment pinned to an older
RabbitMQ (as this Lab platform necessarily is, using the distro package)
needs the declaration-time approach; only 3.11+ can additionally use
the policy-based override.

**Verified by:** Direct reproduction of the failing command against a
real install, then the working `x-queue-type` declaration confirmed to
produce a genuine quorum queue via the management API's own type field.

---

### F-030 — `rabbitmq-status.py` never drained its own check queue, so one interrupted check broke every future one

**Where:** `examples/ha-frontend-lb/files/frontend-cloud-init.yaml.tftpl`'s `rabbitmq-status.py`; found immediately after failure-mode test 2 (4A-11).

**Symptom:** After test 2's below-quorum window closed and all 3 nodes were confirmed healthy again, `rabbitmq-status.html` stayed stuck on `CRITICAL` (`"consume failed or payload mismatch"`) indefinitely — not a transient blip, and not self-correcting on its own the way every other tier's status page did after its own equivalent test.

**Root cause:** Every check published a new message, then called `get` for exactly one message and compared its payload to the one just published. During test 2, several publishes were correctly rejected by RabbitMQ (see F-031's confirmation this is genuinely correct quorum behavior) — but that only stops the *publish*, not any message from an *earlier*, successful check that a prior `get` had failed to consume. Once even one message was left sitting in the queue, every future check's `get` (FIFO) returned that stale message instead of the one it had just published, guaranteeing a permanent payload mismatch — a self-perpetuating false failure with no way to recover on its own.

**Fix:** `DELETE /api/queues/%2F/status-heartbeat/contents` (purge) immediately before each check's publish, so every check starts from a guaranteed-empty queue and its own `get` can only ever return its own message.

**Consequence:** Not a RabbitMQ bug or a CloudCore platform issue — a straightforward bug in this project's own monitoring script, the kind of thing worth remembering for any future status-check script that does a publish-then-consume round-trip against a queue that isn't guaranteed empty beforehand.

**Verified by:** Applied the fix, re-ran failure-mode tests 1 and 5 again with the corrected script in place — `rabbitmq-status.html` recovered to `OK` cleanly and promptly both times, no lingering `CRITICAL` after either recovery.

---

### F-031 — RabbitMQ's below-quorum recovery is automatic; `haFullStack.md` §10's "requires manual intervention" claim is wrong for a transient outage

**Where:** `haFullStack.md` §10 (Failure Mode Analysis), the "RabbitMQ — 2 nodes fail simultaneously" row; resolved by failure-mode test 2 (4A-11).

**Symptom/question:** §10 claims a 2-of-3 node loss "requires manual intervention (`rabbitmqctl force_boot` on a surviving node) to restore service." Given F-021 found MySQL's equivalent claim backwards (assumed protection that didn't exist), this got its own dedicated test rather than being assumed correct or incorrect either way.

**Result — a more nuanced finding than F-021, not a simple confirm/deny:** the *quorum protection itself* is real and works exactly as documented — with only 1 of 3 nodes reachable, a publish attempt was cleanly rejected (`400`, "Unable to publish message. Check queue limits.") in well under a second, not silently accepted the way MySQL's writes were. But the specific **recovery claim is wrong**: `rabbitmqctl force_boot` was never run, and was never needed — simply restarting the two stopped nodes (`cloudcore` instance `start`, nothing more) let the tier rejoin and resume accepting publishes automatically, confirmed in 0.04s once both nodes were reachable again. `force_boot` is a *different* tool, for forcing a node to boot standalone when peers are believed permanently unreachable (e.g. a genuine, un-recoverable network partition) — not something a transient, recoverable outage needs at all.

**Fix:** `haFullStack.md` §10's RabbitMQ 2-node-failure row corrected: recovery is automatic once real majority returns; `force_boot` is reserved for the case where the missing nodes are never coming back.

**Verified by:** Direct reproduction — stopped 2 of 3 nodes, confirmed the publish rejection, restarted both stopped nodes with no other command, confirmed a successful publish 0.04s after both were reachable again, with zero manual `rabbitmqctl` intervention of any kind.

---

## Phase 5.A — Lab, OpenTofu (TLS and Mutual TLS)

### F-032 — ProxySQL's client-facing TLS certs have no config variable — fixed datadir paths instead

**Where:** `proxysql-cloud-init.yaml.tftpl`'s `setup-proxysql-tls.sh`, researched directly against a real ProxySQL install before writing the template.

**Symptom/question:** `haFullStack-LLD.md` §5.3.1's original draft assumed a config variable would exist for ProxySQL's own client-facing certificate (mirroring `mysql-ssl_p2s_ca`/`_cert`/`_key`, the *backend* proxy-to-server TLS variables, which do exist and were confirmed working). `SHOW VARIABLES LIKE 'mysql-ssl%'` against a real install showed only the `_p2s_*` set — nothing for the client-facing side.

**Root cause:** ProxySQL auto-generates a self-signed client-facing cert at first start and expects any replacement to simply overwrite the files at fixed paths: `/var/lib/proxysql/proxysql-{ca,cert,key}.pem`. There's no config-driven way to point it elsewhere.

**Fix:** `setup-proxysql-tls.sh` stops ProxySQL, copies the CA-issued cert/key/ca files directly to those fixed paths, `chown proxysql:proxysql`, restarts. Confirmed via `mysql --ssl-mode=REQUIRED` against ProxySQL's client port showing the new (not the auto-generated) cipher/cert.

**Verified by:** Direct reproduction against a real ProxySQL install before the template was written, then reconfirmed against the actual deployed Lab stack.

---

### F-033 — RabbitMQ's TLS `cacertfile` must be the full chain, not just the root

**Where:** `rabbitmq-cloud-init.yaml.tftpl`'s `setup-rabbitmq-tls.sh`.

**Symptom:** A client presenting a CA-issued cert (intermediate-signed) to RabbitMQ's TLS listener (5671, `ssl_options.verify = verify_peer`) was rejected with an "unknown ca" TLS alert (`openssl s_client ... SSL alert number 48`), even though the client itself sent its own certificate correctly and OpenSSL-style chain-building normally lets a server verify against just the root.

**Root cause:** Confirmed directly — Erlang's SSL stack (which RabbitMQ's TLS listener runs on) doesn't reconstruct the chain from what the client presents the way OpenSSL does; it needs the full chain (root + intermediate) available locally in `ssl_options.cacertfile`. With only the root cert in that file, verification failed even though the client's own certificate chain was valid and complete.

**Fix:** `setup-rabbitmq-tls.sh` concatenates root + intermediate into a single `ca.crt` (`cat root_ca.crt intermediate_ca.crt > ca.crt`) and points `ssl_options.cacertfile` at that, not the root alone. The same full-chain requirement was applied to Keystone's `SSLCACertificateFile` proactively once this was found, rather than waiting to hit the identical symptom there too.

**Verified by:** Direct reproduction — probed with `openssl s_client` presenting a client cert, reproduced the "unknown ca" rejection with a root-only `cacertfile`, then confirmed a clean handshake (`Verify return code: 0 (ok)`) after switching to the concatenated chain.

---

### F-034 — Restarting mysqld to apply TLS config after Group Replication has already started kills GR, with nothing to restart it

**Where:** `mysql-cloud-init.yaml.tftpl`'s `runcmd` ordering — `setup-group-replication.sh` then `setup-mysql-tls.sh` (the ordering the earlier ALTER-USER-needs-existing-accounts fix had settled on).

**Symptom:** All three MySQL nodes showed `MEMBER_STATE: OFFLINE` shortly after a fresh apply, despite `setup-group-replication.sh`'s own log showing the bootstrap node successfully starting Group Replication, self-electing primary, and running normally — for about 24 seconds.

**Root cause:** `setup-mysql-tls.sh` (the *next* script in `runcmd`) writes `zz-tls.cnf` and calls `systemctl restart mysql` to apply it — which kills the mysqld process Group Replication is running inside, exactly like stopping the server. Nothing in the boot sequence re-issues `START GROUP_REPLICATION` after that restart, so every node — bootstrap and joiners alike, since all three run both scripts in the same order — ends up permanently `OFFLINE`. MySQL error log confirmed it precisely: `'Group membership changed: This member has left the group.'` at almost exactly the timestamp `setup-mysql-tls.sh` would have run.

**Fix:** Split the TLS script into two: `setup-mysql-tls-certs.sh` (cert issuance, `zz-tls.cnf`, the one `systemctl restart mysql` this needs) now runs **before** `setup-group-replication.sh`, so the one restart happens on a not-yet-clustered server; `setup-mysql-tls-accounts.sh` (the bootstrap-only `ALTER USER ... REQUIRE X509` block, no restart) runs **after**, once those accounts exist. `runcmd` order is now: certs → group-replication → accounts.

**Verified by:** Direct reproduction against the real Lab stack (all three nodes stuck `OFFLINE`), then a full rebuild with the corrected ordering — all three nodes reached `ONLINE` with the correct `PRIMARY`/`SECONDARY` roles and stayed there.

---

### F-035 — `require_secure_transport=ON` also blocks Group Replication's own internal recovery channel, which was deliberately left plaintext

**Where:** `mysql-cloud-init.yaml.tftpl`'s `zz-tls.cnf`. Found immediately after fixing F-034 — the restart-ordering fix alone wasn't sufficient; joiners still failed to reach `ONLINE`.

**Symptom:** After F-034's fix, the bootstrap node reached `ONLINE`/`PRIMARY` correctly, but joiners failed to complete distributed recovery: `Replica I/O for channel 'group_replication_recovery': ... Connections using insecure transport are prohibited while --require_secure_transport=ON. Error_code: MY-003159`, followed by `'Fatal error during the incremental recovery process... The server will leave the group.'`

**Root cause:** `require_secure_transport=ON` is a transport-level gate applied before authentication even begins — it rejects *every* plaintext TCP connection to the server, regardless of which account is connecting. That includes Group Replication's own internal recovery/donor channel (the `repl` user), which `haFullStack-LLD.md` §5.1 already scoped as deliberately out of this slice (no cert issued to it, plaintext by design, same reasoning as RabbitMQ's inter-node Erlang distribution TLS). Setting the flag server-wide broke that channel as an unintended side effect of securing the client-facing accounts.

**Fix:** Removed `require_secure_transport = ON` from `zz-tls.cnf` entirely. Client-facing TLS enforcement is unaffected by this — `ALTER USER ... REQUIRE X509` on `appuser`/`keystone`/`proxysql_monitor` (already in place) enforces TLS **per-account**, completely independently of the server-wide flag; a plain connection using any of those three accounts is still cleanly rejected. Only the *unscoped, every-account* enforcement the global flag provided is gone — and that scope was never actually intended to cover `repl` in the first place.

**Verified by:** Direct reproduction (joiners stuck failing recovery with `MY-003159` in the error log), then confirmed all three nodes reach `ONLINE` with the flag removed, and separately reconfirmed `appuser`'s own `REQUIRE X509` still rejects a plain connection and still enforces mTLS on a TLS one.

---

### F-036 — `step ca renew`'s positional arguments must come immediately after the subcommand, and needs `--ca-url`/`--root` explicitly on every non-bootstrapped node

**Where:** Every TLS-enabled tier's `step-renew*.service` unit (`mysql-`, `proxysql-`, `keystone-`, `rabbitmq-`, `nginx-`, `frontend-cloud-init.yaml.tftpl`) — the same bug, copy-pasted across all six.

**Symptom:** `step-renew.service` crash-looping (`systemctl status` showing repeated restarts) with `too many positional arguments were provided in 'step ca renew <crt-file> <key-file>'` in the journal, on every node that had it.

**Root cause:** Two compounding issues, both confirmed directly. First, the unit's `ExecStart` was written as `step ca renew --daemon --force <crt> <key> --exec "..."` — flags before the positional cert/key arguments — but `step ca renew` requires the positional arguments to come immediately after the subcommand, before any flags; reordering to `step ca renew <crt> <key> --daemon --force ...` resolved the parse error. Second, none of these nodes had ever run `step ca bootstrap` (no local `$STEPPATH` trust config), so `step ca renew` had no way to know the CA's own address — it also needs `--ca-url`/`--root` passed explicitly, the same flags the original `step ca certificate` issuance call already used.

**Fix:** All six `step-renew*.service` units corrected to the right argument order, with `--ca-url "https://${ca_ip}:8443"` and `--root <persisted-root-cert-path>` added. A dedicated `root_ca.crt`/`root_ca.pem` copy is now saved alongside each service's own `ca.crt`/`ca.pem` specifically for this — some of those files are the full chain (RabbitMQ, Keystone), and step's own docs don't guarantee `--root` accepts a chain file the same way `cacertfile` does, so a root-only copy is kept separately rather than assumed interchangeable.

**Verified by:** Direct reproduction (`systemctl status step-renew` crash-looping, journal showing the exact parse error) on a real node, then confirmed `systemctl is-active` reports `active` (not crash-looping) after the fix, across all six tiers.

---

### F-037 — Concurrent Lab rebuilds can exhaust the Ubuntu mirror path (no IPv6 route + a flaky mirror IP), and cloud-init marks the boot "done" even when a module genuinely failed

**Where:** Observed rebuilding the full 17-node stack for this slice — not a CloudCore bug, a Lab-environment characteristic worth knowing for any future large concurrent rebuild.

**Symptom:** Several nodes (`frontend-01/02`, `nginx-a/b`, `keystone-02`) silently failed their package installs mid-boot (`cloud-init status` → `error`, `package_update_upgrade_install` failed with apt exit code 100), while `/var/lib/cloud/instance/boot-finished` was still written — meaning a naive `test -f boot-finished` health check (used earlier in this same session to poll rebuild progress) reports a node as done even when it never actually finished setting up.

**Root cause:** This Lab's bridged network (`ccbr0`) has no IPv6 route, but `archive.ubuntu.com` resolves to both IPv6 and IPv4 addresses — every apt attempt burns several seconds failing each unreachable IPv6 candidate before falling through to IPv4, and with ~14 nodes reinstalling concurrently, one or more of the IPv4 mirror addresses itself became temporarily unresponsive, compounding into an outright package-install timeout on the affected nodes. Confirmed directly: `ping 8.8.8.8` succeeded throughout (general connectivity was fine), `curl http://archive.ubuntu.com/` eventually succeeded after trying several addresses, and a plain retry of `apt-get update` a few minutes later succeeded cleanly once the burst of concurrent installs had eased.

**Second trap found recovering from it:** a plain `sudo reboot` does **not** make cloud-init retry a failed module — the "already processed this instance" semaphore in `/var/lib/cloud/instance/` survives a reboot untouched, so a rebooted node just replays the same (failed) outcome instantly rather than reprocessing anything. `sudo cloud-init clean --logs --reboot` is required to actually force a fresh run.

**Not a CloudCore bug** — nothing to fix at the platform or template level; documented as an operational characteristic of this specific Lab network topology (no IPv6 route) combined with concurrent-rebuild load, and as a reminder that `boot-finished` alone isn't sufficient evidence a node's cloud-init actually succeeded — `cloud-init status` (or checking the specific service/file the boot was supposed to produce) is needed too.

---

### F-038 — `step ca renew` preserves the original certificate's requested duration; it does not adopt a changed CA default

**Where:** Found deliberately changing the Lab CA's leaf-cert duration from `step-ca`'s unconfigured 24h default to 365 days (`haFullStack-LLD.md` §5.1, `haFullStack.md` §4.1).

**Symptom/question:** After updating the CA's `authority.claims` (`maxTLSCertDuration`/`defaultTLSCertDuration` to `8760h`) and confirming a **fresh** `step ca certificate` issuance correctly picked up the new 365-day validity, running `step ca renew` against an already-issued (24h-window) certificate produced a renewed cert still only ~24h from expiry — not 365 days out.

**Root cause:** `step ca renew` re-requests a certificate for the *same duration the original certificate had*, not the CA's current default duration, unless `--expires-in` is passed explicitly to override it. A CA-side policy change doesn't propagate to already-issued certificates on their next scheduled renewal — only to certificates issued fresh after the change.

**Fix/consequence:** No template fix needed (this is `step-ca`'s intended renewal semantics, not a bug) — but every node in the running Lab stack needed its certificate **reissued** (`step ca certificate ... --force`, the same command each node's own setup script already uses), not merely renewed, to actually pick up the new 365-day window. Done across all 12 certificate-holding nodes (MySQL ×3, the merged ProxySQL+NGINX pair ×2 — two certs each — Keystone ×2, RabbitMQ ×3, frontend ×2) and confirmed each one's new expiry landed exactly 365 days out.

**Verified by:** Direct reproduction — renewed a cert and observed its expiry stayed at the original ~24h window, then reissued the same cert and observed the new 365-day expiry, isolating the difference to renew-vs-reissue rather than any propagation delay on the CA side.

---

## Phase 6.A — Lab, OpenTofu (Local Package Repository)

### F-039 — `cloudcore_nfs_server`'s Create() only waited for `status == "running"`, not also a populated `private_ip`

**Where:** `provider/internal/resources/nfs_server.go`'s `Create()` polling loop.

**Symptom:** `module.nfs.private_ips_by_key` came back as an empty string in Terraform state after a clean `tofu apply` with no errors — every downstream node depending on it (`local.nfs_ip`) would have tried to `mount -t nfs :/exports/apt-repo`, an obviously-broken empty host.

**Root cause:** For a bridge-mode instance, the backend (`api/nfs.py`'s `create_nfs_server`) marks `status = RUNNING` immediately after the libvirt domain launches — before the guest has actually booted and picked up its DHCP-leased IP. The NFS server resource's own `Create()` polling loop only checked `poll.Status == "running"` before returning; it never also checked `poll.PrivateIP != ""`, unlike the `Instance` resource's own polling loop, which already guards against exactly this same race (confirmed by reading `instance.go` directly: `if poll.Status == "running" && poll.PrivateIP != ""`).

**Fix:** Added the same `&& poll.PrivateIP != ""` condition to `NFSServerResource.Create()`'s polling loop. Rebuilt the provider binary, redeployed it to the dev-override path, and confirmed a fresh `tofu apply` now returns a real IP on the first attempt (previously empty; confirmed correct after the fix without any retry needed).

**Verified by:** Direct reproduction (empty `private_ip` in state after a clean apply), then confirmed the fix by rebuilding the provider and re-creating the resource — real IP populated immediately.

---

### F-040 — No update path exists for an existing NFS share's `clients` field

**Where:** `provider/internal/resources/nfs_server.go`'s `Update()`, and `api/nfs_routes.py`'s `add_share` endpoint.

**Symptom:** Changing an existing share's `clients` value (from `"vpc"` to an explicit CIDR) and re-applying produced `Error: Provider produced inconsistent result after apply` — `.shares[0].clients: was cty.StringVal("192.168.100.0/24"), but now cty.StringVal("vpc")`, i.e. the change silently didn't take.

**Root cause:** The provider's `Update()` only handles shares that are new (present in the plan but not by name in the prior state) or removed (present in state but not the plan) — it has no code path for "a share with this name already exists, but one of its fields changed." The backend's own `POST /v1/nfs-servers/<id>/shares` endpoint matches this: it returns `409 Conflict` if a share with that name already exists at all, with no equivalent `PATCH`/update endpoint.

**Fix/workaround (original):** Not fixed generically at the time — the NFS server had no real data on it yet, so the pragmatic fix was to `-replace` the resource, recreating it with the correct `clients` value baked in from creation (which already threads the field through correctly).

**Fix (generic, added later):** A new `PATCH /v1/nfs-servers/<id>/shares/<name>` backend endpoint (`api/nfs_routes.py`) plus a matching provider `Update()` change — comparing each planned share against the *existing* share by name (not just detecting adds/removes) and calling the new endpoint when a field like `clients` differs — closes this properly. No `-replace` needed any more, including against a server already holding real files.

**Verified by:** Direct reproduction of the inconsistent-result error and the original `-replace` workaround; the generic fix verified separately, live, against a real NFS server with an existing share, confirming an in-place `clients` change now applies and persists correctly.

---

### F-041 — NFS export `clients = "vpc"` resolves to the CloudCore VPC's declared CIDR, not the Lab bridge's real DHCP subnet

**Where:** `main.tf`'s `module "nfs"` share definitions.

**Symptom:** Every node's `mount -t nfs <nfs-ip>:/exports/apt-repo ...` failed with `mount.nfs: access denied by server`, despite the NFS server itself being up and its `/exports` correctly configured according to its own `shares` list.

**Root cause:** The default `clients = "vpc"` share option resolves (per `api/nfs.py`'s `_export_line_raw`) to `vpc.cidr_block` — the CloudCore VPC object's own declared CIDR (`var.cidr_block`, e.g. `10.20.0.0/16`). But every bridged instance in this Lab gets its real address from the bridge's own DHCP pool (`192.168.100.0/24`, `local.bridge_cidr`) instead, regardless of the VPC/subnet CIDR objects — the same mismatch already worked around everywhere else in this stack's security-group rules. The `/etc/exports` ACL was therefore scoped to a subnet no real client ever actually connects from.

**Fix:** Set `clients = local.bridge_cidr` explicitly on both shares instead of leaving the `"vpc"` default. Required recreating the NFS server (see F-040 — this field can't be updated in place once shares already exist) to take effect.

**Verified by:** Direct reproduction of `access denied by server`, then confirmed a clean, working mount immediately after recreating the NFS server with the corrected `clients` value.

---

### F-042 — cloud-init's own `apt_configure` module silently overwrites a `bootcmd`-written `/etc/apt/sources.list`

**Where:** Every TLS-enabled tier's cloud-init template (all six) — the `bootcmd` block added to point apt at the NFS-hosted local repo.

**Symptom:** Every node still installed its packages from the real `archive.ubuntu.com` mirror despite `bootcmd` successfully mounting the NFS repo share and rewriting `/etc/apt/sources.list` to `deb [trusted=yes] file:///mnt/apt-repo/ ./` — confirmed directly: `cat /etc/apt/sources.list` on a running node showed the stock default mirror content, not the rewritten line, with no error anywhere in the log.

**Root cause:** cloud-init ships its own `cc_apt_configure` module, which regenerates `/etc/apt/sources.list` from its own default-mirror template as part of the normal boot sequence — running *after* `bootcmd` but *before* the `packages:` module — silently discarding whatever `bootcmd` had written, with zero indication of the overwrite in any log.

**Fix:** Added `apt_preserve_sources_list: true` as a top-level cloud-config key on all six templates, which stops cloud-init's own apt module from touching `/etc/apt/sources.list` at all, so the `bootcmd` rewrite survives into the `packages:` stage. (Cloud-init's own schema validator flags this key as deprecated as of 22.1, scheduled for removal in 27.1 — but it's still fully functional on this Ubuntu 22.04 image and is the only mechanism available for this cloud-init version.)

**Verified by:** Direct reproduction (`sources.list` showing the stock mirror despite a successful `bootcmd` rewrite), then confirmed `sources.list` correctly showed the NFS-repo line — and a real package install actually sourced from it — after adding the directive.

---

### F-043 — The bootstrap `apt-get update` needed just to install `nfs-common` still pulled the *entire* default sources.list, reproducing F-037's mirror congestion at an earlier point in boot

**Where:** Every tier's `bootcmd` block, the step before mounting the NFS repo (`nfs-common` has to come from the real mirror — nothing can mount the repo before an NFS client exists to mount it with).

**Symptom:** With 15 nodes booting concurrently, several took 20+ minutes (one measured at 27+ minutes and still not finished) on what should have been a near-instant `apt-get update && apt-get install -y nfs-common` — the console log showed it slowly working through the full index set (`main`, `universe`, `multiverse`, `backports`, `security` — ~40MB combined) with multi-minute gaps between individual file fetches.

**Root cause:** A plain `apt-get update` refreshes the index for *every* repo currently in `/etc/apt/sources.list` (still the full stock default at this point in boot, before the NFS-repo rewrite happens later in the same `bootcmd` block) — not just the one component (`main`) that `nfs-common` actually lives in. Fetching ~40MB of index data per node, times 15 concurrent nodes, reproduced the exact same real-mirror congestion this whole slice was built to eliminate (F-037) — just relocated to a step that runs *before* the NFS repo is even mounted, so the repo itself couldn't help.

**Fix:** Scoped the bootstrap `apt-get update`/`install` calls to a minimal, temporary sources file containing only `deb http://archive.ubuntu.com/ubuntu jammy main` (via `-o Dir::Etc::sourcelist=... -o Dir::Etc::sourceparts=-`), rather than the full default list. Confirmed directly: a node recreated with this fix reached the same point in its own boot log (past the entire package-install phase, including Keystone's full OpenStack dependency chain) in under 6 minutes total, versus 22+ minutes stuck on the bootstrap step alone before the fix.

**Verified by:** Direct before/after comparison of console-log timestamps for the same boot stage on two different nodes (one un-fixed, one recreated with the fix) — roughly a 4x-or-greater speedup, and the un-fixed node's own congestion pattern (multi-minute gaps between individual `Get:` lines) disappeared entirely on the fixed node.

---

### F-044 — A `-target`-scoped `tofu apply` left dependent nodes holding stale baked-in IPs from before the targeted resource was replaced

**Where:** The ProxySQL+NGINX tier's own `nginx_keystone_conf`/`nginx_stream_conf` — computed once and baked into that tier's `user_data` at its own apply time, referencing `module.keystone.private_ips_by_key`.

**Symptom:** After replacing just the Keystone tier (`tofu apply -target=module.keystone`) to pick up a template fix, the frontend's `tls-status.html`/`keystone-status.html` pages started failing with `no live upstreams` / `connect() failed (113)` in NGINX's own error log — pointing at Keystone IPs that no longer existed (the *previous* Keystone nodes' addresses, from before the targeted replace).

**Root cause:** `-target` intentionally limits an apply to the named resource (and its dependencies), not resources that merely *depend on* it. ProxySQL+NGINX's own `nginx_keystone_conf` local depends on `module.keystone`'s output, but that tier's `user_data` had already been computed and applied in an *earlier*, separate apply — a later targeted replace of Keystone alone doesn't retroactively recompute or push updated config to nodes that already baked in the old value. This is standard, documented `-target` behavior, not a bug — but an easy trap when a fix is scoped narrowly under time pressure.

**Fix:** A full (non-targeted) `tofu plan`/`apply` correctly recomputed `nginx_keystone_conf` with the current Keystone IPs and force-replaced the ProxySQL+NGINX nodes to pick it up (a `user_data` change is always a forced replacement for this platform's compute instances).

**Verified by:** Direct reproduction of the stale-IP `connect() failed` errors in NGINX's log, then confirmed clean, live upstream IPs and a working `keystone-status`/`tls-status` round-trip after the full reconciliation apply.

---

### F-045 — `nginx`+`keepalived`'s combined package set intermittently leaves `nginx-core` dpkg-unconfigured, unrelated to network congestion

**Where:** The merged ProxySQL+NGINX tier's package install — reproduced three separate times across this slice's rebuilds, including on freshly-recreated nodes with a fully populated, reachable local repo (ruling out F-037/F-043's mirror-congestion explanation).

**Symptom:** `cloud-init status` reports `error`, with `dpkg -l` showing `nginx-core` as `iF` (install-failed, half-configured) and `nginx` as `iU` (unpacked, not configured) — despite every *other* tier (including ones with much larger package sets, like Keystone's full OpenStack dependency chain) completing cleanly every time.

**Root cause:** Not fully root-caused — confirmed this is *not* the earlier network-congestion or conffile-corruption explanations found in F-037 (the local repo is fast and reliable, and `/etc/apt/nginx.conf` was intact each time, not truncated). Appears to be a dpkg trigger-processing quirk specific to this particular package combination (`nginx` + `keepalived` + their shared/overlapping trigger set), independent of install source speed.

**Fix/workaround:** Not eliminated at the template level — but reliably and quickly resolved every time via `apt-get install -f -y -o Dpkg::Options::="--force-confold"`, which cleanly finishes configuring the half-installed packages with no data loss or corruption. Worth a template-level investigation in a future pass (e.g. splitting the `packages:` list into two separate installs, or adding an automatic self-healing `apt-get install -f` to `runcmd`) if it keeps recurring, but not chased further for this slice given the reliable one-line fix.

**Verified by:** Reproduced identically three times (twice on the same original nodes across rebuilds, once more on nodes recreated fresh afterward with the network-congestion fix already in place) — same specific packages (`nginx-core`, `nginx`) every time, same one-line fix working cleanly every time.

---

## Platform Hardening (post-Phase 6.A review) — Host-Level Package Repository

### F-046 — `nfs.py`'s cloud-init `write_files` used `owner: 'ubuntu:ubuntu'` on entries that run before that user is guaranteed to exist, aborting the whole module silently

**Where:** `api/nfs.py`'s `_cloud_init_iso()` — the two `write_files` entries for `/home/ubuntu/.ssh/cloudcore_ed25519` and `.../cloudcore_ed25519.pub`.

**Symptom:** Two consecutive fresh NFS-server builds (during F-039/F-040/F-041's live verification) failed cloud-init with `Unknown user or group: ubuntu` and no SSH key material present on the instance at all — not just the two SSH-key files, since `write_files` aborts the entire module on the first entry that fails, not just that one entry.

**Root cause:** The same class of bug as F-026 — `write_files` runs before the base image's default-user creation is guaranteed complete, so an `owner:` referencing a not-yet-existing user aborts silently. `api/compute.py`'s own `_build_write_files_block` already has an explicit comment establishing "no `owner:` on `write_files`" as house rule for exactly this reason; `api/nfs.py`'s own cloud-init generator was written separately and didn't follow it. A `runcmd` chown step for both files was already present in the same generator — it had simply been unreachable dead code since `write_files` failed first, every time.

**Fix:** Removed `owner: 'ubuntu:ubuntu'` from both `write_files` entries; the pre-existing `runcmd` chown step now actually runs and is reachable.

**Verified by:** Direct reproduction (`Unknown user or group: ubuntu`, no key material present) on 2 of 2 consecutive builds before the fix; confirmed clean on the next build after removing `owner:`.

---

### F-047 — A genuinely stalled (not merely slow) apt-mirror TCP connection during a live `cloudcore-repo` builder run, requiring a manual kill to unstick

**Where:** `api/build-package-repo.sh`'s throwaway builder instance, mid `apt-get update` against `archive.ubuntu.com`.

**Symptom:** The build log stopped advancing entirely for 10+ minutes partway through `apt-get update`'s index fetch, despite an `ESTAB` TCP connection to the mirror IP the whole time — distinct from F-037/F-043's "slow due to real congestion" pattern, where `Get:` lines kept advancing, just slowly. Confirmed genuinely stalled, not just slow, via `/proc/<pid>/io`'s `read_bytes` on the apt `http` method worker process staying byte-for-byte identical across a 20-second window.

**Root cause:** Not root-caused beyond "a real-world mirror-side or path-side TCP stall" — the connection was fully established (no SYN retransmits, no connection-refused) but delivered zero bytes indefinitely. Killing the stuck `apt` method worker process (`sudo kill -9 <pid>`) made `apt-get update` itself error out (`Method http has died unexpectedly!`), which correctly tripped the script's own `set -e` and tore the throwaway build down cleanly via its `cleanup()` trap — no orphaned resources — rather than hanging indefinitely.

**Fix/workaround:** Not something to fix in the script itself (a genuinely external, transient mirror condition) — simply re-running `build-package-repo.sh` from scratch worked on the next attempt, with all previously-completed artifact downloads already skipped via `curl -C -` resume/completion checks, so only the VM-provisioning and apt-install phases had to repeat.

**Verified by:** Direct reproduction (zero `read_bytes` growth over 20s on an `ESTAB` connection), confirmed the kill correctly triggered the existing `cleanup()` trap with no orphaned VPC/SG/subnet left behind, and confirmed a full clean re-run completed successfully (652 packages indexed) immediately after.

---

## Phase 7.A — Lab, OpenTofu (Backend Tier)

### F-048 — No new security-group rules were needed on proxysql/keystone/rabbitmq to let backend reach them

**Where:** `examples/ha-frontend-lb`'s `security_groups` module — the existing ingress rules on the `proxysql`, `keystone`, and `rabbitmq` security groups.

**Symptom:** Not a failure — the opposite. Building the backend tier's own security group and expecting to also need new `source_sg_id`-scoped ingress rules added to proxysql/keystone/rabbitmq's own security groups (matching the plan's own stated approach), it turned out none were required at all — backend's real TLS handshakes against ProxySQL `:6033`, Keystone `:5443`, and RabbitMQ `:5671` succeeded on the first attempt with zero changes to those three tiers' SGs.

**Root cause:** Every existing ingress rule on proxysql/keystone/rabbitmq's own security groups is already scoped to `local.bridge_cidr` — the entire shared bridge subnet (`192.168.100.0/24`) — not narrowed to specific source security groups, despite `source_sg_id`-based rules being a real, already-used capability elsewhere in this same `security_groups` module (e.g. keystone's SG already allows ingress from frontend's SG by ID for other paths). Any instance anywhere on the bridge, including a brand new backend tier that didn't exist when those rules were written, is already implicitly permitted.

**Assessment:** Not a bug to fix — this is a real, accurate observation about how permissive this Lab's existing security-group posture already is (every bridged instance trusts every other bridged instance at the network layer; TLS/mTLS is what actually enforces trust at the application layer, per §5). Worth carrying forward as a known characteristic if this design is ever ported to On-Prem/AWS, where CIDR-wide trust across an entire subnet is a much bigger blast radius than it is here — those environments' own SGs should almost certainly use `source_sg_id`-scoped rules properly rather than inheriting this Lab-specific shortcut, per the LLD §8.4/§8.5 On-Prem/AWS notes.

**Verified by:** Direct reproduction — no SG changes made to proxysql/keystone/rabbitmq at all, and `openssl s_client` from a backend node against all three, presenting backend's own CA-issued client cert, returned `Verify return code: 0` on the first real attempt.

---

## Document History

| Version | Date | Author | Change Summary |
|---|---|---|---|
| v0.1 | 2026-09-10 | Paul Scott | First entries — architecture review (F-001/F-002) and Phase 1.A, Lab/OpenTofu (F-003–F-009). |
| v0.2 | 2026-09-10 | Paul Scott | Platform Hardening review before Phase 1.B — bridge-mode SG enforcement fixed (F-010), instance error surfacing added (F-011), CloudCore's own DNS server fixed (F-012–F-014). |
| v0.3 | 2026-09-10 | Paul Scott | First real build of Phase 2.A (Database Tier) — a genuine platform bug (F-015, bridge-mode egress_rules silently never enforced) plus four MySQL Group Replication bootstrapping gotchas (F-016–F-019: plugin load ordering, hostname resolution, config file load order, auth plugin), found and fixed in the order a real deploy hits them. Cluster reached ONLINE/PRIMARY with ProxySQL correctly auto-detecting the primary and the frontend status page showing a live, advancing heartbeat through the full real path. |
| v0.4 | 2026-09-10 | Paul Scott | Failure-mode testing (2A-11, 2A-12, 2A-14): secondary loss and primary failover both self-healed automatically with ProxySQL auto-retagging, measured primary-failover RTO ~3.5s (beats the assumed ~5-10s). Restart-and-rejoin confirmed working for a joiner node but revealed F-020 — the bootstrap node has no permanent seed and can't rejoin an existing group on its own after a restart, unlike joiner nodes; fixed manually for this run, flagged as a real open item rather than templated. |
| v0.5 | 2026-09-10 | Paul Scott | F-021 — the most significant finding this session: test 3 (2A-13, stop 2 of 3 nodes) revealed the survivor does not actually refuse writes after losing quorum by default, contradicting `haFullStack.md` §5.3's own original claim. `haFullStack.md` corrected to v1.3. Real split-brain protection is a new, undecided open item, not a same-session fix. |
| v0.6 | 2026-09-10 | Paul Scott | F-021 fixed: `quorum-watchdog.py` deployed and verified against a real below-quorum cycle run twice. A second bug found and fixed building the fix itself — `super_read_only=OFF` doesn't clear the separate `read_only` flag, which silently blocked ProxySQL from ever re-admitting a recovered node as a writer. `haFullStack.md` corrected to v1.4. |
| v0.7 | 2026-09-10 | Paul Scott | Platform Hardening (post-Phase 2.A review): guest-visible DNS + `.local`→`.internal` zone rename (F-022, motivated by F-017's hostname-resolution gap); SQLite write-serialization fix for a real hang/`database is locked` failure mode under concurrent load, including a worse indefinite-hang bug caught and fixed in the first attempt before it shipped (F-023); `help_articles.slug` uniqueness corrected to exclude soft-deletes via a partial unique index (F-024). All three found and fixed outside any specific slice, ahead of starting Phase 3. |
| v0.8 | 2026-09-10 | Paul Scott | Third phase built and failure-tested for real — Phase 3.A, Identity Tier (Keystone). Two real deploy bugs found and fixed (missing Fernet-key base64 padding, F-025; cloud-init `write_files` running before packages install plus a related missing-package gap, F-026). Failure test 2 (3A-11) resolved the memcached question definitively — shared Fernet keys, not memcached, enable cross-node validation, correcting `haFullStack.md` §7 (F-027). A platform-level TCP-reachability gap after instance restart, distinct from ICMP/SSH readiness, observed and documented rather than chased to a fix (F-028). |
| v0.9 | 2026-09-10 | Paul Scott | F-028 corrected after being deliberately left open — not a CloudCore platform/networking gap at all. Reproduced with a clean isolated service (zero gap) and then with real Keystone/Apache on the same instance (exact symptom reproduced, ~30s gap measured precisely) — the cause is `mod_wsgi`'s own worker-process startup time, not bridge/ARP staleness. Nothing to change in CloudCore for this finding. |
| v0.10 | 2026-09-10 | Paul Scott | Fourth phase, Message Broker Tier (RabbitMQ) — building 4A-01–4A-07. Found `haFullStack.md` §6.3's documented `set_policy` quorum-queue command doesn't work on the actual available RabbitMQ version (3.9.27, Ubuntu 22.04's distro package) — that policy key was only added in 3.11+ (F-029). Fixed by declaring quorum type at queue-declaration time instead. Cluster-join sequence (seed/joiner, IP-based node naming, remote-status checks) fully validated directly against real throwaway instances before writing the template. |
| v0.11 | 2026-09-10 | Paul Scott | Phase 4.A built and failure-tested for real (4A-01–4A-14 done). Found and fixed two more bugs along the way: NGINX's `stream{}` block only proxied AMQP, not the management API the status script needs (plain oversight); the status script's own publish/consume check never drained its queue, so one interrupted check broke every future one until fixed (F-030). Failure test 2 (4A-11) gave the most nuanced result yet — unlike MySQL (F-021), RabbitMQ's quorum protection genuinely works as documented, but `haFullStack.md` §10's specific `force_boot` recovery claim is wrong: recovery from a transient below-quorum window is fully automatic once the missing nodes are simply restarted (F-031). `haFullStack.md` §10 corrected. |
| v0.12 | 2026-09-11 | Paul Scott | Fifth phase, TLS and Mutual TLS, built and verified for real across all four existing tiers (MySQL/ProxySQL, Keystone, RabbitMQ, NGINX) plus a new TLS/mTLS status page on the frontend dashboard. Two gotchas found during pre-build research (ProxySQL's fixed-datadir client cert paths, F-032; RabbitMQ's `cacertfile` needing the full chain not just the root, F-033). Two real, compounding bugs found rebuilding MySQL with TLS — a runcmd-ordering bug where restarting mysqld to apply TLS config after Group Replication had already started killed GR outright with nothing to restart it (F-034), and once fixed, a deeper one underneath it: `require_secure_transport=ON` also blocked GR's own internal recovery channel, which was deliberately left plaintext (F-035). A `step ca renew` argument-order bug crash-looping the renewal daemon on all six TLS-enabled tiers (F-036). A Lab-environment characteristic, not a CloudCore bug, cost real troubleshooting time: concurrent rebuilds exhausting the Ubuntu mirror path (no IPv6 route in this Lab), with cloud-init marking the boot "done" even when a module had failed, and a plain reboot not being sufficient to force cloud-init to retry (F-037). |
| v0.13 | 2026-09-11 | Paul Scott | The Lab's NGINX/Keepalived LB tier merged into the ProxySQL tier (two fewer nodes; only the node actively holding the VIP ever serves real traffic anyway, so co-locating with ProxySQL cost nothing functionally) — resolved a genuine self-reference (the merged node's own `stream{}` config needing to know its own address) by pointing the MySQL upstream at `127.0.0.1` instead of a 2-node list, which turned out to be more correct than the pre-merge design, not just simpler. `haFullStack.md`/`haFullStack-LLD.md` updated to the new topology. Separately, the Lab CA's leaf-cert lifetime changed from `step-ca`'s unconfigured 24h default to a deliberate 365 days, to mirror realistic On-Prem/AWS PKI lifetimes ahead of building those slices — found along the way that `step ca renew` preserves a certificate's original requested duration rather than adopting a changed CA default, so every already-issued certificate needed reissuing, not just renewing, to actually pick up the new window (F-038). |
| v0.14 | 2026-09-11 | Paul Scott | Sixth slice — Phase 6.A, a local apt repo + pinned-artifact cache served over NFS, so a full stack rebuild no longer hammers the real Ubuntu mirror (F-037). Built and verified for real, not draft: a real `dpkg-scanpackages`-indexed repo (260 packages, the full closure across every tier) plus the pinned `step-ca`/`step-cli`/`proxysql` `.deb`s, populated once by a gated one-shot builder node. Found and fixed a real provider bug along the way — `cloudcore_nfs_server`'s `Create()` never waited for a populated `private_ip`, only `status == "running"`, the same race the `Instance` resource already guarded against (F-039) — plus five more real findings hit building this for real: no update path for an existing NFS share's `clients` field (F-040), the `"vpc"` share default resolving to the wrong CIDR for this Lab's bridge network (F-041), cloud-init's own apt module silently overwriting the NFS-repo `sources.list` rewrite (F-042), the bootstrap `apt-get update` itself still hammering the full mirror even after the main fix (F-043), a `-target`-scoped apply leaving stale baked-in IPs on a dependent tier (F-044), and a recurring, network-independent `nginx`+`keepalived` dpkg quirk (F-045). All four dashboard checks (MySQL, Keystone, RabbitMQ, TLS) confirmed `OK` twice in a row on the finished rebuild. |
| v0.15 | 2026-09-11 | Paul Scott | New Platform Hardening review (post-Phase 6.A) — the per-project NFS repo generalized into a host-level, always-available `cloudcore-repo` service, extended to cover every example template (not just `ha-frontend-lb`), including two third-party apt repos (Adoptium, Kismet) and several pinned release artifacts. F-040 resolved for real (a generic backend `PATCH` endpoint plus provider support, replacing the original `-replace`-only workaround). Two new findings from live verification: a `write_files`/`owner:` race in `api/nfs.py`, the same class as F-026 (F-046), and a genuinely stalled — not merely slow — apt-mirror TCP connection hit during a live builder run, recovered by killing the stuck apt method worker (F-047). |
| v0.16 | 2026-09-12 | Paul Scott | `ha-frontend-lb` retrofitted onto the host-level `cloudcore-repo` service, retiring its own per-project NFS repo entirely (15 nodes instead of 17). No new findings — verifying this for real confirmed two existing ones still hold exactly as documented, not new behavior: F-042 (`apt_preserve_sources_list: true` is still required, now protecting a plain `sources.list` rewrite pointing at the host-level repo instead of an NFS mount) and F-045 (the `nginx`/`keepalived` dpkg race recurred once during the verification build, resolved with its usual one-line fix, still not root-caused). |
