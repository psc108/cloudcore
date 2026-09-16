"""WireGuard tunnel lifecycle for cross-host peering.

One shared `cc0` interface per host, one `[Peer]` block per approved
pairing — the idiomatic WireGuard mesh pattern (a single interface
natively supports many peers; adding peer #3 hot-reloads via
`wg syncconf` without bouncing #1/#2's live tunnels). Created at
pairing-approval time, not lazily on first remote build — approval is
already the one privileged, consented moment, and an idle-but-
established tunnel adds no attack surface (WireGuard stays silent to
anyone without a valid key regardless of traffic).

Transit addressing: each host's own `cc0` address is derived
deterministically from its own `network.bridge_subnet_octet` setting
(already guaranteed unique across paired hosts — that's the whole
point of that setting) as `10.99.<octet>.1` — no coordination round-
trip needed, and no separate "transit IP negotiation" step. A peer's
own transit address is derived the same way from their advertised
`wg_bridge_subnet` (e.g. "192.168.101.0/24" -> "10.99.101.1").

The config always lives at the canonical /etc/wireguard/cc0.conf, not
under this repo's own directory — confirmed live as a real, reliably
reproducible requirement: `sudo -n wg-quick up <path-under-$HOME>`
fails reading a config under a normal user's home directory (plain
`sudo cat` on the identical file, same target user, succeeds — this
is specific to wg-quick's own invocation, not a general permissions
problem), while the exact same content at /etc/wireguard/cc0.conf
works every time. Written there via a narrowly-scoped `sudo -n tee`
grant rather than the unprivileged API process ever writing into
/etc itself.

Privileged operations (`wg-quick up/strip`, `wg syncconf/show`, that
one `tee`) go through the same narrowly-scoped `sudo -n` NOPASSWD
pattern api/sg.py already uses for iptables (api/setup-wireguard.sh
installs the grant) — no new root daemon or IPC channel.
"""
from __future__ import annotations

import re
import subprocess
import time
from typing import Optional

import identity
import settings_store

ETC_CONF = "/etc/wireguard/cc0.conf"
IFACE = "cc0"


def _run(cmd: list[str], **kwargs) -> subprocess.CompletedProcess:
    # "-n" (non-interactive): a missing sudoers grant fails fast with a
    # clear stderr message instead of hanging a background thread
    # waiting on a password prompt that can never arrive — same
    # reasoning as api/sg.py's own _run().
    return subprocess.run(["sudo", "-n"] + cmd, capture_output=True, text=True, **kwargs)


def my_transit_ip() -> str:
    octet = settings_store.get("network.bridge_subnet_octet", 100)
    return f"10.99.{octet}.1"


def peer_transit_ip(bridge_subnet: Optional[str]) -> Optional[str]:
    """A peer's own cc0 transit address, derived from their advertised
    bridge subnet the same deterministic way as my_transit_ip() derives
    ours. None if bridge_subnet isn't a recognizable 192.168.<n>.0/24
    (e.g. not yet known, mid-handshake)."""
    m = re.match(r"192\.168\.(\d+)\.0/24$", bridge_subnet or "")
    return f"10.99.{m.group(1)}.1" if m else None


def _iface_exists() -> bool:
    return subprocess.run(["ip", "link", "show", IFACE], capture_output=True).returncode == 0


def render_config(peers: list[dict]) -> str:
    listen_port = settings_store.get("network.wg_listen_port", 51820)
    privkey = identity.WG_PRIVKEY.read_text().strip()
    lines = [
        "[Interface]",
        f"Address = {my_transit_ip()}/16",
        f"ListenPort = {listen_port}",
        f"PrivateKey = {privkey}",
        "",
    ]
    for peer in peers:
        if peer.get("status") != "approved" or not peer.get("wg_pubkey"):
            continue
        allowed = []
        if peer.get("wg_bridge_subnet"):
            allowed.append(peer["wg_bridge_subnet"])
        transit_ip = peer_transit_ip(peer.get("wg_bridge_subnet"))
        if transit_ip:
            allowed.append(f"{transit_ip}/32")
        if not allowed:
            continue
        lines.append("[Peer]")
        lines.append(f"PublicKey = {peer['wg_pubkey']}")
        lines.append(f"AllowedIPs = {', '.join(allowed)}")
        if peer.get("wg_endpoint"):
            lines.append(f"Endpoint = {peer['wg_endpoint']}")
        lines.append("")
    return "\n".join(lines)


def apply(peers: list[dict]) -> tuple[bool, str]:
    """Render the current approved-peers config and apply it — `up` the
    first time cc0 doesn't exist yet, `syncconf` (hot add/remove,
    doesn't disturb other already-live peers) otherwise. Returns
    (ok, message)."""
    config_text = render_config(peers)
    write_result = _run(["tee", ETC_CONF], input=config_text)
    if write_result.returncode != 0:
        return False, (write_result.stderr or "could not write " + ETC_CONF).strip()

    if not _iface_exists():
        result = _run(["wg-quick", "up", IFACE])
        if result.returncode != 0:
            return False, (result.stderr or result.stdout).strip()
        # Same FORWARD-accept reasoning as setup-network.sh's own ccbr0
        # rule: needed even with a valid handshake if something else
        # (commonly Docker) has already set FORWARD's default policy to
        # DROP. Already covered by the existing cloudcore-sg sudoers
        # grant (same two binaries setup-network.sh grants for ccbr0).
        _run(["iptables", "-C", "FORWARD", "-i", IFACE, "-j", "ACCEPT"]).returncode == 0 or \
            _run(["iptables", "-I", "FORWARD", "-i", IFACE, "-j", "ACCEPT"])
        _run(["iptables", "-C", "FORWARD", "-o", IFACE, "-j", "ACCEPT"]).returncode == 0 or \
            _run(["iptables", "-I", "FORWARD", "-o", IFACE, "-j", "ACCEPT"])
        return True, "cc0 up"

    # `wg syncconf` (unlike `wg-quick up`) only understands the raw
    # wg-setconf format — no `Address =` line, which is a pure
    # wg-quick/`ip addr` concept the WireGuard kernel module itself has
    # no notion of. `wg-quick strip` does that translation; it also
    # unconditionally self-elevates via sudo for every subcommand
    # (confirmed directly in its own source — not just for up/down).
    # Piped straight into `wg syncconf`'s stdin (via /dev/stdin) rather
    # than through a second on-disk file — one less thing to place at a
    # privileged path.
    strip_result = _run(["wg-quick", "strip", IFACE])
    if strip_result.returncode != 0:
        return False, (strip_result.stderr or strip_result.stdout).strip()

    result = _run(["wg", "syncconf", IFACE, "/dev/stdin"], input=strip_result.stdout)
    if result.returncode != 0:
        return False, (result.stderr or result.stdout).strip()
    return True, "cc0 synced"


def handshake_status(pubkey: str) -> str:
    """'up' if `wg show` reports a completed handshake for this peer's
    pubkey, 'down' if the peer is configured but hasn't handshaked
    (yet), 'unknown' if cc0 isn't up at all or the query itself failed."""
    if not _iface_exists():
        return "unknown"
    result = _run(["wg", "show", IFACE, "dump"])
    if result.returncode != 0:
        return "unknown"
    for line in result.stdout.splitlines()[1:]:
        parts = line.split("\t")
        if len(parts) >= 5 and parts[0] == pubkey:
            return "up" if int(parts[4]) > 0 else "down"
    return "unknown"


def on_peer_approved(peer_row: dict, all_approved_peers: list[dict]) -> dict:
    """Called right after a peer becomes approved (from either side of
    the handshake — peers_routes.py's approve and complete handlers).
    Re-renders and applies the tunnel config, then polls briefly for a
    real handshake. Returns the fields to store back on the peer row."""
    ok, msg = apply(all_approved_peers)
    if not ok:
        return {"wg_tunnel_status": "error", "wg_transit_ip": None}
    if not peer_row.get("wg_pubkey"):
        return {"wg_tunnel_status": "unknown", "wg_transit_ip": None}
    transit_ip = peer_transit_ip(peer_row.get("wg_bridge_subnet"))
    # Capped well under peer_client.TIMEOUT so a slow handshake here
    # can't itself be the reason a caller waiting on this (e.g. the
    # /v1/peers/complete callback handler) times out.
    for _ in range(6):
        if handshake_status(peer_row["wg_pubkey"]) == "up":
            return {"wg_tunnel_status": "up", "wg_transit_ip": transit_ip}
        time.sleep(1)
    return {"wg_tunnel_status": "down", "wg_transit_ip": transit_ip}


def on_peer_revoked(all_approved_peers: list[dict]) -> None:
    """Re-render + apply after a peer is revoked, dropping their
    [Peer] block without disturbing anyone else's live tunnel."""
    if _iface_exists():
        apply(all_approved_peers)
