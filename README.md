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

### Optional: Sentinel (log-intelligence advisor)

[Sentinel](https://github.com/psc108/sentinel) is a standalone
companion tool, in its own repo — it watches a running lab's
centralized-logging tier (`examples/ha-frontend-lb`'s Loki/Grafana
tier, `haFullStack-LLD.md` §12), flags log activity that looks like
real trouble, and matches it against a knowledge base seeded from
`haFullStack-Findings-Log.md` — surfacing suggestions through its own
web UI. It has no source dependency on CloudCore (only the API and
Loki's own HTTP endpoints), so it's entirely optional and safe to skip
— nothing here requires it.

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
| Ansible examples | `ansible/examples/` | Ready-to-run playbooks (01–08) |

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

Twelve ready-to-run configurations in `examples/`:

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

Twelve ready-to-run playbooks in `ansible/examples/` (plus `07-teardown.yml` and `12-teardown.yml`, which tear down everything their respective numbered playbooks create):

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
