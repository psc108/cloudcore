# NGINX High-Availability Load Balancing Architecture — Findings Log

**Multi-Service Platform — Frontend, Backend, MySQL, Keystone, RabbitMQ**

v0.2 | Paul Scott

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

## Document History

| Version | Date | Author | Change Summary |
|---|---|---|---|
| v0.1 | 2026-09-10 | Paul Scott | First entries — architecture review (F-001/F-002) and Phase 1.A, Lab/OpenTofu (F-003–F-009). |
| v0.2 | 2026-09-10 | Paul Scott | Platform Hardening review before Phase 1.B — bridge-mode SG enforcement fixed (F-010), instance error surfacing added (F-011), CloudCore's own DNS server fixed (F-012–F-014). |
