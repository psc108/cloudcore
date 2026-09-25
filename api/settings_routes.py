"""Settings API routes. Scoped by category (currently just 'tofu') — the
underlying store (settings_store.py) is a generic key/value table, so a new
category is just a new route + validation function, no schema change."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from flask import Blueprint, jsonify, request

import settings_store
import discovery
import peer_listener

settings_bp = Blueprint("settings", __name__)

API_TOKEN = os.environ.get("CLOUDCORE_API_TOKEN", "dev-token")

_SETUP_NETWORK_SH = Path(__file__).parent / "setup-network.sh"
_TEARDOWN_NETWORK_SH = Path(__file__).parent / "teardown-network.sh"


def _rebuild_bridge(new_octet: int) -> dict:
    """Actually move ccbr0 onto the new subnet, rather than letting
    bridge_subnet_octet become a DB value with no bearing on live
    state — confirmed live as a real incident otherwise: the setting
    was changed weeks ago, nothing rebuilt the interface to match, and
    it silently diverged until a WireGuard route-add collision with a
    peer's own bridge subnet surfaced it days later as a confusing,
    unrelated-looking tunnel failure. Deliberately does NOT pass
    --force to teardown-network.sh — that script's own guard (refusing
    to cut off cloudcore-repo/loki/grafana-server without confirmation)
    is exactly the check that should still apply here; failing loudly
    in the API response is the fix, not bypassing the guard.
    Requires the NOPASSWD grant setup-network.sh's own sudoers block
    installs for exactly these two scripts — on a host that hasn't
    re-run setup-network.sh since that grant was added, this fails
    with a clear permission-denied detail rather than hanging."""
    teardown = subprocess.run(
        ["sudo", "-n", "bash", str(_TEARDOWN_NETWORK_SH)],
        capture_output=True, text=True, timeout=30)
    if teardown.returncode != 0:
        return {"status": "blocked", "detail": (teardown.stderr or teardown.stdout).strip()}

    setup = subprocess.run(
        ["sudo", "-n", "bash", str(_SETUP_NETWORK_SH), str(new_octet)],
        capture_output=True, text=True, timeout=30)
    if setup.returncode != 0:
        return {"status": "failed", "detail": (setup.stderr or setup.stdout).strip()}
    return {"status": "ok", "detail": (setup.stdout or "").strip().splitlines()[0] if setup.stdout else "rebuilt"}


def _auth():
    token = request.headers.get("Authorization", "").removeprefix("Bearer ") \
            or request.args.get("token", "")
    if token != API_TOKEN:
        return jsonify({"status": 401, "title": "Unauthorized"}), 401
    return None


@settings_bp.get("/v1/settings/tofu")
def get_tofu_settings():
    err = _auth()
    if err: return err
    return jsonify(settings_store.get_prefixed("tofu"))


@settings_bp.put("/v1/settings/tofu")
def update_tofu_settings():
    err = _auth()
    if err: return err
    body = request.get_json(force=True) or {}

    if "parallelism" in body:
        val = body["parallelism"]
        if val is not None:
            try:
                val = int(val)
            except (TypeError, ValueError):
                return jsonify({"status": 400, "title": "Bad Request",
                                 "detail": "parallelism must be an integer or null"}), 400
            if val < 1:
                return jsonify({"status": 400, "title": "Bad Request",
                                 "detail": "parallelism must be >= 1"}), 400
        settings_store.set("tofu.parallelism", val)

    return jsonify(settings_store.get_prefixed("tofu"))


@settings_bp.get("/v1/settings/network")
def get_network_settings():
    err = _auth()
    if err: return err
    settings = settings_store.get_prefixed("network")
    settings.setdefault("bridge_subnet_octet", 100)
    return jsonify(settings)


@settings_bp.put("/v1/settings/network")
def update_network_settings():
    err = _auth()
    if err: return err
    body = request.get_json(force=True) or {}

    bridge_rebuild = None
    if "bridge_subnet_octet" in body:
        val = body["bridge_subnet_octet"]
        try:
            val = int(val)
        except (TypeError, ValueError):
            return jsonify({"status": 400, "title": "Bad Request",
                             "detail": "bridge_subnet_octet must be an integer"}), 400
        # 0 and 255 are broadcast/network-reserved for a /24; kept out of
        # range rather than merely discouraged.
        if not (1 <= val <= 254):
            return jsonify({"status": 400, "title": "Bad Request",
                             "detail": "bridge_subnet_octet must be between 1 and 254"}), 400
        old_val = settings_store.get("network.bridge_subnet_octet", 100)
        settings_store.set("network.bridge_subnet_octet", val)
        # Keep live state from ever silently diverging from this setting
        # again — see _rebuild_bridge()'s own docstring for the incident
        # this closes. Only when it's an actual change: re-running the
        # rebuild on every PUT that merely repeats the current value
        # would needlessly bounce a live bridge (and any guests on it).
        if val != old_val:
            bridge_rebuild = _rebuild_bridge(val)

    settings = settings_store.get_prefixed("network")
    settings.setdefault("bridge_subnet_octet", 100)
    if bridge_rebuild is not None:
        settings["bridge_rebuild"] = bridge_rebuild
    return jsonify(settings)


@settings_bp.get("/v1/settings/discovery")
def get_discovery_settings():
    err = _auth()
    if err: return err
    settings = settings_store.get_prefixed("discovery")
    settings.setdefault("enabled", False)
    settings["advertising"] = discovery.is_advertising()
    return jsonify(settings)


@settings_bp.put("/v1/settings/discovery")
def update_discovery_settings():
    err = _auth()
    if err: return err
    body = request.get_json(force=True) or {}

    if "enabled" in body:
        enabled = bool(body["enabled"])
        settings_store.set("discovery.enabled", enabled)
        # Applied live, no restart needed — a host that just turned this
        # off should go silent on the wire immediately, not at the next
        # process restart; a host that just turned it on should start
        # being discoverable right away. The peer-facing listener
        # (api/peer_listener.py) is tied to this same single toggle,
        # not a separate one — "opt in to participate in peering" means
        # the port doesn't even exist to scan or abuse until a human on
        # this machine explicitly turns this on, matching the "we're
        # opening a port on a laptop to abuse" concern this whole
        # feature was designed around.
        if enabled:
            discovery.advertise()
            peer_listener.start(discovery.peer_listener_port())
        else:
            discovery.stop_advertise()
            peer_listener.stop()

    settings = settings_store.get_prefixed("discovery")
    settings.setdefault("enabled", False)
    settings["advertising"] = discovery.is_advertising()
    return jsonify(settings)
