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

1. Install system packages (`qemu-kvm`, `libvirt`, `haproxy`, `dnsmasq`, `lvm2`, `nfs-common`)
2. Install Python dependencies (`pip3 install --user -r requirements.txt`)
3. Build and install the Ansible collection
4. Download the Ubuntu 22.04 cloud image into `api/images/`
5. Generate the CloudCore SSH keypair in `api/keys/`
6. Install and start the `ccbr0` bridge network (system service, requires sudo)
7. Install and start the API and terminal as systemd user services

After the script completes, open **http://127.0.0.1:8080** in your browser.

> **Note:** If this is the first time your user has been added to the `libvirt` group,
> log out and back in (or run `newgrp libvirt`) before creating VM instances.

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

---

## Artefacts

| Artefact | Path | Purpose |
|---|---|---|
| REST API + UI | `api/` | Python/Flask — core platform |
| OpenTofu provider | `provider/` | Go — Terraform Plugin Framework |
| OpenTofu modules | `modules/` | HCL — composable module library |
| OpenTofu examples | `examples/` | Ready-to-run configurations |
| Ansible collection | `ansible/collections/cloudcore/` | Python — FQCN `cloudcore.cloudcore` |
| Ansible examples | `ansible/examples/` | Ready-to-run playbooks (01–07) |

## Requirements

| Tool | Version | Required |
|---|---|---|
| Ubuntu | `22.04+` | Yes |
| Python | `>= 3.10` | Yes |
| Ansible | `>= 2.15` | Yes (installed by `scripts/install.sh`) |
| OpenTofu | `>= 1.8.0` | Optional — needed to run OpenTofu builds |
| Go | `>= 1.22` | Optional — only needed to rebuild the provider binary |

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

Three reusable modules live in `modules/`:

| Module | Path | Resources |
|---|---|---|
| VPC | `modules/vpc/` | `cloudcore_vpc` |
| Compute | `modules/compute/` | `cloudcore_instance` |
| Load Balancer | `modules/load-balancer/` | `cloudcore_load_balancer` |

All modules follow the standard argument contract:

| Variable | Type | Default | Required |
|---|---|---|---|
| `enabled` | `bool` | `true` | No |
| `environment` | `string` | — | Yes |
| `project` | `string` | — | Yes |
| `owner` | `string` | — | Yes |
| `tags` | `map(string)` | `{}` | No |

### Examples

Five ready-to-run configurations in `examples/`:

| Directory | Creates |
|---|---|
| `examples/vpc-only/` | Single VPC |
| `examples/compute-basic/` | VPC + 1 instance |
| `examples/load-balanced-web/` | VPC + 2 instances + L7 ALB |
| `examples/network-lb/` | VPC + 2 instances + internal L4 NLB |
| `examples/full-stack/` | VPC + 3 instances + ALB |

All examples accept a `suffix` variable to keep resource names unique across runs:

```bash
cd examples/vpc-only
tofu init
tofu apply -var="suffix=abc123"
```

Or run them end-to-end from the UI via **Builds → OpenTofu**.

> **Note:** DNS and NFS resources are not yet implemented in the provider. Use the Ansible collection for those resource types.

### Provider Resources

| Resource | Data Source | API path |
|---|---|---|
| `cloudcore_vpc` | `cloudcore_vpc` | `/v1/vpcs` |
| `cloudcore_instance` | `cloudcore_instance` | `/v1/instances` |
| `cloudcore_load_balancer` | `cloudcore_load_balancer` | `/v1/load-balancers` |

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

### Examples

Seven ready-to-run playbooks in `ansible/examples/`:

| Playbook | Creates |
|---|---|
| `01-vpc-only.yml` | Single VPC |
| `02-compute-basic.yml` | VPC + 1 instance |
| `03-compute-with-dns.yml` | VPC + instance + DNS zone + A record |
| `04-load-balanced-web.yml` | VPC + 2 instances + L7 ALB |
| `05-network-lb.yml` | VPC + 2 instances + internal L4 NLB |
| `06-full-stack.yml` | VPC + 3 instances + ALB + DNS zone + CNAME |
| `07-nfs-shared-storage.yml` | VPC + NFS server + 2 instances with shared mount |

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
| Instance | `cloudcore_instance` | `cloudcore.cloudcore.instance` | `/v1/instances` |
| Load Balancer | `cloudcore_load_balancer` | `cloudcore.cloudcore.load_balancer` | `/v1/load-balancers` |
| DNS Zone | — | `cloudcore.cloudcore.dns_zone` | `/v1/dns/zones` |
| DNS Record | — | `cloudcore.cloudcore.dns_record` | `/v1/dns/zones/{zone}/records` |
| NFS Server | — | `cloudcore.cloudcore.nfs_server` | `/v1/nfs-servers` |
| NFS Mount | — | `cloudcore.cloudcore.nfs_mount` | `/v1/nfs-servers/{id}/shares/{name}/mount-config` |

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
