"""A second, narrow network-facing HTTP listener for cross-host peering.

cloudcore-api's own dashboard bind (127.0.0.1:8080, unchanged) stays
exactly as it's always been — unreachable from the network at all,
protected implicitly by never accepting a connection from anywhere but
this machine. Peer-to-peer traffic (the pairing bootstrap, the
approval callback, and later Stage 5's proxied instance calls)
genuinely needs to be reachable from another host — but blanket-
exposing the *whole* dashboard API (protected only by the single
shared, often-default `dev-token`) to the LAN would be a real security
regression: anyone on the network could hit `POST /v1/instances` with
the well-known default token.

Instead, the *same* Flask `app` object (server.py) is served a second
time, on a second port (network.peer_listener_port, default 8082,
bound 0.0.0.0 — this one genuinely needs to be network-reachable), via
a second werkzeug server run in a background thread. server.py
registers a `before_request` gate that restricts *that* bind to an
explicit allowlist of peer-reachable endpoints (peers_routes.py's own
PEER_REACHABLE_ENDPOINTS) — everything else 403s there regardless of
any token presented. The gate distinguishes binds by which local port
actually accepted the connection (WSGI's own SERVER_PORT, populated by
the accepting socket — not something a client can influence), not by
anything client-supplied.

Tied to the same discovery.enabled setting as mDNS advertising — "opt
in to participate in peering" closes or opens both at once, rather
than needing a second toggle: the port doesn't even exist to scan or
abuse unless a human on this machine explicitly turned this on.
"""
from __future__ import annotations

import logging
import threading

from werkzeug.serving import make_server

log = logging.getLogger(__name__)

_app = None
_server = None
_thread = None
_lock = threading.Lock()


def init(app) -> None:
    """Called once at server.py startup, before start() can ever be
    called (e.g. from a settings PUT or a resumed-on-boot advertise)."""
    global _app
    _app = app


def start(port: int) -> None:
    global _server, _thread
    with _lock:
        if _server is not None or _app is None:
            return
        _server = make_server("0.0.0.0", port, _app, threaded=True)
        _thread = threading.Thread(target=_server.serve_forever, daemon=True, name="peer-listener")
        _thread.start()
        log.info("Peer listener started on 0.0.0.0:%d", port)


def stop() -> None:
    global _server, _thread
    with _lock:
        if _server is None:
            return
        _server.shutdown()
        _thread.join(timeout=5)
        _server, _thread = None, None
        log.info("Peer listener stopped")


def is_running() -> bool:
    with _lock:
        return _server is not None
