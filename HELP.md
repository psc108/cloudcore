# CloudCore Console — Help

## Navigation

The console header contains dropdown menus and standalone buttons. The coloured dot in the top-right shows API connectivity (green = connected, red = unreachable).

| Button / Menu | Contents |
|---|---|
| **Dashboard** | Live resource overview |
| **Infrastructure ▼** | VPCs, Instances, Load Balancers, Terminal |
| **Networking ▼** | DNS |
| **Builds ▼** | Ansible Build Manager, OpenTofu Build Manager |
| **Editor** | File editor for Ansible playbooks and OpenTofu configs |
| **About** | Component and dependency versions |
| **Help** | This document |

Each section auto-loads its data when selected.

---

## Dashboard

The default landing page. Shows all deployed resources at a glance and auto-refreshes every 60 seconds.

### Stat Tiles
Four tiles show counts for VPCs, Instances, Load Balancers, and DNS zones. Click any tile to navigate to that section.

### Resource Panels
Four panels show live rows for each resource type. Click any row to open a **detail modal** with the full field set.

### Detail Modal
Shows all key fields for the selected resource. Click **Open in full view →** to navigate to the full section for that resource type.

### Refresh
Click **↻ Refresh** to reload immediately. The last refresh time is shown below the heading.

---

## VPCs

Virtual Private Clouds are the network containers for instances and load balancers.

### Create a VPC
1. Click **+ Create VPC**.
2. Fill in **Name** (required), **CIDR Block** (default `10.0.0.0/16`), and **DNS Support**.
3. Optionally add **Tags** as comma-separated `key=value` pairs (e.g. `env=dev, team=platform`).
4. Click **Create VPC**.

### Delete a VPC
Click **Delete** on the row. Instances and load balancers referencing the VPC are not automatically removed.

### Bulk Delete
Tick one or more checkboxes and click **Delete Selected**. Deletions run in parallel.

### Columns
| Column | Description |
|---|---|
| Name | Human-readable label |
| ID | Truncated UUID |
| CIDR | IP address range |
| DNS | Whether DNS resolution is enabled |
| Status | `active` or `deleted` |
| Created | Local date/time of creation |

---

## Instances

Compute instances backed by KVM/libvirt. Each instance gets a cloud-init ISO with the CloudCore shared keypair pre-installed.

### Launch an Instance
1. Click **+ Launch Instance**.
2. Select an **Image**, **Flavor**, **VPC**, and **Subnet ID**.
3. Optionally paste **User Data** (cloud-init YAML).
4. Click **Launch Instance**. Status starts as `pending` and polls to `running` every 5 seconds.

### Terminate an Instance
Click **Terminate** on the row. The VM is destroyed and storage is released.

### Bulk Delete
Tick one or more checkboxes and click **Delete Selected**.

### SSH Access
Click **SSH ▾** on any running instance row to expand the SSH panel:

| Command | Purpose |
|---|---|
| SSH into instance | Open an interactive shell |
| SCP — upload file | Copy a local file to the instance |
| SCP — download file | Copy a file from the instance locally |
| SSH between instances | Passwordless inter-instance SSH via the shared CloudCore keypair |

Click **⎘** to copy any command to the clipboard.

### User Management
Click **N users ▾** to expand the Users panel.

- **Add a user**: enter a username, choose sudo (NOPASSWD), and optionally paste an SSH public key. On a `running` instance the user is created immediately via SSH.
- **Remove a user**: click **Remove** on the user row.

### Flavors
| Flavor | vCPU | RAM |
|---|---|---|
| `standard.nano` | 1 | 512 MB |
| `standard.small` | 1 | 1 GB |
| `standard.medium` | 2 | 2 GB |
| `standard.large` | 4 | 4 GB |

### Available Images
Images must be present on disk before they appear in the dropdown. Run `bash api/fetch-image.sh` to download the default Ubuntu 22.04 image.

---

## Load Balancers

HAProxy-backed load balancers. Each LB gets a dedicated HAProxy process and a host port in the range 8200–8299.

### Create a Load Balancer
1. Click **+ Create Load Balancer**.
2. Enter a **Name**, choose **Type** (Application L7 or Network L4), select a **VPC**, and set the **Scheme**.
3. Click **Create Load Balancer**. HAProxy starts automatically.

### Delete a Load Balancer
Click **Delete** and confirm. The HAProxy process is stopped and the port is released.

### Bulk Delete
Tick one or more checkboxes and click **Delete Selected**.

### Backends
Click **N backends ▾** to expand the backends panel.

- **Add a backend**: enter **Name**, **Address** (IP or hostname), and **Port**. HAProxy config reloads immediately.
- **Remove a backend**: click **Remove**. HAProxy config reloads.

### Columns
| Column | Description |
|---|---|
| Name | LB label |
| Type | `application` (L7) or `network` (L4) |
| Listen Port | Host port HAProxy listens on |
| Backends | Count with expand button |
| Scheme | Internet-facing or Internal |
| Status | `active` or `deleted` |

---

## Terminal

Browser-based SSH terminal powered by xterm.js and a WebSocket proxy (port 8081).

### Open a Terminal
1. Go to **Infrastructure → Terminal**. Running instances are listed as cards.
2. Click **Open Terminal** on any running instance.
3. The session connects as the first non-sudo user. If none exists, add one via the Users panel first.

### Terminal Window
- **Drag**: click and drag the title bar to reposition.
- **Resize**: drag the triangle handle in the bottom-right corner.
- **Close**: click the red **✕** button.
- Multiple terminals can be open simultaneously.

### Prerequisites
- Terminal service must be running: `systemctl --user status cloudcore-terminal`
- Instance must be `running` with an SSH port assigned
- CloudCore keypair must be present at `api/keys/cloudcore_ed25519`

---

## DNS

Manage DNS zones and records. Two built-in zones are always present:

| Zone | Purpose |
|---|---|
| `instances.cloudcore.local` | Auto-populated A records when instances reach `running` |
| `lb.cloudcore.local` | Auto-populated A records when load balancers are created |

### Create a Zone
Go to **Networking → DNS**, click **+ Add Zone**, enter a zone name, and click **Create Zone**.

### Records
Click **Records ▾** on any zone card to expand. Fill in **Name**, **Type** (A, CNAME, or TXT), **Value**, and **TTL**, then click **Add**.

Records marked **auto** are system-managed and cannot be manually deleted.

---

## NFS Servers

NFS servers are KVM VMs with an LVM data disk, scoped to a VPC. Instances in the same VPC can mount the shared exports.

### Create an NFS Server
1. Go to **Infrastructure → NFS Servers** (or via the API).
2. Provide a **Name**, **VPC**, **Flavor**, and **Disk size (GB)**.
3. Optionally define **Shares** at creation time.
4. The server provisions asynchronously — status moves from `pending` to `running`.

### Shares
Each share is a named export. The `clients` field controls access:

| Value | Access |
|---|---|
| `vpc` | Entire VPC CIDR (e.g. `10.10.0.0/16`) |
| `["ip1","ip2"]` | Specific IP addresses only |

### Mount Config
Call `GET /v1/nfs-servers/{id}/shares/{name}/mount-config` to get the exact `mount` command and `/etc/fstab` entry for any share.

### Networking
NFS servers require bridge networking (`ccbr0`). SLIRP instances cannot reach NFS servers.

---

## Builds — Ansible

The Ansible Build Manager provisions infrastructure end-to-end by running Ansible playbooks against the CloudCore API. Go to **Builds → Ansible**.

### Select a Template
Click any template card. The **vars panel** opens below with all configurable variables.

### Variables
| Variable | Purpose |
|---|---|
| `default_project` | Project label baked into resource names |
| `default_environment` | Environment label (e.g. `dev`, `prod`) |
| `build_suffix` | 6-digit random suffix — makes every build's resource names unique |
| `cloudcore_api_url` | API endpoint the playbook talks to |
| `cloudcore_api_token` | Bearer token for API auth |

Resource names follow the pattern `{project}-{environment}-{build_suffix}-{role}`.

### Run a Build
Click **Run Build**. A log panel streams live output from `ansible-playbook`.

### Available Templates
| Template | Creates |
|---|---|
| VPC Only | Single VPC |
| Basic Compute | VPC + 1 instance |
| Compute with DNS | VPC + instance + DNS zone + A record |
| Load-Balanced Web (L7 ALB) | VPC + 2 instances + application load balancer |
| Network Load Balancer (L4) | VPC + 2 instances + internal network load balancer |
| Full Stack | VPC + 3 instances + ALB + DNS zone + CNAME |
| NFS Shared Storage | VPC + NFS server + 2 instances with shared mount |

### Build History and Destroy
See [Build History and Destroy](#build-history-and-destroy) below — the behaviour is identical for both Ansible and OpenTofu builds.

---

## Builds — OpenTofu

The OpenTofu Build Manager runs `tofu init` then `tofu apply -auto-approve` against the example configurations in `examples/`. Go to **Builds → OpenTofu**.

### Select a Template
Click any template card. The **vars panel** opens with configurable variables extracted from the example's `main.tf`.

### Variables
| Variable | Purpose |
|---|---|
| `project` | Project label baked into resource names |
| `environment` | Environment label |
| `owner` | Owner tag value |
| `suffix` | Optional suffix appended to map keys to keep resource names unique across runs |
| `cloudcore_api_url` | API endpoint |
| `cloudcore_api_token` | Bearer token |

### Run a Build
Click **Run Apply**. A log panel streams live output from `tofu init` and `tofu apply`.

### Available Templates
| Template | Creates |
|---|---|
| VPC Only | Single VPC |
| Basic Compute | VPC + 1 instance |
| Load-Balanced Web (L7 ALB) | VPC + 2 instances + application load balancer |
| Network Load Balancer (L4) | VPC + 2 instances + internal network load balancer |
| Full Stack | VPC + 3 instances + ALB |

> **Note:** DNS and NFS templates are not yet available for OpenTofu — those provider resources are not yet implemented.

### Prerequisites
OpenTofu must be installed and `tofu` must be on `PATH`. Install from [opentofu.org](https://opentofu.org/docs/intro/install/).

### Build History and Destroy
See [Build History and Destroy](#build-history-and-destroy) below.

---

## Build History and Destroy

Both the Ansible and OpenTofu build managers share the same history and destroy behaviour.

### History Table
| Column | Description |
|---|---|
| ID | Truncated build UUID |
| Template | Template name |
| Status | `pending`, `running`, `success`, `failed`, or `destroyed` |
| Resources | Count of resources provisioned, or `destroyed` if already torn down |
| Started | Local date/time the build started |
| Duration | Wall-clock time from start to finish |
| Log | Opens the full build log |

### View a Log
Click **Log** on any row. If the build is still running, the log streams live.

### Destroy Resources
Builds with a green **Resources** badge can be destroyed:

1. Tick the checkbox on one or more rows.
2. Click **🗑 Destroy Resources** and confirm.

All resources provisioned by the selected builds are deleted via the API. The build status changes to `destroyed`. This cannot be undone.

---

## Editor

A browser-based file editor for Ansible playbooks and OpenTofu configurations. Go to **Editor**.

### Root Tabs
Two tabs at the top switch between file trees:

| Tab | Path |
|---|---|
| **Ansible** | `ansible/examples/` — playbooks 01–07 |
| **OpenTofu** | `examples/` — OpenTofu example directories |

### Open a File
Click any filename in the left-hand tree. The file loads into the editor with syntax highlighting (YAML for `.yml`, HCL-style for `.tf`).

### Edit and Save
- Edit directly in the editor pane.
- A **●** indicator appears in the header when there are unsaved changes.
- Click **Save** or press **Ctrl+S** / **Cmd+S** to save.
- Navigating away without saving silently discards changes.

### Create a New File
1. Click **+ New File**.
2. Enter a filename (e.g. `08-my-playbook.yml` or `main.tf`).
3. Click **Create**. The file is created at the root of the active tree and opened immediately.

### Delete a File
1. With a file open, click **Delete**.
2. The button turns red and shows **Confirm delete?** — click again within 4 seconds to confirm.

### Keyboard Shortcuts
| Shortcut | Action |
|---|---|
| Ctrl+S / Cmd+S | Save current file |
| Ctrl+/ | Toggle line comment |
| Ctrl+D | Select next occurrence |

---

## About

Go to **About** to see live version information for every software component:

| Section | Contents |
|---|---|
| **CloudCore** | API version, Ansible collection version |
| **Python Runtime** | Python, Flask, libvirt-python, Paramiko, websockets, PyYAML |
| **System Tools** | Ansible, QEMU/qemu-img, HAProxy, dnsmasq |

Click **↻** to refresh the versions live from the running system.

---

## API Reference

The REST API runs on `http://127.0.0.1:8080`. All requests (except `/help` and `/v1/about`) require:

```
Authorization: Bearer dev-token
```

### Core Resources
| Method | Path | Description |
|---|---|---|
| GET | `/v1/dashboard` | Aggregated live resource summary |
| GET | `/v1/vpcs` | List VPCs |
| POST | `/v1/vpcs` | Create VPC |
| GET | `/v1/vpcs/{id}` | Get VPC |
| PUT | `/v1/vpcs/{id}` | Update VPC |
| DELETE | `/v1/vpcs/{id}` | Delete VPC |
| GET | `/v1/instances` | List instances |
| POST | `/v1/instances` | Launch instance (async, returns 202) |
| GET | `/v1/instances/{id}` | Get instance |
| PUT | `/v1/instances/{id}` | Update instance |
| DELETE | `/v1/instances/{id}` | Terminate instance |
| POST | `/v1/instances/{id}/users` | Add user |
| DELETE | `/v1/instances/{id}/users/{username}` | Remove user |
| GET | `/v1/load-balancers` | List load balancers |
| POST | `/v1/load-balancers` | Create load balancer |
| GET | `/v1/load-balancers/{id}` | Get load balancer |
| PUT | `/v1/load-balancers/{id}` | Update load balancer |
| DELETE | `/v1/load-balancers/{id}` | Delete load balancer |
| POST | `/v1/load-balancers/{id}/backends` | Add backend |
| DELETE | `/v1/load-balancers/{id}/backends/{name}` | Remove backend |

### DNS
| Method | Path | Description |
|---|---|---|
| GET | `/v1/dns/zones` | List zones |
| POST | `/v1/dns/zones` | Create zone |
| DELETE | `/v1/dns/zones/{name}` | Delete zone |
| GET | `/v1/dns/zones/{name}/records` | List records |
| POST | `/v1/dns/zones/{name}/records` | Add record |
| DELETE | `/v1/dns/zones/{name}/records/{name}/{type}` | Delete record |

### NFS
| Method | Path | Description |
|---|---|---|
| GET | `/v1/nfs-servers` | List NFS servers |
| POST | `/v1/nfs-servers` | Create NFS server (async, returns 202) |
| GET | `/v1/nfs-servers/{id}` | Get NFS server |
| DELETE | `/v1/nfs-servers/{id}` | Delete NFS server |
| POST | `/v1/nfs-servers/{id}/shares` | Add share |
| DELETE | `/v1/nfs-servers/{id}/shares/{name}` | Remove share |
| GET | `/v1/nfs-servers/{id}/shares/{name}/mount-config` | Get mount command and fstab entry |

### Ansible Builds
| Method | Path | Description |
|---|---|---|
| GET | `/v1/builds/templates` | List Ansible templates |
| GET | `/v1/builds/templates/{filename}/vars` | Get variable schema |
| POST | `/v1/builds` | Submit a build |
| GET | `/v1/builds` | List all builds |
| GET | `/v1/builds/{id}` | Get build detail |
| GET | `/v1/builds/{id}/log` | Get log (SSE stream if `Accept: text/event-stream`) |
| DELETE | `/v1/builds/{id}` | Destroy provisioned resources |

### OpenTofu Builds
| Method | Path | Description |
|---|---|---|
| GET | `/v1/tofu/templates` | List OpenTofu templates |
| GET | `/v1/tofu/templates/{dir}/vars` | Get variable schema |
| POST | `/v1/tofu/builds` | Submit a build |
| GET | `/v1/tofu/builds` | List all builds |
| GET | `/v1/tofu/builds/{id}` | Get build detail |
| GET | `/v1/tofu/builds/{id}/log` | Get log (SSE stream if `Accept: text/event-stream`) |
| DELETE | `/v1/tofu/builds/{id}` | Destroy provisioned resources |

### Editor
| Method | Path | Description |
|---|---|---|
| GET | `/v1/editor/roots` | List named roots (`ansible`, `opentofu`) |
| GET | `/v1/editor/tree?root={name}` | Get file tree for a root |
| GET | `/v1/editor/file?root={name}&path={rel}` | Read a file |
| PUT | `/v1/editor/file?root={name}&path={rel}` | Write a file |
| POST | `/v1/editor/file?root={name}` | Create a new file at root |
| DELETE | `/v1/editor/file?root={name}&path={rel}` | Delete a file |

### Misc
| Method | Path | Description |
|---|---|---|
| GET | `/v1/images` | List available images |
| GET | `/v1/ssh-key` | Get CloudCore public key |
| GET | `/v1/about` | Component version information |
| GET | `/help` | This help document (Markdown) |

---

## Networking

Instances prefer bridge networking via `ccbr0` (`192.168.100.1/24`) when available. The bridge provides real DHCP IPs and is required for inter-instance communication and NFS. SSH is forwarded to host ports starting at 12200.

If the bridge is absent, instances fall back to SLIRP networking (no root required). The private IP inside every SLIRP instance is `10.0.2.15`. NFS servers are not reachable from SLIRP instances.

To set up or repair the bridge: `sudo bash api/setup-network.sh`  
To check bridge status: `ip link show ccbr0`

---

## Service Management

```bash
# Status
systemctl --user status cloudcore-api cloudcore-terminal

# Logs
journalctl --user -u cloudcore-api -f
journalctl --user -u cloudcore-terminal -f

# Restart
systemctl --user restart cloudcore-api
systemctl --user restart cloudcore-terminal
```

To change the API token:
```bash
systemctl --user edit cloudcore-api.service
# Add:
# [Service]
# Environment=CLOUDCORE_API_TOKEN=your-token
systemctl --user restart cloudcore-api.service
```
