# Ghidra Workstation

Stands up a single CloudCore instance with a full graphical desktop (XFCE)
and [Ghidra](https://ghidra-sre.org/) pre-installed, for reverse-engineering
work — CTF binaries, malware samples, firmware images, whatever you'd
normally point Ghidra at.

The desktop is fronted by a load balancer, so using it is point-and-shoot:
open a URL in a browser and the desktop is just *there*. No SSH tunnel to
set up, no VNC client to install. SSH is still there alongside it for
moving files in and out.

## What it creates

VPC + public subnet + a security group (SSH + desktop, scoped to a CIDR you
choose) + one `standard.large` instance (4 vCPU / 4 GB — the largest flavor
CloudCore currently offers) + a network load balancer in front of it. Cloud-init
on first boot installs:

- XFCE desktop + TigerVNC + noVNC/websockify
- Temurin JDK 21 (Ghidra 12.1.3's requirement)
- Ghidra itself, downloaded from the official GitHub release and verified
  against a pinned SHA-256 before it's unzipped

First boot takes a few minutes — it's pulling down the JDK, desktop
packages, and a ~570 MB Ghidra archive. A small placeholder web server
occupies `desktop_url` for that window (systemd hands the port to the real
desktop the instant it's ready, via a `Conflicts=` relationship between the
two services — see the cloud-init template) so visiting the URL early
shows a "still building" page instead of a browser connection error.

## Access model

Everything here binds to loopback somewhere along the chain — the same
model as the rest of CloudCore (`ssh_commands` on every other example, the
dashboard at `127.0.0.1:8080`) — so the short version is: **you need to be
on the CloudCore host itself, or already tunneled into it.** From there, no
further tunneling is needed; see the two access methods below.

The load balancer is `type = "network"` (L4 TCP passthrough), deliberately
not `application`: this platform's HTTP-mode HAProxy sets
`option http-server-close`, which closes the connection after every
request/response — fatal to the WebSocket noVNC needs to stay open.
TCP passthrough sidesteps that. HAProxy on this platform also has a fixed
30-second idle timeout with no exposed override, which would otherwise drop
your session anytime you're not actively moving the mouse — `websockify`
runs with `--heartbeat 15` to send a WebSocket ping every 15 seconds and
keep the connection alive indefinitely. Verified by holding a connection
open for 45+ seconds with zero real traffic and confirming it survived.

### Graphical access — point and shoot

```bash
tofu output desktop_url
# "http://127.0.0.1:<lb_port>/vnc.html"
```

1. Open that URL in a browser on the CloudCore host — right away, no need
   to wait first.
2. For roughly the first 90 seconds (the instance still booting) it may
   fail to load. After that, it shows a "still building" page — leave the
   tab open. It switches to the real desktop automatically once ready
   (a few minutes total, no action needed, same URL throughout).
3. Click **Connect** and enter the `vnc_password` you set below.

You'll land on an XFCE desktop with a **Ghidra** icon on the desktop —
double-click it (or run `/opt/ghidra/ghidraRun` from a terminal inside the
session) to launch the GUI.

### CLI access (SSH)

```bash
tofu output ssh_commands
# "01" = "ssh ubuntu@127.0.0.1 -p <port>"
```

Run that command directly. From there:

- `~/samples/` — put binaries you want to analyze here (`scp -P <port> ./sample.bin ubuntu@127.0.0.1:~/samples/`)
- `~/ghidra-projects/` — Ghidra project files live here; pull results back out the same way via `scp`
- `/opt/ghidra` — the Ghidra install itself (`/opt/ghidra/support/analyzeHeadless` if you want headless/scripted analysis instead of the GUI)

### Fallback graphical access (SSH tunnel)

If you ever need the desktop independent of the load balancer — debugging
it, say — this bypasses the LB entirely:

```bash
tofu output vnc_tunnel_commands
# "01" = "ssh -p <port> -L 6080:localhost:80 ubuntu@127.0.0.1"
```

Run that command (it blocks; `Ctrl-C` to close), then open
`http://127.0.0.1:6080/vnc.html`.

## Usage

```bash
export TF_VAR_vnc_password="<8 chars>"     # required — see note below

tofu init
tofu apply
```

Only `vnc_password` needs setting — everything else, including `admin_cidr`,
has a working default. That's deliberate: this template is meant to be
usable by someone with binary-analysis training and zero networking
background, e.g. deployed through a self-service build catalog. Nobody
should have to know what a CIDR is to get a Ghidra box.

| Variable | Required | Notes |
|---|---|---|
| `vnc_password` | Yes | Classic VNC auth (TigerVNC) only honours the **first 8 characters** — anything beyond that is silently ignored. Any password of your choosing — no networking knowledge needed. |
| `admin_cidr` | No | Defaults to `0.0.0.0/0`. Safe to leave as-is on CloudCore specifically — see "Access model" above for why the security group isn't the real access boundary here. Only override this if you know you want to (e.g. defense-in-depth, or a platform where that property doesn't hold). |
| `instance_flavor` | No | Defaults to `standard.large`. Ghidra's decompiler and XFCE both want the headroom; don't go smaller unless you're only doing headless analysis. |
| `lb_port` | No | Host port the desktop is served on. Defaults to `8600`, chosen clear of every other port range this platform auto-allocates. Change it if that collides with something else on your host. |
| `ghidra_version` / `ghidra_release_tag` / `ghidra_zip_name` / `ghidra_sha256` | No | Pinned to Ghidra 12.1.3 by default. To bump the version, update all four together from the [releases page](https://github.com/NationalSecurityAgency/ghidra/releases) — the SHA-256 is published in each release's notes. |

Everything else (`project`, `environment`, `owner`, `suffix`, `cidr_block`)
follows the same conventions as the other examples in this repo.

## Teardown

```bash
tofu destroy
```
