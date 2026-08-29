# CloudCore — Session Continuity Document

_Last updated: 2026-08-25. Update this file at the end of every session._

---

## System

- **OS**: Ubuntu Linux, user `scottp`
- **Host**: `127.0.0.1`
- **Python**: 3.12
- **Go**: 1.22
- **Node**: 20
- **KVM backend**: `qemu:///session` (no root required)
- **Workspace**: `/home/scottp/IdeaProjects/CloudProject/`

---

## Running Services

| Service | Port | Systemd unit | Auto-start |
|---|---|---|---|
| Flask API | 8080 | `cloudcore-api.service` | enabled ✓ |
| Terminal WS | 8081 | `cloudcore-terminal.service` | enabled ✓ |

Start API manually until Stage 1 is done:
```bash
cd ~/IdeaProjects/CloudProject/api && python3 server.py &
```

---

## Project File Map

| Path | Purpose |
|---|---|
| `ui/index.html` | Single-page console. Dark theme. Tabs: VPCs, Instances, Load Balancers, Terminal, DNS (planned). One `<script>` block — always syntax-check with `node --check` after JS edits. |
| `api/server.py` | Flask app port 8080. All REST endpoints. Auth: `Bearer dev-token`. |
| `api/compute.py` | libvirt/KVM backend. IMAGE_CATALOGUE, cloud-init ISO generation, SLIRP vs bridge selection. |
| `api/models.py` | Dataclasses: VPC, Instance, LoadBalancer. |
| `api/store.py` | Thread-safe in-memory store + JSON persistence to `api/state.json`. |
| `api/lb.py` | HAProxy manager. Ports 8200–8299. Config/pid/sock in `api/lb/`. |
| `api/terminal.py` | WebSocket SSH proxy. paramiko PTY. websockets v17 legacy API. |
| `api/dns.py` | DNS zone + record store. Persists to `api/dns.json`. Built-in zones: `instances.cloudcore.local`, `lb.cloudcore.local`. |
| `api/state.json` | Persisted VPCs, instances, LBs. |
| `api/dns.json` | DNS persistence. Auto-created on first startup. |
| `api/keys/cloudcore_ed25519` | Shared ed25519 keypair injected into every instance via cloud-init. |
| `api/setup-network.sh` | Creates `ccbr0` bridge. Requires `sudo`. |
| `api/teardown-network.sh` | Removes `ccbr0` bridge. Requires `sudo`. |
| `HELP.md` | Help documentation. Served at `GET /help`. Edit freely — no restart needed. |
| `.changelog` | Append-only change log. Format: `[YYYY-MM-DD HH:MM] ACTION path | description` |
| `.amazonq/rules/conventions.md` | Amazon Q rules — coding conventions, tagging model, file layout. Always active. |

---

## Networking — Current State

Instances currently use **SLIRP** (`qemu:///session`). This means:
- `private_ip` inside every VM is always `10.0.2.15`
- SSH is forwarded to host ports starting at **12200** (`127.0.0.1:<port>`)
- Instances cannot reach each other directly by IP

The bridge (`ccbr0`) code exists in `compute.py` — it detects the bridge at launch and uses it if present. When active:
- Subnet: `192.168.100.0/24`
- Gateway: `192.168.100.1`
- DHCP range: `192.168.100.10 – 192.168.100.254`
- Instances get real routable IPs and can reach each other

---

## Bridge Networking Setup

The bridge requires `sudo` and needs to be re-created after every reboot (no persistence yet — that is addressed in Stage 2 below).

### One-time setup
```bash
sudo bash ~/IdeaProjects/CloudProject/api/setup-network.sh
```

### Verify it's up
```bash
ip addr show ccbr0
# Should show: inet 192.168.100.1/24
```

### Make it persist across reboots (do this after logout/in)

The bridge is a plain `ip link` creation — it doesn't survive reboot. The cleanest approach without NetworkManager conflicts is a systemd system service. Create `/etc/systemd/system/cloudcore-bridge.service`:

```ini
[Unit]
Description=CloudCore ccbr0 bridge
After=network.target
Before=libvirtd.service

[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart=/bin/bash /home/scottp/IdeaProjects/CloudProject/api/setup-network.sh
ExecStop=/bin/bash /home/scottp/IdeaProjects/CloudProject/api/teardown-network.sh

[Install]
WantedBy=multi-user.target
```

Then:
```bash
sudo systemctl daemon-reload
sudo systemctl enable --now cloudcore-bridge
```

### Why logout/in is needed
After `sudo setup-network.sh` the bridge exists but `qemu:///session` may not have picked up the new interface. A fresh login session ensures libvirt's session daemon sees `ccbr0`. Also check group membership — `libvirt` and `kvm` groups are not currently in `scottp`'s groups. If libvirt was installed after initial login, a re-login is required for group membership to take effect:
```bash
groups  # should include kvm and libvirt-dnsmasq after re-login
```

---

## Staged Improvement Plan

### Stage 1 — Enable API auto-start ✅ DONE

One command:
```bash
systemctl --user enable --now cloudcore-api
```

The service file at `~/.config/systemd/user/cloudcore-api.service` is already correct. After this the API starts automatically on login (user session) and restarts on failure.

**Do this immediately after re-login.**

---

### Stage 2 — Startup reconciliation ✅ DONE

**Problem**: After reboot, `state.json` has instances marked `running` and LBs marked `active`, but the KVM VMs and HAProxy processes are gone.

**Solution**: Add a `reconcile()` function to `server.py`, called once at startup before `app.run()`.

What it does:
- Walks all non-deleted instances in the store
- For each, calls `compute.get_instance_status(domain_name)` via libvirt
  - Domain exists + running → keep `running`, re-fetch `private_ip` if empty
  - Domain exists + stopped → set status `stopped`
  - Domain not found → set status `stopped` (VM was lost)
- Walks all non-deleted LBs
- For each, calls `lb_backend.start(lb)` to restart HAProxy and re-assign listen port
  - HAProxy stores config in `api/lb/` — config files survive reboot, so restart is clean
- Saves updated state

No new files. Changes only to `server.py`.

Also: make the bridge persistent using the systemd unit above so that reconciled instances with bridge networking come back on the correct interface.

---

### Stage 3 — DNS ✅ DONE

**New file**: `api/dns.py`
**New file**: `api/dns.json` (auto-created)

#### Data model

```
Zone:
  name: str          # e.g. "instances.cloudcore.local"
  created_at: str

Record:
  name: str          # e.g. "my-instance"  → FQDN: my-instance.instances.cloudcore.local
  type: str          # A | CNAME | TXT
  value: str         # IP address or target name
  ttl: int           # default 300
  resource_type: str # "instance" | "lb" | "manual"
  resource_id: str   # UUID of the linked resource, or "" for manual
  created_at: str
```

#### Built-in zones (created on startup if absent)

| Zone | Used for |
|---|---|
| `instances.cloudcore.local` | Auto-registered instance A records |
| `lb.cloudcore.local` | Auto-registered LB A records |

#### Auto-registration hooks (in `server.py`)

| Event | Action |
|---|---|
| Instance transitions to `running` | Upsert A record `{name}` in `instances.cloudcore.local` → `private_ip` (bridge) or `127.0.0.1` (SLIRP) |
| Instance deleted | Remove record from `instances.cloudcore.local` |
| LB created | Upsert A record `{name}` in `lb.cloudcore.local` → `127.0.0.1` |
| LB deleted | Remove record from `lb.cloudcore.local` |
| Reconcile on startup | Re-register all running instances and active LBs |

#### A record value strategy

| Networking mode | Instance A record value | Notes |
|---|---|---|
| SLIRP | `127.0.0.1` | Only useful from host. SSH port stored separately in instance record. |
| Bridge (`ccbr0`) | `192.168.100.x` (actual DHCP IP) | Routable between instances and from host. |

LB A records always use `127.0.0.1` (HAProxy binds to host loopback).

#### REST endpoints

| Method | Path | Description |
|---|---|---|
| GET | `/v1/dns/zones` | List all zones |
| POST | `/v1/dns/zones` | Create a zone `{name}` |
| DELETE | `/v1/dns/zones/{zone}` | Delete zone and all its records |
| GET | `/v1/dns/zones/{zone}/records` | List records in zone |
| POST | `/v1/dns/zones/{zone}/records` | Create a record `{name, type, value, ttl}` |
| DELETE | `/v1/dns/zones/{zone}/records/{name}/{type}` | Delete a specific record |

#### UI — new DNS tab

- Zones listed as expandable cards
- Each zone shows its records in a table: Name, FQDN, Type, Value, TTL, Resource, Actions
- "Add Record" form per zone (manual records)
- "Add Zone" form at top
- Auto-registered records show a lock icon / greyed Delete (or allow override)

#### No actual resolver (yet)

This is a **registry** — like Route 53's record store. It does not run a DNS server. A future stage could wire the records into the dnsmasq instance already started by `setup-network.sh` by writing a hosts file or using dnsmasq's `--addn-hosts` option.

---

## Key Technical Notes

### JS in index.html
- Single `<script>` block — never add a second one
- After any JS edit: `node --check ui/index.html` won't work directly; use:
  ```bash
  python3 -c "
  import re
  with open('ui/index.html') as f: html = f.read()
  m = re.search(r'<script>(.*?)</script>', html, re.DOTALL)
  open('/tmp/check.js','w').write(m.group(1))
  "
  node --check /tmp/check.js && echo SYNTAX OK
  ```
- Raw ANSI bytes or literal newlines inside JS strings cause silent parse failures — always use `\\r\\n\\x1b[...]`

### websockets v17
- Use `websocket.path` (not `websocket.request.path`)
- Import: `websockets.legacy.server.serve`

### xterm.js
- Version 5.3.0 + xterm-addon-fit 0.8.0 from CDN
- FitAddon namespace: `FitAddon.FitAddon`

### state.json key remapping
- `ssh_port` in JSON → `ssh_host_port` in dataclass (store.py load() remaps this)
- Any new dataclass fields need a `setdefault` in `store.py load()` for backward compat

### HAProxy
- Config/pid/sock files in `api/lb/`
- Port range 8200–8299
- Config files survive reboot — restart is just `haproxy -f <config> -D -p <pid>`

### Images
- Stored in `api/images/`
- Currently present: `ubuntu-22.04.qcow2`
- Fetch with: `bash api/fetch-image.sh <image-id>`
- IMAGE_CATALOGUE in `compute.py` defines all 4 supported images

### Conventions (from .amazonq/rules/conventions.md)
- After every file modification append to `.changelog`:
  `[YYYY-MM-DD HH:MM] ACTION path/to/file | description`
- Naming: lowercase hyphen-separated, `{project}-{environment}-{purpose}`
- All OpenTofu modules: `main.tf`, `variables.tf`, `outputs.tf`, `locals.tf`, `versions.tf`

---

## After Re-login Checklist

1. `groups` — confirm `libvirt` and `kvm` are present
2. `ip addr show ccbr0` — confirm bridge is up (or run `sudo bash api/setup-network.sh`)
3. `systemctl --user enable --now cloudcore-api` — **Stage 1**
4. `systemctl --user status cloudcore-api cloudcore-terminal` — confirm both running
5. Open `http://127.0.0.1:8080` — confirm UI loads and API dot is green
6. Proceed with **Stage 2** (reconciliation) then **Stage 3** (DNS)
