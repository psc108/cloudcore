# Kiwix Library

Stands up a single CloudCore instance serving an offline content archive
via [Kiwix](https://www.kiwix.org/) — Wikipedia, Wiktionary, Project
Gutenberg, Stack Exchange, and hundreds of other archives packaged as ZIM
files, browsable like a normal website with zero internet access needed
once it's built.

Point-and-shoot, same as the Ghidra workstation template: open a URL in a
browser and the library is just *there*. No SSH tunnel, no VNC client, no
login. `kiwix-serve` (the thing actually serving the content) is a plain
static binary with no runtime dependencies and no built-in auth — this is
architecturally simpler than the Ghidra template, which needs a full
remote desktop. See "Why this is simpler than Ghidra" below if you're
comparing the two.

## What it creates

VPC + public subnet + a security group (SSH + HTTP, scoped to a CIDR you
choose) + one `standard.medium` instance + an HTTP load balancer in front
of it. Cloud-init on first boot installs:

- `kiwix-serve` (from [kiwix-tools](https://github.com/kiwix/kiwix-tools),
  pinned version, checksum-verified)
- A ZIM content archive (pinned URL, checksum-verified) — defaults to
  English Wikipedia's "top" articles, text-only (~2.2 GB)

First boot takes a couple of minutes — mostly the ZIM download. A small
placeholder web server occupies `desktop_url` for that window (systemd
hands the port to `kiwix-serve` the instant it's ready, via a
`Conflicts=` relationship between the two services, the same mechanism
the Ghidra template uses) so visiting the URL early shows a "still
building" page instead of a browser connection error.

Both downloads use a stall-tolerant retry loop (`curl --speed-limit
... -C -`, resume rather than restart) rather than a single attempt —
this platform's guest networking has been observed to occasionally stall
completely mid-transfer on large files, which plain `curl --retry`
doesn't catch since the connection stays technically open.

## Access model

Everything here binds to loopback somewhere along the chain — the same
model as every other example in this repo — so the short version is:
**you need to be on the CloudCore host itself, or already tunneled into
it.** From there, no further tunneling is needed.

### Point and shoot

```bash
tofu output desktop_url
# "http://127.0.0.1:<port>/"
```

Open that URL in a browser — right away, no need to wait first. For
roughly the first 30 seconds (the instance still booting) it may fail to
load; after that it shows a "still building" page, then switches to the
real Kiwix library automatically once ready. Same URL throughout.

The library's homepage lists the loaded archive(s) — click through to
browse. No login, no password.

### CLI access (SSH)

```bash
tofu output ssh_commands
# "01" = "ssh ubuntu@127.0.0.1 -p <port>"
```

Useful for adding more ZIM files later: drop them in `/opt/kiwix/data/`
and restart `kiwix-serve.service` (you'll need to point it at the new
file(s) — see `kiwix-serve --help` for serving multiple ZIMs at once).

### Fallback direct access (SSH tunnel)

If you ever need the instance independent of the load balancer:

```bash
tofu output direct_url_via_ssh_tunnel
# "01" = "ssh -p <port> -L 8080:localhost:80 ubuntu@127.0.0.1"
```

Run that command (it blocks; `Ctrl-C` to close), then open
`http://127.0.0.1:8080/`.

## Why this is simpler than Ghidra

Worth noting for anyone extending this template catalog further:
`kiwix-serve` is a normal stateless HTTP server, not a persistent
WebSocket tunnel like Ghidra's noVNC — so this uses the platform's plain
`application`-type load balancer with instance auto-discovery (the same
pattern `load-balanced-web` already uses), not an explicit target
group/listener pointed at a `network`-type (TCP passthrough) load
balancer. None of Ghidra's WebSocket-specific workarounds apply here —
no `option http-server-close` conflict, no 30-second idle-timeout
heartbeat needed, since a browser just opens a fresh connection per page
load rather than holding one open indefinitely.

## Usage

```bash
tofu init
tofu apply
```

No variables are required — every default works out of the box. This is
deliberate: like the Ghidra template, this is meant to be usable by
someone with zero networking or ops background.

| Variable | Required | Notes |
|---|---|---|
| `admin_cidr` | No | Defaults to `0.0.0.0/0`. Safe to leave as-is on CloudCore specifically — see "Access model" above for why the security group isn't the real access boundary here. |
| `instance_flavor` | No | Defaults to `standard.medium`. `kiwix-serve` is light (disk-I/O bound); only go bigger for a much larger ZIM library than the default. |
| `zim_url` / `zim_filename` / `zim_md5` | No | Pinned to English Wikipedia's "top" articles (text-only, ~2.2 GB) by default. Point these at any other archive from [library.kiwix.org](https://library.kiwix.org) instead — Wiktionary, Project Gutenberg, Stack Exchange, other languages, the full-image Wikipedia set (8+ GB) — update all three together. The checksum is published alongside each file as `<url>.md5`. |
| `kiwix_tools_version` / `kiwix_tools_url` / `kiwix_tools_sha256` | No | Pinned to kiwix-tools 3.8.2. Kiwix's own download server only publishes MD5 for these — the SHA-256 here was computed independently from a verified download as a stronger pin. Update all three together when bumping the version. |

Everything else (`project`, `environment`, `owner`, `suffix`,
`cidr_block`) follows the same conventions as the other examples in this
repo.

## Teardown

```bash
tofu destroy
```
