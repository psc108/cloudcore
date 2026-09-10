# NGINX High-Availability Load Balancing Architecture — Findings Log

**Multi-Service Platform — Frontend, Backend, MySQL, Keystone, RabbitMQ**

v0.3 | Paul Scott

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

## Document History

| Version | Date | Author | Change Summary |
|---|---|---|---|
| v0.1 | 2026-09-10 | Paul Scott | First entries — architecture review (F-001/F-002) and Phase 1.A, Lab/OpenTofu (F-003–F-009). |
| v0.2 | 2026-09-10 | Paul Scott | Platform Hardening review before Phase 1.B — bridge-mode SG enforcement fixed (F-010), instance error surfacing added (F-011), CloudCore's own DNS server fixed (F-012–F-014). |
| v0.3 | 2026-09-10 | Paul Scott | First real build of Phase 2.A (Database Tier) — a genuine platform bug (F-015, bridge-mode egress_rules silently never enforced) plus four MySQL Group Replication bootstrapping gotchas (F-016–F-019: plugin load ordering, hostname resolution, config file load order, auth plugin), found and fixed in the order a real deploy hits them. Cluster reached ONLINE/PRIMARY with ProxySQL correctly auto-detecting the primary and the frontend status page showing a live, advancing heartbeat through the full real path. |
| v0.4 | 2026-09-10 | Paul Scott | Failure-mode testing (2A-11, 2A-12, 2A-14): secondary loss and primary failover both self-healed automatically with ProxySQL auto-retagging, measured primary-failover RTO ~3.5s (beats the assumed ~5-10s). Restart-and-rejoin confirmed working for a joiner node but revealed F-020 — the bootstrap node has no permanent seed and can't rejoin an existing group on its own after a restart, unlike joiner nodes; fixed manually for this run, flagged as a real open item rather than templated. |
| v0.5 | 2026-09-10 | Paul Scott | F-021 — the most significant finding this session: test 3 (2A-13, stop 2 of 3 nodes) revealed the survivor does not actually refuse writes after losing quorum by default, contradicting `haFullStack.md` §5.3's own original claim. `haFullStack.md` corrected to v1.3. Real split-brain protection is a new, undecided open item, not a same-session fix. |
| v0.6 | 2026-09-10 | Paul Scott | F-021 fixed: `quorum-watchdog.py` deployed and verified against a real below-quorum cycle run twice. A second bug found and fixed building the fix itself — `super_read_only=OFF` doesn't clear the separate `read_only` flag, which silently blocked ProxySQL from ever re-admitting a recovered node as a writer. `haFullStack.md` corrected to v1.4. |
