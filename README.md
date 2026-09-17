# CloudCore

A cloud provider platform delivering VPCs, compute instances and L4/L7 load balancers.
Includes a REST API, web UI, Ansible collection, OpenTofu provider and modules.

## Getting Started

### Prerequisites

- Ubuntu 22.04+ host with KVM support (`egrep -c '(vmx|svm)' /proc/cpuinfo` must be > 0)
- `sudo` access
- Internet access (to download the Ubuntu cloud image on first run, ~600 MB)

### Install

```bash
git clone https://github.com/<your-org>/cloudcore.git
cd cloudcore
bash scripts/install.sh
```

The script is idempotent — safe to re-run. It will:

1. Install system packages (`qemu-system-x86`, `qemu-utils`, `libvirt`, `haproxy`, `dnsmasq`, `lvm2`, `nfs-common`, `golang-go`)
2. Install OpenTofu (official installer, `deb` method — skipped if `tofu` is already on `PATH`)
3. Build the CloudCore OpenTofu provider from source and register it via `dev_overrides` in `~/.tofurc` — it's built locally, not published to any registry, so this is the only way OpenTofu can find it (skipped if `~/.tofurc` already has an entry for it)
4. Install Python dependencies (`pip3 install --user -r requirements.txt`)
5. Build and install the Ansible collection
6. Download the Ubuntu 22.04 cloud image into `api/images/`
7. Generate the CloudCore SSH keypair in `api/keys/`
8. Install and start the `ccbr0` bridge network (system service, requires sudo)
9. Install and start the host-level package repo (`cloudcore-repo`, system service, requires sudo) — installed empty, see below
10. Install and start the API and terminal as systemd user services

After the script completes, open **http://127.0.0.1:8080** in your browser.

> **Note:** If this is the first time your user has been added to the `libvirt` group,
> log out and back in (or run `newgrp libvirt`) before creating VM instances.

### Populate the package repo

The install script sets up the `cloudcore-repo` service (an always-available
local apt repo + artifact cache, served over HTTP from the bridge gateway
address) but deliberately leaves it empty — populating it needs a throwaway
builder VM and several GB of real downloads, so it isn't something a one-time
install script should do unattended on your behalf. Before building anything
that installs packages (most of `examples/`), run it once:

```bash
CLOUDCORE_API_URL=http://127.0.0.1:8080 CLOUDCORE_API_TOKEN=dev-token \
  bash api/build-package-repo.sh jammy
```

Takes 15-20+ minutes. Only needs re-running when the target Ubuntu release
changes or a cached package needs a security update — not on every build.

### Set up centralized logging (Loki + Grafana)

Every example template ships its guest instances' logs to a host-level
Loki + Grafana service — one always-on install, not tied to any
specific build, so any example (not just one) gets real log coverage.
Like the package repo above, it isn't installed by `scripts/install.sh`
itself: it needs the `grafana`/`loki` `.deb`s the step above just
cached, and it's a materially different class of action (installing
real packages and starting new systemd services **on this host**, not
inside a disposable guest VM) — worth a deliberate, separate step
rather than something a one-time install script does unattended.

```bash
sudo bash api/setup-logging-service.sh
```

Grafana's admin password comes from `CLOUDCORE_LOGGING_ADMIN_PASSWORD`
(default `changeme-admin` if unset) — only needed for actually
administering Grafana (adding dashboards, data sources, users); normal
browsing doesn't need it at all, see below. Set it first if you want a
real one:

```bash
CLOUDCORE_LOGGING_ADMIN_PASSWORD=yourpassword sudo -E bash api/setup-logging-service.sh
```

(`-E` so `sudo` preserves the env var — plain `sudo` drops it, and the
default fires silently otherwise.)

Once it's running:

| Setting | Value |
|---|---|
| Loki | `http://192.168.100.1:3100` |
| Grafana | `http://192.168.100.1:3000` (Loki pre-provisioned as its datasource, anonymous Editor access — opens straight to Explore/dashboards, no login screen; `Viewer` doesn't get Explore access in this Grafana version, `Editor` is the least-privileged role that does) |

One-time — only needs re-running if the `loki`/`grafana-server`
services are ever removed. `systemctl is-active loki grafana-server`
confirms it's up; the [Sentinel](#optional-sentinel-log-intelligence-advisor)
section below depends on this being done first.

### Cross-host peering (optional)

Discover another CloudCore install on your real (physical) LAN, pair
with it (a human click on *its* dashboard is required — never silent),
and build instances on it from your own Terraform/Ansible/dashboard —
real clustering across separate machines, connected by a WireGuard
tunnel carrying the guest bridge traffic. Off by default and fully
opt-in: a host announces nothing on the network until you explicitly
turn it on.

```bash
sudo apt-get install -y wireguard-tools
sudo bash api/setup-wireguard.sh
```

Then, in the Dashboard, **Settings → Networking**: turn on "Announce
this host on the network" (and, if you intend to pair with a host that
also uses the default bridge subnet, give this host a different
**Bridge subnet octet** first, then re-run `sudo bash
api/setup-network.sh <octet>` to apply it). Pairing, approval, and
picking a peer inside a template all happen from the Dashboard's
**Peers** section — see `haFullStack-LLD.md` §13 for the full design.

### Scheduler & 7B LLM ingestion (optional)

Schedule any Terraform/Ansible example to build itself on a recurring
or one-off basis — daily, weekly, every N minutes/hours, or a specific
date/time — from the Dashboard's **Builds → Scheduler** page. The
picker only ever exposes those simple options; full cron syntax runs
underneath but is never something you type.

One schedule *kind* is special: **7B LLM Sentinel Ingest**. Each
wakeup it checks [Sentinel](#optional-sentinel-log-intelligence-advisor)
for new events since the last check; if there's nothing new, it stops
there (no cost). Otherwise it builds a real distributed-inference
cluster (`examples/llm-chat`'s own coordinator + RPC worker mechanism,
splitting a 7B model across this host and one or more paired peers —
see "Cross-host peering" above), has the model draft an understanding
of what Sentinel flagged, writes any new findings/suggestions back
into Sentinel's own knowledge base, pushes the same findings to every
currently-online peer's own Sentinel instance, and tears the cluster
back down. Which peers actually get used each cycle is decided
automatically from their current CPU/memory/disk load (the same
green/amber/red traffic light the Resource Placement page shows) —
you pick the *candidate pool* once, not which ones run each time.

Every run's own timing (cluster build time, model load time, inference
time), token counts, and per-host resource usage during the run are
on the Dashboard's **Builds → LLM Performance** page — a history of
real numbers from actual runs, not a live dashboard (the cluster is
ephemeral, so there's usually nothing running to watch live).

For a persistent, human-facing chat session instead of the automated
ingestion above, build `examples/llm-chat` (or
`ansible/examples/14-llm-chat.yml`) directly and open its own
`chat_url` output in a real browser — llama-server's own built-in Web
UI, not a custom frontend. A fresh chat session starts from technical,
low-hallucination defaults out of the box (sampling temperature 0.2
and a system prompt telling the model not to claim code does something
it doesn't) — still fully editable per-session in the browser's own
Settings panel via `webui_temperature`/`webui_system_message`.

Both `llm-chat` and `distributed-llm` can also run the larger, higher-
precision Q8_0 variant of the same model on the new `standard.xlarge`
flavor (6 vCPU / 8GB RAM) instead of the default Q4_K_M/`standard.large`
pairing — override `model_filename`/`model_sha256`/`coordinator_flavor`/
`worker_flavor` when building. Whichever flavor you pick, every worker
peer's own real available RAM is checked against it *before* the build
is even submitted — an under-resourced peer is rejected with a clear
message instead of silently OOM-killing partway through model load.

### Default credentials

| Setting | Value |
|---|---|
| API token | `dev-token` |
| UI URL | `http://127.0.0.1:8080` |
| API base | `http://127.0.0.1:8080/v1/` |

To use a different token, override the environment variable in the service:

```bash
systemctl --user edit cloudcore-api.service
# Add:
# [Service]
# Environment=CLOUDCORE_API_TOKEN=your-token
systemctl --user restart cloudcore-api.service
```

### Service management

```bash
# Status
systemctl --user status cloudcore-api cloudcore-terminal

# Logs
journalctl --user -u cloudcore-api -f
journalctl --user -u cloudcore-terminal -f

# Restart
systemctl --user restart cloudcore-api
```

To restart everything together — CloudCore, the host-level Loki/Grafana
service, and (if a sibling checkout is found) Sentinel too, in the correct
order, ending with a refreshed Sentinel knowledge base:

```bash
bash scripts/restart-stack.sh
```

Safe to re-run any time. Needs `sudo` for the Loki/Grafana restart step, so
run it yourself in a real terminal rather than piping it through anything
that can't answer a password prompt. Mirrored at Sentinel's own
`restart-stack.sh`, usable from either repo.

Tearing down the bridge network itself (`ccbr0`) is a separate, rarely-needed
step — most people never need this:

```bash
sudo bash api/teardown-network.sh
```

Refuses if `cloudcore-repo`, `loki`, or `grafana-server` are still active on
it (tearing the bridge down wouldn't stop them, just silently cut every guest
off from them) — stop those first, or pass `--force` to proceed anyway.

### Optional: Sentinel (log-intelligence advisor)

[Sentinel](https://github.com/psc108/sentinel) is a standalone
companion tool, in its own repo — it watches this platform's
host-level Loki service (set up above, "Set up centralized logging"),
flags log activity that looks like real trouble, and matches it
against a knowledge base seeded from `haFullStack-Findings-Log.md` —
surfacing suggestions through its own web UI. Since Loki is always-on
and shared by every example template, not tied to any one build,
Sentinel watches from the moment it starts regardless of what's
currently built. It has no source dependency on CloudCore (only
Loki's own HTTP query API), so it's entirely optional and safe to
skip — nothing here requires it.

**Requires the logging service above to already be running** —
Sentinel's own `install.sh` checks Loki's reachability at the end and
tells you exactly which step is missing if it isn't, but there's
nothing to watch either way until `setup-logging-service.sh` has run.

```bash
git clone https://github.com/psc108/sentinel.git ../sentinel
cd ../sentinel
bash install.sh
```

`install.sh` auto-detects a sibling CloudCore checkout (whatever its
local directory is actually named — it isn't assumed to be `cloudcore`
or anything else) and seeds Sentinel's knowledge base from its
`haFullStack-Findings-Log.md` automatically. Pass a path explicitly
(`bash install.sh /path/to/haFullStack-Findings-Log.md`) if this repo
lives somewhere the auto-detection won't find.

Installs itself as two always-on systemd user services and seeds its
knowledge base from this repo's own findings log in one step (also
downloads a small pretrained matching model, ~90MB one-time, cached
locally afterward — same "download once, work offline forever after"
pattern as this repo's own package repo). UI at
**http://localhost:8900/**. Full setup, CLI reference, and how to
(re)train it from real usage: see
[Sentinel's own README](https://github.com/psc108/sentinel#readme).

---

## Artefacts

| Artefact | Path | Purpose |
|---|---|---|
| REST API + UI | `api/` | Python/Flask — core platform |
| OpenTofu provider | `provider/` | Go — Terraform Plugin Framework |
| OpenTofu modules | `modules/` | HCL — composable module library |
| OpenTofu examples | `examples/` | Ready-to-run configurations |
| Ansible collection | `ansible/collections/cloudcore/` | Python — FQCN `cloudcore.cloudcore` |
| Ansible examples | `ansible/examples/` | Ready-to-run playbooks (01–14) |

## Requirements

| Tool | Version | Required |
|---|---|---|
| Ubuntu | `22.04+` | Yes |
| Python | `>= 3.10` | Yes |
| Ansible | `>= 2.15` | Yes (installed by `scripts/install.sh`) |
| OpenTofu | `>= 1.8.0` | Yes, if using `examples/` — installed by `scripts/install.sh` |
| Go | `>= 1.22` | Yes — installed by `scripts/install.sh`; builds the provider binary, which isn't published anywhere and isn't committed to this repo |

## OpenTofu

### Provider

The provider is pre-built. To rebuild from source:

```bash
cd provider
go build -o terraform-provider-cloudcore .
```

Configure via environment variables or a provider block:

```hcl
provider "cloudcore" {
  api_url   = "http://127.0.0.1:8080"
  api_token = "dev-token"
}
```

```bash
export CLOUDCORE_API_URL=http://127.0.0.1:8080
export CLOUDCORE_API_TOKEN=dev-token
```

### Modules

Eleven reusable modules live in `modules/`:

| Module | Path | Resources |
|---|---|---|
| VPC | `modules/vpc/` | `cloudcore_vpc` |
| Subnets | `modules/subnets/` | subnet metadata |
| Internet Gateway | `modules/internet-gateway/` | `cloudcore_internet_gateway` |
| Route Table | `modules/route-table/` | `cloudcore_route_table` |
| Compute | `modules/compute/` | `cloudcore_instance` |
| Instance Group | `modules/instance-group/` | `cloudcore_instance` (count-based) |
| Load Balancer | `modules/load-balancer/` | `cloudcore_load_balancer` |
| Security Groups | `modules/security-groups/` | `cloudcore_security_group` |
| NFS Server | `modules/nfs-server/` | `cloudcore_nfs_server` |
| DNS Zone | `modules/dns-zone/` | `cloudcore_dns_zone` |
| DNS Records | `modules/dns-records/` | `cloudcore_dns_record` |

All modules follow the standard argument contract:

| Variable | Type | Default | Required |
|---|---|---|---|
| `enabled` | `bool` | `true` | No |
| `environment` | `string` | — | Yes |
| `project` | `string` | — | Yes |
| `owner` | `string` | — | Yes |
| `tags` | `map(string)` | `{}` | No |

### Examples

Fourteen ready-to-run configurations in `examples/`:

| Directory | Creates |
|---|---|
| `examples/vpc-only/` | Single VPC |
| `examples/compute-basic/` | VPC + 1 instance |
| `examples/dns-with-compute/` | VPC + instance + DNS zone + A record |
| `examples/load-balanced-web/` | VPC + 2 instances + L7 ALB |
| `examples/network-lb/` | VPC + 2 instances + internal L4 NLB |
| `examples/full-stack/` | VPC + 3 instances + ALB |
| `examples/nfs-shared-storage/` | VPC + NFS server + 2 instances with shared mount |
| `examples/openstack-services/` | VPC + 6 named instances + admin/NFS + frontend ALB + backend NLB |
| `examples/ha-frontend-lb/` | VPC + HA frontend instance-group + 2 ProxySQL/NGINX/Keepalived nodes sharing a floating VIP + MySQL Group Replication + RabbitMQ + Keystone + backend tier + shared NFS storage + centralized logging (Loki/Grafana) |
| `examples/ghidra-workstation/` | VPC + security group + XFCE desktop with Ghidra, browser-accessible via noVNC through a network LB |
| `examples/kiwix-library/` | VPC + security group + instance serving an offline Kiwix content library over HTTP through a load balancer |
| `examples/wifi-sniffer/` | VPC + security group + instance running Kismet + aircrack-ng, driven by a passed-through USB WiFi adapter, through a network LB |
| `examples/distributed-llm/` | VPC + coordinator instance + one RPC worker per peer, splitting a 7B GGUF model across hosts via llama.cpp's RPC backend — built for the [Scheduler's own 7B LLM ingestion job](#scheduler--7b-llm-ingestion-optional), not usually built by hand |
| `examples/llm-chat/` | Same distributed coordinator + RPC worker(s) as above, but for an interactive human chat session — open the `chat_url` output in a real browser |

All examples accept a `suffix` variable to keep resource names unique across runs:

```bash
cd examples/vpc-only
tofu init
tofu apply -var="suffix=abc123"
```

Or run them end-to-end from the UI via **Builds → OpenTofu**.

### Provider Resources

| Resource | Data Source | API path |
|---|---|---|
| `cloudcore_vpc` | `cloudcore_vpc` | `/v1/vpcs` |
| `cloudcore_internet_gateway` | — | `/v1/internet-gateways` |
| `cloudcore_route_table` | — | `/v1/route-tables` |
| `cloudcore_instance` | `cloudcore_instance` | `/v1/instances` |
| `cloudcore_load_balancer` | `cloudcore_load_balancer` | `/v1/load-balancers` |
| `cloudcore_security_group` | `cloudcore_security_group` | `/v1/security-groups` |
| `cloudcore_nfs_server` | `cloudcore_nfs_server` | `/v1/nfs-servers` |
| `cloudcore_dns_zone` | `cloudcore_dns_zone` | `/v1/dns/zones` |
| `cloudcore_dns_record` | `cloudcore_dns_record` | `/v1/dns/zones/{zone}/records` |
| `cloudcore_lb_target_group` | — | `/v1/load-balancers/{id}/target-groups` |
| `cloudcore_lb_listener` | — | `/v1/load-balancers/{id}/listeners` |

Dashboard-only, no Terraform resource behind them — moving files onto an
NFS share's export directory (e.g. a large tarball ahead of an app
install) via the same SSH channel the platform already uses to manage
`cloudcore_nfs_server` itself, not a new listener on the NFS server VM:

| Purpose | API path |
|---|---|
| List a share's files | `GET /v1/nfs-servers/{id}/shares/{name}/files` |
| Upload a file (raw body, `Content-Type: application/octet-stream`) | `PUT /v1/nfs-servers/{id}/shares/{name}/files/{filename}` |
| Delete a file | `DELETE /v1/nfs-servers/{id}/shares/{name}/files/{filename}` |

The Dashboard's **NFS Servers** page exposes this as a drag-and-drop
upload zone under each share's "Files" panel. `filename` is restricted
to a single path segment (letters, digits, `.`, `_`, `-`) — no
subdirectories, no traversal.

## Ansible Collection

The collection is installed automatically by `scripts/install.sh`. To rebuild manually:

```bash
cd ansible/collections/cloudcore
ansible-galaxy collection build
ansible-galaxy collection install cloudcore-cloudcore-*.tar.gz --force
```

### Modules

| Module | API path |
|---|---|
| `cloudcore.cloudcore.vpc` | `/v1/vpcs` |
| `cloudcore.cloudcore.instance` | `/v1/instances` |
| `cloudcore.cloudcore.load_balancer` | `/v1/load-balancers` |
| `cloudcore.cloudcore.dns_zone` | `/v1/dns/zones` |
| `cloudcore.cloudcore.dns_record` | `/v1/dns/zones/{zone}/records` |
| `cloudcore.cloudcore.nfs_server` | `/v1/nfs-servers` |
| `cloudcore.cloudcore.nfs_mount` | `/v1/nfs-servers/{id}/shares/{name}/mount-config` |
| `cloudcore.cloudcore.security_group` | `/v1/security-groups` |
| `cloudcore.cloudcore.lb_target_group` | `/v1/load-balancers/{id}/target-groups` |
| `cloudcore.cloudcore.lb_listener` | `/v1/load-balancers/{id}/listeners` |
| `cloudcore.cloudcore.usb_device_info` | `/v1/usb-devices` (read-only) |

### Examples

Fourteen ready-to-run playbooks in `ansible/examples/` (plus `07-teardown.yml` and `12-teardown.yml`, which tear down everything their respective numbered playbooks create):

| Playbook | Creates |
|---|---|
| `01-vpc-only.yml` | Single VPC |
| `02-compute-basic.yml` | VPC + 1 instance |
| `03-compute-with-dns.yml` | VPC + instance + DNS zone + A record |
| `04-load-balanced-web.yml` | VPC + 2 instances + L7 ALB |
| `05-network-lb.yml` | VPC + 2 instances + internal L4 NLB |
| `06-full-stack.yml` | VPC + 3 instances + ALB + DNS zone + CNAME |
| `07-nfs-shared-storage.yml` | VPC + NFS server + 2 instances with shared mount |
| `08-openstack-services.yml` | VPC + 6 named instances + admin/NFS + frontend ALB + backend NLB |
| `09-ghidra-workstation.yml` | VPC + security group + XFCE desktop with Ghidra, browser-accessible via noVNC through a network LB |
| `10-kiwix-library.yml` | VPC + security group + instance serving an offline Kiwix content library over HTTP through a load balancer |
| `11-wifi-sniffer.yml` | VPC + security group + instance running Kismet + aircrack-ng, driven by a passed-through USB WiFi adapter, through a network LB |
| `12-ha-frontend-lb.yml` | VPC + security groups + HA frontend instance group + 2 ProxySQL/NGINX/Keepalived nodes sharing a floating VIP + MySQL Group Replication + RabbitMQ + Keystone + backend application tier + shared NFS storage + centralized logging (Loki/Grafana) + DNS records |
| `13-distributed-llm.yml` | VPC + coordinator instance + one RPC worker per peer, splitting a 7B GGUF model across hosts via llama.cpp's RPC backend — built for the [Scheduler's own 7B LLM ingestion job](#scheduler--7b-llm-ingestion-optional), not usually built by hand |
| `14-llm-chat.yml` | Same distributed coordinator + RPC worker(s) as above, but for an interactive human chat session — open the printed chat URL in a real browser |

Run directly:

```bash
cd ansible/examples
ansible-playbook -i ../inventory.ini 01-vpc-only.yml
```

Or run end-to-end from the UI via **Builds → Ansible**.

## Resource Coverage

| Resource | OpenTofu provider | Ansible module | API path |
|---|---|---|---|
| VPC | `cloudcore_vpc` | `cloudcore.cloudcore.vpc` | `/v1/vpcs` |
| Internet Gateway | `cloudcore_internet_gateway` | — | `/v1/internet-gateways` |
| Route Table | `cloudcore_route_table` | — | `/v1/route-tables` |
| Instance | `cloudcore_instance` | `cloudcore.cloudcore.instance` | `/v1/instances` |
| Load Balancer | `cloudcore_load_balancer` | `cloudcore.cloudcore.load_balancer` | `/v1/load-balancers` |
| Security Group | `cloudcore_security_group` | `cloudcore.cloudcore.security_group` | `/v1/security-groups` |
| DNS Zone | `cloudcore_dns_zone` | `cloudcore.cloudcore.dns_zone` | `/v1/dns/zones` |
| DNS Record | `cloudcore_dns_record` | `cloudcore.cloudcore.dns_record` | `/v1/dns/zones/{zone}/records` |
| NFS Server | `cloudcore_nfs_server` | `cloudcore.cloudcore.nfs_server` | `/v1/nfs-servers` |
| NFS Mount | — | `cloudcore.cloudcore.nfs_mount` | `/v1/nfs-servers/{id}/shares/{name}/mount-config` |
| LB Target Group | `cloudcore_lb_target_group` | `cloudcore.cloudcore.lb_target_group` | `/v1/load-balancers/{id}/target-groups` |
| LB Listener | `cloudcore_lb_listener` | `cloudcore.cloudcore.lb_listener` | `/v1/load-balancers/{id}/listeners` |
| USB Device (read-only) | — | `cloudcore.cloudcore.usb_device_info` | `/v1/usb-devices` |

## Tests

```bash
# API tests only (fast, no VMs)
python3 tests/run_tests.py --skip-vm --skip-scenarios

# Include integration scenarios
python3 tests/run_tests.py --skip-vm

# Full suite including KVM instance tests (slow)
python3 tests/run_tests.py
```

The API must be running before any test run (`systemctl --user start cloudcore-api`).
