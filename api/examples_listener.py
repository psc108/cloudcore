"""A second, narrow network-facing HTTP listener for llm-chat's Phase 3
example capture — structurally the same pattern as peer_listener.py
(the *same* Flask `app` object served a second time, on a second port,
bound 0.0.0.0, gated by server.py's own before_request checking which
port actually accepted the connection) but deliberately NOT the same
listener and NOT gated by discovery.enabled.

Per direct decision: coupling example capture to the peering opt-in
toggle would mean disabling peering silently stops capture too — this
listener starts unconditionally at server.py startup instead, same as
how Loki/the package repo are always-on host-level capabilities (see
promtail-config.yml's own comment), not something a human has to
separately opt into. The port itself is a fixed constant, not a
Settings-page tunable, for the same reason those services' ports are
fixed: cloud-init templates bake in a real address at build time and
have no way to look up a runtime-configurable value.

Reachable at http://192.168.100.1:8083 (the same bridge-gateway
address Loki/the package repo already use) from local guests, and
confirmed live across the WireGuard tunnel from a peer-placed guest's
own host network too (192.168.100.0/24 is included in a paired peer's
own AllowedIPs — see wireguard.py's render_config) — one fixed address
works for both a local and a peer-placed coordinator, no
placement-specific templating needed.
"""
from __future__ import annotations

import logging
import threading

from werkzeug.serving import make_server

log = logging.getLogger(__name__)

PORT = 8083

_app = None
_server = None
_thread = None
_lock = threading.Lock()


def init(app) -> None:
    global _app
    _app = app


def start(port: int = PORT) -> None:
    global _server, _thread
    with _lock:
        if _server is not None or _app is None:
            return
        _server = make_server("0.0.0.0", port, _app, threaded=True)
        _thread = threading.Thread(target=_server.serve_forever, daemon=True, name="examples-listener")
        _thread.start()
        log.info("Examples listener started on 0.0.0.0:%d", port)


def stop() -> None:
    global _server, _thread
    with _lock:
        if _server is None:
            return
        _server.shutdown()
        _thread.join(timeout=5)
        _server, _thread = None, None
        log.info("Examples listener stopped")


def is_running() -> bool:
    with _lock:
        return _server is not None
