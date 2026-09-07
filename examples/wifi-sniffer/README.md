# WiFi Sniffer

Stands up a single CloudCore instance running [Kismet](https://www.kismetwireless.net/)
— a live WiFi/Bluetooth/SDR monitoring dashboard — fed by a real USB WiFi
adapter passed through from the CloudCore host, running in monitor mode.
Also installs the aircrack-ng suite, tshark, and hcxtools/hcxdumptool for
deeper offline analysis (WPA handshake capture and conversion to
hashcat-crackable format) over SSH.

Point-and-shoot, same as the Ghidra and Kiwix templates: open a URL in a
browser and the dashboard is just *there*. No SSH tunnel, no client
install for the primary use case.

This template needs CloudCore's USB device passthrough feature and a
physical monitor-mode-capable USB WiFi adapter attached to the CloudCore
host — there's no software-only version of this one.

## Supported hardware

Pinned to the **Alfa AWUS036ACH** (Realtek RTL8812AU chipset) specifically.
The driver installed is the **aircrack-ng-maintained fork**
(`github.com/aircrack-ng/rtl8812au`) — confirmed via its own README
("Monitor mode: working", "Frame Injection: working"), not the more
commonly-suggested `morrownr` fork, which states explicitly in its own
README that it does **not** support monitor mode. If you use different
hardware, you'll need a different driver — check what your chipset
actually needs before assuming this template's cloud-init applies as-is.

## What it creates

VPC + public subnet + a security group (SSH + dashboard, scoped to a CIDR
you choose) + one `standard.medium` instance, with the WiFi adapter passed
through via `usb_device_id`, fronted by a network (L4) load balancer.
Cloud-init on first boot:

- Installs Kismet (official apt repo), the aircrack-ng suite, tshark,
  hcxtools/hcxdumptool, tcpdump
- Builds and loads the RTL8812AU driver via DKMS, against the exact
  running kernel (not the "-generic" floating package — see the comment
  at the top of the cloud-init template for why that matters: it pulled
  in a *different, not-yet-booted* kernel version during testing, which
  needed a reboot to reconcile and left the modules mismatched with what
  was actually running in the meantime)
- Detects the adapter's actual interface name at runtime (USB WiFi
  adapters get persistent names like `wlx<mac>`, not `wlan0` — this
  isn't knowable ahead of time) and points Kismet at it
- Disables USB autosuspend for the adapter specifically (a common cause
  of flaky, hard-to-diagnose capture/injection behavior)

First boot takes a couple of minutes. A placeholder page occupies
`desktop_url` for that window, same handoff mechanism as the Ghidra
template (`Conflicts=` between `placeholder.service` and `kismet.service`).

## Access model

Everything here binds to loopback somewhere along the chain — the same
model as every other example in this repo — so the short version is:
**you need to be on the CloudCore host itself, or already tunneled into
it.** From there, no further tunneling is needed.

The load balancer is `type = "network"` (TCP passthrough), the same
architecture the Ghidra template uses and for the same reason: Kismet's
live dashboard is genuinely WebSocket-driven (`/eventbus/events.ws`,
confirmed directly from its own JS source and by a real handshake test
through this exact LB during development — a plain HTTP-mode LB would
break it via `option http-server-close`, which closes the connection
after every request/response).

### Point and shoot

```bash
tofu output desktop_url
# "http://127.0.0.1:<port>/"
```

Open that URL in a browser — right away, no need to wait first. It'll
show a "still building" page for the first couple of minutes, then
switch to the real dashboard automatically.

**First visit to the real dashboard** prompts you to set your own admin
username and password right there in the browser — this is Kismet's own
first-run setup (confirmed directly against a live instance), not an
auto-generated credential you need to dig out over SSH.

### CLI access (SSH) — deeper analysis

```bash
tofu output ssh_commands
```

- `aircrack-ng`, `tshark`, `hcxdumptool`/`hcxtools`, `tcpdump` are all
  installed and usable directly (the `ubuntu` user is in both the
  `wireshark` and `kismet` groups — no `sudo` needed for packet capture).
- Kismet's own capture logs land in `/home/ubuntu/kismet-logs/`.

### Fallback direct access (SSH tunnel)

If you ever need the dashboard independent of the load balancer:

```bash
tofu output direct_url_via_ssh_tunnel
```

Run that command (it blocks; `Ctrl-C` to close), then open
`http://127.0.0.1:8080/`.

## Usage

First, find your adapter's `vendor_id:product_id` once it's plugged into
the CloudCore host:

```bash
curl http://<cloudcore-host>:8080/v1/usb-devices -H "Authorization: Bearer <token>"
```

Look for your adapter in the list (`blocked: false`, description matching
your hardware), then:

```bash
tofu init
tofu apply -var usb_device_id="0bda:8812"   # your adapter's actual ID
```

`usb_device_id` is the only required variable — there's no generic
default possible the way Ghidra pins a release or Kiwix pins a ZIM file;
this template needs your specific physical hardware.

| Variable | Required | Notes |
|---|---|---|
| `usb_device_id` | **Yes** | `"vendor_id:product_id"` of the WiFi adapter, from `cloudcore_usb_devices`. Validated at apply time by CloudCore itself (device must exist, not be blocked as HID/hub, and not already be attached elsewhere) — you'll get a clean error, not a broken VM, if it's wrong. |
| `admin_cidr` | No | Defaults to `0.0.0.0/0`. Safe to leave as-is on CloudCore specifically — see "Access model" above for why the security group isn't the real access boundary here. |
| `instance_flavor` | No | Defaults to `standard.medium`. |
| `lb_port` | No | Defaults to `8700`, chosen clear of every other port range this platform auto-allocates or the other templates default to. |
| `rtl8812au_driver_ref` | No | Pinned to `v5.6.4.2` of the aircrack-ng driver fork. Override to track a newer release. |

Everything else (`project`, `environment`, `owner`, `suffix`,
`cidr_block`) follows the same conventions as the other examples in this
repo.

## Known caveat — read before relying on packet injection

A resolved-as-"known, won't fix" upstream issue
(`aircrack-ng/rtl8812au#451`) reports packet injection failing for this
exact chipset specifically under **VirtualBox** USB passthrough (monitor
mode and capture worked fine there; only `aireplay-ng`-style injection
failed). No equivalent report was found for KVM/QEMU/libvirt — which is
what CloudCore actually uses, and this session already validated that
libvirt's `managed='yes'` USB hostdev passthrough correctly presents a
device's exact identity to the guest (proven with a USB flash drive: the
guest kernel enumerated it correctly, checksum-verified data survived a
full attach/detach cycle). Treat this as "watch for it," not "expect it."

## Teardown

```bash
tofu destroy
```
