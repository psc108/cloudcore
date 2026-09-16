"""Cross-host peering routes (see the session's cross-host peering plan).

Stage 2 only for now: on-demand mDNS discovery. Pairing handshake
routes (POST /v1/peers, the pairing-request bootstrap, approve/reject,
GET/DELETE /v1/peers) land here in Stage 3.
"""
from __future__ import annotations

import os
from flask import Blueprint, jsonify, request

import discovery

peers_bp = Blueprint("peers", __name__)

API_TOKEN = os.environ.get("CLOUDCORE_API_TOKEN", "dev-token")


def _auth():
    token = request.headers.get("Authorization", "").removeprefix("Bearer ") \
            or request.args.get("token", "")
    if token != API_TOKEN:
        return jsonify({"status": 401, "title": "Unauthorized"}), 401
    return None


@peers_bp.get("/v1/peers/discovered")
def list_discovered():
    """One-shot LAN scan for other CloudCore hosts currently
    advertising (discovery.enabled=true on their own end) — feeds the
    dashboard's "Scan" button. Not itself gated on this host's own
    discovery.enabled: browsing for others is independent of whether
    this host chooses to be visible to them."""
    err = _auth()
    if err: return err
    timeout = request.args.get("timeout", default=3.0, type=float)
    timeout = max(0.5, min(timeout, 10.0))
    return jsonify({"items": discovery.browse(timeout=timeout)})
