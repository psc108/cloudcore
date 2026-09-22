# NGINX High-Availability Load Balancing Architecture — Findings Log

**Multi-Service Platform — Frontend, Backend, MySQL, Keystone, RabbitMQ**

v0.30 | Paul Scott

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

## Platform Hardening (post-Phase 7.A review) — Operational tooling: Node/Java/jasypt, ecs user, DNS

### F-049 — CloudCore's own guest-visible DNS resolver was never reloaded after the API server's own startup

**Where:** `api/server.py` / `api/dns_server.py` — the local dnsmasq instance (127.0.0.1:5353) that answers `*.cloudcore.internal` lookups forwarded from the bridge's own dnsmasq (F-022).

**Symptom:** Adding a short-hostname DNS record for a freshly-created test instance and then trying to resolve it from a guest failed outright (`Temporary failure in name resolution`) even for the record's full FQDN form, despite the record existing correctly in the API's own store and despite the platform-level DHCP domain-search fix (below) being confirmed present in the guest's `/etc/resolv.conf`.

**Root cause:** `dns_server.start()` — which generates `dnsmasq.conf` from the current `dns_records` table and (re)starts the dnsmasq process — is called exactly once, in `api/server.py`'s `if __name__ == "__main__":` block, right after `reconcile()` populates initial records at boot. None of the 7 call sites elsewhere in the file that mutate the DNS store (instance create/get-refresh/delete, LB create/delete, and the manual `POST`/`DELETE /v1/dns/zones/.../records` endpoints) ever called `dns_server.reload()` afterward. The result: guest-visible DNS silently only ever reflected whatever records existed at the moment the API server process last started — any record created or changed during that process's lifetime was invisible to every guest until the next full API server restart. This had gone unnoticed because reconcile()-time state (existing instances present at boot) always looked correct; only newly-created records during a live session were affected.

**Fix:** Added `dns_server.reload()` immediately after each of the 7 mutation call sites in `api/server.py`. Also found and cleaned up a related but separate operational issue while debugging this: an orphaned dnsmasq process from a prior ungracefully-terminated API server session was still holding port 5353, which made the very first post-fix restart fail with "Address already in use" until the stale process was killed manually — not a code bug, just confirms `dns_server.stop()`'s SIGTERM-based cleanup only works when the API server itself shuts down cleanly.

**Verified by:** Direct reproduction against a live instance — before the fix, neither the FQDN nor the bare short-name form of a newly-created record resolved from a guest; after restarting the API server with the fix applied, both the platform-level DHCP domain-search option (`instances.cloudcore.internal`, confirmed present via `resolvectl status`) and a live `ping <short-name>`/`ssh <short-name>` round-trip succeeded end-to-end.

### F-050 — cloud-init's `users:` key silently drops the image's own default user unless `default` is explicitly listed

**Where:** `api/compute.py`'s `_build_users_block()` — used whenever an instance is created with one or more `extra_users` (the new `users` attribute on `cloudcore_instance`, added this slice to support an "ecs" operational account on every node in `ha-frontend-lb`).

**Symptom:** The first full-stack build with `users = [{ username = "ecs", sudo = true }]` set on every instance produced instances where the "ecs" user existed correctly (with working NOPASSWD sudo), but SSH access to the base image's own "ubuntu" user was completely broken — `Permission denied (publickey)` even though the exact same top-level `ssh_authorized_keys:` block that normally provisions "ubuntu" was present, byte-for-byte, in the rendered cloud-config.

**Root cause:** cloud-init's `users:` module has a well-known but easy-to-miss semantic: including a `users:` key in cloud-config **replaces** its normal implicit behavior of also creating the image's own default user, unless the literal string `"default"` is included as one of the list's own entries (e.g. `users: [default, {name: ecs, ...}]`). `_build_users_block()` had never needed to handle this before this slice — `instance.users` (and the whole `POST /v1/instances/{id}/users` side-channel it originally existed for) had never actually been wired into the `POST /v1/instances` create path until this slice's own fix earlier in the same investigation (see below), so no real build had ever populated `extra_users` at instance-creation time before, and this latent gap had never been exercised.

**Fix:** `_build_users_block()` now always prepends the literal `"- default"` entry to its `users:` list whenever any extra users are requested, restoring the image's own default-user creation unconditionally alongside whatever extra users are configured.

**A second, related gap found and fixed in the same investigation:** `POST /v1/instances` never read a `users` key from its request body at all — `compute.create_instance()`/`_cloud_init_iso()` had always fully supported baking `instance.users` into an instance's initial cloud-init, but the only way to populate that field was the separate `POST /v1/instances/{id}/users` endpoint, which is applied too late for bridge-mode instances (the cloud-init ISO is already built by the time that endpoint could run) and only applies live via SSH in SLIRP mode. Fixed by reading and validating a `users` list in the create-instance request body, and by adding a matching `users` attribute (list of `{username, sudo, ssh_keys, password_hash}`) to the Go provider's `cloudcore_instance` resource, `RequiresReplace` on change since it's baked in at initial boot only, matching `user_data`'s existing convention.

**Verified by:** A full three-iteration real build/destroy cycle against the entire `ha-frontend-lb` stack (48 resources: 15 instances + VPC/subnet/SGs + 15 short-hostname DNS records) — iteration 1 caught F-049's stale-DNS-resolver gap, iteration 2 caught this finding (ubuntu SSH broken stack-wide once "ecs" was added), iteration 3 confirmed clean: `ecs` exists with working NOPASSWD sudo on every instance, `ubuntu` SSH access is unaffected, short-hostname DNS resolves, and `ssh ecs@<short-name>` succeeds passwordlessly between every tier type exercised (an `instance-group`-managed tier and a `compute`-managed tier), landing with working sudo on the far side too. Stack destroyed cleanly afterward (48/48 resources both times).

### F-051 — A third Apache `WSGIDaemonProcess` pool OOM-killed Keystone outright on `standard.small`

**Where:** `keystone-cloud-init.yaml.tftpl`'s new `:35357` admin-API vhost (added to support an `env.sh` OpenStack CLI environment file, per direct request).

**Symptom:** After adding the classic OpenStack admin port (`:35357`) alongside the existing `:5000`/`:5443` Keystone vhosts, cross-instance connections to Keystone started failing with an instant `Connection refused` — not a timeout, and not scoped to the new port: the pre-existing, previously-working `:5000` path failed identically. Chased as a security-group/DNS problem first (bridge-mode SG rules, chain contents, DNS reload) before finding the real cause. One Keystone node's `systemctl status apache2` showed `Active: failed (Result: oom-kill)`; the other node was completely unresponsive, including SSH — a fully memory-exhausted guest with no swap configured, not merely a crashed service.

**Root cause:** The `:35357` vhost, as first written, gave itself its own `WSGIDaemonProcess ... processes=3` pool — a naive but easy mistake, since it looks exactly like the pattern the existing `:5443` mTLS vhost already uses. But `:5000` (the keystone package's own default vhost) already runs its own pool at `processes=5`, and `:5443` adds another 3 — so a third pool added 3 more mod_wsgi worker processes (each a full Python interpreter with the Keystone app loaded) on top of the existing 8, pushing total memory demand past what `standard.small` (957MB, no swap) can sustain. The instant "connection refused" was a red herring from chasing the wrong layer first — once apache itself is dead, nothing is listening, which manifests as instantly-refused connections, easily mistaken for a firewall/security-group rejection rather than an application-layer resource crash.

**Fix:** The `:35357` vhost no longer declares its own `WSGIDaemonProcess` — it sets `WSGIProcessGroup keystone-public`, reusing the exact same already-running pool that `:5000` uses (the same unified `keystone-wsgi-public` app either port would route to anyway). This costs zero additional memory — the same 5 existing workers now also answer on the second port.

**Verified by:** A full destroy/rebuild of the entire `ha-frontend-lb` stack with the corrected vhost — both Keystone nodes confirmed `apache2` `active` (not oom-killed) after boot, all three ports (`5000`/`5443`/`35357`) listening on both nodes, and a real end-to-end pass: `ecs` sourcing the generated `/etc/openstack/env.sh` (symlinked into `root`/`ecs`/`ubuntu`'s home directories) and authenticating through the shared VIP on `:35357` (`201 Created`), with `:5000` confirmed still working identically. A related, smaller placement bug was also found and fixed in the same pass: `env.sh` was first written to `/etc/keystone/env.sh`, but that directory is `keystone:keystone 0750` — `ecs`/`ubuntu` couldn't even traverse into it to reach an otherwise-`0644` file. Moved to `/etc/openstack/env.sh` (a plain `0755` directory, world-traversable) instead.

### F-052 — F-045's dpkg-postinst-failure class recurred on `rabbitmq-server`, not just `nginx`+`keepalived`

**Where:** RabbitMQ tier package install, verifying the new application service-account/vhost import (`setup-rabbitmq.sh`'s extended `is_seed` block).

**Symptom:** `cloud-init status` reported `error` on the seed node, with `package_update_upgrade_install` failing — `rabbitmq-server package post-installation script subprocess returned error exit status 1`. Despite this, every subsequent `runcmd` step (Erlang cookie, clustering, the new user/vhost creation, TLS setup) ran and succeeded regardless — `rabbitmq-server` itself came up healthy (`systemctl is-active` → `active`) and fully functional.

**Assessment:** The same class of issue as F-045 (a dpkg trigger-processing quirk on a specific package's postinst script, not network/mirror-related — this repo build was fast and local), just hit on a different package (`rabbitmq-server` instead of `nginx`+`keepalived`) — worth its own cross-referencing entry rather than folding into F-045's history, per this log's own convention. Not chased further for the same reason F-045 wasn't: the actual service came up working despite the reported error, and this stack's dashboard/functional checks are what actually matter, not cloud-init's own exit status. Still not root-caused across either occurrence.

**Verified by:** Despite the `error` status, confirmed for real that `setup-rabbitmq.sh`'s new application-account step fully succeeded: all 13 service users (`blobstore`, `catalog`, `customer`, `event`, `fulfillment`, `pricing`, `reporting`, `request`, `svcinst`, `config`, `export`, `ticketing`, `extevt`) and the `/ssp` vhost with full permissions for each, created on the seed and confirmed visible from a joiner node via RabbitMQ's own Mnesia-based cluster-wide propagation (no separate sync step needed, same as the pre-existing `admin` user already relied on).

---

### F-053 — `~/.my.cnf` doesn't work as a `mysql -u root` credentials source across `runcmd` scripts — cloud-init runs them without a real `$HOME`

**Where:** MySQL tier — giving `root@localhost` a real password (replacing auth_socket) so every other pre-existing `mysql -u root` call in this file keeps working unchanged afterward.

**Symptom:** Writing `/root/.my.cnf` with the new root credentials, immediately after the `ALTER USER` that set them, looked correct and passed static review — but the very next script in `runcmd` (`setup-group-replication.sh`) failed every one of its own `mysql -u root` calls with `ERROR 1045 (28000): Access denied for user 'root'@'localhost' (using password: NO)`. Because that script is what creates the `repl` account and starts Group Replication, this broke the entire cluster outright — every node stayed `OFFLINE`, and none of `appuser`/`keystone`/`ssp_*` ever got created either, since they're in the same now-unreachable SQL block.

**Root cause:** cloud-init's `runcmd` entries are each their own subprocess invocation, run without a real login-shell environment — `$HOME` is not set to `/root` the way it would be for an actual root shell session (interactive, or via `sudo`). The `mysql` client's default `~/.my.cnf` lookup is `$HOME`-relative, so with `$HOME` unset or wrong, it silently found nothing and fell back to a passwordless connection attempt — which auth_socket no longer permitted once the password had been set. Manually testing the same command over SSH via `sudo mysql` didn't reproduce this at all (`sudo` itself sets `HOME=/root` for the command), which is what made this easy to miss in ad-hoc verification.

**Fix:** Moved the credentials out of `~/.my.cnf` entirely and into `/etc/mysql/mysql.conf.d/zz-client-root.cnf`'s `[client]` section instead — a fixed, `$HOME`-independent path already on this file's own config-loading path (`mysqld` itself just ignores the `[client]` section, same as any other client-only block in a file it doesn't fully own). Explicitly `chmod 600` it — unlike `~/.my.cnf`, MySQL doesn't refuse to use an over-permissive file at this location, so without the explicit chmod the password would sit world-readable.

**Verified by:** Reproduced directly (destroy/rebuild with the broken `~/.my.cnf` version first — confirmed the exact error and a cluster stuck fully `OFFLINE` with no `repl`/`appuser`/`ssp_*` accounts at all), then destroy/rebuilt again with the fix — Group Replication reached `ONLINE`/`PRIMARY` on the bootstrap node and `RECOVERING`/`SECONDARY` (progressing normally) on a replica, both `root@localhost` and the new `root@'%'` present and correctly replicated to the secondary, and a real remote connection through the shared VIP → ProxySQL (`root`'s new network-reachable account, added to `mysql_users` alongside `appuser`/`keystone`) succeeded end-to-end.

### F-054 — CloudCore Dashboard's Terminal feature blocked every `ha-frontend-lb` instance once "ecs" was added, on a client-side JS bug (not an SSH problem at all)

**Where:** `ui/src/js/11-terminal.js` (CloudCore platform UI, not this project's own templates) — the Dashboard's Terminal tab.

**Symptom:** Reported as "we can't terminal/login to any instance — it sees all users as sudo (including ubuntu)." Every instance in the stack now shows `users: [{username: "ecs", sudo: true}]` (this slice's own operational-user work), and the Terminal tab's per-instance card displayed "⚠ All users on this instance have sudo privileges" and refused to offer an "Open Terminal" button at all, for every single node.

**Root cause:** Not an SSH, sshd_config, or backend problem — direct testing proved SSH access was never actually broken: both `ssh ecs@<ip>` and the Terminal feature's own backend (`api/terminal.py`, driven directly via its real WebSocket protocol) connect and authenticate correctly, landing as `ubuntu` with real sudo confirmed working. The bug is a genuine inconsistency in the UI's own client-side gating logic: `_pickNonSudoUser()` (`11-terminal.js:5-10`) correctly falls back to `instance.ssh_user` ("ubuntu") when every explicitly-tracked user is sudo — exactly matching `api/terminal.py`'s own `_pick_user()`/`_terminal_handler()` logic — but a *separate* `allSudo` check a few lines below it (used only to decide whether to show the warning and hide the button) never considered that same fallback, checking only `instance.users`. The moment any instance has at least one tracked sudo user (now every node, via the "ecs" work), `allSudo` evaluates true and blocks the UI entirely, regardless of whether a perfectly valid non-sudo login (the default image user) actually exists.

**Fix:** `allSudo` now derives directly from the same `nonSudo` value `_pickNonSudoUser()` already computes (`allSudo = !isNfs && !nonSudo`), instead of independently re-deriving an inconsistent answer. Rebuilt via `ui/build.sh` — a static-file change, picked up immediately with no API server restart needed.

**Verified by:** Created a real throwaway instance with `users: [{username: "ecs", sudo: true}]` (the exact `ha-frontend-lb` pattern) — before the fix, the UI's own logic (replayed against the real API response) evaluated `allSudo = true`; after the fix, `false`. Confirmed the full real path end-to-end directly against the live WebSocket endpoint (`ws://127.0.0.1:8081/terminal?instance_id=...`): connected successfully as `ubuntu`, received a real interactive shell banner. No sshd_config or platform SSH changes were needed anywhere — access was already fully functional the whole time.

### F-055 — F-054's fix didn't reproduce on a second machine: the Dashboard's `index.html` was cached by the browser, not stale on disk

**Where:** `api/server.py`'s `GET /` route — serves `ui/index.html`, the single file `ui/build.sh` bundles the *entire* dashboard into (every `ui/src/js/*.js` inlined).

**Symptom:** After confirming F-054's fix (`git pull` on a second machine, code on disk verified correct and up to date), the exact same "No SSH port" symptom the fix was supposed to resolve was still reported — but this time for a *different* reason. The affected instance's own API data was completely correct (`private_ip` populated, `ssh_user: "ubuntu"`, `users: [{ecs, sudo:true}]` — every field the fixed JS logic needs to correctly show a working "Open Terminal" button), ruling out any backend/data bug on this second machine entirely.

**Root cause:** `send_from_directory`'s default `Cache-Control` behavior let the browser hold onto a previously-loaded copy of `index.html` indefinitely, across page loads — not just within one open tab. Since the *entire* dashboard is one file, an already-cached copy silently keeps running whatever JS was current the last time that browser fetched it, with no visible error or version indicator to reveal it's stale — a normal (non-hard) refresh doesn't force a re-fetch if the browser's cache considers the copy still fresh.

**Fix:** `GET /` now explicitly sets `Cache-Control: no-store, no-cache, must-revalidate, max-age=0` and `Pragma: no-cache`, so every load re-fetches the current `index.html` regardless of browser cache heuristics — no hard-refresh should ever be needed again for a dashboard update to take effect.

**Verified by:** Confirmed the response header directly (`curl -sI http://127.0.0.1:8080/`) before and after the fix. A related, separate, real issue was surfaced by the same diagnostic round-trip and is *not* fixed by this — every instance on the second machine also showed `error_message: "... sudo -n iptables -N ... returned non-zero exit status 1"`, meaning bridge-mode security-group enforcement is silently failing there entirely (same class as F-010): the NOPASSWD sudo grant `api/setup-network.sh` installs is scoped to whichever OS user ran it, and doesn't automatically apply if a different user runs `api/server.py`. This needs to be resolved on that machine directly (matching the sudo grant to the right user, or re-running `setup-network.sh` as it), not a code fix.

### F-056 — `install.sh`'s automated path never installed the security-group sudoers grant at all, for anyone

**Where:** `api/setup-network.sh`'s security-group NOPASSWD sudoers grant, installed via `scripts/install.sh` → `cloudcore-bridge.service` (systemd) rather than a manual `sudo bash setup-network.sh` invocation.

**Symptom:** F-055's follow-up — the reported user (`scottp`, confirmed via `sudo -l` to have full, real sudo access) still got `sudo -n iptables -N ... exit status 1` on every single instance. Not a wrong-user problem as first suspected: no user had the grant at all.

**Root cause:** `SG_SUDOERS_USER=${SUDO_USER:-$USER}` is correct for the documented manual workflow, but `install.sh` never invokes `setup-network.sh` directly — it installs and enables `cloudcore-bridge.service`, and systemd sets *neither* `SUDO_USER` nor `USER` for the processes it starts. That doesn't just pick the wrong identity — it resolves to an **empty string**, producing a sudoers line with no username at all (`" ALL=(root) NOPASSWD: ..."`), which `visudo -c`'s own syntax check correctly rejects. The script's existing validation-failure branch already handled this "gracefully" (skip, print a warning to a log nobody was watching), which is exactly why it went unnoticed until instances started actually failing to get security-group enforcement.

**Fix:** New `CLOUDCORE_BRIDGE_USER` environment variable, injected into the systemd unit's own `Environment=` line by `install.sh` (set to `$CURRENT_USER`, which the script already captures via `whoami` for an unrelated `libvirt` group step earlier). `setup-network.sh` checks it first, falling back to the unchanged `SUDO_USER`/`USER` behavior for manual invocation. Also added an explicit "could not determine a user, skipping" warning for any future case where all three are still empty, rather than relying on the generic `visudo` rejection message to surface it.

**Verified by:** Reproduced the exact failure directly — running the grant-generation logic under a clean, systemd-like environment (`env -i`, no `SUDO_USER`/`USER`) produces an empty username and a `visudo -c` syntax error, confirmed byte-for-byte matching what a fresh `install.sh`-provisioned machine would hit. The fix, run under the same clean environment with only `CLOUDCORE_BRIDGE_USER` set, correctly resolves the real user and produces a `visudo -c`-accepted rule. This means bridge-mode security groups have been silently unenforced on every machine provisioned via `install.sh` to date — a real, meaningful gap this fix closes for any future install, though existing installs still need the one-time manual `sudo bash api/setup-network.sh` re-run to pick it up (sudoers grants aren't retroactively applied by a code change alone).

### F-057 — F-054's already-correct fix still didn't reproduce, a third time: `cloudcore-terminal` is a separate systemd unit nothing was restarting

**Where:** `api/cloudcore-terminal.service` — the Terminal WebSocket server, installed as its own independent user-level systemd unit, entirely separate from `cloudcore-api.service`.

**Symptom:** The "No SSH port available" report persisted through a full `git pull`, an `install.sh` re-run, a manual `sudo bash api/setup-network.sh` (fixing F-056), a hard browser refresh (ruling out F-055), a fresh `tofu apply`, and a manual `systemctl --user restart cloudcore-api`. Every other diagnostic came back clean: instance data was correct, the sudoers grant was now installed, the dashboard bundle was current.

**Root cause:** `api/terminal.py` runs as `cloudcore-terminal.service`, a completely separate unit from `cloudcore-api.service` — restarting one has no effect on the other. Python doesn't hot-reload its own imports, so a long-running `terminal.py` process keeps using whatever `store.py`/`models.py`/etc. looked like whenever *it* last started, regardless of how many times `cloudcore-api` gets restarted or how current the code on disk is. Compounding this: `install.sh` starts it with `systemctl --user enable --now cloudcore-terminal.service`, and `enable --now` is a no-op on an already-running unit — it does not force a restart. So none of install.sh, setup-network.sh, a browser refresh, or restarting cloudcore-api could ever have touched this specific process, and nothing about the symptom pointed at it directly until the process list was checked and its start time was found to predate every fix in this whole investigation.

**Fix:** Added `PartOf=cloudcore-api.service` to `cloudcore-terminal.service`'s `[Unit]` section — this propagates stop/restart (one-way) from `cloudcore-api` to this unit, so a single `systemctl --user restart cloudcore-api` now restarts both.

**Verified by:** Installed the updated unit file, started both services fresh, recorded `cloudcore-terminal`'s `MainPID`, then ran `systemctl --user restart cloudcore-api` only — confirmed `cloudcore-terminal`'s `MainPID` changed too (a real restart, not a no-op), with both services reporting `active` and the API responding `200` afterward. On the reporting machine, manually restarting `cloudcore-terminal` directly is what actually resolved the original symptom, confirming the diagnosis before this fix was even written.

### F-058 — `openssl pkey -in X -out X` (identical path) silently corrupts the key it's meant to encrypt

**Where:** `rabbitmq-cloud-init.yaml.tftpl` and `keystone-cloud-init.yaml.tftpl`'s TLS setup scripts, encrypting each node's freshly-issued private key with `var.admin_password` (per direct instruction, matching this project's own real-world passphrase-protected-key convention).

**Symptom:** `rabbitmq-server` crash-looped immediately after the TLS setup script ran, with `openssl pkey`'s very next invocation failing "Could not read key from /etc/rabbitmq/server.key" — a file `step ca certificate` had just written successfully moments earlier.

**Root cause:** The encryption command was `openssl pkey -aes256 -in server.key -out server.key -passout pass:...` — `-in` and `-out` pointing at the same path. `openssl` opens (and truncates) the `-out` file before it has finished reading the `-in` file, so the key is destroyed mid-read; the file that's left behind is neither the original key nor a valid encrypted one.

**Fix:** Write to a temporary path (`server.key.enc` / `key.pem.enc`) and `mv` it into place afterward, for both the initial issuance and every future `step ca renew` cycle's re-encryption.

**Verified by:** Reproduced directly (RabbitMQ crash-looping with the exact "Could not read key" error), fixed, then confirmed a full destroy/rebuild leaves both services with a valid, correctly-encrypted key that Apache/RabbitMQ start against successfully.

### F-059 — RabbitMQ's `listeners.tcp.default = none` / `management.tcp.port = none` are invalid cuttlefish syntax

**Where:** `rabbitmq-cloud-init.yaml.tftpl`'s `rabbitmq.conf`, written to disable the plain AMQP and management listeners now that both are TLS-only.

**Symptom:** `rabbitmq-server` failed to boot at all: `Error preparing configuration in phase transform_datatypes: Error transforming datatype for: management.tcp.port — "none" cannot be converted to a(n) integer` (and the same for `listeners.tcp.default`), a hard, unrecoverable boot failure on every start attempt.

**Root cause:** Cuttlefish's schema expects an integer (or IP) for these specific keys — the literal string `"none"` is not a special-cased disable value at `.default`/`.port` granularity, despite `none` being valid syntax elsewhere in RabbitMQ's config surface. The actual disable mechanism is different for each: `listeners.tcp = none` (the bare top-level key, no `.default`) is cuttlefish's real special case for AMQP; the management plugin has no equivalent bare-key special case at all — it's disabled simply by never setting `management.tcp.port` in the first place while `management.ssl.port` is configured.

**Fix:** Changed to `listeners.tcp = none` and removed the `management.tcp.port` line entirely.

**Verified by:** Reproduced directly (repeated `BOOT FAILED`/`failed_to_prepare_configuration` crash loop with the exact datatype error in `/var/log/rabbitmq/rabbitmq-server.log`), then confirmed the corrected config starts cleanly with `ss -tlnp` showing only `5671`/`15671` (no `5672`/`15672`).

### F-060 — RabbitMQ's `management.ssl.*` is a fully independent config stanza from `ssl_options.*` — it needs its own mTLS enforcement directives

**Where:** `rabbitmq-cloud-init.yaml.tftpl`'s `rabbitmq.conf`.

**Symptom:** A plain `curl` with no client certificate against the management HTTPS API (`:15671`) received a normal HTTP `401` (missing-credentials) response instead of being rejected at the TLS layer — the API was encrypted but not actually enforcing mutual TLS, unlike every other TLS surface in this stack.

**Root cause:** `ssl_options.verify`/`ssl_options.fail_if_no_peer_cert` (already set, governing the AMQP listener) have no effect on the management plugin's listener — it reads its own, separate `management.ssl.verify`/`management.ssl.fail_if_no_peer_cert`, which were never set.

**Fix:** Added `management.ssl.verify = verify_peer` and `management.ssl.fail_if_no_peer_cert = true` alongside the existing `management.ssl.*` cert/key/password lines.

**Verified by:** Confirmed via the running node's own effective config (`rabbitmqctl eval "application:get_env(rabbitmq_management, ssl_config)."`) showing both flags set, then a real curl round-trip: no client cert → TLS alert `certificate required`; valid client cert + Basic-Auth → `200 OK` with the full API response body.

### F-061 — Apache's `SSLPassPhraseDialog` cannot appear inside a `<VirtualHost>` block

**Where:** `keystone-cloud-init.yaml.tftpl`'s `:5443` vhost, adding passphrase support for the now-encrypted server key (same `var.admin_password` convention as F-058/RabbitMQ).

**Symptom:** `apache2` refused to start outright: `AH00526: Syntax error on line 7 of /etc/apache2/sites-enabled/keystone-tls.conf: SSLPassPhraseDialog cannot occur within <VirtualHost> section` — a hard config-parse failure, not a runtime issue.

**Root cause:** `SSLPassPhraseDialog` is a server-level (global) `mod_ssl` directive, not a per-vhost one — placing it inside `<VirtualHost *:5443>` is invalid regardless of context.

**Fix:** Moved it into its own file (`/etc/apache2/conf-available/keystone-ssl-passphrase.conf`), enabled via `a2enconf`, rather than inline in the vhost.

**Verified by:** `apache2ctl configtest` clean, `systemctl start apache2` succeeding, and a live TLS handshake against `:5443` completing using the encrypted key (proving the passphrase script was actually invoked and worked, not just that Apache started).

### F-062 — The Keystone package's default plain vhost is a site named `keystone`, not `wsgi-keystone`

**Where:** `keystone-cloud-init.yaml.tftpl`'s `setup-keystone-tls.sh`, disabling the plain `:5000` vhost now that `:5443` is the only listener.

**Symptom:** After the disable logic ran, `:5000` was still listening alongside `:5443` — the plain listener this whole change was meant to close remained open.

**Root cause:** The disable logic checked for `/etc/apache2/{conf,sites}-enabled/wsgi-keystone.conf`, an assumed filename that was never verified against a real install. The Ubuntu `keystone` package actually ships this as a **site** literally named `keystone` (`/etc/apache2/sites-available/keystone.conf`) — the check for a nonexistent filename silently matched nothing and disabled nothing.

**Fix:** Changed the check to the confirmed real filename/site name: `a2dissite keystone`.

**Verified by:** `ss -tlnp` on a rebuilt node showing only `:5443` listening, `:5000` gone entirely.

### F-063 — The boot-time role-import script's target (`127.0.0.1`) isn't in its own node's certificate SAN list

**Where:** `keystone-cloud-init.yaml.tftpl`'s `setup-keystone-roles.sh`, which runs `import_p3.py` against Keystone's own local API once it moved from plain `:5000` to mTLS `:5443`.

**Symptom:** Every import attempt failed with `CertificateError: hostname '127.0.0.1' doesn't match either of '<node-ip>', '<vip>'` — a TLS hostname-verification failure despite a perfectly valid certificate and correctly-presented client cert.

**Root cause:** Each node's own server certificate (`setup-keystone-tls.sh`'s `step ca certificate ... --san "$LOCAL_IP" --san "${vip_address}"`) only ever carried SANs for its real private IP and the VIP — never `127.0.0.1` — so connecting to `https://127.0.0.1:5443` can never pass hostname verification, regardless of certificate validity.

**Fix:** `setup-keystone-roles.sh` now computes its own `LOCAL_IP` (same `hostname -I` pattern already used elsewhere in this stack) and connects via that instead of `127.0.0.1`.

**Verified by:** Manually re-ran the corrected script against a live node — both `roles.yml` and `system_domain.yml` imports completed with `Finished`, no certificate errors.

### F-064 — `setup-keystone-tls.sh` enabled the new `:5443` site but never reloaded the already-running Apache process before the role-import script needed it

**Where:** `keystone-cloud-init.yaml.tftpl`'s `runcmd` ordering — `setup-keystone.sh` → `setup-keystone-tls.sh` → `setup-keystone-roles.sh`, with a single trailing `systemctl restart apache2` previously placed as the very last `runcmd` step.

**Symptom:** Every role-import attempt failed with a plain connection-level error — `Failed to establish a new connection: [Errno 111] Connection refused` — for the entire 300-second readiness-retry window, despite `:5443`'s vhost config having just been written and `a2ensite`'d correctly.

**Root cause:** `a2ensite`/`a2dissite`/`a2enconf` only change what Apache *would* load on its next start/reload — they don't touch the `apache2` process already running (auto-started earlier by cloud-init's `packages:` stage against the OLD, now-superseded `:5000`-only config). Since the only restart in the whole boot sequence was the very last `runcmd` step, `setup-keystone-roles.sh` (which runs *before* that) always found `:5443` genuinely not listening, no matter how long it waited.

**Fix:** Moved the `systemctl restart apache2` call into the end of `setup-keystone-tls.sh` itself, immediately after the site/config changes — so `:5443` is live before anything downstream depends on it. Removed the now-redundant trailing `runcmd` step.

**Verified by:** Full destroy/rebuild — `grep -c 'did not complete after 3 attempts' /var/log/cloud-init-output.log` went from 2 (both imports failing) to 0, with the log showing real import activity (`Create role`, `Create user`, `Finished`).

### F-065 — Keystone's role/user import raced both nodes against the same shared MySQL data, producing intermittent 409s

**Where:** `keystone-cloud-init.yaml.tftpl`'s `setup-keystone-roles.sh`, run identically on both Keystone nodes (this tier is "genuinely active-active, no per-node role" by design — see this file's own header comment).

**Symptom:** On one otherwise-fully-fixed rebuild (all of F-061–F-064 already applied), `keystone-02`'s `roles.yml` import still failed after 3 attempts — `Create role implication error 409` followed by a secondary `RuntimeError: No active exception to reraise` inside `import_p3.py`'s own exception handling, crashing the whole import.

**Root cause:** Correctly identified by direct question, not initially caught: Keystone has no data replication of its own — every role/domain/user this script imports lives entirely in the one shared `keystone` MySQL database both nodes already connect to. `keystone-manage db_sync`/`bootstrap` are safe to run concurrently on both nodes because they're properly idempotent SQL operations against that shared store, but `import_p3.py`'s own check-then-create pattern is not — running it from both nodes at once is a genuine, if narrow, race for the same inserts. `keystone-02`'s run in this instance found 128 roles already created by `keystone-01` and then lost a race creating one of the last role implications.

**Fix:** Gated the actual import behind a hostname check — `case "$(hostname)" in *-01) ;; *) exit 0 ;; esac` — so only the first node (`modules/instance-group`'s own documented naming convention guarantees `-01` exists as long as any instance does) runs `import_p3.py` at all; every other node is a clean no-op, relying on `-01` having already populated the shared database. `db_sync`/`bootstrap` are unaffected and still run on every node.

**Verified by:** Full destroy/rebuild — `keystone-01`'s log showed the real import completing cleanly (0 failures, hundreds of lines of genuine `Create role`/`Create user` activity); `keystone-02`'s log showed the new skip message and zero `import_p3.py` errors or tracebacks of any kind.

### F-066 — F-060's own fix (mTLS on RabbitMQ's management API) made the browser-based admin console unreachable

**Where:** `rabbitmq-cloud-init.yaml.tftpl`'s `rabbitmq.conf`, `management.ssl.*`.

**Symptom:** Reported from a separate ("fresh install") machine after pulling F-058–F-065's fixes: browsing to the RabbitMQ management console on any of the 3 nodes showed the server as unavailable. RabbitMQ itself was confirmed healthy — `active (running)`, correct `rabbitmq.conf`, `:5671`/`:15671` listening and nothing else — the node was never actually broken.

**Root cause:** F-060 added `management.ssl.verify = verify_peer`/`fail_if_no_peer_cert = true` to match "every other TLS surface in this stack," treating the management API the same as AMQP. But a plain web browser has no client certificate to present — the TLS handshake itself is refused before RabbitMQ ever serves a login page, which looks exactly like a dead server, not an auth prompt. This was an unforced design decision (extending F-060's fix by analogy) rather than something the user had actually asked for on the management API specifically — the earlier confirmation to require a client cert was scoped to AMQP.

**Fix:** Per direct instruction, removed `management.ssl.verify`/`fail_if_no_peer_cert` (and the now-unused `management.ssl.cacertfile`) — the management API is server-TLS-only (HTTPS-encrypted, ordinary username/password login, reachable from any browser). AMQP's `ssl_options.*` mTLS enforcement is unchanged.

**Verified by:** Full destroy/rebuild — management API confirmed reachable without a client cert (`401` unauthenticated, `200` with just Basic-Auth credentials); AMQP's effective config (`rabbitmqctl eval "application:get_env(rabbit, ssl_options)."`) confirmed still `fail_if_no_peer_cert=true, verify=verify_peer`, unaffected.

### F-067 — `nfs-common` wasn't cached in the host-level package repo, so the new frontend/backend NFS mount silently never ran

**Where:** `api/build-package-repo.sh` (`PACKAGES` array) and the new `setup-nfs-mount.sh` (added to both `frontend-cloud-init.yaml.tftpl` and `backend-cloud-init.yaml.tftpl` for the new `module.nfs` shared-storage tier).

**Symptom:** First real build after adding the shared NFS export: every frontend/backend node's `packages:` list (now including `nfs-common`) failed apt install — `cloud-init.log` showed `apt.py[DEBUG]: The following packages were not found by APT so APT will not attempt to install them: ['nfs-common']`. `/mnt/shared` was never created on the affected nodes, `showmount`/`mount.nfs` weren't installed, and `setup-nfs-mount.sh` had nothing to work with.

**Root cause:** This project's host-level local package repo (§7, `haFullStack-LLD.md`) is a curated, pinned subset of Ubuntu's archive built once per host by `api/build-package-repo.sh` and served from a throwaway builder VM — not a passthrough mirror of the real Ubuntu archive. Adding `nfs-common` to a template's own `packages:` list is necessary but not sufficient: the apt source every node actually points to (`http://192.168.100.1:8090/jammy/apt-repo`) only ever contains what `build-package-repo.sh`'s own `PACKAGES` array named, and `nfs-common` had never been added there. Same class of gap as this session's earlier `python3-openstackclient`-on-Keystone finding — a per-host build artifact (`api/package-repo/`, gitignored) that a template change alone can't populate.

**Fix:** Added `nfs-common` to `build-package-repo.sh`'s default `PACKAGES` array (ha-frontend-lb section) and ran a full rebuild (`bash api/build-package-repo.sh jammy`, throwaway builder VM, confirmed `nfs-common_1:2.6.1-1ubuntu1.2_amd64.deb` present in the regenerated `api/package-repo/jammy/apt-repo/`).

**Verified by:** Full destroy/rebuild after the repo rebuild — fresh frontend and backend nodes both installed `nfs-common` successfully (`dpkg -l` showed it `ii`/configured on the node that completed cloud-init cleanly). `/mnt/shared` mounted (`192.168.100.35:/exports/shared` over NFSv4.2, ~9.8G available, matching `var.nfs_disk_gb`'s 10GB default), `/etc/fstab` carried the persistent entry, and a real cross-node read/write round-trip was confirmed: a file written from the frontend node's `/mnt/shared` was read back correctly from the backend node's `/mnt/shared`, and vice versa — proving a genuine shared export, not two independent local directories. One node (`frontend-01`) needed `setup-nfs-mount.sh` re-run manually after boot — its cloud-init run stalled partway through an unrelated step (`dpkg -i` of the `step-cli` package) under the resource contention of all 16 nodes building concurrently against the same package-repo host, the same pre-existing `package_update_upgrade_install`-class flakiness already noted elsewhere in this log (e.g. RabbitMQ's own apt install, and F-028's cold-start note) — not a defect in the NFS mount script itself, which ran to completion correctly the moment it was actually invoked.

### F-068 — A freshly created NFS server's export directory doesn't exist yet at the moment its status first reads `running`

**Where:** `api/nfs.py`'s new `list_files()`/`upload_file()` (Dashboard "Files" drag-and-drop panel, `GET`/`PUT`/`DELETE .../shares/{name}/files...`, built to give a real way to get data — large tarballs ahead of the pending application install — onto a share without SFTP-ing to a separate mounting instance first).

**Symptom:** Uploading to a share on an NFS server that had just reached `status: running` (~90s after creation) failed: `tee: /exports/data/test-tarball.tar.gz: No such file or directory`, `502 Bad Gateway`.

**Root cause:** `get_nfs_server_status()` reflects the libvirt *domain* state (the VM process has started), not whether cloud-init has finished running inside the guest. `_cloud_init_iso()`'s own `runcmd` does `pvcreate`/`vgcreate`/`lvcreate`/`mkfs.ext4`/`mount` on the data disk, *then* `mkdir`s each share's export directory, *then* `exportfs -ra` — real, measurably slow steps (package install + LVM + filesystem creation) that can still be mid-flight well after the domain itself is `running` and even after SSH is reachable (SSH's own `authorized_keys` is written by an earlier, faster `write_files` step in the same cloud-init). A share that's already a member of `nfs.shares` (and so already listable/uploadable through the Dashboard) can therefore predate its own directory actually existing on disk.

**Fix:** `list_files()`/`upload_file()` now `sudo mkdir -p` the target export directory as the first step of the same SSH command that lists or writes to it — idempotent and harmless once cloud-init has genuinely finished (the directory already exists by then), and self-healing during the boot-time race rather than requiring a caller-side retry/wait loop.

**Verified by:** Reproduced directly — a 50MB upload attempt ~90s after a real NFS server reached `running` hit exactly this error. Fixed, API server restarted, same upload retried successfully (`{"name":"test-tarball.tar.gz","size":52428800}`, ~3.3s over SSH); content integrity confirmed via `sha256sum` on both the original file and the copy read directly off the export (`ssh ubuntu@<nfs-ip> sha256sum /exports/data/...`) — identical. List and delete also exercised successfully against the same server; a set of filename-validation probes (URL-encoded `../`, shell metacharacters, a leading-dot hidden-file name) were each correctly rejected (`404` from Flask's own single-segment route converter, or `400` from `_validate_filename()`).

### F-069 — CloudCore's own internal `dnsmasq` resolver (127.0.0.1:5353) had been silently down for two days — a stopped process's pidfile deletion doesn't mean the process actually died

**Where:** `api/dns_server.py` — `stop()`/`reload()`, called on every DNS-record change and on every `api/server.py` start.

**Symptom:** Noticed incidentally (unrelated to the NFS work in progress) via `dns_server: dnsmasq failed to start: dnsmasq: failed to create listening socket for 127.0.0.1: Address already in use` in a fresh `api/server.py` startup log. `api/dns/dnsmasq.pid` was empty/missing, yet a real `dnsmasq --conf-file=.../api/dns/dnsmasq.conf` process (`ps aux`) had been running continuously since 2026-09-12 — two days, surviving every `api/server.py` restart in between, each one failing to bind port 5353 the same way and just logging a warning rather than resolving it.

**Root cause:** `stop()` sent a single best-effort `SIGTERM` to the tracked PID and then *unconditionally* deleted the pidfile regardless of whether the process actually exited — daemonized dnsmasq children don't reliably die from a bare `SIGTERM` sent by a process that isn't their direct parent. Some earlier `stop()`/`reload()` cycle (predating this session) hit exactly that: the signal didn't take, the pidfile vanished anyway, and every subsequent `_is_running()` check (which only ever trusts the pidfile) reported "not running" — so `start()` kept trying to bind a port a real, un-tracked orphan already held, failing the exact same way every time with no mechanism to ever notice or recover.

**Fix:** New `_terminate_pid()` (used by both `stop()` and the new `_reap_orphans()`) sends `SIGTERM`, polls for actual exit for up to 2s, then escalates to `SIGKILL` if the process is still alive — `stop()` no longer deletes the pidfile on faith. `start()` now also calls `_reap_orphans()` first, which finds *any* `dnsmasq` process invoked with this module's own `--conf-file=` path via `pgrep -f` (safe to target unconditionally — that exact conf-file argument can only ever be a process this module itself started in some earlier, uncleanly-ended run) and kills it before attempting to bind, so a pidfile-less orphan from a past crash/force-kill no longer blocks every future start indefinitely.

**Verified by:** Reproduced live — confirmed the real orphaned process (PID from `ps aux`, running since Sep 12) via a raw DNS query direct to `127.0.0.1:5353` (a real configured PTR record answered correctly, proving *a* dnsmasq was serving, just not one CloudCore's own code could see or manage). Fixed, then killed `api/server.py` outright (`kill`, no clean shutdown — the same failure mode that caused this) with the orphan still present, and restarted it: log showed `dns_server: reaping orphaned dnsmasq pid 3537212 (no pidfile tracked it)`, the orphan was gone from `ps aux` immediately after, a fresh `dnsmasq` process started and bound port 5353 successfully with an accurate pidfile, and both a made-up hostname (correctly `REFUSED`/`Not Ready` — expected, no upstream forwarder configured) and the pre-existing real PTR record (correctly resolved, `NOERROR`) confirmed the new instance was genuinely serving queries, not just running.

### F-070 — RabbitMQ's and Keystone's `step-renew.service` has always been broken: `step ca renew` cannot decrypt an already-passphrase-encrypted key on its own

**Where:** `examples/ha-frontend-lb/files/{keystone,rabbitmq}-cloud-init.yaml.tftpl` and the identically-ported `ansible/examples/templates/ha-frontend-lb/{keystone,rabbitmq}-cloud-init.yml.j2` — both tiers' `step-renew.service` `ExecStart=`. Found during the Ansible port's own live verification (§11.4, `haFullStack-LLD.md`), but the command is byte-identical to the already-shipped OpenTofu version — this bug has been latent there since RabbitMQ's and Keystone's server keys were first passphrase-protected (v0.21/v0.29 in this log), never noticed because every prior verification pass checked the status pages and the live listener, never `systemctl status step-renew` specifically.

**Symptom:** On a fresh build, both Keystone nodes' and all 3 RabbitMQ nodes' `step-renew.service` were `failed`, having crash-looped 5 times in under 4 seconds immediately on first start before hitting systemd's restart-rate limit ("Start request repeated too quickly") and giving up permanently. `journalctl -u step-renew` showed `error loading private key: : no such device or address` on every attempt. The *current* listener (Apache `:35357`, RabbitMQ `:5671`) was unaffected — only future certificate auto-renewal was silently broken, from the very first boot.

**Root cause:** `setup-{keystone,rabbitmq}-tls.sh` encrypts the freshly-issued key in place with `openssl pkey -aes256` before `step-renew.service` is ever created. That unit's `ExecStart` then calls `step ca renew <crt> <key> --daemon --force ...` directly against that same now-encrypted key file — but plain `step ca renew` has no way to decrypt it to prove possession for the renewal CSR, and `--force` (confirmed directly) makes the daemon attempt a renewal immediately on every start rather than waiting near expiry, so the failure isn't a maybe-someday problem, it fires on the very first `systemctl enable --now step-renew`, every time. ProxySQL's and NGINX's own `step-renew-*` units were never affected — neither of those two tiers encrypts its key at all, so they never exercise this path. `step ca renew --help` confirms the actual fix: a `--password-file` flag exists ("the file containing the password to encrypt or decrypt the private key") that plain `step ca renew` was never given.

**Fix:** Both tiers now write a second, plain-content password file (`{keystone,rabbitmq}/server.key.passfile` — distinct from Keystone's existing `server.key.password.sh`, which is an *executable* Apache `SSLPassPhraseDialog exec:` target, not something `step`'s own `--password-file` can read directly) and pass `--password-file <that file>` to `step ca renew`. The unit's own `--exec` re-encryption hook (`openssl pkey -aes256 -in ... -out ... -passout ...`) also needed `-passin pass:'...'` added — once `step ca renew` succeeds, the key it leaves on disk is already correctly re-encrypted with the same passphrase (confirmed directly), so the hook's own `openssl pkey -in` call was now reading an encrypted file it had no way to open either, the identical class of bug one layer down.

**Verified by:** Reproduced live on real nodes during the Ansible port's own verification build (2 Keystone, 3 RabbitMQ nodes, all `step-renew` failed identically). Confirmed the bare fix in isolation first (`step ca renew ... --password-file ...` by hand: succeeded, `exit 0`, and the resulting key file was still `BEGIN ENCRYPTED PRIVATE KEY`). Then patched the full unit (both the new `--password-file` flag and the `--exec` hook's `-passin` fix) live on one Keystone node and one RabbitMQ node, reset each unit's failure state, and restarted: both reached `active (running)` with `NRestarts=0` after 10+ seconds settled, and their respective listeners (`:35357`, `:5671`/`:15671`) stayed up throughout. Applied the same fix to both the OpenTofu `.tftpl` source files and the Ansible `.yml.j2` templates, keeping the two IaC front-ends in parity rather than letting only the Ansible port end up fixed.

---
### F-071 — The `loki` .deb never creates or owns its own data directory, so the service crash-loops on every first boot

**Where:** `examples/ha-frontend-lb/files/logging-cloud-init.yaml.tftpl` and its Ansible twin `ansible/examples/templates/ha-frontend-lb/logging-cloud-init.yml.j2` — the new single-node centralized-logging tier (`haFullStack-LLD.md` new §12), Loki's own `setup-logging.sh`.

**Symptom:** On the very first real build after this tier was added, `loki.service` crash-looped continuously (`NRestarts` climbing past 50 within a couple of minutes) and never bound port `3100` at all — `journalctl -u loki` showed `mkdir /var/lib/loki: permission denied`, `error initialising module: ruler-storage` on every attempt. Grafana came up fine; only Loki was affected.

**Root cause:** `dpkg -L loki` confirms the package genuinely ships no `var/lib` entries whatsoever — unlike Grafana's own `.deb`, which does create and own `/var/lib/grafana` in its postinst, Loki's package leaves creating and owning its own `path_prefix` (`/var/lib/loki`, set in this stack's own `/etc/loki/config.yml`) entirely up to whoever configures it. `loki.service`'s unit file runs as `User=loki`, a low-privilege system account that has no permission to `mkdir` under `/var/lib` (owned `root:root 755`) — so the very first thing the process tries to do on every single boot fails immediately, with no self-recovery possible since the directory is never created by anything else either.

**Fix:** `setup-logging.sh` now does `mkdir -p /var/lib/loki && chown -R loki:nogroup /var/lib/loki` immediately before `systemctl enable --now loki` — no dedicated `loki` group exists either (`getent passwd loki` shows its primary GID is `65534`/`nogroup`), so that's the group used rather than the more obvious-looking but nonexistent `loki:loki`.

**Verified by:** Reproduced live on the real first-boot node (the exact `mkdir`/`permission denied` error above). Fixed the file, applied the same two commands live to confirm the mechanism (`chown loki:loki` was tried first and correctly rejected — `invalid group` — before finding the right group), then did a full `tofu destroy`/`tofu apply` cycle from cold and confirmed `NRestarts=0` on `loki.service` with the service bound and serving (`/ready` → `ready`) on the very first boot, no manual intervention. Applied identically to the Ansible `.yml.j2` twin and confirmed the same zero-restart result on an independent Ansible-built stack.

### F-072 — `promtail`'s own service user isn't a member of the `adm` group, so it can never read `/var/log/cloud-init-output.log`

**Where:** All 8 tier cloud-init templates that ship promtail (`ca`, `mysql`, `proxysql`, `keystone`, `memcached`, `rabbitmq`, `backend`, `frontend` — both the `.tftpl` and `.yml.j2` versions of each).

**Symptom:** `journalctl -u promtail` on every non-logging node showed a continuous stream of `failed to start tailer error="open /var/log/cloud-init-output.log: permission denied"` every ~10s, indefinitely — the journal-scrape stage worked fine, but the single log file this whole project's own debugging has repeatedly reached for (see F-028, F-037, F-042, F-045, F-052 and others, all first spotted here) was never actually shipped to Loki.

**Root cause:** `/var/log/cloud-init-output.log` is `root:adm 0640` on this distro — readable by root or anyone in the `adm` group, no one else. The `promtail` .deb creates its own unprivileged `promtail` system user (`getent passwd promtail` → primary group `nogroup`, same pattern as `loki` in F-071) but never adds it to `adm`, and nothing in this stack's own cloud-init did either — the journal-scrape stage worked because journald grants its own separate, broader read access that doesn't depend on Unix file permissions at all, masking the fact that the file-scrape stage was silently failing the entire time.

**Fix:** Added `usermod -aG adm promtail` to every one of the 8 tier templates' `runcmd`, immediately before `systemctl enable --now promtail`.

**Verified by:** Reproduced live (the exact `permission denied` loop above) on `mysql-a`, `keystone-01`, and `frontend-01`. Applied the fix live to all three and confirmed `promtail`'s own log immediately showed `"tail routine: started" path=/var/log/cloud-init-output.log` with no further errors. Then confirmed via a full cold `tofu destroy`/`apply` cycle that a real `cloud-init-output.log` stream for `backend-01` was queryable through Loki with genuine content, and separately confirmed the identical fix on the Ansible-built stack.

### F-073 — `promtail`'s package postinst auto-starts the service before this stack's own `runcmd` gets a chance to fix up the `__HOSTNAME__` placeholder — same first-boot race class as Grafana's admin password

**Where:** The same 8 tier templates as F-072, plus `examples/ha-frontend-lb/files/promtail-config.yml.tftpl`/its Ansible twin (the shared config fragment that defines the `__HOSTNAME__` placeholder).

**Symptom:** After fixing F-072, Loki's own `host` label values included a literal `"__HOSTNAME__"` alongside every node's real hostname — a handful of log lines from every node had genuinely been ingested under the wrong, unsubstituted label before eventually switching over to the correct one partway through the same boot.

**Root cause:** Like every other package in this stack, `promtail`'s own postinst starts the service immediately once installed — but `write_files` (which writes `/etc/promtail/config.yml`, still containing the literal `__HOSTNAME__` placeholder at that point) runs before `packages:` installs the `.deb`, and this stack's own `sed "s/__HOSTNAME__/$(hostname)/"` fixup only runs later still, in `runcmd`. `journalctl -u promtail` confirmed the exact sequence directly: `Started Promtail service` (postinst, `host="__HOSTNAME__"` in its very first "Adding target" log line) followed roughly two minutes later by `Stopped`/`Started` again (this stack's own `runcmd`-driven restart, after which the label was correct) — the identical first-boot race already known and handled for Grafana's admin password (`logging-cloud-init.yaml.tftpl`'s `systemctl stop grafana-server || true; rm -f /var/lib/grafana/grafana.db` sequence), just not yet applied to promtail.

**Fix:** Added `systemctl stop promtail || true` immediately before the existing `sed` fixup in all 8 templates' `runcmd`, and removed the now-redundant separate `systemctl restart promtail` line that used to follow `systemctl enable --now promtail` — the package's own auto-started, wrongly-labeled instance is now stopped before the config is corrected, so `enable --now` performs the only real start, against the already-correct config, rather than starting twice.

**Verified by:** Reproduced live via `journalctl -u promtail | grep -E 'Started|Adding target'` on `frontend-01`, showing the exact two-start sequence and the mislabeled first "Adding target" line. Fixed all 8 templates, then a full cold `tofu destroy`/`apply` cycle confirmed Loki's `host` label values (`/loki/api/v1/label/host/values`) contained only real hostnames with no `__HOSTNAME__` entry anywhere — checked again after every tier had finished booting, including the slow Keystone tier. Confirmed identically on the Ansible-built stack.

*(Also observed, not a bug: Grafana's first-boot migration — run every time because `setup-logging.sh` deliberately deletes `/var/lib/grafana/grafana.db` to force the configured admin password, same technique as F-071/F-072's neighbor — took roughly 6 minutes to complete on a `standard.small` node before the UI became reachable on `:3000`. Expected, not investigated further; worth knowing before assuming the node has failed to boot.)*

### F-074 — RabbitMQ's own log files are group-owned `rabbitmq`, not `adm`, so promtail can't read them even with F-072's fix applied

**Where:** `examples/ha-frontend-lb/files/rabbitmq-cloud-init.yaml.tftpl` and its Ansible twin, extending the per-service log-file scrape coverage added to the shared `promtail-config.yml` (`job_name: rabbitmq`, `__path__: /var/log/rabbitmq/*.log`) beyond the journal-only coverage F-071–F-073 already fixed — prompted directly by "are we watching the instance logs / the general logs as well (in fact all logs we can)?".

**Symptom:** `journalctl -u promtail` on every RabbitMQ node showed `failed to start tailer error="open /var/log/rabbitmq/rabbit@<host>.log: permission denied"`, even though F-072's `usermod -aG adm promtail` was already in place — the journal and cloud-init-output stages both worked fine; only this new file target failed.

**Root cause:** `ls -la /var/log/rabbitmq/` confirmed the directory and its log files are owned `rabbitmq:rabbitmq 640`, not `root:adm` like `cloud-init-output.log` — an entirely separate group from the one F-072 already fixed, so that fix's own group membership doesn't help here at all.

**Fix:** Added `usermod -aG rabbitmq promtail` immediately after the existing `usermod -aG adm promtail` line. Group membership (not a file-specific ACL, contrast F-075 below) is fine here specifically because no credential or key material lives in `/var/log/rabbitmq/` — unlike ProxySQL's own log directory.

**Verified by:** Reproduced live on `rabbitmq-a` (the exact `permission denied` line above), applied the fix live and confirmed `promtail`'s own log immediately picked up the target with no further errors, then confirmed via a real cross-tier Loki query that genuine RabbitMQ log content (not just the journal-sourced `rabbitmq-server.service` unit stream) was reaching Grafana's Explore view, labeled `job="rabbitmq"`.

### F-075 — ProxySQL's log file shares a group (and 660 permissions) with its own TLS private key in the same directory — a naive group-membership fix for promtail would also grant it key-read access

**Where:** `examples/ha-frontend-lb/files/proxysql-cloud-init.yaml.tftpl` and its Ansible twin — same per-service log-file coverage effort as F-074, this stack's other tier with a non-`adm`-owned log file.

**Symptom:** Not a runtime symptom — caught by inspection before it ever shipped. `ls -la /var/lib/proxysql/` showed `proxysql.log` owned `proxysql:proxysql 660`, the exact same owner, group, and mode as `proxysql-key.pem` (this node's own TLS private key, written by `setup-proxysql-tls.sh`, §5.3) sitting in the same directory.

**Root cause:** ProxySQL's own data directory (`/var/lib/proxysql`) is where it keeps both its runtime log and the TLS material this stack copies in for NGINX/Keepalived's benefit (§5.3.1's own `proxysql-cert.pem`/`proxysql-key.pem`/`proxysql-ca.pem`) — unlike RabbitMQ (F-074), there was no group that covered the log file without also covering the private key. Applying F-074's own fix verbatim (`usermod -aG proxysql promtail`) would have technically worked for shipping the log, but would have also handed promtail's OS user plain read access to `proxysql-key.pem` — a real, avoidable credential-exposure widening that a "just make it match the other tier's fix" shortcut would have introduced silently.

**Fix:** A POSIX ACL on the one file instead of group membership on the whole directory: `setfacl -m u:promtail:r /var/lib/proxysql/proxysql.log`, added to `packages:` (`acl`, not installed by default on this image) and to `runcmd`, placed *after* `setup-proxysql-tls.sh`'s own `systemctl start proxysql` so the ACL lands on the file the running process actually holds open, not a pre-restart copy. promtail's read access is scoped to that one filename; `proxysql-key.pem`'s own permissions are completely untouched.

**Verified by:** Confirmed via `getfacl /var/lib/proxysql/proxysql.log` on a live node that promtail has read access to the log alone (`sudo -u promtail cat proxysql-key.pem` still correctly denied), then confirmed via a real cross-tier Loki query that genuine ProxySQL log content was reaching Grafana's Explore view, labeled `job="proxysql"`.

### F-076 — F-073's own fix ran too late in ProxySQL's `runcmd` to actually close the `__HOSTNAME__` race for its new per-service log-file target

**Where:** All 8 tier cloud-init templates (`.tftpl` and `.yml.j2`), found via `ha-frontend-lb`'s host-level-Loki migration (`haFullStack-LLD.md` §12.3) — the live re-verification this migration required is what caught it, not new code from the migration itself.

**Symptom:** A real, time-bounded Loki query (`{job="proxysql"}`, last 15 minutes) still returned rows labeled `host="__HOSTNAME__"` after a completely clean rebuild — the exact symptom F-073 was supposed to have eliminated for good, but scoped to only the `proxysql` job; `nginx`/`mysql`/`rabbitmq`/`apache2` all showed clean, correctly-labeled data from the same rebuild.

**Root cause:** F-073's own fix (`systemctl stop promtail || true; sed ...; systemctl enable --now promtail`) was correct in mechanism but wrong in *placement* — every tier except `frontend` ran it at the *end* of `runcmd`, after that tier's own primary service-setup script(s). For most tiers this window was short enough in practice to not visibly manifest (or, per F-074/F-075's own addition of new per-service file targets, simply hadn't been specifically checked against a time-bounded query before). `proxysql` was the one tier where the gap was genuinely large: `install-proxysql.sh` (which starts writing `proxysql.log`) ran as `runcmd`'s *first* step, while the promtail fix sat at the very end — after `setup-proxysql-tls.sh`, `render-keepalived-conf.sh`, `setup-nginx-tls.sh`, and the nginx/keepalived restarts, several real steps involving `curl` calls, `dpkg` installs, and TLS issuance against the CA. `journalctl -u promtail` on a live node confirmed the exact mechanism: the package's own postinst-started, `__HOSTNAME__`-labeled instance registered its `proxysql.log` file target at `15:47:56`; the F-073 fix's own `Stopped`/`Started` cycle didn't run until `15:48:26` — a real ~30-second window, long enough for genuine ProxySQL log content to be shipped under the wrong label before the fix took effect.

**Fix:** Moved the whole F-072/F-073 promtail-fix block (`systemctl stop promtail`, the `__HOSTNAME__` `sed`, `usermod -aG adm` [+ `usermod -aG rabbitmq` where applicable], `systemctl enable --now promtail`) to the very *first* steps of every tier's `runcmd`, before any other service-starting script — matching the ordering `frontend`'s own template already happened to use. For ProxySQL specifically, the file-specific `setfacl` step (F-075) necessarily stays where it was, after `install-proxysql.sh`/`setup-proxysql-tls.sh` create and restart the service — but no longer needs to also gate promtail's own restart, since promtail (now already running, correctly labeled, before ProxySQL even exists) simply starts succeeding on its own next file-target poll once the ACL is granted, no promtail restart required.

**Verified by:** Reproduced live via `journalctl -u promtail | grep -E 'Started|Stopped|Adding target'` on a real `proxysql-a` node showing the exact ~30s exposure window and the mislabeled target registrations. Applied the reordering fix to all 8 templates on both IaC front-ends, then a full cold `tofu destroy`/`apply` cycle followed by a *time-bounded* Loki query (`start=<now-15m>`, not the unbounded default a first pass had mistakenly relied on and been misled by leftover data from the pre-fix build reusing the same suffix) confirmed zero `__HOSTNAME__` rows across every job label (`journal`, `cloud-init-output`, `nginx`, `mysql`, `rabbitmq`, `apache2`, `proxysql`), `NRestarts=0` held for both `loki` and `promtail` on spot-checked nodes, and the F-075 key-safety property (`getfacl`, `sudo -u promtail cat proxysql-key.pem` denied, zero key/cert mentions in `journalctl -u promtail`) still held unchanged.

### F-077 — Two real gaps found asking "is Loki actually reachable?" on a fresh install: `setup-logging-service.sh` was never mentioned as a runnable step in the README, and `teardown-network.sh`'s own safety guard never learned about it

**Where:** `README.md` (top-level "Getting Started" flow) and `api/teardown-network.sh`.

**Symptom:** On a separate, previously-set-up machine, `192.168.100.1:3000`/`:3100` were both unreachable — reported directly as "the ip address is never created" investigating a Sentinel question. Diagnosis showed the `ccbr0` bridge and the cached `grafana`/`loki` `.deb`s were both present and correct, but `loki.service`/`grafana-server.service` didn't exist at all: `sudo bash api/setup-logging-service.sh` (added this session, §12.3) had genuinely never been run there. Separately, `teardown-network.sh`'s own active-service guard — added specifically to stop someone silently cutting guests off from `cloudcore-repo` by tearing down the bridge while it's still bound to `192.168.100.1:8090` — was never updated when `loki`/`grafana-server` started binding that same gateway address too, so it would have let someone do the exact same silent-cutoff thing to logging instead.

**Root cause:** `setup-logging-service.sh` was always printed by `scripts/install.sh`'s own terminal output (and mentioned in passing in `README.md`'s Sentinel section), but never got its own dedicated, browsable README section the way `build-package-repo.sh` already had — easy to miss in a wall of install output, with no durable place to rediscover it later. `teardown-network.sh` predates the logging service entirely (written for `cloudcore-repo` alone) and nothing flagged it as needing an update when a second host-level service started sharing the same bridge gateway address.

**Fix:** Added a full "Set up centralized logging (Loki + Grafana)" section to `README.md`, mirroring "Populate the package repo"'s own structure (command block, the admin-password env var, a settings table, explicit note that Sentinel depends on it). `teardown-network.sh`'s guard now loops over `cloudcore-repo`, `loki`, and `grafana-server` rather than checking only the first. Also, Sentinel's own `install.sh` (separate repo) now checks Loki's reachability at the end of every install and, if it's down, diagnoses which of the three CloudCore-side steps (bridge / package cache / `setup-logging-service.sh`) is actually missing and prints the exact command to fix it — rather than a fresh Sentinel install silently reporting "running" with nothing to watch.

**Verified by:** Reproduced the diagnostic sequence live against the actual affected machine's reported `ip addr show ccbr0` / `systemctl is-active loki grafana-server` / package-repo `ls` output — confirmed the exact "debs cached, service never installed" case. `teardown-network.sh -n` syntax-checked; its new loop confirmed correct against this machine's own live `cloudcore-repo`/`loki`/`grafana-server` state (all three active, so it correctly refuses without `--force`). Sentinel's `install.sh` diagnostic block tested directly against both a reachable and a deliberately-unreachable `SENTINEL_LOKI_URL`, confirmed it correctly distinguishes "bridge missing" / "debs not cached" / "service not installed" using this same machine's real `CLOUDCORE_DIR`.

### F-078 — Grafana's Loki datasource provisioning never set a fixed `uid`, so every install gets a different, unpredictable one — breaks any deep link built against a known value

**Where:** `api/setup-logging-service.sh`'s own `/etc/grafana/provisioning/datasources/loki.yaml` — surfaced building Sentinel's new "View in Grafana" links (a separate repo's own feature, `sentinel/ui/index.html`), which need to reference the datasource by `uid` in Grafana's Explore URL scheme.

**Symptom:** Not a runtime failure — caught checking `GET /api/datasources` on this session's own live host-level Grafana before building anything that depended on it: `"uid": "P8E80F9AEF21F6940"`, an opaque, Grafana-generated value with no relationship to anything this project controls. A deep link hardcoding that value would work on this one machine and silently break on literally every other install, each of which would get its own different random uid.

**Root cause:** The datasource provisioning YAML (`apiVersion: 1` → `datasources:`) never set `uid:` explicitly. Grafana provisions a random one whenever it isn't given, which is fine for a human clicking through the UI (Grafana resolves "Loki" by name there) but useless for anything — like a URL — that has to reference the datasource before a human is looking at a screen.

**Fix:** Added `uid: loki` to the datasource YAML in `setup-logging-service.sh`. Fixed, human-chosen, identical on every install from here on — exactly what Sentinel's new `SENTINEL_GRAFANA_DATASOURCE_UID` (default `"loki"`) now assumes. Not yet re-applied to this session's own already-provisioned host (Grafana's provisioning sync only picks up YAML changes on its own next restart) — a one-line `sed` + `systemctl restart grafana-server`, not run automatically since it touches a live host-level service rather than a disposable guest.

**Verified by:** Confirmed the stale, random uid directly via `GET /api/datasources` before the fix. The fix itself is a one-line provisioning change with no separate runtime logic to verify beyond a restart re-reading it — deferred to whenever this host (or any other) next runs `setup-logging-service.sh`/restarts `grafana-server`.

### F-079 — RabbitMQ's own `systemd` unit redirects stdout/stderr straight to two `root`-owned log files, unreachable by any group fix

**Where:** `examples/ha-frontend-lb/files/rabbitmq-cloud-init.yaml.tftpl`/its Ansible twin, and `examples/openstack-services/files/rabbitmq-cloud-init.yaml.tftpl`/the inline rabbitmq block in `08-openstack-services.yml` — found via a genuinely unrelated task (testing Sentinel's new "View in Grafana" links against real event data), which surfaced a real `promtail` permission-denied error sitting in the journal from an already-torn-down build.

**Where from — extracted directly from the cached `.deb`:** `rabbitmq-server`'s own packaged `/lib/systemd/system/rabbitmq-server.service` sets `User=rabbitmq Group=rabbitmq` for the process itself, but also `StandardOutput=append:/var/log/rabbitmq/rabbitmq-server.log` and `StandardError=append:/var/log/rabbitmq/rabbitmq-server.error.log`.

**Symptom:** `journalctl -u promtail` on a RabbitMQ node showed `failed to start tailer error="open /var/log/rabbitmq/rabbitmq-server.log: permission denied"` (and the `.error.log` twin) — despite F-074's own `usermod -aG rabbitmq promtail` fix already being in place and confirmed working for RabbitMQ's *other* log file, `rabbit@<host>.log`.

**Root cause:** `systemd` (running as root, PID 1) is what actually opens/creates these two specific files via its own `StandardOutput=append:`/`StandardError=append:` directives — before it execs and drops privileges to the unit's own `User=`/`Group=`. Confirmed directly: `ls -la /var/log/rabbitmq/` on a live node showed `rabbit@<host>.log` and `rabbit@<host>_upgrade.log` owned `rabbitmq:rabbitmq` (written by RabbitMQ's own Erlang runtime, the files F-074 already covers), but `rabbitmq-server.log`/`rabbitmq-server.error.log` owned `root:root 640` — a completely different, unreachable-by-group-membership owner, since granting promtail the `root` group would be a real privilege escalation, not a targeted log-read grant.

**Fix:** A file-specific ACL, same pattern and reasoning as F-075's ProxySQL fix: `setfacl -m u:promtail:r /var/log/rabbitmq/rabbitmq-server.log /var/log/rabbitmq/rabbitmq-server.error.log`, `acl` added to `packages:`, placed after RabbitMQ's own final restart so the ACL lands on the files the running process actually holds open. Confirmed the content itself carries no credential material first (just RabbitMQ's own startup banner and feature-flag notices, before it hands off to its own `rabbit@*.log` handler) — a plain read grant, nothing more sensitive to scope around.

**Verified by:** Reproduced live on a throwaway `compute-basic` instance with `rabbitmq-server` manually installed: confirmed the exact `root:root 640` ownership and the resulting `permission denied` for the `promtail` user, applied the ACL fix live and confirmed `sudo -u promtail cat rabbitmq-server.log` succeeds, then confirmed via a real Loki query that genuine `rabbitmq-server.log` content (`"Starting broker... completed with 0 plugins."`) reached Loki under `job="rabbitmq"` with the correct hostname. Applied to both affected examples on both IaC front-ends.

### F-080 — `indent()` and HCL's `<<-` heredoc dedent don't compose: the "brand-new minimal cloud-init" examples added in Stage 5 shipped with genuinely broken, crash-looping promtail configs that `tofu validate` never caught

**Where:** `compute-basic`, `dns-with-compute`, `network-lb`, `load-balanced-web`, `full-stack` — the five Stage 5 examples (`haFullStack-LLD.md` §12.3, v0.28) that embedded the shared `promtail_config` directly inside an inline `<<-EOT ... EOT` heredoc local, rather than a separate `templatefile()`-rendered `.tftpl` file the way every other example in this repo already does it.

**Symptom:** Found live, entirely by accident — testing Sentinel's new "View in Grafana" deep links against a throwaway `compute-basic` instance, `systemctl status promtail` showed `Active: failed`, `restart counter is at 72`. `promtail -config.file /etc/promtail/config.yml -dry-run` gave the real error: `Unable to parse config: /etc/promtail/config.yml: yaml: line 4: did not find expected key`. **None of these five examples had ever actually been rebuilt and checked live** — Stage 5's own verification ran `tofu validate`/`tofu fmt` on all seven "simple" examples and did a real live build only of `openstack-services` (which uses the safe `templatefile()` pattern, not the broken heredoc one) — `tofu validate` only checks HCL syntax, it doesn't render `templatefile()`/heredoc interpolations far enough to catch a YAML-shaped bug in their *output*.

**Root cause:** Terraform's `indent(n, string)` function adds `n` spaces to every line of its input *except the first* (by design — the first line is assumed to already sit at the right column via the template's own layout). That's correct when the caller is a plain `content: |` block inside a `.tftpl` file processed by `templatefile()`. It is **not** correct inside an HCL `<<-EOT ... EOT` heredoc: the `<<-` marker's own dedent step runs on the *fully assembled* string, stripping a single uniform amount of leading whitespace (based on the closing `EOT` line's own indentation) from every line — including the lines `indent()` already reformatted. The two dedent/indent operations don't compose: line 1 of the embedded config keeps the heredoc's own literal indentation (since `indent()` left it alone), while every subsequent line carries `indent()`'s *own* added spaces instead, and the heredoc's blanket dedent then subtracts the same fixed amount from both — leaving line 1 under-indented relative to the rest. The result renders as visually-plausible but structurally broken YAML (verified: `server:` at column 0, `http_listen_port:` at column 6, immediately invalid), not something a glance at the source would catch.

**Fix:** Converted all five examples' `promtail_user_data`/`nginx_user_data` locals from inline heredocs to `templatefile()` calls against new per-example `files/promtail-cloud-init.yaml.tftpl` (`compute-basic`, `dns-with-compute`, `network-lb`) or `files/nginx-cloud-init.yaml.tftpl` (`load-balanced-web`, `full-stack`) files — byte-for-byte the same content, `indent(6, promtail_config)` instead of `indent(10, ...)` to match the `.tftpl` file's own base indentation, no heredoc involved anywhere. This is the exact pattern every other example in this repo already used successfully; the heredoc variant was the only place this project ever tried the alternative.

**Verified by:** Rendered `local.promtail_user_data`/`local.nginx_user_data` for all five via `tofu console` and parsed both the outer cloud-config and the embedded promtail config as real YAML (`python3 -c "import yaml; ..."`) — all five now parse cleanly, where the heredoc version had already been confirmed broken on a live node. Then a genuinely fresh `tofu destroy`/`apply` cycle on `compute-basic` (not a manually-patched instance) confirmed `cloud-init status: done`, `NRestarts=0` for `promtail`, and real journal content reaching Loki under the instance's correct hostname. The other four share the identical, now-fixed mechanism and weren't separately rebuilt. Checked the Ansible side for the same class of bug directly (rendered `02-compute-basic.yml`'s own `user_data` block through a real YAML parser) — confirmed unaffected, since that side embeds the already-correctly-indented static text directly rather than calling a Jinja `indent` filter at render time.

### F-081 — Grafana's built-in "Viewer" role doesn't include Explore access in this version, so anonymous `org_role = Viewer` sessions hit a login wall the instant they open Explore

**Where:** `api/setup-logging-service.sh`'s Grafana anonymous-access provisioning — found live, testing Sentinel's new "View in Grafana" links end-to-end for the first time (previously only verified the underlying query API directly, not the actual `/explore` page load a real browser session goes through).

**Symptom:** With `[auth.anonymous] org_role = Viewer` configured, opening Grafana anonymously loaded the home page fine (no login screen, matching the intended fix), but clicking through to Explore — either via Sentinel's own deep links or Grafana's own left-nav — immediately showed an "Unauthorized" toast and no data, despite the underlying Loki datasource and query API both working correctly when called directly (confirmed: `POST /api/ds/query` returned real data for an anonymous, unauthenticated request).

**Root cause:** Confirmed directly with `curl -i http://192.168.100.1:3000/explore?...`: a `302 Found` redirect to `/?redirectTo=...` for an anonymous Viewer session — the Explore *page* itself is gated behind a separate RBAC action (`datasources:explore`, confirmed present as a real permission string in Grafana's own bundled frontend JS) that this Grafana version's built-in "Viewer" fixed role does not include, even though the same anonymous session's basic org-level API access (datasources list, `/api/ds/query`) works fine — the gate is specifically on the Explore page/route, not on querying a datasource in general. OSS Grafana has no supported mechanism to grant an anonymous session a custom, narrower RBAC permission set outside this — that requires Grafana Enterprise's role customization; anonymous access is limited to picking one of the three fixed basic roles (Viewer/Editor/Admin) via `org_role`.

**Fix:** `org_role = Editor` instead of `Viewer` — the least-privileged fixed role that actually includes `datasources:explore`. A real, accepted capability increase, not a workaround without consequence: anonymous visitors can now create/edit/save dashboards too (not just view them), though still short of Admin (no user or datasource management — the provisioned Loki datasource itself can't be altered anonymously). Accepted per direct instruction as a reasonable Lab-only tradeoff, since this Grafana instance never leaves the Lab's own internal bridge network (`192.168.100.1`, unreachable outside it) — the same trust boundary already used to justify skipping TLS on this same service.

**Verified by:** `curl -i http://192.168.100.1:3000/explore?...` returned `302 Found` (→ login wall) with `org_role = Viewer`, and `200 OK` with `org_role = Editor` — a direct, reproducible before/after comparison isolating the cause. Confirmed end-to-end afterward via Sentinel's own UI: clicking suggestion/event "View in Grafana" links now lands in Explore with real log data shown, no login screen at any point.

### F-082 — An orphaned, un-managed `cloudcore-api` process silently held its own port for hours, leaving the real systemd-managed unit crash-looping in the background the entire time

**Where:** This dev machine's own live `cloudcore-api.service`, found building `scripts/restart-stack.sh` (a new script requested to restart CloudCore + Sentinel together in the correct order) — a plain `systemctl --user list-units` check surfaced `cloudcore-api.service` in state `activating (auto-restart)`, not `active`.

**Symptom:** `journalctl --user -u cloudcore-api` showed a continuous restart loop — `NRestarts` at 566 and climbing, every attempt failing identically: `Address already in use — Port 8080 is in use by another program.` The API had nonetheless been responding correctly to every request all session, because a separate, un-managed `python3 server.py` process (`ps` showed it running since the previous day) was the one actually holding port 8080 and serving traffic — started manually at some point outside systemd, then never stopped, so systemd's own properly-managed unit could never rebind its own port on any of its (many) automatic restart attempts.

**Root cause:** Nothing in CloudCore itself — a purely operational artifact of manual testing during this session leaving a stray process behind. Real and worth guarding against regardless, though: any future manual `python3 api/server.py` invocation for quick testing (a completely reasonable thing to do) leaves exactly this trap for the *next* systemd-managed restart, with no error message pointing at the actual cause unless someone thinks to check `journalctl` specifically.

**Fix:** Killed the orphaned PID directly, confirming the systemd-managed unit bound cleanly and immediately on its very next scheduled restart attempt. More durably, `scripts/restart-stack.sh`'s own `ensure_port_clear()` helper now defensively stops the target service first, then checks whether *anything* is still listening on its port and kills it before starting fresh — so this class of problem can no longer silently persist across a restart performed via the new script, on any of the four ports it manages (cloudcore-api 8080, cloudcore-terminal 8081, sentinel-ui 8900, and implicitly loki/grafana-server via the plain `systemctl restart` for those two, which doesn't have the same orphan risk since nothing manually starts those outside systemd in normal use).

**Verified by:** `kill <pid>` on the orphan, then confirmed `systemctl --user status cloudcore-api` went `active (running)` within ~5s (systemd's own `RestartSec`) with `NRestarts` no longer climbing, and the API still answering `GET /v1/dashboard` correctly throughout — no functional regression, purely a hygiene/reliability fix. A genuinely separate bug was also caught building the fix itself: the first version of `ensure_port_clear()`'s own `pid="$(ss -tlnp | grep ... | grep ... | head -1)"` pipeline aborted the *entire* script under `set -euo pipefail` whenever the port was already free — the completely normal case — because `pipefail` propagates `grep`'s own "no match" exit code through the pipeline into a bare assignment statement, which `set -e` then treats as a failed command. Caught immediately by actually running the script (not just `bash -n`), not by inspection — `bash -x` traced the exact abort point. Fixed with a trailing `|| true` inside the substitution.

### F-083 — Both `install.sh` and `restart-stack.sh` correctly *diagnosed* a missing `setup-logging-service.sh` run, but only ever printed the fix — real friction, hit twice by the same person on the same machine, because the printed command sat unread/unrun in scrollback

**Where:** Sentinel's `install.sh` (its Loki-reachability check, added F-077) and both `restart-stack.sh` scripts (F-082) — found live, on the separate fresh-install machine this whole "are we committing findings-log updates?" conversation was about: `curl`/`ss` confirmed `loki.service` and `grafana-server.service` didn't even exist there, twice in a row, despite both scripts already correctly printing `sudo bash api/setup-logging-service.sh` as the fix each time they ran.

**Symptom:** Sentinel's own `/api/status` reported `"status": "watching http://192.168.100.1:3100"` even with Loki completely unreachable — misread as "it's working" — because that status string is unconditional since the Stage 3 discovery removal (`haFullStack-Findings-Log.md`'s own F-076-era work): it no longer distinguishes "configured to watch" from "successfully reaching." Clicking any of Sentinel's own "View in Grafana" links then failed with a browser-level connection-refused, the first real symptom anyone actually noticed — by which point the diagnostic guidance that would have prevented it had already scrolled past, twice.

**Root cause:** Not a bug in the diagnosis itself — both scripts correctly identified the exact missing step every time. The gap was in the *response*: a scripted recommendation printed to a terminal is easy to read past, especially inside a longer install/restart run, and there was no mechanism forcing the next step to actually happen before the script considered itself "done."

**Fix:** Both `restart-stack.sh` scripts and Sentinel's `install.sh` now interactively *offer* to run `setup-logging-service.sh` right there (`read -r -p "Set it up now? Needs sudo. [y/N] "`), for the one diagnosis branch where that's fast and low-risk (`.deb`s already cached, just the host-level install step missing) — gated on `[ -t 0 ]` so a non-interactive invocation (cron, CI, a piped run) still just prints the command rather than hanging on a `read` nobody can answer. Deliberately **not** extended to the slow branch (`api/build-package-repo.sh`, 15-20+ minutes of real bandwidth) — that stays informational-only, matching this project's own established convention that a routine restart/install script shouldn't silently balloon into a long unattended operation.

**Verified by:** Syntax-checked all three scripts (`bash -n`). The non-interactive fallback path tested directly (this environment has no TTY, so `[ -t 0 ]` is reliably false here) against a synthetic "service not installed, `.deb`s cached" scenario — confirmed it prints the fix command and reaches the end of the script cleanly, no hang. The interactive `read` path itself needs a real terminal to click-test, same category as this session's earlier Grafana-Explore-link caveat — not yet confirmed against an actual keypress, but the underlying `read -r -p ... && [[ ... =~ ... ]]` idiom is standard and low-risk.


### F-084 — `db.get_db()` silently ignored whatever path was passed to `db.init()`, always reconnecting to the real, live `cloudcore.db` regardless

**Where:** `api/db.py`, found building the first stage of cross-host peering (new `peers`/`pairing_requests` tables, a `host_id` column on `instances`) — verifying the new schema/migration against an isolated scratch database before touching anything real.

**Symptom:** `db.init(db.Path("/tmp/scratch.db"))` correctly opened and migrated the scratch file via its own module-level `_conn` — but every actual read/write in the application (`store.py`, `settings_store.py`, anything using `db.get_db()`) still landed on the real `api/cloudcore.db`, because `get_db()`'s thread-local connections were opened against the hardcoded `_DB_FILE` module constant, never the path `init()` was actually given. Caught directly: a `settings_store.set("network.bridge_subnet_octet", ...)` call made against a scratch DB during this same verification pass showed up in the real, live `cloudcore.db`'s `settings` table instead — found and removed immediately, no functional impact (nothing reads that key yet on the currently-running, unmodified service).

**Root cause:** `init(db_file=...)` accepted a custom path and used it for its own migration bookkeeping (`_conn`), but `get_db()` and `_migrate_json()`'s sibling-JSON-file lookup both independently referenced the fixed `_DB_FILE` constant instead of whatever `init()` actually resolved. Invisible in production, since `server.py`'s only call site (`db.init()`, no argument) always makes the two coincide — a pure testability gap, but a dangerous one: any future isolated test pointing `init()` at a scratch file would keep silently reading and writing the real database underneath it.

**Fix:** `init()` now records the path it actually opened in a new module-level `_active_db_file`; `get_db()` and `_migrate_json()` both read that back instead of the `_DB_FILE` constant. Production behavior is byte-for-byte unchanged (still defaults to the same file, same call site, no argument).

**Verified by:** Reproduced first (isolated `db.init()` call, then confirmed via `sqlite3 cloudcore.db` that a test write had actually landed there), fixed, then re-ran the identical isolated test and confirmed via direct `sqlite3` inspection of both files that the scratch file received the write and the real `cloudcore.db` did not. `server.py` re-imported cleanly afterward with no behavior change against its own real, unmodified call site.

### F-085 — `install.sh`'s own `cloudcore-api` start step was `systemctl --user start`, a no-op on an already-running unit — the exact class of gap F-057 fixed for `cloudcore-terminal`, just hit in a different spot

**Where:** `scripts/install.sh`, found live testing cross-host peering's Stage 2 (mDNS discovery) across two machines — a fresh `git pull` + `bash scripts/install.sh` re-run on the second machine (Llywyn-Y-Groes), followed immediately by `curl -X PUT .../v1/settings/discovery`, returned a plain `404 Not Found` for a route that genuinely existed in the just-pulled source.

**Symptom:** `install.sh` itself reported success end-to-end (`==> API is up.`, its own `curl -H Authorization ... /v1/dashboard` health check passed) — the process was up and answering, just not with the code that was actually on disk.

**Root cause:** `install.sh` enables the service (`systemctl --user enable cloudcore-api.service`, no `--now`) then separately runs `systemctl --user start cloudcore-api.service`. `start` is a no-op against a unit that's already `active` — exactly the same shape of gap `cloudcore-terminal.service`'s own `PartOf=cloudcore-api.service` comment already documents from F-057 (`enable --now` being a no-op there), just never applied to the `cloudcore-api` step itself. Since Python doesn't hot-reload its own imports, the already-running process kept serving whatever code was on disk *before* the `git pull` — indefinitely, until something actually restarted it. `install.sh`'s own end-of-run health check couldn't catch this because it only proves the process is alive and answering *some* route, not that it's running today's code.

**Fix:** `systemctl --user start cloudcore-api.service` → `systemctl --user restart cloudcore-api.service`. Behaves identically on a genuinely fresh install (nothing was running to restart); on a re-run after a `git pull` — install.sh's own documented, expected usage pattern — it now actually guarantees fresh code. `cloudcore-terminal.service`'s existing `PartOf=cloudcore-api.service` (F-057) means it now correctly follows this restart too, with no separate fix needed for it.

**Verified by:** Reproduced live on a real second machine (404 on a route that existed on disk), unblocked immediately with a direct `systemctl --user restart cloudcore-api.service`, then fixed at the source. `bash -n` clean; the actual proof is the next full `install.sh` re-run on that same machine picking up new code without anyone needing to know to restart anything by hand.

### F-086 — `serve-package-repo.py`'s dynamic bind-address fix (Stage 0) never actually worked — it silently fell back to the hardcoded default on every single run

**Where:** `api/serve-package-repo.py`'s `_bind_addr()`, found live on Llywyn-Y-Groes moving its bridge subnet from the default octet (100) to 101 for cross-host peering (non-overlapping subnets are a prerequisite for Stage 4's WireGuard tunnels) — `cloudcore-repo` stayed unreachable at the new address (`192.168.101.1:8090`) after a full teardown/setup/restart cycle.

**Symptom:** `curl http://192.168.101.1:8090/` timed out/refused even though `ccbr0` was confirmed correctly reconfigured (`ip addr show` showed `192.168.101.1/24`) and `cloudcore-repo.service` had been restarted.

**Root cause:** `_bind_addr()` (added in this session's own Stage 0 work, v0.44) calls `settings_store.get(...)`, which calls `db.get_db()` — but `serve-package-repo.py` is a standalone script that never calls `db.init()` first, and `get_db()` raises `RuntimeError("db.init() has not been called")` rather than silently connecting. The function's own `except Exception: return "192.168.100.1"` fallback swallowed that error every time, on every host, since the very commit that introduced it — meaning the "dynamic bind address" feature never actually worked at all; it always silently served the old hardcoded default regardless of the real setting. Invisible on stourport (still on the default octet, so the fallback's wrong answer happened to be the right one) and only surfaced once a host's actual octet diverged from 100.

**Fix:** `_bind_addr()` now calls `db.init()` (no argument — the real, single production DB, same as every other entry point) before reading the setting.

**Verified by:** Reproduced (mocked `db.init()` never having been called, confirmed the fallback path fired); fixed; re-verified both against a scratch DB with a non-default octet (returned the correct dynamic address) and read-only against stourport's own real production DB (still correctly returns the default, since stourport itself hasn't changed octets).

### F-087 — `wg-quick` reliably fails to read its own config from a path under a normal user's home directory, even as root via `sudo -n` — the canonical `/etc/wireguard/` location is required

**Where:** `api/wireguard.py`, found live on Llywyn-Y-Groes bringing up the first real cross-host WireGuard tunnel for Stage 4 — `sudo -n wg-quick up api/wireguard/cc0.conf` (and the same with an absolute path) failed with `wg-quick: line 40: <path>: Permission denied`, even though the sudoers grant itself was confirmed working (`sudo -n wg --version` succeeded) and root could read the identical file fine via plain `sudo cat <path>`.

**Symptom:** `wg_tunnel_status: "error"` on the approving side; the underlying `wg-quick up` invocation died reading its own config file, specifically only when invoked non-interactively via the `sudo -n` NOPASSWD grant, not via an interactive `sudo` session running a different command against the same file.

**Root cause:** Not fully explained even after ruling out AppArmor (no profile loaded), SELinux (not installed), NFS/`root_squash` (plain local ext4), and a CWD/relative-path issue (reproduced identically with an absolute path) — but empirically, conclusively reproducible: the exact same rendered content at `/etc/wireguard/cc0.conf` (root-owned, the conventional location every other real-world wg-quick usage assumes) works every time, while a path under `$HOME` does not, specifically through `wg-quick`'s own file-open at its `parse_options()` function (`done < "$CONFIG_FILE"`).

**Fix:** `api/wireguard.py` no longer stages the rendered config under this repo's own directory at all — it pipes the config text straight through a narrowly-scoped `sudo -n tee /etc/wireguard/cc0.conf` (exact path, not a blanket `tee` grant) and then always operates on the bare `cc0` interface name (`wg-quick up cc0`, `wg-quick strip cc0`), letting `wg-quick` resolve the canonical path itself. `api/setup-wireguard.sh`'s sudoers grant extended to cover that one exact `tee` invocation.

**Also fixed alongside this** (found by inspection while debugging the timeout on the *other* side of the same test): the `/v1/peers/complete` callback's `peer_client.TIMEOUT` (8s) had no real safety margin over `on_peer_approved()`'s own handshake-status polling loop (up to 10s) running synchronously inside the handler *receiving* that callback — a slow-but-successful tunnel bring-up on the receiving side could make the caller's own request time out even though nothing was actually broken. Polling loop capped to 6s, timeout raised to 15s, restoring real margin.

**Verified by:** Reproduced by hand on Llywyn-Y-Groes (identical failure with both relative and absolute home-directory paths; plain `sudo cat` on the same file succeeding, isolating it to `wg-quick` specifically); root-caused by testing the exact same content copied to `/etc/wireguard/cc0.conf`, which brought `cc0` up cleanly (`ip link add`, `wg setconf`, address/route all applied) on the first real attempt. Code fixed to always use the canonical path; full live re-verification (an actual handshake between the two real hosts) next.

### F-088 — Two real gaps found running the first live cross-host Ansible playbook: the original "Ansible needs zero changes" plan premise no longer held, and remote instances' status never refreshed except via an individual GET

**Where:** `ansible/collections/cloudcore/plugins/modules/instance.py`, `api/server.py`'s `_create_remote_instance()` — found running a real, hand-written Ansible playbook against the live two-machine peering setup for Stage 7.

**Symptom/root cause 1 (design premise, not a bug in the old sense):** the original cross-host peering plan's Stage 7 said Ansible needed zero code changes, reasoning that a task could just point `api_url` directly at a peer's own API. That reasoning predated Stage 3's own discovery that `cloudcore-api` has only ever bound `127.0.0.1:8080` — a peer's dashboard API is never reachable from another host at all, only the narrow, peer-token-gated peer-listener port is, and that's restricted to a specific route allowlist, not general instance management. Ansible therefore needed the same `peer_id`-through-the-local-API mechanism the Terraform provider uses (Stage 6), not direct remote API access. Fixed: `instance.py` gained a `peer_id` parameter, passed through on create only (immutable, same RequiresReplace-equivalent treatment as the provider); new `peer_info.py` module (mirrors the existing `usb_device_info.py` pattern) lists peers for playbooks to select from.

**Symptom/root cause 2 (a real bug):** even with `peer_id` wired through, a real playbook run's own polling loop (re-invoking the idempotent `instance` module with `state: present`, waiting for `status == running`) never saw the status change, timing out after 12 retries. Traced directly: the module's own idempotency check (`find_by_name`) reads `GET /v1/instances` (the LIST endpoint), which — unlike an individual `GET /v1/instances/<id>` — never live-refreshed a remote instance's status at all. Local instances don't have this problem because their own background creation thread proactively keeps the DB-stored status current independent of anyone polling; remote instances had no equivalent, so nothing ever updated a remote wrapper row's status except a direct GET on that exact instance.

**Fix, part 1:** `_create_remote_instance()` now also spawns its own background polling thread (mirroring the existing local `_launch()` pattern), refreshing the local wrapper row from the peer every 5s until a genuinely terminal state, capped at ~2 minutes.

**Fix, part 2 (found immediately re-testing part 1):** the first re-test showed `status: running` but `private_ip: ""` stuck that way — the background poller was exiting the instant status left "pending", the same "bridged instance reports running before its DHCP lease is known" race the Go provider's own `Create()` polling comment already documents and guards against, just not yet applied to this new background thread. Fixed to keep polling until status is genuinely terminal AND (if running) `private_ip` is actually populated, matching the provider's own more careful condition.

**Verified by:** A real, hand-written two-host playbook (`peer_info` → `instance` with `peer_id` → poll via idempotent re-invocation → `instance: state=absent`) run three times end-to-end as each gap was found and fixed — first run timed out (gap 1 found), second timed out differently (`status: running`, empty IP — gap 2 found), third succeeded cleanly (`failed=0`, real `private_ip`), confirmed gone from `virsh list --all` on Llywyn-Y-Groes after the delete task.

### F-089 — `let _peersPollTimer` threw a temporal-dead-zone `ReferenceError` on every single nav click, dashboard-wide, not just on the Peers section — headless verification couldn't have caught it

**Where:** `ui/src/js/26-peers.js`, found live by the user in their real browser's DevTools console immediately after Stage 8 shipped — every nav click except Peers-related ones showed a permanent "Loading…" placeholder and did nothing.

**Symptom:** `Uncaught ReferenceError: can't access lexical declaration '_peersPollTimer' before initialization`, thrown from `stopPeersPoll()`, called from `showSection()`, called from `15-init.js`'s own top-level `showSection('dashboard', ...)` bootstrap call that fires on every page load.

**Root cause:** `ui/build.sh` concatenates `ui/src/js/*.js` in filename order (`00-`…`26-`), and `showSection()` unconditionally calls `stopPeersPoll()` for every section that isn't `'peers'` — including the very first, automatic `'dashboard'` load. That bootstrap call lives in `15-init.js`, numbered *before* `26-peers.js` — so by the time the browser's interpreter reaches it during the bundle's initial top-to-bottom execution, it hasn't yet reached `26-peers.js`'s own `let _peersPollTimer = null;` line. Referencing a `let`/`const` before its own declaration line has executed throws in its temporal dead zone — and since that throw happened inside `showSection()` itself, it aborted every subsequent `if (name === ...)` branch in that same function call, breaking every section's load call, not just Peers'.

**Fix:** `let` → `var` for `_peersPollTimer`/`_lastDiscovered` — `var` is hoisted with an initial `undefined` at parse time regardless of where in the file it's declared, sidestepping the TDZ entirely. (`06-dashboard.js`'s own `let _dashTimer` never hit this because file `06` sorts before `15-init.js`'s bootstrap call — this was specifically a "my new file landed late in the concatenation order" problem, not a general let-vs-var issue in this codebase.)

**Verified by:** This is the one class of bug this session's headless environment structurally could not have caught on its own — `google-chrome --headless --dump-dom` doesn't surface page-level JS exceptions to stderr (confirmed directly: the earlier Stage 8 headless check reported zero errors against this exact, still-broken bundle). Root-caused directly from the user's own real-browser DevTools console output (stack trace pointed straight at the line), fixed, then verified by executing the real concatenated `ui/index.html` bundle's own inline script directly in Node in its real source order (ran cleanly, matching the fix) — plus direct user confirmation in their own browser afterward.

### F-090 — `discovery.browse()` created a brand-new `Zeroconf()` instance on every single scan, and something in that create/destroy cycle wasn't fully releasing its own resources — eventually exhausted the whole process's open-file limit

**Where:** `api/discovery.py`, found live on Llywyn-Y-Groes — a `tofu destroy` mid-session failed deleting a real remote instance with an opaque `Peer 'Llywyn-Y-Groes' rejected the delete: {}`, and `journalctl` on that host showed the real cause: `OSError: [Errno 24] Too many open files`, taking down every subsequent request on that process (new DB connections, new sockets, even importing Flask's own debug module to render the error page).

**Symptom:** A long-running `cloudcore-api` process that had `GET /v1/peers/discovered` (→ `discovery.browse()`) called many times over one session's worth of live testing (Stage 2 verification, re-tests, dashboard JS testing) eventually hit its process file-descriptor ceiling. `lsof` on the affected process showed a large accumulation of thread-pool-associated file descriptors — consistent with the `zeroconf` library's own per-instance background engine thread (and the socket(s)/pipe(s) that thread owns) not being fully released by `Zeroconf.close()` across many rapid create-then-destroy cycles.

**Root cause:** `browse()` created a fresh `Zeroconf()` — and therefore a fresh engine thread and fresh sockets — on *every single call*, closing it again at the end. `advertise()`/`stop_advertise()` don't have this problem in normal use (one long-lived instance per enable, not recreated per call), but `browse()`'s "on-demand scan" design point (deliberately not a persistent background browser, so an opt-in feature stays minimal when idle) meant a fresh instance every time someone scanned — and scanning is exactly the operation most likely to happen repeatedly in a real session.

**Fix:** A separate lazy singleton (`_get_browse_zc()`) — the underlying `Zeroconf` engine is created once, the first time anything calls `browse()`, and reused for the life of the process; only the lightweight per-scan `ServiceBrowser` (which doesn't open its own sockets or threads — it registers a listener against the already-running shared engine) is still created and cancelled on every call, preserving the "no persistent browsing" behavior that was actually the point.

**A related, different issue found but not yet root-caused while investigating this:** rapidly toggling `advertise()`/`stop_advertise()` back-to-back can throw `zeroconf._exceptions.NonUniqueNameException` — appears to be a timing race between `stop_advertise()`'s own mDNS "goodbye" packet propagating/expiring and an immediate re-`advertise()` under the same hostname, not the same file-descriptor leak (advertise/stop_advertise already uses a single long-lived instance per enable, not a fresh one per call). Left open rather than guessed at — real usage (a human occasionally toggling a settings checkbox) is unlikely to hit rapid back-to-back cycling the way automated testing did.

**Verified by:** A real stress test — 30 consecutive `browse()` calls against the fix, checking this process's own `/proc/<pid>/fd` count directly: grew once (4 → 17) on the very first call (the expected one-time cost of actually opening the shared engine's sockets), then stayed completely flat for the next 15 calls, confirming the repeated-leak pattern is gone. The real incident itself was resolved by restarting the affected host's `cloudcore-api`.

### F-091 — Nothing stopped re-pairing with an already-paired host, leaving two fully-approved `peers` rows for the same physical machine — and both ended up in the same rendered WireGuard config with the identical `PublicKey`

**Where:** `api/peers_routes.py`, found live wiring per-resource `peer_id` support into a real Ansible example (`04-load-balanced-web.yml`) — the Build Manager's own peer picker rendered two entries both labeled "Llywyn-Y-Groes", with different peer ids.

**Symptom:** `GET /v1/peers?status=approved` genuinely returned two rows for the same host — same `pubkey`/`pubkey_fpr`/`api_url`/`wg_pubkey`, different `id`s and `created_at` timestamps roughly an hour apart, consistent with an earlier re-pairing done during this session's own testing without revoking the first one first.

**Root cause:** Neither `POST /v1/peers` (the initiating side) nor `POST /v1/peers/pairing-requests` (the receiving/bootstrap side) ever checked whether an approved (or in-flight) relationship with the same host already existed — pairing was designed around the happy path (a genuinely new host) with no thought given to "what if this one's already paired." Beyond the confusing duplicate dashboard/picker entries, `api/wireguard.py`'s `render_config()` iterates every approved peer with a `wg_pubkey` and emits one `[Peer]` block each — two rows sharing the identical `PublicKey` means the rendered config carried a duplicate peer definition.

**Fix:** Two checks, one per side, using the strongest identity signal available at that point in the handshake. `POST /v1/peers` dedups by `api_url` (the target's own `pubkey_fpr` isn't known yet at that point — only learned via the later `/v1/peers/complete` callback), refusing with `409` if an `approved` or `pending_outbound` row already targets the same address:port. `POST /v1/peers/pairing-requests` dedups by `pubkey_fpr` (known immediately, and the stronger, identity-based signal), refusing with `409` before a new `pairing_requests` row is even created if an `approved` peer with that fingerprint already exists. Both point at reusing `DELETE /v1/peers/<id>` (revoke) first if a genuine re-pair is actually wanted.

**Verified by:** Both checks tested directly: a second `POST /v1/peers` to the same address:port correctly refused with `409` after the first succeeded; a second signed bootstrap request from the identical identity, after the first was approved, correctly refused with `409` before creating any new pending request. The real duplicate `peers` row found live was revoked.

### F-092 — A peer-placed instance's `security_group_ids` referencing an id from the *originating* host, not the target peer, was accepted silently and produced a DROP-only iptables chain — the instance built, booted, and reported `running` while being completely unreachable

**Where:** `api/server.py`'s `POST /v1/instances`, found live building the real cross-host proof of `load-balanced-web` — `web-02` placed on Llywyn-Y-Groes via `modules/instance-group`'s `placement_overrides` (F-089/v0.54).

**Symptom:** the instance reported `status: "running"` with a correct `private_ip`, `virsh -c qemu:///session list --all` on the peer confirmed the domain genuinely running, and the dnsmasq lease file showed a valid, current DHCP lease for that exact IP/MAC — yet it answered neither `ping` nor an HTTP request from across the (otherwise reliable, all session) WireGuard tunnel. haproxy's own health check against it reported `L4TOUT` (a Layer-4 TCP connect timeout).

**Root cause:** the demo's security group was created locally on stourport and passed through unchanged as `security_group_ids` when the create request was proxied to Llywyn-Y-Groes (`api/peer_client.py` forwards the body verbatim — IDs are deliberately opaque across the proxy boundary, same as `vpc_id`/`subnet_id`, per `server.py`'s own `_create_remote_instance` comment). On the executing host, `sg_routes._merged_rules()` silently skips any `sg_id` it can't find in its own `security_groups` table (`if sg: ...`, no else) rather than erroring — so the instance launched with `ingress = egress = []`. `sg.apply_bridge()` still ran regardless, installing a per-instance iptables chain terminated by an unconditional `DROP` with zero `ACCEPT` rules ahead of it, and jumped into that chain from `FORWARD` matched on the instance's own source MAC. Since `xt_mac` only matches a packet's *source* MAC (documented in `apply_bridge()`'s own docstring — this chain was already known to only enforce egress, not ingress, per F-007), inbound pings/SYNs reached the guest fine over the bridge's general policy, but every reply — the instance's own egress traffic — hit the empty chain and was dropped. The VM was never actually broken; its own replies just never left the host.

**Fix:** `POST /v1/instances` now rejects the request outright (`400`) if any `security_group_ids` entry doesn't resolve in the *executing* host's own `sg_store`, before the instance is ever created — `server.py`, right after the `peer_id` proxy branch, so it fires on the local host for an ordinary create and on the *peer's* own `server.py` for a peer-placed one (the proxied body has `peer_id` already stripped by that point, so it flows through the same local-creation path there). `modules/instance-group` gained a `security_group_ids` field on `placement_overrides`, mirroring `peer_id`/`vpc_id`/`subnet_id` — a peer-placed instance entry now carries its own peer-side security group list instead of inheriting the group's local one. `examples/load-balanced-web` gained a matching `web_02_peer_security_group_id` variable. `ansible/examples/04-load-balanced-web.yml`'s instance task never attaches `security_group_ids` at all (no SGs in play there), so it was never exposed to this bug and needed no change.

**Verified by:** live, on the actual broken instance. Diagnosed the exact failure mode via the peer's own haproxy stats socket (`show stat` over the Unix socket showed `web-02` `DOWN`/`L4TOUT`) and a direct `ping`/`curl` from stourport (both dead) despite `virsh`/dnsmasq confirming a genuinely running, correctly-leased VM. Manually created a matching security group in Llywyn-Y-Groes's own catalogue, corrected the instance's stored `security_group_ids` to that id, and fired a no-op `PUT` on the new SG (which re-triggers `sg_routes._reapply_to_instances()` without touching the running VM) — `ping` and the LB health check both recovered immediately. Reran `tofu validate` against both `modules/instance-group` and `examples/load-balanced-web` after the fix; confirmed the LB then genuinely round-robined real traffic across both the local and the cross-host backend (haproxy's own `stot` counters climbing on both `web-01` and `web-02` across repeated requests through `http://127.0.0.1:8201/`, not just repeated `200`s that could've all come from one side).

### F-093 — A load balancer target group's own targets are explicit instance ids, not VPC membership, but every caller resolved them via a VPC-filtered instance list — silently dropping any cross-host target whose local wrapper row's `vpc_id` is the *peer's* own, not this LB's

**Where:** `api/lb.py`'s `_write_config()`/`_resolve_tg_servers()`, found live wiring `examples/load-balanced-web/main.tf`'s previously-missing backend-registration step onto real `cloudcore_lb_target_group`/`cloudcore_lb_listener` resources (per direct request, "lets fix item 1").

**Symptom:** a target group created with both web instances as targets rendered a real haproxy `backend` block containing only `web-01` (local) — `web-02` (on Llywyn-Y-Groes) was silently missing from the generated config entirely, no error anywhere.

**Root cause:** `create_target_group`/`add_listener` both call `lb_backend.reload(lb, vpc_instances=store.list_instances_by_vpc(lb.vpc_id))`, and `_write_config()` builds its `instance_id → Instance` map solely from that vpc-filtered list. A peer-placed instance's local wrapper row deliberately stores the *peer's own* `vpc_id` (not this build's local one — the whole point of `web_02_peer_vpc_id`, F-089/F-092), so it can never match `WHERE vpc_id = <this LB's local vpc_id>`. A target group's own `targets` list is explicit `instance_id`s, not a VPC membership query, so filtering by VPC at all was the wrong resolution strategy for this specific path — it happened to work for same-host LBs only because every target's wrapper row trivially shared the LB's own local vpc_id.

**Fix:** `_write_config()` now also resolves any target `instance_id` missing from the vpc-scoped map with a direct `store.get_instance(id)` lookup, independent of `vpc_id`. No caller changes needed — the fix is local to where the map is built.

**Verified by:** live. Re-triggered a target-group reload after the fix (a no-op `PUT` with the same targets) and confirmed the regenerated haproxy config contained real `server` lines for both `example-dev-web-peertest-01` (local) and `example-dev-web-peertest-02` (remote) in the target group's own backend block.

### F-094 — A load balancer target group's `health_check` defaults to interval/threshold 0 (not the documented 30/2/2) whenever the Terraform provider sends its own unconfigured zero-value struct instead of omitting the field — haproxy then rejects the whole config outright

**Where:** `api/server.py`'s `create_target_group`/`update_target_group`, found live in the same F-093 wiring session — the very first `tofu apply` of the new `cloudcore_lb_target_group` resource (no `health_check` block set in HCL, relying on the provider's own documented defaults).

**Symptom:** haproxy refused to (re)start at all after the target group was created: `[ALERT] config : invalid value 0 for argument 'inter' of server default-server..` — the entire load balancer went down, not just the new backend, since haproxy loads one `.cfg` file as a whole.

**Root cause:** `cloudcore_lb_target_group.health_check` is an `Optional`+`Computed` nested object whose *own* fields carry provider-side defaults (`int64default.StaticInt64(30)`, etc.) — but those per-field defaults only apply once the object itself is materialized; leaving `health_check` out of an HCL block entirely means the object stays null in the plan, so the provider's request-building code skips it and serializes Go's zero-value struct instead: explicit `interval: 0`, `healthy_threshold: 0`, `unhealthy_threshold: 0`, not an absent key. `create_target_group`'s own `hc.get("interval", 30)` (and `update_target_group`'s equivalent) treats a *present* key at face value regardless of its value, so an explicit `0` was taken as "the caller really wants zero" rather than "unconfigured."

**Fix:** since 0 is never a valid health-check interval or threshold in any real configuration, treat a falsy incoming value as unset: `hc.get("interval") or 30` (and the same for both thresholds and `path`), in both `create_target_group` and the update path — the latter chained all the way to the literal default (`hc.get(...) or tg["health_check"].get(...) or default`), not a single fallback to the existing stored value, since that value could itself already be a stale `0` from before this fix. A proper fix on the provider side (an object-level default so `health_check` is never null in the first place) was considered and deferred — this API-side fix needed no provider rebuild and directly addresses the actual failure mode.

**Verified by:** live. Confirmed `GET` on the target group returned `interval: 0` before the fix; after restarting the API and re-`PUT`ting the same target group, `interval: 30`/`healthy_threshold: 2`/`unhealthy_threshold: 2` came back and the regenerated haproxy config passed `haproxy -c` validation and actually bound its listener. 20 requests through the fixed load balancer then split exactly 10/10 across the local and cross-host backend per haproxy's own `stot` counters — the full item-1 fix (F-093 and F-094 together) proven live, not just independently validated.

### F-095 — llama.cpp's prebuilt CPU release binaries crash-loop on a stock Ubuntu 22.04 cloud image: `libgomp.so.1` isn't present by default

**Where:** `examples/distributed-llm`'s two cloud-init templates (both `.tftpl` and `.yml.j2` sides), found live provisioning the first real coordinator + RPC worker pair for the new distributed-LLM-inference example.

**Symptom:** both `llama-server` (coordinator) and `ggml-rpc-server` (worker) systemd units crash-looped immediately: `error while loading shared libraries: libgomp.so.1: cannot open shared object file: No such file or directory`.

**Root cause:** the prebuilt llama.cpp CPU release archive links against GNU OpenMP's runtime library, which the base Ubuntu 22.04 cloud image doesn't ship. Already resolvable without adding a new mirrored package — `libgomp1` is a transitive dependency of something else already present in the cached apt-repo mirror, just never explicitly requested as a top-level package before.

**Fix:** added `libgomp1` to `packages:` in all four cloud-init templates (coordinator and worker, Terraform `.tftpl` and Ansible `.j2`).

**Verified by:** live on both real hosts — `sudo apt-get install -y libgomp1` plus a service restart brought both units to `active` immediately; confirmed permanently fixed by rebuilding from the corrected templates.

### F-096 — `-ngl 99` (the usual GPU-offload "offload everything" convention) tried to push nearly the entire model onto the single RPC worker instead of splitting with the coordinator's own CPU

**Where:** `examples/distributed-llm`'s coordinator cloud-init (both sides), same live provisioning session as F-095, after fixing it.

**Symptom:** the coordinator's `llama-server` failed to load the model: `failed to allocate RPC0[<worker-ip>:50052] buffer of size 4296556544` (~4.3GB) against a worker with only 4096MB total RAM.

**Root cause:** llama.cpp's `-ngl` flag means "how many of the model's layers to offload to non-CPU backends" in total — not "layers per worker" and not a percentage. With exactly one RPC worker configured, every offloaded layer goes to that one worker. `-ngl 99` (the standard GPU-offload convention meaning "offload everything possible") tried to hand nearly the whole model to the single worker instead of splitting it with the coordinator's own local CPU share.

**Fix:** replaced the hardcoded `-ngl 99` with a tunable (`rpc_offload_layers` — new Terraform variable, default 16; new Ansible playbook var, same default and comment) set to roughly half of Mistral-7B-Instruct-v0.3's 32 transformer layers, giving a roughly even coordinator/worker split. Documented explicitly in both templates that this needs re-tuning by hand for a different model's layer count or a different worker count — there's no automatic even-split behavior.

**Verified by:** live. After the fix, the coordinator's model load completed successfully (~3m47s, "model loaded", "listening on http://0.0.0.0:8610"), and a real chat completion request succeeded directly against the coordinator.

### F-097 — The load balancer marked a genuinely healthy coordinator unhealthy: llama-server's root path doesn't return a 2xx

**Where:** `examples/distributed-llm`'s load balancer wiring (Terraform's `cloudcore_lb_target_group.coordinator`; Ansible's simple-backends `/backends` registration), same live provisioning session, after F-095/F-096.

**Symptom:** `curl` through the load balancer's own endpoint returned 503, while the identical request straight to the coordinator's own IP returned 200 and a real completion succeeded — the coordinator itself was fine, the load balancer's own health check was marking it down.

**Root cause:** a target group's `health_check` defaults to `GET /` when no path is specified (see F-094's own default-handling logic, unrelated bug, same code area), but `llama-server`'s root path returns HTTP 415 (Unsupported Media Type) — only `/health` returns 200. The default health-check path was simply the wrong endpoint for this specific backend.

**Fix:** Terraform's `cloudcore_lb_target_group.coordinator` now sets `health_check = { path = "/health" }` explicitly. Ansible's playbook — which registers the coordinator via the older simple-backends `/backends` endpoint rather than a target group — now calls `PUT /v1/load-balancers/{id}/health-check` with `{"protocol":"HTTP","path":"/health"}` right after registering the backend, for the same explicit-health-check parity (the simple-backends path defaults to *no* active health check at all when unset, so this wasn't strictly required to unblock Ansible, but matches the Terraform fix and gives real health enforcement rather than none).

**Verified by:** live. Fixed the running demo first via `PUT /v1/load-balancers/{lb_id}/target-groups/{tg_id}` with the same body; waited for the next health-check interval; confirmed the backend flipped to UP and a real chat completion succeeded through the actual load-balancer endpoint. Both templates re-validated afterward (`tofu validate`; Ansible YAML/Jinja parse) with the permanent fixes in place.

### F-098 — Every `systemctl --user restart cloudcore-api` was silently SIGTERM-ing every currently-running guest VM, not just reloading the API's own code

**Where:** `api/cloudcore-api.service` — found live via a direct user report: a real `examples/llm-chat` conversation "stopped responding mid way through a response" on a deliberately short, easy request (a bash CPU-performance script), immediately after an unrelated `cloudcore-api` restart done to reload code for the same session's own auto-cleanup feature (F-098's own neighbor in this log).

**Symptom:** the coordinator VM's own systemd journal (inside the guest) showed generation actively in progress (`n_gen = 165, tg = 1.32 t/s`) and then simply *stopped* — no graceful shutdown sequence, no OOM-killer message, no kernel panic, just silence, followed by a fresh boot 43 seconds later. `llama-server`'s own restart counter and PID had changed. The guest's own `dmesg` showed nothing — because the guest itself was never told to shut down; something outside it killed the whole VM.

**Root cause:** `api/compute.py`'s `QEMU_URI = "qemu:///session"` means every guest VM's own `qemu-system-x86_64` process is spawned as a **child of `cloudcore-api.service`'s own process**, landing in that service's systemd cgroup (`/user.slice/user-1000.slice/user@1000.service/app.slice/cloudcore-api.service`) alongside `libvirtd`/`virtlogd`/`dnsmasq`/`haproxy` — everything CloudCore itself ever starts. systemd's `KillMode` default is `control-group`, which sends the stop signal to *every process in that cgroup*, not just the unit's own `ExecStart` process. Confirmed directly in the VM's own libvirt qemu log: `qemu-system-x86_64: terminating on signal 15 from pid 1021271 (/usr/lib/systemd/systemd)` at the exact second of a routine `systemctl --user restart cloudcore-api` — a code-reload restart with no connection to any specific VM — followed by `shutting down, reason=crashed` and a brand-new QEMU process starting moments later. This had been happening on *every single* `cloudcore-api` restart performed throughout this entire session whenever a VM happened to be running at that moment, not something specific to `examples/llm-chat`.

**Fix:** added `KillMode=process` to `api/cloudcore-api.service` — scopes systemd's stop signal to the main process only, so every child CloudCore has ever spawned (VMs very much included) survives an API restart. `scripts/install.sh` copies this file verbatim into `~/.config/systemd/user/`, so a fresh install already gets the fix; the already-installed live copy was updated and `daemon-reload`d directly. `cloudcore-terminal.service`/`cloudcore-bridge.service` were checked and don't share this problem — the terminal service's own child processes are per-connection SSH sessions, where dying on a terminal-service restart is the expected, unsurprising behavior (unlike a long-running guest VM workload); the bridge service is a `Type=oneshot` script with no persistent children of its own.

**Verified by:** live. Confirmed the coordinator VM's own libvirt domain ID and `llama-server`'s own `MainPID` were **unchanged** across a real `systemctl --user restart cloudcore-api` performed *after* the fix — the exact same restart that would previously have killed it. Both other service files reviewed and confirmed not to need the same change.

### F-102 — Every LB mutation endpoint did an unlocked read-modify-write, so Terraform's own concurrent applies against one LB silently lost writes

**Where:** `api/server.py`'s load-balancer mutation endpoints (`create_target_group` and every sibling that follows the same shape — `update_target_group`, `delete_target_group`, `add_listener`, `update_listener`, `remove_listener`, `add_backend`, `remove_backend`, `update_lb`, the health-check set/delete routes) plus `api/store.py`. Found live via a direct user report on `examples/llm-chat`'s Stage 5 LB `routing_rules` rollout: "tried building llm-chat and we get 503 unvailable after quite some time of waiting."

**Symptom:** the LB returned 503 for every real request through it, while the coordinator's own `verify-proxy`/`llama-server` answered perfectly healthy when queried directly on the instance. The generated HAProxy config showed the coordinator's own backend server line pointing at port 80 — nothing listens there — while a second, newly-added target group (the terminal one, added in the same apply) showed the correct port.

**Root cause:** every one of these endpoints did a bare `get_lb()` → mutate in memory → `put_lb()` (a full-row upsert), with no locking. Terraform applies independent resources against the same LB concurrently by default (no `depends_on` between two unrelated `cloudcore_lb_target_group` blocks), so the two target-group creates in this apply — the coordinator's own and the new terminal one — genuinely raced: both read the LB row before either write committed, each appended its own target group to its own in-memory copy, and whichever `put_lb()` committed second silently overwrote the first's `target_groups` list with one that never contained it. The coordinator's own listener still pointed at the now-vanished target group's id, so `api/lb.py`'s config generation fell back to its legacy auto-discovered-backend path, which defaults an instance's own address to plain port 80 when nothing else is known.

**Fix:** a new per-`lb_id` lock (`store.lb_lock`, a small dict-of-locks keyed by lb id) wrapping every affected handler's *entire* read-modify-write span, not just the final write. Structural, not llm-chat-specific — any example whose LB gets more than one target group/listener added in the same apply was equally exposed.

**Verified by:** live. Manually repaired the already-broken live deployment's LB state, then reproduced the fix's correctness on a genuinely fresh `tofu apply` (a real concurrent creation of both target groups) — both came through correctly with no lost update, confirmed directly against the stored LB record.

### F-103 — HAProxy's classic health-check directive sends HTTP/1.0, which `websockets` 16 rejects outright before it ever reaches the app

**Where:** `examples/llm-chat/files/sandbox_terminal.py` (the Stage 5 terminal WebSocket service) and `api/lb.py`'s health-check config generation.

**Symptom:** after F-102's fix, the new terminal target group's own HAProxy health check still failed, marking the backend down and 503ing `/terminal` even though the WebSocket service itself was confirmed reachable and correct.

**Root cause:** HAProxy's classic `option httpchk GET <path>` directive (no explicit HTTP version) sends a bare HTTP/1.0 request line. `websockets` 16's own request parser rejects HTTP/1.0 outright ("unsupported HTTP version") before the request ever reaches `sandbox_terminal.py`'s own `process_request` health hook — confirmed directly by sending the exact same request by hand over both HTTP/1.0 and HTTP/1.1 against the live service.

**Fix:** added a `GET /health` short-circuit at the very start of the terminal service's own connection handling (same reasoning as the existing `process_request` hook) and switched `api/lb.py`'s health-check generation to HAProxy 2.x's `http-check send meth GET uri <path> ver HTTP/1.1 hdr Host localhost` form, for both target-group and LB-level checks — a general fix, not scoped to this one backend, since any HTTP/1.1-only backend on any example's LB would hit the identical failure.

**Verified by:** live. Raw `nc`/`curl` byte-for-byte comparison of the HTTP/1.0 vs HTTP/1.1 request against the running service reproduced the exact rejection; HAProxy's own `show stat` output confirmed the backend flipping from `DOWN`/`L4CON` to `UP`/`L7OK` after the fix.

### F-104 — `sandbox_terminal.py` bound to loopback, unreachable from HAProxy running on the host

**Where:** `examples/llm-chat/files/sandbox_terminal.py`'s `WS_HOST` constant.

**Symptom:** even with F-102/F-103 fixed, `/terminal` still 503'd — HAProxy's own health check (and every real request) got a raw connection-refused against the coordinator's real bridge address.

**Root cause:** `sandbox_terminal.py` bound its WebSocket server to `127.0.0.1` rather than `0.0.0.0`. HAProxy runs on the CloudCore host itself, not inside the guest, and reaches a bridge-mode target group's server via the instance's own real `private_ip` (`api/lb.py`'s `_resolve_tg_servers`), never loopback — `verify_proxy.py` already binds `0.0.0.0` for exactly this reason, but `sandbox_terminal.py` (added later, in Stage 5B) never matched that convention.

**Fix:** changed `WS_HOST` to `0.0.0.0`, matching `verify_proxy.py`'s own already-correct binding.

**Verified by:** live — `ss -tlnp` on the coordinator confirmed the listening address changed from `127.0.0.1:8622` to `0.0.0.0:8622`, and a subsequent request through the real LB reached the service instead of getting connection-refused.

### F-105 — A plain (non-raw) Python triple-quoted string silently mangled embedded JS escape sequences, breaking the entire sandbox page's script

**Where:** `examples/llm-chat/files/verify_proxy.py`'s `SANDBOX_PAGE_HTML` constant.

**Symptom:** once the LB itself was fully healthy (F-102–F-104), a direct report — the Ask panel showed no "Thinking…" indicator, and the Terminal panel's Start button did nothing at all.

**Root cause:** `SANDBOX_PAGE_HTML` is a plain (non-raw) Python triple-quoted string, so every backslash in its embedded JS is resolved by Python's own string-literal parser at import time, not left intact for the browser's JS parser. Two ANSI-color `term.write('\r\n\x1b[31m' + ... )` lines added for the new Terminal panel used single-escaped sequences; Python collapsed `\r`, `\n`, `\x1b` to their real control-byte values before the page was ever served, leaving a literal, unescaped newline inside a single-quoted JS string in the served page — a hard `SyntaxError`. Because the entire Run/Ask/Terminal logic lives in one inline `<script>` tag, that one syntax error broke all of it at once, not just the Terminal feature that introduced it.

**Fix:** double-escaped the two affected lines (`\\r\\n\\x1b[...]`), matching the convention every other JS-facing backslash in the same string already correctly used (e.g. `'\\n\\n'` for a `buf.split()` call elsewhere in the same file).

**Verified by:** live and directly reproducible offline — extracting the previously-shipped page's own inline script and running Node's `--check` against it reproduces the exact `SyntaxError` at the exact broken line; the same check against the fixed version passes. Confirmed live afterward: the served page's script parses cleanly, and both Ask and Terminal work again through the real deployment.

### F-106 — `sandbox_system_message`'s own on-disk file silently takes priority over a redeployed Python default, so editing `verify_proxy.py`'s source alone doesn't change the live prompt

**Where:** `examples/llm-chat/files/verify_proxy.py`'s `SANDBOX_SYSTEM_MESSAGE` loading (`_SANDBOX_SYSTEM_MESSAGE_PATH`, defaulting to `/opt/llama.cpp/sandbox-system-message.txt` — written once by cloud-init from `var.sandbox_system_message`, never touched again after that).

**Symptom:** live-testing a real prompt change (adding Terminal/preview-port awareness plus a `__PREVIEW_PORTS_LIST__` substitution token) against the real running coordinator, the model's real answers kept showing the old prompt's behavior — no port-list substitution at all, an invented port number instead — even after redeploying the updated `verify_proxy.py` and restarting the service.

**Root cause:** `SANDBOX_SYSTEM_MESSAGE` is loaded from the on-disk file first, falling back to the Python module's own `_SANDBOX_SYSTEM_MESSAGE_DEFAULT` constant only if that file is missing or empty — a deliberate design (the file is how a real Terraform/Ansible-configured `sandbox_system_message` value actually reaches the running process, the same convention this file uses for every other free-form-prose setting). The live coordinator's file had been written once, early on, from an earlier draft of the prompt; redeploying `verify_proxy.py` itself changes nothing about the file already on disk, so the running service kept loading the stale text regardless of how many times the module source was redeployed and the service restarted. The same shadowing applies to any other setting this codebase delivers as a written file rather than an env var (e.g. `webui-config.json`) — redeploying only the source is never sufficient to change behavior driven by one of these; the on-disk file itself must be redeployed too (or the instance rebuilt from a fresh apply, which regenerates it from the current variable value).

**Fix:** not a code bug — this is the design working as intended (a deployed prompt override must survive a code-only redeploy, not get silently reset). The actual fix for the live coordinator was redeploying `sandbox-system-message.txt` itself, not just the `.py` source; caught by directly re-reading the live file's own content after a "should be fixed" redeploy, via a raw `exec()` of the live source (bypassing any bytecode-cache ambiguity) to confirm `SANDBOX_SYSTEM_MESSAGE`'s real resolved value, rather than trusting the redeploy step alone.

**Verified by:** live. Confirmed the resolved `SANDBOX_SYSTEM_MESSAGE` matched the stale file's content, not the `.py` module's new default, until the file itself was redeployed — after which a real `/sandbox/ask` round trip produced the expected, corrected output (a real configured preview port in the generated code, not an invented one).

### F-107 — `chroot ... /bin/bash -c '...'` nested three quote-levels deep inside an `ssh "..."` argument reliably corrupted partway through, unrelated to anything in the script's own content

**Where:** `api/build-firecracker-rootfs.sh`'s remote build command — the golden Firecracker rootfs's own chroot provisioning step.

**Symptom:** every real rebuild attempt failed at the exact same point (`Setting up sudo/bash-completion`, right before the first embedded heredoc), with `bash: line 39: warning: here-document ... delimited by end-of-file (wanted `EOS')` — a phantom failure that named "link-local" (text from an unrelated comment deep inside one of the heredoc bodies) as if it were a script being executed. Reproduced identically on the completely unmodified, pre-existing script (`git show HEAD:...`), ruling out anything in this session's own edits.

**Root cause:** the remote command was `ssh host "sudo chroot "$ROOTFS_DIR" /bin/bash -c '...(the whole real provisioning script, including several of its own heredocs)...'"` — three quoting layers deep (ssh's own double-quoted argument, a single-quoted `-c` argument, and heredocs inside that). Extracting the exact same script text and running it as a real file (`bash /chroot_inner.sh`) inside an interactively-built chroot on a throwaway instance completed start to finish with zero errors, every time — conclusively isolating the fault to the nested-quoting mechanism itself, not the script's own logic or content.

**Fix:** restructured to write the identical script to a real file first (`cat > /tmp/build-inner.sh <<'BUILDEOF' ... BUILDEOF`, a quoted outer heredoc delimiter so nothing nested inside gets expanded while writing it) and execute it with `bash <file>` instead of the fragile inline `-c '...'` form. A related, easy-to-miss detail: the write step itself needs `sudo` to land inside the (root-owned, debootstrap-created) chroot directory — writing to a plain `/tmp/build-inner.sh` first, then `sudo cp` into place, avoids a `cat > file: Permission denied` from a bare unprivileged redirect.

**Verified by:** live, repeatedly, across several full real rebuilds after the fix — the exact same provisioning content that previously corrupted mid-stream now completes cleanly every single time.

### F-108 — `network-online.target` is satisfied trivially on a minimal debootstrap image, so a one-shot boot unit needing real network readiness can fire before it actually exists

**Where:** the Firecracker golden rootfs's own boot-time systemd units (`api/build-firecracker-rootfs.sh`).

**Symptom:** a new one-shot `apt-get update` unit, `Wants=network-online.target`/`After=network-online.target`, failed permanently and immediately on real boots with `Temporary failure resolving 'archive.ubuntu.com'` — despite the exact same real internet access working correctly a few seconds later in the same session.

**Root cause:** this minimal image has no NetworkManager or systemd-networkd `wait-online` unit installed, so nothing gives `network-online.target` real meaning — it is satisfied essentially trivially, before the interface/routing is genuinely usable.

**Fix:** wrapped the one-shot command in a small retry loop (10 attempts, 2s apart, matching `fetch-mmds-key.sh`'s own existing retry shape) instead of trusting the target alone. Best-effort, same reasoning as that script's own explicit `exit 0` — an exhausted retry budget must never be reported as a hard unit failure.

**Verified by:** live — confirmed via `journalctl -u refresh-apt-index.service` showing the retry loop actually running past the first failed attempt.

### F-109 — `/etc/resolv.conf` set at image-build time is silently replaced at boot by systemd's own resolved-stub symlink, which is never actually running on this minimal image — every DNS query fails permanently, not just at boot

**Where:** the Firecracker golden rootfs (`api/build-firecracker-rootfs.sh`) — `/etc/resolv.conf`.

**Symptom:** even after fixing F-108's retry timing, `apt-get update` kept failing on every single attempt, indefinitely — not a transient boot-timing race. `getent hosts archive.ubuntu.com` returned nothing at all, minutes into a live session, despite the image-build step explicitly writing a working `nameserver 8.8.8.8` into `/etc/resolv.conf`.

**Root cause:** installing `systemd`/`systemd-sysv` (needed for F-095-adjacent reasons — see the base-package comment in this same file) pulls in systemd's own resolv.conf management, which points the file at `127.0.0.53` (systemd-resolved's stub listener) on boot — silently overwriting the plain, working nameserver line set at image-build time. `systemd-resolved.service` itself is never installed/enabled on this minimal image, so nothing is actually listening on that address; every query fails outright, permanently, regardless of retry count.

**Fix:** the same retry script now force-writes a real, static `/etc/resolv.conf` on every attempt (not just once at image-build time), sidestepping whatever keeps re-managing the file rather than chasing down and disabling systemd's own resolved-stub logic specifically.

**Verified by:** live — `/etc/resolv.conf` confirmed holding `nameserver 8.8.8.8` (not the stub symlink) mid-session, `getent hosts archive.ubuntu.com` resolving real addresses, and a full `apt-get install` completing and the installed package actually running correctly afterward.

### F-110 — This lab's own recycled throwaway-builder IP pool can hand a later build run an IP a previous run already recorded a *different* SSH host key for, and `StrictHostKeyChecking=no` alone does not save it

**Where:** `api/build-firecracker-rootfs.sh` and `api/build-package-repo.sh` — both scripts' shared `SSH_OPTS`.

**Symptom:** an occasional, non-deterministic-looking build failure: `Offending ED25519 key in /home/scottp/.ssh/known_hosts` followed by `Password authentication is disabled` — ssh refusing to connect at all to a freshly-created throwaway builder instance.

**Root cause:** `StrictHostKeyChecking=no` alone only skips the interactive prompt for a genuinely *new* host; a *changed* host key on an already-present `known_hosts` entry is still refused outright (ssh's own real man-in-the-middle protection). This lab's own small throwaway-instance IP pool recycles addresses across independent builder runs, so a later run landing on an IP an earlier run already has a (different) key recorded for hits this refusal.

**Fix:** added `-o UserKnownHostsFile=/dev/null` to both scripts' `SSH_OPTS` — these are genuinely ephemeral, single-use hosts; persisting any host-key state across runs was never correct for them in the first place.

**Verified by:** live — reproduced the exact "Offending key" refusal once, confirmed it stopped recurring across several subsequent runs after the fix, with fresh instances on both new and previously-used IPs.

### F-111 — The Firecracker golden rootfs's own image-build cleanup left every `apt install` reporting a package as not found, indistinguishable from it genuinely not existing

**Where:** `api/build-firecracker-rootfs.sh` — the golden rootfs's own end-of-build cleanup step.

**Symptom:** direct report — "if you start a terminal and try to vi a file (existing or new) vi (vim) is not found, if you try to apt install vi (vim) it's not found." `vim` itself was a simple missing-base-package oversight (fixed by adding it alongside the existing `nano`); the second half was a real, general platform gap.

**Root cause:** the image-build step strips `/var/lib/apt/lists/*` to keep the shipped golden image small — correct for a frozen artifact reused across many future sessions, but it means every session starts with a genuinely empty package index, so `apt install <anything>` fails with "Unable to locate package" regardless of what is actually being asked for.

**Fix:** a new boot-time unit (`refresh-apt-index.service`) refreshes the index fresh every session over that session's own real internet access — see F-108/F-109 for the two further bugs found making that unit actually work reliably.

**Verified by:** live, end to end, through a real terminal session on the redeployed image: `which vi vim` resolves to real binaries, `vim --version` runs, and `sudo apt-get install -y bc` genuinely downloads, installs, and the installed binary runs correctly (`echo 2+2 | bc` → `4`). Separately flagged, not silently limited: debootstrap's own default `sources.list` only enables the `main` component, so a package living in `universe` (`tree`, the first one tried) still reports not-found even with a fully working index — apt itself is confirmed working; component coverage is a known, narrower boundary.

### F-112 — Sandbox's new "Regenerate" button was broken in the exact state it was enabled in, and never synced its own enabled/disabled state from saved history

**Where:** `examples/llm-chat/files/verify_proxy.py` (and its `ansible/examples/templates/llm-chat/` mirror) — `regenerateAsk()` and `renderTranscript()` in `SANDBOX_PAGE_HTML`'s embedded JS, part of this round's 5 sandbox student-experience improvements.

**Symptom:** two related bugs, both caught only by driving real browser JS via Chrome DevTools Protocol, not by `py_compile`/`node --check` static checks: (1) `regenerateAsk()` checked that the *last* history entry has `role === 'user'` before proceeding — but by the time the button is enabled (in `_runAsk`'s own `finally` block, after a full round trip), history's last entry is always the just-added `assistant` reply, so clicking Regenerate immediately after any normal Ask failed with "Ask a question first." (2) `regenBtn.disabled` was only ever set inside `_runAsk`'s `finally` block — a returning student (page reload, history restored from `localStorage`) or a student who just hit Clear both saw a `regenBtn` state left over from whatever happened last, not one reflecting the actual current history.

**Root cause:** the button's enable/disable logic was written to track "did a request just finish" rather than "does saved history actually contain a question to regenerate" — the two diverge on page load, after Clear, and immediately after the request the button itself was designed to be used following.

**Fix:** `regenerateAsk()` now scans backward for the most recent `role === 'user'` entry instead of requiring it to be the very last one, and passes everything before that question as context (identical to a first Ask). A new `_updateRegenBtnState()` helper (`regenBtn.disabled = !history.some(m => m.role === 'user')`) is called at the end of `renderTranscript()`, so page load, post-Ask, post-Regenerate, and post-Clear all derive the button's state from the same single source of truth.

**Verified by:** live, via Chrome DevTools Protocol driving the real deployed page (`192.168.100.242:8620`) end to end: fresh page → `regenBtn` disabled; `localStorage`-restored history (simulating a reload) → correctly enabled without any live ask; a real `regenerateAsk()` call against the live model — no "Ask a question first." bailout, history grew `user, assistant, assistant` (new reply appended alongside the original, not replacing it); `regenBtn` stayed enabled after; Clear correctly reset it to disabled. All 7 assertions passed on the redeployed, restarted `verify-proxy` service.

### F-113 — The Firecracker golden rootfs leaked the throwaway build instance's own hostname into every session, and had no `/etc/hosts` at all; `fdisk` genuinely missing

**Where:** `api/build-firecracker-rootfs.sh` — `build-inner.sh`'s package-install line and the chroot's `/etc/hostname`/`/etc/hosts` (never set at all).

**Symptom:** direct report — running `fdisk` in a Terminal session from a Linux Help suggestion showed `sudo: unable to resolve host cloudcore-fcrootfs-builder-1790072530: Name or service not known` followed by `sudo: fdisk: command not found`. `lsblk` worked but the shell prompt itself showed the same odd hostname (`student@cloudcore-fcrootfs-builder-1790072530`).

**Root cause:** two independent gaps in the same minbase debootstrap chroot. First, `debootstrap --variant=minbase` leaves `/etc/hostname` however it happened to resolve during the build — which turned out to be the *builder instance's own real hostname* (this script's own `$SUFFIX`, `cloudcore-fcrootfs-builder-<unix-timestamp>`), baked into the golden image and shipped identically to every future session since they all boot from the same frozen artifact; confirmed live via `cat /etc/hostname`/`hostname`/`cat /proc/sys/kernel/hostname` all returning that exact leftover string. Second, `/etc/hosts` doesn't exist in a minbase chroot at all (`cat /etc/hosts` → "No such file or directory") — nothing in the build script ever created one — so `sudo`'s own attempt to resolve its logging hostname has nothing to resolve against locally and falls through to a DNS lookup that fails, printing the warning on *every* `sudo` invocation (non-fatal — `sudo` still runs the command regardless, confirmed live: `sudo fdisk -l` worked despite the warning once `fdisk` was actually installed). Separately, `fdisk` itself is genuinely not pulled in by anything in the current package list — same class of gap as F-111's `vim` fix: on jammy it's its own package (`fdisk`, in `main`), not bundled into base `util-linux` the way `lsblk`/`mount`/`blkid` are.

**Fix:** `build-inner.sh` now sets `/etc/hostname` to a static, student-meaningful `sandbox` (no per-session uniqueness needed — this guest is already fully isolated and single-tenant, so there's nothing a unique hostname would actually buy) and writes a real `/etc/hosts` (`127.0.0.1 localhost`, `127.0.1.1 sandbox`). `fdisk` added to the base `apt-get install` list alongside `vim`/`nano`/etc.

**Verified by:** live, end to end. Rebuilt the rootfs for real (matching this project's own F-107 lesson that this exact nested chroot-heredoc-inside-heredoc quoting is not reliably reasoned about locally — a local dry-run reproduction attempt gave misleading results and was abandoned in favor of just running the real build) — new pinned artifact, sha256 `d4662e397860a133ce7a2b8d803faee55ade15ccd297a39c7e48f76dded28fb4`, deployed to the running coordinator's `/opt/firecracker/golden-rootfs.ext4` (same path/ownership `sandbox_terminal.py`'s own `MicroVM` expects). A fresh real Terminal session confirmed: `hostname`/`cat /etc/hostname`/`cat /proc/sys/kernel/hostname` all now read `sandbox`, the shell prompt itself shows `student@sandbox`, `cat /etc/hosts` shows the two real entries, `which fdisk` resolves to `/usr/sbin/fdisk`, and `sudo fdisk -l /dev/vda` / `lsblk` both run cleanly with **zero** "unable to resolve host" warning.

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
| v0.17 | 2026-09-12 | Paul Scott | Node.js/Java/jasypt/BouncyCastle added to the host-level repo for frontend and backend, verified against a real throwaway instance (Node v20.20.2, Temurin 21.0.12.1, `jasypt-dist.zip`'s `encrypt.sh` run for real against the fresh JVM). A new "ecs" operational user (NOPASSWD sudo, reusing the existing CloudCore inter-instance keypair) added to every instance in `ha-frontend-lb`, plus a short-hostname `cloudcore_dns_record` per instance — both required real platform-layer fixes, not just example-level Terraform: a DNS resolver that never picked up post-boot record changes (F-049) and a cloud-init `users:` semantics gap that silently broke default-user SSH access stack-wide the first time `extra_users` was actually exercised at instance-creation time (F-050). Verified with a real three-iteration build/destroy cycle against the full 15-node stack; both bugs caught and fixed live, final iteration clean. |
| v0.18 | 2026-09-12 | Paul Scott | Keystone tier extended with a classic OpenStack admin-API listener (`:35357`) and a generated `/etc/openstack/env.sh` OpenStack CLI environment file symlinked into `root`/`ecs`/`ubuntu`'s home directories, per direct request — `OS_AUTH_URL` points at the shared VIP (a static, Terraform-render-time-known value, so no systemd runtime IP-discovery step is needed). A naive first implementation gave `:35357` its own `WSGIDaemonProcess` pool, which OOM-killed Apache outright once combined with the two pools `:5000`/`:5443` already run (F-051) — chased initially as a security-group/DNS problem before the real cause (a resource crash, not a network rejection) was found. Fixed by sharing the existing `:5000` pool instead of spawning a new one; a related `env.sh`-placement permission bug (`/etc/keystone/` isn't traversable by non-keystone-group users) fixed in the same pass. Verified with a full destroy/rebuild: both Keystone nodes stable post-boot, `ecs` authenticating through the VIP on both `:5000` and `:35357`. |
| v0.19 | 2026-09-12 | Paul Scott | Custom `/etc/keystone/policy.json` (179 RBAC rules, provided verbatim) added, staged the same way as the existing Fernet keys (write_files runs before packages install, so `/etc/keystone`/the `keystone` user don't exist yet). No new findings — worked cleanly on the first real attempt: verified the rendered `templatefile()` output re-parses as valid JSON before ever booting an instance, then confirmed on a real build that the file lands `keystone:keystone 644`, Apache stays healthy, token issuance still works, and a policy-governed endpoint (`GET /v3/users`, `identity:list_users`) returns `200` for the admin token — confirming the custom policy is actually loaded and enforced, not just present on disk. |
| v0.20 | 2026-09-13 | Paul Scott | Ported the user's own `import_p3.py`/`roles.yml`/`system_domain.yml` (125 custom application roles, a "system" domain with 22 service/admin accounts) into `setup-keystone-roles.sh`, run locally on each Keystone node against its own `127.0.0.1:5000`, per direct instruction. Required adding `python3-requests`/`python3-yaml` to the host-level repo (F-039-adjacent — neither was previously needed by any example) and escaping `system_domain.yml`'s 22 `${password}` placeholders as `$${password}` so Terraform's `templatefile()` passes them through literally instead of trying to resolve its own interpolation. Verified for real: a live build hit repeated transient `Lost connection to MySQL server during query` errors under the combined write load of both Keystone nodes importing concurrently (125 roles + 22 users each, via the shared VIP→NGINX→ProxySQL→MySQL path) — the existing 3-attempt retry wrapper absorbed all of them, and the two nodes' independent, idempotent attempts converged cleanly on identical final state (128 roles, 22 users, no duplicates) with no manual intervention. Hardened afterward so an exhausted retry budget surfaces as a real script failure instead of a silent success. |
| v0.21 | 2026-09-13 | Paul Scott | Ported the user's own `create_users.sh`/`create_vhost.sh` (13 application service accounts, a `/ssp` vhost with full permissions for each) directly into `setup-rabbitmq.sh`'s existing `is_seed` conditional, per direct instruction — the "combined script" is that existing script, extended, not a new file. Much simpler than Keystone's equivalent: this tier already has a seed/joiner split (not active-active), and RabbitMQ's user/vhost database already propagates cluster-wide via Mnesia once nodes join (the same mechanism this file's own pre-existing `admin` user already relied on), so no retry/concurrency handling was needed at all. Verified for real: all 13 users and the vhost created cleanly on the seed and confirmed visible from a joiner immediately. One recurrence of F-045's dpkg-postinst-quirk class hit along the way, this time on `rabbitmq-server` rather than `nginx`/`keepalived` (F-052) — cosmetic (`cloud-init status` reported `error`) and didn't affect the actual outcome; the service came up healthy and everything else in `runcmd` succeeded regardless. |
| v0.22 | 2026-09-13 | Paul Scott | Ported the user's own `create_db_users.sh`/`createUser.sh` (13 `ssp_*` application databases + matching users, one database each) into `setup-group-replication.sh`'s existing `is_bootstrap` block, run once on the MySQL bootstrap node per direct instruction — same reasoning as Keystone/RabbitMQ's equivalents (a write-heavy, one-time provisioning step, not something every node should race to do). `mysql_native_password` used instead of the script's implicit 8.0 default, and `REQUIRE X509` applied afterward in `setup-mysql-tls-accounts.sh` — both deviations from the literal original script, deliberately matching this stack's own established, hard-learned conventions for every other MySQL account here (F-019, and the `appuser`/`keystone`/`proxysql_monitor` accounts' own TLS enforcement) rather than reproducing a bug class already fixed elsewhere in this exact file. No new findings — verified for real: all 13 databases/users/grants confirmed on the bootstrap node and replicated correctly to a secondary via Group Replication. |
| v0.23 | 2026-09-13 | Paul Scott | New `var.admin_password` (variables.tf) consolidates every admin/service-account login credential across the stack — MySQL's `repl`/`proxysql_monitor`/`appuser`/`keystone`/`ssp_*` accounts, Keystone's bootstrap `admin` user (and `env.sh`'s `OS_PASSWORD`) plus its 22 system-domain accounts, and RabbitMQ's `admin` user plus its 13 application accounts — into one settable value, replacing the scattered per-service `changeme-*` locals and two literal `admin`/`admin` hardcodes in the Keystone/RabbitMQ cloud-init templates. Deliberately left out: VRRP's auth secret, the Erlang cookie, Keystone's Fernet keys, and the CA provisioner password — internal cryptographic/protocol material, not login credentials, where sharing one value across unrelated purposes would reduce rather than improve security. MySQL's `root` account was also left untouched — it has no password at all (unix-socket auth only), and giving it one would mean touching every `mysql -u root` call site across the tier, a materially bigger and riskier change than unifying the already-existing named accounts. Verified for real with `-var admin_password=...` on a full build: the custom value confirmed working end-to-end for MySQL's `repl` account, RabbitMQ's `admin` and an application account (`catalog`, via `rabbitmqctl authenticate_user`), and Keystone's bootstrap `admin` and a system-domain service account (`catalogSvc`) — plus `env.sh`'s rendered `OS_PASSWORD` matching. |
| v0.24 | 2026-09-13 | Paul Scott | MySQL's `root` account given a real password after all, per direct follow-up instruction — the backend application connects remotely and needs it. Two accounts: `root@localhost` (every node, independently, replacing auth_socket, for manual/ad-hoc admin work) and a new `root@'%'` (created once on bootstrap alongside `appuser`/`keystone`/`ssp_*`, replicates normally, `REQUIRE X509` applied same as every other client-facing account) reachable through the shared VIP → ProxySQL path — the same route `appuser`/`keystone` already use, since it already handles Group-Replication primary discovery the backend would otherwise have to reimplement itself. A real, cluster-breaking bug found and fixed getting there (F-053): `~/.my.cnf` doesn't survive into the next `runcmd` script's environment (cloud-init runs each one without a real `$HOME`), which caused every subsequent `mysql -u root` call to fail outright — the entire cluster stayed `OFFLINE`, with `repl`/`appuser`/`ssp_*` all silently never created, on the first real build. Fixed by moving root's client credentials to `/etc/mysql/mysql.conf.d/` instead, a `$HOME`-independent location `mysqld` already loads from. Verified for real with a destroy/rebuild cycle showing the failure, then a second destroy/rebuild confirming the fix: Group Replication `ONLINE`/`PRIMARY`, both root accounts present and correctly replicated to a secondary, and a real remote connection through the VIP → ProxySQL succeeding end-to-end. |
| v0.25 | 2026-09-13 | Paul Scott | Investigated a report that no `ha-frontend-lb` instance could be reached via the CloudCore Dashboard's Terminal feature, described as "it sees all users as sudo (including ubuntu)" — reproduced, but found the actual cause was a genuine CloudCore platform UI bug (F-054, `ui/src/js/11-terminal.js`), not an SSH/sshd_config problem: direct testing proved SSH access (both `ssh ecs@<ip>` and the Terminal feature's own backend WebSocket protocol) was fully functional the entire time. The UI's own "all sudo, block the button" check never considered the same `ssh_user` fallback its sibling function already correctly used, so any instance with even one tracked sudo user — every node in this stack, once "ecs" was added — got incorrectly blocked. Fixed and rebuilt via `ui/build.sh`; no sshd_config or platform SSH changes needed anywhere. |
| v0.26 | 2026-09-13 | Paul Scott | F-054's fix confirmed correct on disk but the exact same symptom still reported from a second machine — traced to a second, unrelated CloudCore platform bug (F-055): the Dashboard's `index.html` (the entire dashboard bundled into one file) was being cached by the browser across page loads, so an already-fixed dashboard silently kept serving old JS with no visible error. Fixed by disabling caching on `GET /` outright. Surfaced a further, genuinely separate, real issue on that same machine that isn't a code bug: bridge-mode security-group enforcement failing on every instance there (`sudo -n iptables` denied) — same class as F-010, needs the NOPASSWD sudo grant matched to whichever user actually runs `api/server.py` on that machine. |
| v0.27 | 2026-09-13 | Paul Scott | The security-group sudoers grant turned out not to be a wrong-user problem but a genuine CloudCore platform bug (F-056): `install.sh` never invokes `setup-network.sh` directly, only via `cloudcore-bridge.service` — and systemd sets neither `SUDO_USER` nor `USER`, which the grant logic depended on. That resolves to an empty username, producing an invalid sudoers line `visudo -c` silently rejects, meaning the grant has never been installed for anyone on any `install.sh`-provisioned machine. Fixed with a new `CLOUDCORE_BRIDGE_USER` environment variable, injected into the systemd unit by `install.sh` from the real invoking user it already captures. Reproduced and verified directly under a clean, systemd-like environment (`env -i`): old logic → empty username → `visudo -c` syntax error; fixed logic → correct user → valid, accepted rule. Existing installs still need a one-time `sudo bash api/setup-network.sh` re-run to actually pick up the grant. |
| v0.28 | 2026-09-13 | Paul Scott | The "No SSH port available" saga's actual final cause (F-057): `cloudcore-terminal.service` (the Terminal WebSocket server) is a separate systemd unit from `cloudcore-api.service`, and nothing in the whole investigation — `install.sh`, `setup-network.sh`, a hard browser refresh, or a manual `cloudcore-api` restart — ever touched it. Python doesn't hot-reload its own imports, so the long-running process kept using stale code indefinitely; `install.sh`'s own `enable --now` is a no-op on an already-running unit, so even a fresh install couldn't have force-restarted it. Fixed with `PartOf=cloudcore-api.service` on the terminal unit, propagating restart/stop from the API service to it — verified directly by restarting only `cloudcore-api` and confirming `cloudcore-terminal`'s own PID changed too. Manually restarting `cloudcore-terminal` is what actually resolved the symptom on the reporting machine, confirming the diagnosis. |
| v0.29 | 2026-09-13 | Paul Scott | RabbitMQ AMQP + management API and Keystone's identity API closed to TLS-only, per direct instruction — RabbitMQ's plain `5672`/`15672` and Keystone's plain `:5000`/`:35357` are gone entirely (not kept alongside the TLS listeners), both server keys now passphrase-protected with `var.admin_password`, and `ecs`/`import_p3.py` gained real client certificates for their own mTLS access. MySQL deliberately left unchanged — every account an application uses already had `REQUIRE X509` enforced. Seven real bugs found and fixed via direct live-node testing across several full destroy/rebuild cycles: an `openssl pkey` same-path in/out corrupting keys mid-write (F-058); RabbitMQ's `listeners.tcp.default`/`management.tcp.port = none` being invalid cuttlefish syntax, with the real disable mechanism being the bare `listeners.tcp = none` plus simply omitting `management.tcp.port` (F-059); `management.ssl.*` needing its own independent `verify`/`fail_if_no_peer_cert` from `ssl_options.*` (F-060); Apache's `SSLPassPhraseDialog` being invalid inside a `<VirtualHost>` block (F-061); the real Keystone default site being named `keystone`, not the assumed `wsgi-keystone` (F-062); the boot-time role-import script's `127.0.0.1` target not matching any SAN on the node's own certificate (F-063); `setup-keystone-tls.sh` never reloading the already-running Apache process before the role-import script needed the new `:5443` listener (F-064); and, spotted by direct question rather than error output, both Keystone nodes racing to import the same role/user data into the one shared MySQL database they both already use — fixed by gating the import to the `-01` node only (F-065). |
| v0.30 | 2026-09-13 | Paul Scott | Reported from a second machine after pulling v0.29's fixes: the RabbitMQ management console appeared unreachable on all 3 nodes. RabbitMQ itself was healthy — the actual cause (F-066) was v0.29's own `management.ssl` mTLS requirement, an unforced extension of F-060's fix by analogy rather than something asked for on the management API specifically, which made the console unreachable from any plain browser (no client cert to present). Reverted to server-TLS-only for `management.ssl` per direct instruction — AMQP's own mTLS requirement is unaffected. Verified against a real rebuild: console reachable with just username/password, AMQP's `REQUIRE`-equivalent config confirmed unchanged. |
| v0.31 | 2026-09-14 | Paul Scott | New shared NFS storage tier for the frontend + backend tiers (`module.nfs`, app-install prep — `haFullStack-LLD.md` new §10). One real bug found and fixed (F-067): `nfs-common` was added to both tiers' `packages:` list but never added to `api/build-package-repo.sh`'s own `PACKAGES` array, so the host-level package repo never actually cached it — a real, deterministic "not found by APT" failure, not transient flakiness. Fixed and rebuilt; verified with a full destroy/rebuild showing a genuine cross-node read/write round-trip through the real NFS export. |
| v0.32 | 2026-09-14 | Paul Scott | Added a real way to get data onto a share — a CloudCore Dashboard "Files" panel per NFS share (drag-and-drop upload, browse, delete) plus the `GET`/`PUT`/`DELETE .../shares/{name}/files...` API it's built on, relayed through the same already-existing SSH channel the platform uses to manage the NFS server VM itself (no new listener, no new SG rule — `cloudcore_nfs_server` has none to add one to). One real bug found and fixed (F-068): a freshly `running` NFS server's export directory can still not exist yet (cloud-init's own LVM/mkdir/exportfs steps genuinely take longer than the libvirt domain state does to report `running`) — fixed with a defensive `mkdir -p` on every list/upload call. Verified with a real 50MB upload, SHA-256-confirmed byte-identical on the export, plus list/delete and filename-validation (traversal, shell metacharacters, hidden-file) probes all behaving correctly. README, the searchable help system's "NFS Servers" article, and Flask's dev server (`threaded=True`, so a long upload doesn't stall the rest of the Dashboard) all updated to match. |
| v0.33 | 2026-09-14 | Paul Scott | F-069: CloudCore's own internal DNS resolver (`api/dns_server.py`, 127.0.0.1:5353) had been silently unreachable for two days — an earlier `stop()`/`reload()` cycle sent a dnsmasq child a `SIGTERM` it didn't act on, then deleted the pidfile anyway, leaving an untracked orphan permanently squatting on the port and every subsequent `api/server.py` start failing to bind it with no recovery path. Fixed: `stop()` now verifies actual termination (escalating to `SIGKILL`) instead of trusting a single signal, and `start()` reaps any orphaned dnsmasq process matching its own `--conf-file=` invocation before binding. Verified live — killed `api/server.py` outright with the real two-day-old orphan still present and confirmed the next start log the reap, bind port 5353 successfully, and correctly answer both a real pre-existing DNS record and a made-up one. |
| v0.34 | 2026-09-14 | Paul Scott | Ansible port of `examples/ha-frontend-lb` complete (`ansible/examples/12-ha-frontend-lb.yml`, `haFullStack-LLD.md` new §11) — full parity, all 9 tiers, 17 instances + NFS. One real collection gap fixed (`cloudcore.cloudcore.instance` had no `users` parameter despite the API fully supporting it). One real, previously-undiscovered bug found during the port's own live verification and fixed in *both* IaC front-ends (F-070): RabbitMQ's and Keystone's `step-renew.service` has been broken since their keys were first passphrase-protected (v0.21/v0.29) — `step ca renew` was never given `--password-file`, so it couldn't decrypt the already-encrypted key it was renewing, crash-looping to a permanently `failed` unit on every single boot without ever affecting the live listener enough to be noticed by a status-page check. Verified with a full, real end-to-end `ansible-playbook` run (zero task failures) plus live on-node patching of the actual fix (`NRestarts=0`, stable, both affected services' listeners unaffected throughout). |
| v0.35 | 2026-09-15 | Paul Scott | New centralized-logging tier — Loki + Grafana on a single non-HA node (same precedent as the CA node), with promtail shipping the systemd journal plus `/var/log/cloud-init-output.log` from every other tier (`haFullStack-LLD.md` new §12), per direct request for a Lab-friendly, industry-standard log search UI. Three real first-boot bugs found and fixed in both IaC front-ends: the `loki` .deb never creates or owns its own data directory, crash-looping the service on every boot (F-071); `promtail`'s own service user isn't in the `adm` group, so it could never read `cloud-init-output.log` — only journal logs ever actually shipped (F-072); and `promtail`'s package postinst auto-starts the service before this stack's own `runcmd` can fix up the `__HOSTNAME__` placeholder, leaving a handful of stray `__HOSTNAME__`-labeled log lines on every boot — same first-boot race class already known from Grafana's admin password (F-073). All three verified clean from a genuinely cold `tofu destroy`/`apply` cycle (zero service restarts, no `__HOSTNAME__` label ever appearing) and confirmed at parity on an independently-built Ansible stack, with real cross-tier log queries proving both the journal and file scrape stages work end-to-end (MySQL's `mysqld`, Keystone's `apache2`, the CA's `step-ca`, and a frontend/backend node's `cloud-init-output.log` stream, all genuinely queryable through Grafana's Loki datasource on both stacks). Both full-stack rebuilds (34 VMs total, briefly run concurrently) pushed the build host to 1.8GB free RAM / 87% swap used before the already-verified OpenTofu stack was torn down mid-session to relieve it — a real capacity lesson for this Lab host, not a code finding. |
| v0.36 | 2026-09-15 | Paul Scott | Per-service log-FILE coverage added on top of v0.35's journal-only scrape (`promtail-config.yml`/its Ansible twin): nginx, MySQL, RabbitMQ, Apache2, and ProxySQL log files, plus the logging node watching itself — a real blind spot, only found by asking "are we watching everything?" directly. Two real permission bugs found and fixed in both IaC front-ends: RabbitMQ's log files are group-owned `rabbitmq`, not `adm`, so F-072's own fix didn't cover them (F-074); and ProxySQL's log file shares its directory, owner, group, and 660 mode with that node's own TLS private key, so the same group-membership fix used for RabbitMQ would have also granted promtail's OS user plain read access to the key — caught by inspection before shipping, fixed instead with a file-specific `setfacl` ACL scoped to the log alone (F-075). Verified live: both permission errors reproduced, fixed, and confirmed resolved on real nodes, with genuine per-service log content (not just the journal-sourced unit stream) confirmed reaching Grafana's Explore view for every one of the five new job labels. |
| v0.37 | 2026-09-15 | Paul Scott | `ha-frontend-lb` migrated onto the new host-level Loki + Grafana service (`api/setup-logging-service.sh`, `haFullStack-LLD.md` §12.3) — every example now ships to one fixed, always-on address (`192.168.100.1:3100`) instead of a dedicated per-build node; `module.logging` and its security group retired, `promtail-config.yml` flattened to a static file, the logging node's own circular-dependency self-watch workaround removed entirely (no longer a guest instance). One real regression found and fixed in both IaC front-ends during the required live re-verification (F-076): F-073's own promtail-fix ran too late in most tiers' `runcmd` (after that tier's own service-setup steps, not before), leaving a real window — ~30s on ProxySQL specifically, the one tier where it actually manifested in a time-bounded Loki query — during which promtail's still-`__HOSTNAME__`-labeled, postinst-started instance could ship genuine per-service log content under the wrong label. Fixed by moving the whole F-072/F-073 fix to the very first steps of every tier's `runcmd`. Verified via two full cold `tofu destroy`/`apply` cycles (the first surfacing F-076 via a **time-bounded** Loki query — an unbounded first-pass query had been misled by leftover data from the pre-fix build reusing the same suffix; the second confirming zero `__HOSTNAME__` rows across every job label), `NRestarts=0` for `loki`/`promtail` on spot-checked nodes, and the F-075 ProxySQL key-safety property re-confirmed unchanged (`getfacl`, `sudo -u promtail cat proxysql-key.pem` denied, zero key/cert mentions in `journalctl -u promtail`). Independently re-verified against a real `ansible-playbook` run of `12-ha-frontend-lb.yml` (`failed=0`, 48/48 tasks) — same clean result: zero `__HOSTNAME__` rows in a time-bounded query, `NRestarts=0`, and the key-safety property held, confirming the fix (and its reasoning) transfers correctly across both IaC front-ends rather than being an OpenTofu-specific coincidence. |
| v0.38 | 2026-09-15 | Paul Scott | Two real gaps found asking "is Loki actually reachable?" on a separate, previously-set-up machine (F-077): `setup-logging-service.sh` had genuinely never been run there (bridge and cached debs both present, but neither `loki.service` nor `grafana-server.service` existed) — traced directly to the step never having its own dedicated, rediscoverable README section, unlike `build-package-repo.sh`. Fixed with a proper "Set up centralized logging" section mirroring that one. Separately, `teardown-network.sh`'s own active-service safety guard — written for `cloudcore-repo` alone, before the logging service existed — never learned that `loki`/`grafana-server` now share the same bridge gateway address, so it would have let someone silently cut guests off from logging the exact way it was built to prevent for the package repo; now loops over all three. Sentinel's own `install.sh` (separate repo) also gained an end-of-install Loki reachability check that diagnoses which of the three CloudCore-side steps is actually missing, rather than a fresh install silently reporting "running" with nothing to watch. |
| v0.39 | 2026-09-16 | Paul Scott | `setup-logging-service.sh`'s Grafana datasource provisioning now sets a fixed `uid: loki` instead of leaving it to Grafana's own per-install random default (F-078) — found building Sentinel's new "View in Grafana" deep links (separate repo), which need to reference the datasource by a value stable across installs. Not yet re-applied to this session's own already-running host (needs a `grafana-server` restart to pick up the provisioning change) — a one-line follow-up, not run automatically against a live host-level service. |
| v0.40 | 2026-09-16 | Paul Scott | Two more real findings, both found investigating a stray `promtail` permission-denied line surfaced while building a throwaway test instance for the Grafana-links work. F-079: RabbitMQ's own systemd unit redirects stdout/stderr straight to two `root:root`-owned files (`rabbitmq-server.log`/`.error.log`), a completely different owner than the `rabbitmq:rabbitmq` files F-074 already covers — fixed with a file-specific ACL, same pattern as F-075, applied to both examples that install `rabbitmq-server` on both IaC front-ends. F-080, more significant: all five of Stage 5's "brand-new minimal cloud-init" examples (`compute-basic`, `dns-with-compute`, `network-lb`, `load-balanced-web`, `full-stack`) shipped with genuinely broken, crash-looping promtail configs — `tofu console`-rendering their own `user_data` locals and parsing the result as real YAML showed malformed indentation, confirmed live as a real `promtail` crash loop (`NRestarts` climbing, `yaml: line 4: did not find expected key`) on a real node. Root cause: Terraform's `indent()` function (which only touches lines after the first) and an HCL `<<-` heredoc's own blanket dedent don't compose correctly when combined directly — a combination `tofu validate` never renders far enough to catch, since it only checks HCL syntax. None of these five had ever actually been rebuilt and checked live during Stage 5 itself; only `openstack-services` (a different, unaffected pattern) was. Fixed by converting all five to the same `templatefile()`-against-a-real-`.tftpl`-file pattern already used successfully everywhere else in this repo — the heredoc variant was the only place this project had ever tried the broken alternative. Verified via real YAML parsing of all five rendered outputs plus a genuinely fresh `tofu destroy`/`apply` cycle on `compute-basic` showing `NRestarts=0` and real journal content reaching Loki. Confirmed the Ansible side was never affected (renders pre-computed static text, not a runtime indent filter). |
| v0.41 | 2026-09-16 | Paul Scott | Anonymous Grafana access (see `haFullStack-LLD.md` v0.31) needed one more real fix after going live: `org_role = Viewer` looked right and matched the "read-only" intent, but Grafana's own built-in Viewer fixed role doesn't include the `datasources:explore` RBAC action in this version — anonymous Viewer sessions hit a `302` redirect straight back to a login wall the instant they opened Explore, confirmed directly and isolated with a clean before/after `curl -i .../explore` comparison against `org_role = Viewer` vs `Editor` (F-081). Switched to `Editor`, the least-privileged fixed role that actually works — OSS Grafana has no supported way to grant anonymous sessions anything narrower. A real capability increase (dashboard save/edit, still short of Admin), accepted as a Lab-only tradeoff since this instance never leaves the internal bridge network. Verified end-to-end via Sentinel's own UI: suggestion/event "View in Grafana" links now land in Explore with real data, no login screen. |
| v0.42 | 2026-09-16 | Paul Scott | New `scripts/restart-stack.sh` (mirrored at Sentinel's own `restart-stack.sh`, usable from either repo) — restarts CloudCore, the host-level Loki/Grafana service, and a sibling Sentinel checkout together in the correct dependency order, then re-ingests Sentinel's knowledge base from this repo's own findings log (always, since `ingest-kb` upserts and is cheap — the simplest way to guarantee it's never stale rather than trying to detect "has it changed"). Building it surfaced two real things (F-082): `cloudcore-api.service` had been silently crash-looping for hours (566 restarts) behind an orphaned, un-managed `python3 server.py` process from an earlier manual test that never got cleaned up and was quietly squatting on its port the whole time — killed directly, and the new script's own `ensure_port_clear()` helper now guards against this exact class of problem on every restart going forward. Separately, actually *running* the new script (not just `bash -n`) caught a genuine `pipefail`+`set -e` bug in that same helper — aborted the whole script on the ordinary "port already free" case — fixed with a trailing `\|\| true`. |
| v0.43 | 2026-09-16 | Paul Scott | Both `restart-stack.sh` scripts and Sentinel's `install.sh` now *offer* to run `sudo bash api/setup-logging-service.sh` interactively when they detect it's missing (F-083), rather than only printing the command — real friction, hit twice by the same person on the same fresh-install machine, because a correctly-diagnosed but merely-printed recommendation is easy to read past in scrollback, and Sentinel's own `/api/status` unconditionally says "watching" regardless of whether Loki is actually reachable (a side effect of the Stage 3 discovery removal), masking the gap until someone actually clicked a Grafana link and hit a real connection-refused. Scoped deliberately: only offered for the fast, low-risk diagnosis branch (`.deb`s already cached, just the host-level install step missing) — the slow branch (`build-package-repo.sh`, 15-20+ minutes) stays informational-only. Gated on `[ -t 0 ]` so a non-interactive invocation never hangs on an unanswerable prompt. |
| v0.44 | 2026-09-16 | Paul Scott | Began cross-host peering (discover/pair/build on another CloudCore host over the real LAN, per direct request — see the session plan). Stage 0: the `192.168.100.0/24` bridge subnet is now per-host-configurable (`network.bridge_subnet_octet` setting, default 100, unchanged behavior until a host opts in), so two paired hosts can use non-overlapping subnets — `compute.py`'s `BRIDGE_CIDR` constant became a live `bridge_cidr()` read, `setup-network.sh`/`teardown-network.sh`/`serve-package-repo.py` parameterized to match. Stage 1: this host's own pairing identity (a dedicated ed25519 keypair, separate from the existing SSH keypair, `api/identity.py`) plus new `peers`/`pairing_requests` tables and an `instances.host_id` column, all schema/round-trip verified against a real copy of this machine's own database. Found and fixed a real platform bug doing that verification (F-084): `db.get_db()` ignored whatever path was passed to `db.init()`, always reconnecting to the real, live `cloudcore.db` underneath — silently invisible in production, but it meant an isolated test using a scratch database was actually reading and writing the production database the whole time; confirmed live (a test setting write really did land in the real DB) and fixed before any further stage work. |
| v0.45 | 2026-09-16 | Paul Scott | Cross-host peering Stage 2: opt-in mDNS discovery (`api/discovery.py`, the pure-Python `zeroconf` library — a new pip dependency, no system avahi-daemon needed), gated by a new `discovery.enabled` setting that's off by default and starts/stops advertising live with no restart. Attempting a genuine cross-host verification (this machine plus a second real machine, Llywyn-Y-Groes, on the same LAN) surfaced F-085 first: `install.sh`'s own `cloudcore-api` start step was a no-op against an already-running unit, so a `git pull` + re-run silently kept serving pre-pull code on *both* machines — the same gap class F-057 fixed for `cloudcore-terminal` specifically, just never applied to `cloudcore-api` itself. Fixed (`start` → `restart`) and both hosts restarted. Full Stage 2 verification then passed cleanly: this host correctly discovered Llywyn-Y-Groes's real hostname/address/port/pairing-fingerprint once it opted in, the two hosts' fingerprints confirmed distinct, this host's own `discovery.enabled=false` state confirmed silent (both via its own live `advertising: false` status and by code inspection — `advertise()` has exactly two call sites, both gated on the setting, and the only UDP 5353 traffic present on this host traced to the pre-existing, unrelated system `avahi-daemon`, not CloudCore), and Llywyn-Y-Groes correctly vanished from a re-scan within one poll after opting back out. |
| v0.46 | 2026-09-16 | Paul Scott | Cross-host peering Stage 3: the human-approved pairing handshake. A real architectural gap surfaced before any handshake code could be written: `cloudcore-api` has only ever bound `127.0.0.1:8080`, but the pairing bootstrap route must be reachable from another host, and blanket-exposing the whole dashboard API (protected only by the single shared `dev-token`) to the LAN would have been a real security regression. Solved by serving the same Flask app on a second bind (`api/peer_listener.py`, only while `discovery.enabled` is true — tied to the same single opt-in toggle, not a separate one) with a new `before_request` gate (`server.py`) restricting that bind to an explicit two-route allowlist, verified directly: a non-allowlisted route 403s on the peer port even with a valid token or no token at all, the allowlisted bootstrap route reaches its handler, and the dashboard bind is completely unaffected. Signing/verification uses `ssh-keygen -Y sign`/`-Y verify` (`api/peer_crypto.py`) rather than a new crypto dependency — proves a pairing request is self-consistent with its own claimed key, not identity authority; the actual trust decision is the human's Approve click. Per-peer bearer tokens (never the shared token) are minted independently by each side at approval time. Fully verified live end-to-end across the two real machines: discover → initiate (`POST /v1/peers`) → signed bootstrap request verified and recorded on the target → human approval on the target's own dashboard → approval callback delivered back to the initiator (`callback_delivered: true`) → both sides independently converged to `status: approved` with matching fingerprints and correctly separate per-direction tokens. Also verified: tampered-signature rejection, the pending-request rate limit (5/hour/source) and total-pending cap, and the reject flow (rejecting a request, then confirming an already-rejected request can't subsequently be approved). |
| v0.47 | 2026-09-16 | Paul Scott | Stage 4 prep: `wireguard-tools` installed on both real machines (each generating its own WireGuard identity keypair via `api/identity.py`'s existing lazy-generation path, confirmed live on both), and Llywyn-Y-Groes moved off the shared default bridge subnet (`192.168.100.0/24` → `192.168.101.0/24`, via the Stage 0 `network.bridge_subnet_octet` setting) — a real prerequisite for WireGuard tunnel routing between the two hosts to be unambiguous. Doing this for real surfaced F-086: `serve-package-repo.py`'s "dynamic bind address" fix from Stage 0 (v0.44) never actually worked on *any* host, including this one — `_bind_addr()` calls `settings_store.get(...)`, which needs `db.init()` first, but that standalone script never called it, so `get_db()`'s `RuntimeError` was silently swallowed by the function's own `except Exception` fallback on every single run since the commit that introduced it, always serving the hardcoded default regardless of the real setting. Invisible on stourport (default octet, so the wrong fallback happened to be the right answer) — only surfaced once Llywyn-Y-Groes's actual octet diverged from 100 and `cloudcore-repo` stayed unreachable at the new address. Fixed by having `_bind_addr()` call `db.init()` itself; verified both against a scratch DB with a non-default octet and read-only against stourport's own real production DB. |
| v0.48 | 2026-09-16 | Paul Scott | Cross-host peering Stage 4: WireGuard tunnel lifecycle (`api/wireguard.py`) — one shared `cc0` interface per host, one `[Peer]` block per approved pairing, created at approval time and torn down on revoke, transit addressing derived deterministically from each side's own bridge-subnet octet (no coordination round-trip). Bringing up the first real tunnel between the two machines surfaced F-087: `wg-quick` reliably fails to read its own config from a path under a normal user's home directory even as root via `sudo -n` (plain `sudo cat` on the identical file, same target user, succeeds — isolated specifically to `wg-quick`'s own file-open), while the canonical `/etc/wireguard/cc0.conf` location works every time — root-caused by testing the exact same content copied there, which brought `cc0` up cleanly on the first attempt. Fixed: the unprivileged API process now pipes the rendered config straight through a narrowly-scoped `sudo -n tee /etc/wireguard/cc0.conf` and always operates on the bare `cc0` interface name rather than staging a file under this repo's own directory at all. Also fixed alongside it: the `/v1/peers/complete` callback's timeout (8s) had no real margin over the receiving side's own synchronous handshake-status polling (up to 10s) — capped the polling to 6s and raised the timeout to 15s. Live two-machine re-verification then passed cleanly on the very next pairing: `callback_delivered: true`, `wg_tunnel_status: "up"` on both sides, `wg show cc0` confirming a genuine recent handshake with real data transfer, and — the actual proof this whole feature exists for — a direct `ping` from stourport across the tunnel to both Llywyn-Y-Groes's transit IP (`10.99.101.1`) and its real bridge gateway (`192.168.101.1`, the subnet real guest VMs will live on), 0% packet loss both times. Cross-host L3 reachability between two independent physical machines, confirmed live. |
| v0.49 | 2026-09-16 | Paul Scott | Cross-host peering Stage 5 (remote-instance proxying) — `POST /v1/instances` gained an optional `peer_id`: when set, the local API proxies create/read/delete to that peer's own API using the per-peer `remote_token` minted at pairing time, keeping only a local wrapper row (`host_id` set) rather than provisioning locally; `GET` live-proxies on every read, distinguishing a genuinely broken remote VM from a merely-unreachable peer via a new `InstanceStatus.UNREACHABLE`; `DELETE` refuses rather than silently discarding the local record if the peer can't currently be reached. `require_auth` extended to accept a valid peer token alongside the shared `dev-token`, with the existing peer-bind gate (Stage 3) still independently restricting what's reachable from the network regardless of token validity. This is the actual point of the whole feature, and it's now proven live end to end: from stourport's own API, `POST /v1/instances` with `peer_id` set to Llywyn-Y-Groes created a genuine libvirt VM *on Llywyn-Y-Groes* — watched progress `pending` → `running` with a real DHCP-assigned IP (`192.168.101.228`) entirely through the proxy path, `ping`ed successfully from stourport across the Stage 4 tunnel (0% packet loss), then `DELETE`d from stourport and confirmed gone from `virsh list --all` on Llywyn-Y-Groes itself, not just the local record. Discovery, human-approved trust, cross-host tunneling, and remote provisioning all working together as one real capability. |
| v0.50 | 2026-09-16 | Paul Scott | Cross-host peering Stage 6 (OpenTofu provider): `cloudcore_instance` gains `peer_id` (Optional, RequiresReplace, mirrors vpc_id/subnet_id) and a computed `host_hostname`; new `data "cloudcore_peers"` mirrors the existing list-everything `cloudcore_usb_devices` pattern; `modules/compute`'s per-key `var.instances` map and `modules/instance-group`'s scalar `var.peer_id` both wired through — confirmed the provider's single-`*Client`-built-once-in-`Configure` assumption needed zero changes, exactly as designed, since the proxying lives entirely server-side (Stage 5). Also extended `update_instance` (PUT) to proxy to the peer too — a real gap Stage 5 left open: without it, a name/tags/usb_device_ids edit on a remote instance would've silently only touched the local wrapper row (a real state-drift bug) and USB sync would've no-op'd entirely (the wrapper's own `domain_name` is always empty, so the local path's own `if ... and instance.domain_name:` guard would never fire). `host_hostname` needed a small enrichment helper (`_instance_dict()`) threaded through every instance-serializing route, since `models.py` has no DB access of its own to look up a peer's hostname. Verified with a real `tofu plan`/`apply`/`refresh`/`destroy` against the live two-machine setup — not mocked: `data.cloudcore_peers` returned real, live peer state (including current `wg_tunnel_status`), `apply` created a genuine libvirt VM on Llywyn-Y-Groes with a real DHCP IP, and `destroy` confirmed gone from `virsh list --all` there. One process-hygiene snag caught immediately, not a code bug: `host_hostname` showed empty right after apply because `cloudcore-api` hadn't been restarted since the Stage 6 code landed — same F-057/F-085 hot-reload class, just self-inflicted this time; restarting resolved it instantly and a direct Python-side check had already confirmed the underlying logic was correct all along. |
| v0.51 | 2026-09-16 | Paul Scott | Cross-host peering Stage 7 (Ansible) — F-088: the original plan's "zero code changes needed" premise for Ansible no longer held once Stage 3 established the peer's dashboard API is loopback-only, so `instance.py` gained a `peer_id` parameter (create-time only, same immutability as the provider's own) plus a new `peer_info` module mirroring the existing `usb_device_info` list pattern. A real hand-written playbook run then surfaced a genuine bug: remote instances' status never refreshed except via an individual GET (the LIST endpoint local instances rely on for the same idempotency-check pattern never proxies), fixed with a background polling thread mirroring local instances' own creation-thread pattern — which itself needed a second fix once re-tested live, for the same running-before-private-ip race the Go provider's own `Create()` already guards against. Three full playbook runs against the live two-machine setup, one per gap found and fixed, the third succeeding cleanly end to end: create via `peer_id` → poll to a real `running`/real IP → delete → confirmed gone from `virsh list --all` on Llywyn-Y-Groes. |
| v0.52 | 2026-09-16 | Paul Scott | Cross-host peering Stage 8 (dashboard UI) — new Peers section (nav under Networking): Pending Pairing Requests (approve/reject, polled every 15s), My Peers (status/tunnel/bridge-subnet, revoke), Discover & Pair (LAN scan plus a manual fallback for a non-advertising host). Settings gained a Networking subsection (the `discovery.enabled` toggle, the bridge subnet octet). The build manager's generic template-variable form now special-cases a `peer_id`-named variable as a real picker populated from live approved peers instead of a plain text box — the concrete delivery of "in the template, be able to select from the agents found." This session's headless environment has no interactive browser tooling (no playwright/puppeteer; Node 20 predates a built-in WebSocket client), so verification ran the real, unmodified source files directly in Node against the live API with minimal DOM stubs rather than skipping browser-level checks entirely: all three new/changed modules produced correct output from real live data (the actual paired peer, a real LAN scan, real settings state, a real peer option in a synthetic template schema) — full interactive click-through in an actual browser is the one thing this environment couldn't provide, noted rather than silently assumed. |
| v0.53 | 2026-09-16 | Paul Scott | F-089: the real-browser gap flagged in v0.52 caught a real bug within minutes of shipping — `let _peersPollTimer` in the new `26-peers.js` threw a temporal-dead-zone `ReferenceError` on *every* nav click dashboard-wide (not just Peers), since `ui/build.sh` concatenates `js/*.js` in filename order and `15-init.js`'s own top-level bootstrap call to `showSection('dashboard', ...)` — which unconditionally calls `stopPeersPoll()` — runs before the parser reaches file `26`'s own declaration. `google-chrome --headless --dump-dom` (the only browser tooling available in this session) doesn't surface page-level JS exceptions at all, confirmed directly against this exact broken bundle — this class of bug was only ever going to be caught by an actual person in an actual browser. Fixed (`let` → `var`, hoisted regardless of source position) directly from the user's own DevTools stack trace, verified by executing the real bundle in Node in its real concatenation order, and confirmed resolved in the user's own browser. |
| v0.54 | 2026-09-16 | Paul Scott | `modules/instance-group` gained real per-instance mixed-host placement (`var.placement_overrides`) — the whole group could previously only land on one host or stay local, with no way to split an identical-replica tier across machines. Verified live: a real 3-instance group with just instance "02" overridden to the paired peer split exactly as planned, the remote instance confirmed on the peer's own libvirt and reachable by ping across the tunnel. Cleaning that demo up then surfaced F-090: `discovery.browse()` created a brand-new `Zeroconf()` instance (its own background engine thread + sockets) on every single scan, and something in that create/destroy cycle wasn't fully releasing everything — enough repeated scans over one session's live testing exhausted Llywyn-Y-Groes's entire process file-descriptor limit, taking `cloudcore-api` down mid-`tofu destroy` (confirmed via `journalctl`'s `OSError: [Errno 24] Too many open files` and `lsof` showing a pile of thread-pool-associated fds). Fixed with a lazy long-lived singleton for the underlying engine, only the lightweight per-scan `ServiceBrowser` still created/cancelled per call — verified with a real 30-call stress test showing the fd count grow once (the expected one-time engine startup cost) then stay completely flat. |
| v0.55 | 2026-09-16 | Paul Scott | Per-resource `peer_id` wired into a real template (`ansible/examples/04-load-balanced-web.yml`) per direct request — "the idea is, for templates, that a user can specify which resource in the template should be farmed out to another cloudcore host." Three new top-level vars (`web_02_peer_id`/`web_02_peer_vpc_id`/`web_02_peer_subnet_id`, blank by default — both web instances stay local unchanged) let just the second of two web instances behind the load balancer go to a paired peer instead, the vpc/subnet pair needed alongside `peer_id` for the same reason `modules/instance-group`'s own `placement_overrides` needed it (a peer has its own separate catalogue). Verified for real against the live API: `extract_template_vars()` correctly classified all three as editable (not derived), and the Build Manager's own picker logic rendered exactly one real `<select>` for `web_02_peer_id` (populated with live approved-peer data) and plain text inputs for the other two. That verification surfaced F-091: two fully-approved `peers` rows existed for the same physical host — nothing had ever stopped re-pairing with an already-paired one, and the duplicate had silently been carried into the rendered WireGuard config as two `[Peer]` blocks sharing the identical `PublicKey`. Fixed with two dedup checks (by `api_url` on the initiating side, before the target's identity is known; by `pubkey_fpr` on the receiving side, the stronger signal, known immediately) — both verified directly, and the real duplicate revoked. |
| v0.56 | 2026-09-17 | Paul Scott | Live cross-host proof of `load-balanced-web` (per direct request — "okay, lets do it") surfaced F-092: `web-02`, placed on Llywyn-Y-Groes via `placement_overrides`, reported `running` with a correct IP and a confirmed valid DHCP lease, yet answered neither ping nor HTTP — its `security_group_ids` referenced a group that only existed on the *originating* host, silently accepted and resolved to zero rules on the executing peer, which still installed a DROP-terminated iptables chain that ate every reply packet. Fixed at the root: `POST /v1/instances` now rejects (`400`) any `security_group_ids` entry that doesn't resolve on the executing host, before the instance is ever created; `modules/instance-group`'s `placement_overrides` gained a `security_group_ids` field alongside `peer_id`/`vpc_id`/`subnet_id`, and `examples/load-balanced-web` a matching `web_02_peer_security_group_id` variable. Verified live end to end: manually corrected the already-broken demo instance's security group to unblock it, confirmed `ping`/haproxy health recovered immediately, then confirmed the load balancer genuinely round-robins real traffic across both the local and the cross-host backend via haproxy's own per-server `stot` counters — the actual scenario asked for, not just individually-passing pieces. |
| v0.57 | 2026-09-17 | Paul Scott | `examples/load-balanced-web` was missing an LB backend-registration step entirely (module.lb created a load balancer nothing routed to) — fixed per direct request ("lets fix item 1") using the already-existing `cloudcore_lb_target_group`/`cloudcore_lb_listener` provider resources, zero provider changes needed. Proving it live surfaced two more real bugs in that same code path: F-093, a target group's own explicit `instance_id` targets were resolved via a VPC-filtered instance list that a cross-host target's local wrapper row (storing the *peer's* own vpc_id) can never match, silently dropping it from the generated haproxy config with no error; and F-094, an unconfigured `health_check` block sends the Terraform provider's Go zero-value struct (explicit `interval`/threshold `0`s) instead of an absent field, and the API took that at face value instead of applying its own documented 30/2/2 defaults, so haproxy rejected the whole config outright (`invalid value 0 for argument 'inter'`) — took the whole load balancer down, not just the new backend. Both fixed API-side (no provider rebuild for either). Also surfaced, in passing and left unfixed (out of scope, unrelated to any of this session's changes): `PUT /v1/instances/<id>` doesn't actually persist `security_group_ids` server-side despite the provider sending it on every update, so Terraform's own state and a peer's live DB can silently drift apart. Verified live end to end: 20 requests through the corrected load balancer split exactly 10/10 across the local and cross-host backend per haproxy's own `stot` counters. |
| v0.58 | 2026-09-17 | Paul Scott | Per-resource peer placement extended to every remaining template (9 Terraform examples, 10 Ansible playbooks), per direct request — "ensure that all templates can allow selection of all resources to any connected peer." Single-instance templates got bare `peer_id`/`peer_vpc_id`/`peer_subnet_id`/`peer_security_group_id`; variable-count groups got one override for node "02" (anchor "01" stays local-only, matching the reference implementation); multi-tier templates (`openstack-services`) got one name-prefixed var-set per tier. `ha-frontend-lb` — a real, currently-active project, `git status` confirmed clean before any edit, additive changes only — got per-node placement for its clustered tiers (MySQL replicas, RabbitMQ joiners) rather than whole-tier, so individual cluster members can genuinely split across hosts independently, matching the original motivation for this whole feature (confirmed as the preferred design over whole-tier placement). That work also surfaced a real, non-obvious operational constraint worth documenting even though it isn't a code bug: ProxySQL's keepalived VIP failover needs VRRP multicast between both nodes, which does not cross a WireGuard peer tunnel onto a different physical bridge subnet — flagged with an explicit CAUTION comment on `proxysql_b_peer_id` rather than silently allowed. Every new variable defaults to `""` (zero behavior change until set) and is named to end in `_peer_id` so the Dashboard's existing picker logic renders a real dropdown with no further UI changes. Verified: `tofu validate` passes on all 9 touched Terraform examples (independently re-run, not just trusted from the rollout itself); all 11 touched Ansible playbooks parse as valid YAML. No `modules/`, `load-balanced-web`, `vpc-only`, or state/lock files touched. |
| v0.59 | 2026-09-17 | Paul Scott | A peer-placed resource's `peer_vpc_id`/`peer_subnet_id`/`peer_security_group_id` no longer need to be already known — per direct request ("we ought to provide default vpc id's and not expect the user to identify one... checking the current vpc id's on the localhost and connected peers"), three new proxy routes (`GET /v1/peers/<id>/vpcs`/`subnets`/`security-groups`) let the Build Manager fetch a peer's own catalogue live, and selecting a peer now cascades real dropdowns for all three sibling fields, auto-selecting the first/only entry. Required widening the peer-listener bind's endpoint allowlist (`list_vpcs`/`list_subnets` in `server.py`, `sg.list_sgs` in `peers_routes.py`) — these are proxied reads landing on the *peer's* own peer-listener bind, which 403s anything not explicitly allowlisted regardless of token validity; read-only, no more sensitive than `create_instance` already being reachable there. Verified live end to end against the real paired peer via Node with DOM stubs (this environment's only available headless-verification method): selecting the real peer correctly populated vpc/subnet/sg selects with that peer's actual catalogue, and resetting to local correctly cleared and disabled all three; family-detection logic (which vpc/subnet/sg keys belong to which `peer_id`, by suffix substitution — works for both bare and slot-prefixed variable names) also checked against synthetic bare and sg-less schemas. Searchable help's "Peers" article updated to match, applied to both `help_seed.json` and the live instance. |
| v0.60 | 2026-09-17 | Paul Scott | Peer placement extended from instances-only to VPCs, subnets, and security groups, per direct request — "do we have to limit resource placement to just instances?" No, a deliberate simplification, not an architectural limit. All three gained the same `host_id` field and CRUD-proxy pattern already proven for instances: create with `peer_id` set proxies to the peer's own API (skipping local catalogue-dependent validation — vpc_id existence, cidr containment, name uniqueness — same reasoning as instance creation), read live-refreshes mutable fields from the peer (no distinct "unreachable" status for these three, unlike instances — there's no meaningfully drifting operational state for a static resource, so a failed refresh just keeps cached values; a genuine 404 still marks it DELETED), delete proxies and refuses rather than silently discarding the local record if the peer's unreachable. Widened the peer-listener bind's allowlist for all twelve new endpoints. Provider (Go), the `vpc`/`security_group` Ansible modules (no `subnet` module exists at all — a raw `ansible.builtin.uri` task already covers the rare real-subnet case), and the three reusable Terraform modules example templates call (`modules/vpc`/`subnets`/`security-groups`, each gaining a `host_hostnames_by_key` output matching `modules/instance-group`'s own) all updated to match. Two background agents dispatched to parallelize the provider/Ansible work both hit a session rate limit and died mid-task (one partway through `vpc.go`, the other before touching anything) — completed directly instead, verified nothing was left inconsistent. The Resource Placement dashboard page (v0.59) extended to show all four peer-placeable resource types in one unified table instead of instances only; Load Balancers and NFS servers explicitly excluded (neither supports peer placement — an LB is always this build's own stable front door, an NFS server's placement is really about which instances mount it). Verified live end to end against the real paired peer through both IaC front-ends: a real `tofu apply` through `modules/vpc` and a real `ansible-playbook` run through the `vpc`/`security_group` modules each created genuine resources on Llywyn-Y-Groes, confirmed via the peer's own catalogue independently of the creating host's records, then destroyed cleanly. Also fixed in passing: a real version-numbering mistake in `haFullStack-LLD.md`'s own Document History — an earlier edit had duplicated `v0.32` across two unrelated entries; renumbered without changing either entry's content. |
| v0.61 | 2026-09-17 | Paul Scott | Capacity-aware placement, built across three direct requests in sequence. First: "we need to be able to collect performance statistics from each peer in order to understand which is overloaded... and which can tolerate it" — new `api/host_stats.py` (pure stdlib: `os.getloadavg()` normalized by core count, `/proc/meminfo`'s `MemAvailable`, `shutil.disk_usage()` on the actual VM-disk filesystem, not root), `GET /v1/system/stats` locally and `GET /v1/peers/<id>/stats` proxied, and a new "Capacity" card on the Resource Placement page showing every host side by side. Second: "we should show a traffic light system, green - use for resources, amber, risky but could try, red - leave alone" — a Placement column computed from the *worst* of CPU/memory/disk (deliberately not an average), reusing the existing badge CSS tiers. Third, the actual point of the whole arc: "we need for us to be able to determine for ourselves (automatically), where to place resource based on our traffic light system... we ought to autofill the location for a resource based on our traffic lights" while keeping the override — new `GET /v1/peers/recommend-placement` (host_stats.py's tier/threshold logic made public and reused server-side, the one place this project's Python/JS split couldn't avoid keeping two copies of the same three-tier logic in sync by hand) picks the lowest-severity candidate, tied broken by lowest CPU load, and both Build Managers now pre-select that peer in every `peer_id` dropdown and immediately fire the existing cascade — real vpc/subnet/security-group values arrive already populated, zero clicks, while the dropdown itself stays a completely normal editable control. Verified live throughout: real numbers from both hosts on the Capacity card (caught this host genuinely at 138.4% CPU load — actually overloaded, not a hypothetical — against Llywyn-Y-Groes's 3.3%); the recommendation endpoint correctly picking Llywyn-Y-Groes over a locally-amber host; and, via a DOM stub faithful enough to parse real `<select>`/option markup rather than just check strings, both `load-balanced-web` (OpenTofu) and `04-load-balanced-web.yml` (Ansible) auto-filling `web_02_peer_id` and cascading its vpc/subnet/sg fields with zero simulated interaction. |
| v0.62 | 2026-09-17 | Paul Scott | New `examples/distributed-llm` (Terraform) and `ansible/examples/13-distributed-llm.yml` (Ansible) — a llama.cpp coordinator + RPC worker(s) splitting a 7B model's layers across two real paired hosts, per direct request ("now that we have an automatic discovery of resources to peer machines we can think about creating an automatic template for an ai model larger than we currently use"). Proving it live against two real machines (this host as coordinator, Llywyn-Y-Groes as worker) found and fixed three real bugs: F-095, both llama.cpp binaries crash-looped on a stock Ubuntu 22.04 image missing `libgomp1`; F-096, the standard GPU-offload convention `-ngl 99` tried to push nearly the entire model onto the single RPC worker instead of splitting with the coordinator's own CPU, since `-ngl` counts total offloaded layers, not layers-per-worker, and the worker OOM'd trying to allocate a ~4.3GB buffer on 4096MB RAM — replaced with a tunable `rpc_offload_layers` (default 16, ~half of Mistral-7B's 32 layers); F-097, the load balancer marked the genuinely-healthy coordinator down because its default health check (`GET /`) hits a path llama-server returns 415 on, not 200 — fixed with an explicit `/health` health check on both the Terraform target group and, for parity, the Ansible playbook's simple-backends registration. All three fixed permanently in both IaC front-ends, not just patched live. Verified end to end: a real chat completion succeeded through the actual load-balancer endpoint, genuinely distributed across both hosts; both templates re-validated (`tofu validate`; YAML/Jinja parse) after the fixes. |
| v0.63 | 2026-09-17 | Paul Scott | F-098: a real user report — "the llm can stop responding mid way through a response" on `examples/llm-chat` — traced to `cloudcore-api.service`'s default systemd `KillMode` (`control-group`) sending every restart's stop signal to *every* process in its cgroup, guest VMs included, since `api/compute.py`'s `qemu:///session` libvirt mode spawns them as children of the API process itself. Every `systemctl --user restart cloudcore-api` performed throughout this entire session — done routinely for code reloads, never intentionally targeting any VM — had been silently killing whatever VM happened to be running at that moment; this specific report was cut off by a restart done moments earlier for an unrelated feature (the failed-build auto-cleanup work, same session). Fixed with `KillMode=process` on the service unit (both the repo's own template, so fresh installs already get it, and the live installed copy). Verified live: the coordinator VM's own libvirt domain ID and `llama-server`'s own `MainPID` were unchanged across a real restart performed after the fix — the same class of restart that had just killed it minutes earlier. |
| v0.64 | 2026-09-17 | Paul Scott | Three-part request: "lets go for fixing it all and at the same time allow the use of a large model with a larger vm (if the peer can afford the resources). configure the web ui defaults to a more technical configuration (per sampling temperature)." (1) `examples/llm-chat`'s coordinator now writes a `--webui-config-file` JSON (`webui_temperature` default 0.2, `webui_system_message` telling the model never to claim untested code works) — the exact schema was reverse-engineered live: extracted the real `llama-server` binary, ran a local instance, fetched its actual webui JS bundle, and confirmed via a constants-map literal (`SYSTEM_MESSAGE:"systemMessage",TEMPERATURE:"temperature"`) that these are flat top-level keys, then confirmed end-to-end that `GET /props`'s own `ui_settings` key echoes the file back verbatim. This directly targets a real hallucination found earlier this session (a 7m28s stress-test response with genuinely wrong percentile math and a described-but-never-called `sorted()`). (2) A new `standard.xlarge` flavor (6 vCPU/8GB) plus a verified second pinned artifact, the Q8_0 quantization of the same already-trusted `bartowski/Mistral-7B-Instruct-v0.3-GGUF` model (~7.17GB; sha256 confirmed via HF's own `X-Linked-ETag`, downloaded and re-hashed locally to confirm a byte-for-byte match) — deliberately the same model family, not a new architecture, so no new chat-template/tokenizer risk. (3) New `api/capacity_gate.py`, called from both Build Managers' own submit routes before a build is even created: for any template with a `worker_peers` list, checks each named peer's real current available RAM (reusing `peers_routes.peer_stats()`) against the chosen `worker_flavor`'s requirement (20% headroom) and rejects with a clear message if it doesn't fit — verified live against both an unreachable/bogus peer (rejected) and the real paired peer with genuine headroom (accepted), through both the OpenTofu and Ansible submit endpoints. F-099: live testing surfaced that the OpenTofu provider's own `flavor` attribute validator (`instance.go`, and separately `nfs_server.go`) hard-coded the four pre-existing flavor names — `standard.xlarge` was rejected at plan time even though `api/compute.py` already knew about it; fixed in both files. F-100: the same testing (a deliberately-bogus-peer build request that should have been rejected pre-reload by the old server process) tripped `tofu_engine.py`'s existing "wipe local `terraform.tfstate` before every build attempt" behavior against `examples/llm-chat`'s real, already-running deployment; the follow-up `destroy`/`apply` both failed at F-099's plan-time validation error before touching anything remote (confirmed: both real instances stayed `running` with unchanged `created_at` throughout), but local state bookkeeping was left empty. Recovering it via `tofu import` hit a second, genuine, previously-undiscovered provider bug: `InstanceResource.ImportState` never set `state.Users` at all, which the framework can't turn into a valid value on its own from a bare Go zero value — every import of a real instance with no extra `users` configured (the common case) failed outright. Fixed with a proper `instanceUsersFromAPI` conversion (new, `instance.go`) following the same null-preserving convention already used for `security_group_ids`/`tags`; all 8 real resources (vpc/subnet/sg/lb/target-group/listener/both instances) re-imported cleanly afterward and a `tofu plan` confirmed no drift against reality except the pre-existing, structural "`user_data` forces replacement on import" limitation (the API never echoes `user_data` back on `GET`, by design — not fixed, flagged as a standing limitation of this resource, not a regression). No `tofu apply` was run against the live deployment at any point; nothing was destroyed. Terraform/Ansible sides re-validated after every change (`tofu validate`, `tofu console` for the rendered webui JSON, a real Jinja2 render of the Ansible template, `ansible-playbook --syntax-check`); the provider rebuilt and reinstalled into the dev-overrides plugin directory; `cloudcore-api` restarted (`KillMode=process`, F-098, confirmed safe again: the real coordinator instance's own `status`/`created_at` were unchanged across the restart). README.md and both `help_seed.json` and the live searchable help DB updated to match. |
| v0.65 | 2026-09-17 | Paul Scott | F-101: a real live-fire regression, found by the user's own real rebuild immediately after v0.64 shipped — F-099's fix (a proper `instanceUsersFromAPI` conversion, called from the shared `instanceMapToState()`) over-corrected: `users` is Optional but not Computed, so Terraform requires Create/Update's final state to equal the *planned* value exactly, but the API's `GET` always returns `"users": []` whether config set `[]` explicitly or left it unconfigured — reconstructing it inside the shared function on every call (not just Import) occasionally collapsed a real, planned `[]` into `null`, tripping "Provider produced inconsistent result after apply" on literally every real instance in this repo, since `modules/instance-group` sets `users` via a module default of `[]` rather than leaving it unset. A real `llm-chat` rebuild failed on `module.workers`' instance with exactly this error; this project's own auto-cleanup-on-failure feature (v0.?? failure-queue work) correctly destroyed the partial resources already created (vpc/subnet/sg/lb/worker instance) before the error, confirmed via the API afterward — nothing orphaned. Fixed by moving the reconstruction out of the shared function entirely: `instanceMapToState()` no longer touches `state.Users` at all (Create/Update/Read already carry the correct value from plan/prior state, same as `user_data` already implicitly relies on); `ImportState` — the only caller with nothing to inherit from — now calls `instanceUsersFromAPI()` itself, after `instanceMapToState()`. Verified live: a real `llm-chat` build (real paired-peer worker + coordinator + lb + sg + vpc) completed successfully end to end with the fixed provider. Separately, per direct request ("mistral itself isn't really the best... code/code production/correction/assistance is all i really care about"): `llm-chat`'s own default model switched from Mistral-7B-Instruct-v0.3 to **Qwen2.5-Coder-7B-Instruct** (same bartowski quantizer, same size class — Q4_K_M ~4.36GB/28 layers vs Mistral's ~4.37GB/32 layers, `rpc_offload_layers` re-tuned from 16→14 accordingly; a Q8_0 pin added too for the `standard.xlarge` path). This is more than a benchmark swap: confirmed live via each model's own real `GET /props` that Mistral-7B-Instruct-v0.3's chat template has `supports_system_role: false` and its raw Jinja has no system-role branch at all (only alternating user/assistant, enforced by a `raise_exception` on violation), while Qwen2.5-Coder-7B-Instruct's real `tokenizer_config.json` chat template is ChatML with an explicit `{%- if messages[0]['role'] == 'system' %}` branch — meaning `webui_system_message` (the v0.64 anti-hallucination system prompt) was likely never actually reaching Mistral regardless of the temperature/localStorage fix, a gap this switch closes as a side effect. `examples/distributed-llm`/`13-distributed-llm.yml` deliberately left on Mistral — Sentinel log summarization is a different task this request wasn't about. Both new artifacts downloaded and sha256-verified against HF's own `X-Linked-ETag` (byte-for-byte match, not trusted blind). README.md, `help_seed.json`, and the live searchable help DB updated to match, including a new caveat on the webui-defaults note explaining the first-visit-only `localStorage` seeding behavior found while investigating why a real browser test kept showing temperature 0.8 despite a correct `--webui-config-file`. |
| v0.66 | 2026-09-21 | Paul Scott | Real live-incident debugging triggered by a direct report — "tried building llm-chat and we get 503 unvailable after quite some time of waiting" — on the first real `tofu apply` to exercise Stage 5's new LB `routing_rules` end-to-end. F-102: `api/server.py`'s every load-balancer mutation endpoint (`create_target_group` and its siblings) did a bare `get_lb()` → mutate → `put_lb()` with no locking; Terraform applies independent resources against the same LB concurrently, so the two `cloudcore_lb_target_group` blocks in this apply (the coordinator's own and the new terminal one) raced, and the second write's `put_lb()` silently clobbered the first's — the coordinator's own target group vanished from the stored LB entirely, its listener's `target_group_id` left dangling, haproxy falling back to a bogus port-80 guess and 503ing everything. Fixed with a new per-`lb_id` lock (`store.lb_lock`) wrapping every such handler's full read-modify-write span — a structural fix affecting every example with more than one target group on an LB, not just this one. F-103: even after that fix, the new terminal target group's own health check failed — HAProxy's classic `option httpchk GET <path>` sends a bare HTTP/1.0 request, and `websockets` 16's own request parser rejects HTTP/1.0 outright ("unsupported HTTP version") before ever reaching `sandbox_terminal.py`'s own `process_request` health hook; fixed by adding a `GET /health` short-circuit inside that hook and switching `api/lb.py`'s health-check generation to HAProxy 2.x's `http-check send ... ver HTTP/1.1` form for both target-group and LB-level checks. F-104: `sandbox_terminal.py` bound its WebSocket server to `127.0.0.1`, but HAProxy runs on the CloudCore host itself and reaches a bridge-mode backend via the instance's own real `private_ip`, never loopback — every LB-routed request got a real connection-refused; bound `0.0.0.0` instead, matching `verify_proxy.py`'s own already-correct convention. F-105: with the LB itself finally healthy, the user reported the sandbox's Ask panel showed no "Thinking…" indicator and the Terminal's Start button did nothing — traced to `verify_proxy.py`'s `SANDBOX_PAGE_HTML` being a plain (non-raw) Python triple-quoted string: the two ANSI-color `term.write('\r\n\x1b[31m'…)` lines added for the Terminal panel used single-escaped sequences, which Python collapsed to real CR/LF/ESC bytes before the page was ever served, leaving a literal unescaped newline inside a single-quoted JS string — a hard `SyntaxError` that broke the entire inline `<script>` tag, killing Run/Ask/Terminal simultaneously (confirmed by extracting the previously-shipped page's own script and running `node --check` against it, reproducing the exact error; the fixed version passes). Fixed by double-escaping those two lines to match every other JS-facing backslash already correctly doubled elsewhere in the same string. All four fixes deployed live to the running coordinator and API process (a manual restart, since it runs as a plain foreground process here, not a systemd unit) and verified end-to-end through the real load balancer: `/`, `/health`, and `/terminal` all healthy, a real terminal session usable, and the served page's script confirmed syntactically valid. |
| v0.67 | 2026-09-22 | Paul Scott | Per direct request — "in the coudcore application we have a llm performance page... at the moment we only show ingestion performance. i'd like to see llm performance itself as well" — built as a general mechanism any example can use, not a one-off special case for `llm-chat` (confirmed via direct clarification: "General registry for any LLM-serving example"). New `llm_deployments` table (`api/db.py`) storing only name/example/port/stats_path — deliberately no address or instance id, since a guest has no way to know its own CloudCore-assigned instance id at cloud-init render time and a stored address could go stale across a rebuild anyway; both are resolved fresh at poll time via `store.find_instance_by_name()`, the same trust boundary `api/lb.py`'s own target-group resolution already relies on rather than a self-reported address. New `api/llm_deployments_store.py` + `api/llm_deployments_routes.py`: `POST /v1/llm-deployments/register` is guest-reachable through the existing `examples_listener.py` bind (added to `server.py`'s `before_request` gate, same shared-token auth as the example-capture endpoint beside it); `GET /v1/llm-deployments` (dashboard-auth) resolves each registered name to its current instance/`private_ip` and live-polls its stats endpoint with a short timeout, reporting a stale/rebuilding entry as offline rather than failing the whole page. `verify_proxy.py` tracks aggregate stats (requests served, completion tokens, avg/last tokens-per-second, model, uptime) from llama-server's own per-response `timings` block, self-registers once at startup, and serves them at a new `GET /llm-stats`. New Dashboard "Live Deployments" section above the existing (now relabeled "Ingestion Run History") table, auto-refreshing every 3 minutes per direct request. Verified live end to end against the real running coordinator: real self-registration through the examples listener, real instance-name resolution to a live `private_ip`, real `/llm-stats` poll through the actual LB, and the `before_request` gate correctly 403ing every other route through that bind; the stats-recording math itself (avg/last tokens-per-second, token totals) verified directly rather than through a full chat round-trip, since this host's own real inference speed (~1 token/sec) made repeated waits impractical during testing. Required restarting the manually-run `api/server.py` process to pick up the new routes/schema. |
| v0.68 | 2026-09-22 | Paul Scott | Two more direct follow-ups. First — "we need to provide the student a way to run their code and see the results/output... there's no way to connect a browser to see the output" of a browser-based program run in the Terminal's isolated microVM — clarified via direct follow-up into a fixed pool of four ports decided once at deploy time ("a student's own program... may be more than one port required so picking one at creation time isn't going to work so well... create 4 ports with high numbers out of the way of anything obviously known... when the student opens the terminal give them a reminder"). New `preview_ports` variable (default `41001–41004`); `sandbox_terminal.py` opens one `asyncio` reverse-proxy listener per port alongside the existing WebSocket terminal, routing each incoming connection to the right student's own currently-connected microVM by reading `X-Forwarded-For` off its first request (the same IP-keyed session model `_terminal_handler` already uses for concurrency) — after that it's a dumb byte splice for the rest of the TCP connection, so whatever the student's program returns passes through unmodified; a plain `GET /health` short-circuits before any of that, the same fix already applied to the terminal target group's own check. `main.tf` gets one target group + listener per preview port (`for_each`, not `count`); the Terminal panel's own description and the shell's "connected" message both remind the student which ports are live. Verified live, full round-trip through the real LB: a real terminal session, a real `python3 -m http.server 41001` started inside the isolated microVM, fetched from a second independent connection through the LB on port 41001 — the student program's own real content came back, 200 OK; a request with no active session got a clean 502 rather than hanging; the microVM tore down with zero leftover processes once the session ended. Second — "lets get the ansible version matching the tofu version please," closing a gap flagged while building the above: `ansible/examples/14-llm-chat.yml` still used the LB's older simple single-backend model (no listener/target-group objects at all), so neither `/terminal` nor the new preview ports were reachable through an Ansible-deployed `llm-chat`'s own LB. Replaced with the existing (already idempotent) `cloudcore.cloudcore.lb_target_group`/`lb_listener` modules, mirroring `main.tf`'s own model exactly — verified via a throwaway playbook exercising both modules directly against the real running LB (real create, correct idempotent no-op on re-run, clean delete) before wiring the real names in. README.md and the searchable help (`llm-performance`, `builds-ansible`, `builds-opentofu`) updated to match both this and v0.67's work, pushed through the live `PUT /v1/help/articles` API as well as the seed file, since `help_articles` only ever seeds once from a genuinely empty table. |
| v0.69 | 2026-09-22 | Paul Scott | Per direct request — asked to reflect on what's been learned about `sandbox_system_message` recently and enhance it. Two real gaps folded into the prompt's default: `webui_system_message`'s own anti-hallucination wording ("only describe what code actually does... say so explicitly if you're not certain"), born from a real stress-test failure this project found but never carried over once that variable went vestigial in Phase 4; and zero awareness the Terminal/preview-ports feature (v0.68) exists at all, so the model had no grounding to correctly answer whether it could install a package or serve a web page. A light concision nudge added too, given this platform's own measured ~1 token/sec generation speed. F-106: live-testing the change caught a real, non-obvious deployment trap — the live coordinator's `sandbox-system-message.txt` (which takes priority over `verify_proxy.py`'s own Python default) had only been redeployed once, before a later port-number fix was added, so two rounds of "redeploy the .py source and restart" silently changed nothing; the model kept confidently inventing a wrong port (Flask's own conventional 5000, then 8080) instead of using one of the real configured `preview_ports`. Fixed by redeploying the actual on-disk file, and by substituting the real `PREVIEW_PORTS` into the prompt at load time (a `__PREVIEW_PORTS_LIST__` token, same mechanism the Terminal panel's own UI reminder already uses) rather than a hardcoded port guess. Verified live end to end: a real `/sandbox/ask` round trip asking to write and view a small Flask app came back with real venv/pip/Flask steps, `port=41001` (an actual configured preview port) in the generated code, and "open this URL in your browser" — no fabricated port, no invented UI control. Per direct follow-up — "every finding is useful for learning, lab user alike" — findings are now logged proactively as part of finishing any real debugging, not only when separately asked. |
| v0.70 | 2026-09-22 | Paul Scott | Direct report — "if you start a terminal and try to vi a file (existing or new) vi (vim) is not found, if you try to apt install vi (vim) it's not found." F-111: the Firecracker golden rootfs's own image-build cleanup strips `/var/lib/apt/lists/*` to keep the shipped artifact small, leaving every session's `apt install` reporting "Unable to locate package" regardless of what was actually asked for; `vim` added to the base package list, and a new boot-time `refresh-apt-index.service` refreshes the index fresh each session. Getting that unit to actually work reliably surfaced two further real bugs (F-108, F-109): `network-online.target` is satisfied trivially on this minimal image, racing real network readiness; and far more persistently, systemd's own resolved-stub management silently replaces the working `/etc/resolv.conf` set at image-build time with a symlink to a stub listener that is never actually running here, breaking every DNS query indefinitely, not just at boot — both fixed with a retry loop that also force-writes a real static resolver on every attempt. Rebuilding the rootfs itself to test these fixes surfaced a much deeper, structural bug (F-107): the script's own `chroot ... /bin/bash -c '...'` form, nested three quote-levels inside an `ssh "..."` argument, reliably corrupted mid-parse on every single attempt — reproduced identically on the completely unmodified original script, conclusively isolating the fault to the nested-quoting mechanism itself rather than anything in this session's own edits; fixed by writing the same script to a real file and running it with `bash <file>` instead. A fifth, unrelated finding (F-110) surfaced along the way from sheer rebuild volume: this lab's own recycled throwaway-instance IP pool can hand a later build an IP a previous run already recorded a *different* SSH host key for, which `StrictHostKeyChecking=no` alone does not save — fixed with `UserKnownHostsFile=/dev/null` in both `build-firecracker-rootfs.sh` and `build-package-repo.sh`. New pinned artifact, sha256 `e3e43bf8ba041a3febb4e590a43d0dcf31e0884a58873139f5bc792de7b74bbf`, deployed live to the running coordinator and verified end to end through a real terminal session: `vim` runs for real, and `sudo apt-get install -y bc` genuinely downloads, installs, and the installed binary computes correctly. Flagged, not silently fixed: debootstrap's own default `sources.list` only enables the `main` component, so a `universe` package (`tree`, the first one tried) still reports not-found even with a now-fully-working index. |
| v0.71 | 2026-09-22 | Paul Scott | Exploratory ask — "can you think of anything more for the sandbox that would improve the student experience?" — followed by direct approval to build all five proposed ideas: (1) a "Use this code" button on each fenced code block in the Ask panel's own replies, wiring `cm.setValue()` straight from the model's answer into the editor; (2) a "Send to Terminal" bridge sending the editor's own contents into a live terminal session as a real file, via a base64+quoted-heredoc pattern deliberately chosen (per this session's own F-107-class lessons about shell-escaping fragility) to be immune to any shell-special characters in the code; (3) a proactive Terminal warning at 300s-before-max-session and 120s-before-idle-timeout, sent as a new `{"type": "warning"}` WS frame from `sandbox_terminal.py`'s own per-session loop; (4) the Preview panel auto-refreshing on a plain 5-second timer, deliberately not genuine "detect when something starts listening" — reasoned through and documented as an honest limitation, since a browser's CORS-opaque `fetch`/`<img>`/`<iframe>` responses cannot distinguish a real 200 from this project's own 502 "no active session" response; (5) a "Regenerate" button re-asking the model's last question unchanged. All five built directly in `verify_proxy.py`/`sandbox_terminal.py` (and their `ansible/` mirrors) and live-verified against the real running coordinator — three via direct WebSocket/curl test harnesses (send-to-terminal round-tripped shell-special-character content byte-perfect and executed correctly; the idle-timeout warning fired at the correct threshold against a temporarily-lowered timeout; the preview panel's screenshot confirmed correct rendering), and, for the two client-side-only features needing real DOM/JS behaviour, via Chrome DevTools Protocol driving a real headless Chrome instance against the live page directly (chosen over waiting on this host's own slow ~1-token/sec real model round trips to get a code block to test against). F-112: that CDP testing caught the Regenerate button broken in the exact state it was meant to be used in — `regenerateAsk()` required the history's *last* entry to be the student's own question, but by the time the button is enabled (after a full round trip) the last entry is always the model's own just-added reply, so clicking Regenerate immediately after any normal Ask failed with "Ask a question first."; separately, the button's enabled/disabled state was only ever set inside the ask-completion handler, never synced from saved history, so a returning student (page reload) or a student who'd just hit Clear both saw a stale state instead of one reflecting what history actually contained. Fixed by scanning backward for the most recent question rather than requiring it to be the very last entry, and by deriving the button's state from history itself on every re-render. Re-verified live end to end via CDP after the fix, including one real model round trip: fresh page → disabled; reload-restored history → correctly enabled with no live ask; a real `regenerateAsk()` call succeeded with no bailout, history grew `user, assistant, assistant` (the new reply appended alongside the original); Clear correctly reset the button. All changes deployed to the redeployed, restarted `verify-proxy`/`sandbox-terminal` services and confirmed healthy through the real LB. |
| v0.72 | 2026-09-22 | Paul Scott | Three rounds of direct follow-up, all part of the same Stage 8 arc. First — "it's time now to allow the llm to provide answers to general linux based questions (all questions from admin to use)," agreed to keep genuinely separate from the coding Ask panel rather than folded in. New "Linux Help" panel with its own system prompt (`LINUX_SYSTEM_MESSAGE`, own file-based load convention) and its own `POST /sandbox/linux-ask` endpoint; `_handle_sandbox_ask` generalized into a shared `_handle_ask`/`_do_handle_ask(system_message, capture_source, verify, include_code)` serving both endpoints under the same per-IP rate-limit/concurrency gate; `_relay_and_verify_stream` gained a `verify` flag that skips both auto-execution and Phase-3-corpus capture together for this panel (that corpus hard-requires real code/exec data this panel never produces). Frontend Ask-panel JS (render/history/ask/regenerate/stop) refactored into one `makeAskPanel(cfg)` factory instantiated twice rather than duplicated; new `runCommandInTerminal()` sends a suggested command into the live Terminal via a randomized-delimiter quoted heredoc (deliberately not the same base64-into-a-file mechanism `sendCodeToTerminal()` uses, since this one needs the shell to interpret the command normally). Verified live via Chrome DevTools Protocol against the real deployed page: a real `/sandbox/linux-ask` round trip, "Run in Terminal" clicked against a real live microVM session actually executing the command with real output visible, the no-terminal-yet case showing real feedback, the existing coding panel regression-checked end to end, concurrency sharing and "no corpus row for a Linux-ask" both confirmed directly. Second — "the linux text input box (ask) needs to be much larger than it is. perhaps at least capable of showing 4 lines of typed text": `linuxQuestion` changed from a single-line `<input>` to a 4-row resizable `<textarea>`, Shift+Enter inserting a newline, plain Enter still submitting; verified live via CDP dispatching real keyboard events for both cases, plus a screenshot confirming the real rendered size. Third — asked directly whether the platform is prepared for a student running something destructive (e.g. partitioning the running disk) from a Terminal session started off a Linux Help suggestion and it becoming unresponsive or destroyed. Answered from the existing architecture (disposable per-session microVMs; Disconnect's client-side `ws.close()` and the server's `SIGKILL`-backed `teardown()` don't depend on the guest at all) and confirmed one real gap: a guest left unusable but whose SSH channel doesn't itself error (kernel/sshd still resident) isn't caught by the existing idle timeout, since retyping into a dead shell still counts as activity. Built and shipped on approval: new `TERMINAL_UNRESPONSIVE_SECONDS` (default 30) tracks real input sent with no real output following it that long, distinct from the idle/max-session timers; a new `{"type":"unresponsive"}` WS frame surfaces a "may be stuck" hint plus a one-click **Start a fresh session** button (`restartTerminal()` = `stopTerminal(); startTerminal();`). A real discovery from live-testing this, not assumed from the code: testing first with a real, safe `sleep 15` command (expecting a false-positive to prove the "suggestion, not an assertion" framing was necessary) instead showed the mechanism is more precise than that — bash's own readline echoes input back immediately as proof of liveness before a slow command's own silence begins, so an ordinary running command never trips the check at all; it only fires when the shell never acknowledges the input in the first place. Confirmed the true-positive case directly and safely with a real `SIGSTOP` on the session's own live Firecracker process on the coordinator (not just a slow command) — the hint fired at the configured threshold in both a raw WebSocket test and the real browser UI via CDP, the restart button correctly tore down the frozen VM and booted a genuinely new one (real "Booting... Connected... Welcome to Ubuntu..." transcript confirmed), and `SIGCONT`-ing the original frozen VM afterward confirmed it would otherwise have resumed cleanly with the buffered command completing and zero data lost — proving the freeze itself, not the recovery mechanism, was the only obstacle. All three rounds' Terraform/Ansible variables threaded through the usual dual-copy convention, deployed live, and documented in README.md, the phased-implementation doc, and the searchable help articles (pushed live via `PUT /v1/help/articles`, confirmed searchable). |
| v0.73 | 2026-09-22 | Paul Scott | Direct report from actually using the new Linux Help / Run in Terminal feature: running `fdisk` after `lsblk` gave `sudo: unable to resolve host cloudcore-fcrootfs-builder-1790072530: Name or service not known` then `sudo: fdisk: command not found`. F-113: two independent golden-rootfs gaps — `/etc/hostname` had silently leaked the throwaway build instance's own real hostname (`api/build-firecracker-rootfs.sh`'s own `$SUFFIX`) into the shipped image, baked into and shared by every future session; `/etc/hosts` didn't exist at all in the minbase chroot, so `sudo`'s own hostname self-lookup had nothing local to resolve against and warned on every single invocation (non-fatal, but real, visible clutter on every `sudo` command); `fdisk` was genuinely never in the package list (its own package on jammy, not bundled into base `util-linux`). Fixed: a static `sandbox` hostname plus a real `/etc/hosts`, and `fdisk` added alongside `vim`/`nano` in the base install list. Rebuilt the rootfs for real rather than hand-reasoning about the nested chroot-heredoc quoting locally (a local dry-run reproduction attempt gave misleading, contradictory results and was abandoned — directly the same lesson F-107 already established for this exact script). New pinned artifact, sha256 `d4662e397860a133ce7a2b8d803faee55ade15ccd297a39c7e48f76dded28fb4`, deployed live to the running coordinator's `/opt/firecracker/golden-rootfs.ext4` and verified end to end through a real fresh Terminal session: hostname now reads `sandbox` everywhere (`hostname`, `/etc/hostname`, `/proc/sys/kernel/hostname`, the shell prompt itself), `/etc/hosts` has real entries, `fdisk` resolves and `sudo fdisk -l`/`lsblk` both run with zero resolution warning. |
