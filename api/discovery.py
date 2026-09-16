"""mDNS presence advertisement + on-demand browsing for cross-host
peering (see api/peers_routes.py, Stage 2/3 of the session's cross-host
peering plan).

Strictly opt-in: advertise() is only ever called while the
discovery.enabled setting is true (api/settings_routes.py's own PUT
handler starts/stops it live, no restart needed) — a host that hasn't
explicitly turned this on stays completely silent on the wire, never
sending so much as a single mDNS packet. What gets advertised is
deliberately minimal: hostname, this host's own pairing-identity
fingerprint (api/identity.py), and the port a peer would need to reach
to actually request pairing — nothing about capacity, what's running,
or anything else.

Uses the pure-Python `zeroconf` library rather than shelling out to
avahi-publish/avahi-browse — no dependency on a pre-installed,
pre-running system avahi-daemon, and advertise()/stop_advertise() can
start and stop exactly when the setting flips, in-process.
"""
from __future__ import annotations

import socket
import threading

from zeroconf import Zeroconf, ServiceInfo, ServiceBrowser, ServiceListener

import identity
import settings_store

SERVICE_TYPE = "_cloudcore._tcp.local."

_lock = threading.Lock()
_zc: Zeroconf | None = None
_service_info: ServiceInfo | None = None

# Separate lazy singleton for browse() — deliberately never a fresh
# Zeroconf() per call. Each Zeroconf() instance spins up its own
# background engine thread and its own socket(s); confirmed live as a
# real, serious bug (F-090): a long-running host that had browse()
# called many times over one session's worth of testing exhausted its
# process's open-file limit entirely (OSError: [Errno 24] Too many
# open files), taking the whole API down with it, including — visible
# in `lsof` — a large pile of leftover thread-pool-associated file
# descriptors, one generation per browse() call whose Zeroconf.close()
# apparently never fully released everything it opened. One shared
# instance, created once and reused for the life of the process,
# removes the repeated create/destroy cycle that actually caused it —
# only the lightweight per-scan ServiceBrowser is still created and
# cancelled each call.
_browse_zc: Zeroconf | None = None
_browse_lock = threading.Lock()


def _local_ip() -> str:
    """This host's own real (non-loopback) LAN address. The
    connect-then-inspect trick below sends no actual traffic (UDP is
    connectionless — connect() here just asks the kernel to resolve
    which local interface/address a packet to that destination would
    leave from) and works regardless of how many interfaces this host
    has, without needing to guess at an interface name."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


def peer_listener_port() -> int:
    """The port a peer would connect to for pairing/proxied instance
    calls (api/peer_listener.py, Stage 3) — configurable since it's a
    new, separate, network-facing listener distinct from the
    dashboard-only cloudcore-api (127.0.0.1:8080)."""
    return settings_store.get("network.peer_listener_port", 8082)


def advertise() -> None:
    """Start broadcasting this host's presence. Idempotent — safe to
    call again while already advertising (e.g. a settings PUT that's
    already true)."""
    global _zc, _service_info
    with _lock:
        if _zc is not None:
            return
        hostname = socket.gethostname()
        fpr = identity.peer_pubkey_fingerprint()
        port = peer_listener_port()
        info = ServiceInfo(
            SERVICE_TYPE,
            f"{hostname}.{SERVICE_TYPE}",
            addresses=[socket.inet_aton(_local_ip())],
            port=port,
            properties={"fpr": fpr, "v": "1"},
            server=f"{hostname}.local.",
        )
        zc = Zeroconf()
        zc.register_service(info)
        _zc, _service_info = zc, info


def stop_advertise() -> None:
    """Stop broadcasting. Idempotent — safe to call when not currently
    advertising."""
    global _zc, _service_info
    with _lock:
        if _zc is None:
            return
        try:
            _zc.unregister_service(_service_info)
        finally:
            _zc.close()
            _zc, _service_info = None, None


def is_advertising() -> bool:
    with _lock:
        return _zc is not None


class _CollectingListener(ServiceListener):
    def __init__(self):
        self.found: dict[str, dict] = {}

    def add_service(self, zc: Zeroconf, type_: str, name: str) -> None:
        info = zc.get_service_info(type_, name, timeout=1500)
        if info is None:
            return
        addresses = info.parsed_scoped_addresses() or info.parsed_addresses()
        if not addresses:
            return
        props = {
            k.decode() if isinstance(k, bytes) else k:
            v.decode() if isinstance(v, bytes) else v
            for k, v in (info.properties or {}).items()
        }
        self.found[name] = {
            "hostname": name[: -len("." + SERVICE_TYPE)] if name.endswith("." + SERVICE_TYPE) else name,
            "address": addresses[0],
            "port": info.port,
            "pubkey_fpr": props.get("fpr", ""),
        }

    def update_service(self, zc: Zeroconf, type_: str, name: str) -> None:
        self.add_service(zc, type_, name)

    def remove_service(self, zc: Zeroconf, type_: str, name: str) -> None:
        self.found.pop(name, None)


def _get_browse_zc() -> Zeroconf:
    global _browse_zc
    with _browse_lock:
        if _browse_zc is None:
            _browse_zc = Zeroconf()
        return _browse_zc


def browse(timeout: float = 3.0) -> list[dict]:
    """One-shot scan for other CloudCore hosts currently advertising on
    the real LAN. Not a persistent *browser* — no ServiceBrowser is left
    running between calls, keeping an opt-in feature's footprint minimal
    when nobody's actively looking to pair — but the underlying Zeroconf
    engine itself (_get_browse_zc()) is a long-lived singleton, not
    recreated per call; see its own comment for why that distinction is
    the actual fix for a real bug. A separate instance from advertise()'s
    own — browsing works independently of whether this host is itself
    advertising."""
    zc = _get_browse_zc()
    listener = _CollectingListener()
    browser = ServiceBrowser(zc, SERVICE_TYPE, listener)
    try:
        threading.Event().wait(timeout)
    finally:
        browser.cancel()
    my_fpr = identity.peer_pubkey_fingerprint()
    return [v for v in listener.found.values() if v["pubkey_fpr"] != my_fpr]
