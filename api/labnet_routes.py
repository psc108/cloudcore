"""Lab-network pairs on this host -- /v1/labnet/pairs (F2).

On the isolated lab bridge (api/setup-lab-network.sh), lab VMs can't reach
each other at layer 2 unless registered as a pair: a run's target and its
prober. The lab-VM broker registers them here -- on whichever host the VMs
run, locally or through the peer link -- and this calls the one root helper
setup-lab-network.sh installed (/usr/local/sbin/cloudcore-labnet, allowed by
a sudoers entry for that script only).

Auth: this host's master token, or an approved peer's own token (how the
broker on another host arrives). Peer-reachable via server.py's allowlist.
"""

from __future__ import annotations

import hmac
import ipaddress
import subprocess

from flask import Blueprint, jsonify, request

import cc_token
import peers_store

labnet_bp = Blueprint("labnet", __name__)
HELPER = "/usr/local/sbin/cloudcore-labnet"
LABNET_PEER_REACHABLE = {"labnet.add_pair", "labnet.remove_ip"}


def _problem(status: int, title: str, detail: str):
    return jsonify({"status": status, "title": title, "detail": detail}), status


@labnet_bp.before_request
def _require_auth():
    auth = request.headers.get("Authorization", "")
    token = auth.removeprefix("Bearer ") if auth.startswith("Bearer ") else ""
    if token and (hmac.compare_digest(token, cc_token.master_token())
                  or peers_store.find_peer_by_local_token(token)):
        return None
    return _problem(401, "Unauthorized", "this host's API token or an approved peer's token is required")


def _ip(value) -> str:
    try:
        return str(ipaddress.IPv4Address(str(value)))
    except ValueError:
        return ""


def helper(*args: str) -> tuple[bool, str]:
    """Run the root helper. It re-validates every address itself."""
    try:
        r = subprocess.run(["sudo", "-n", HELPER, *args], capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.TimeoutExpired) as e:
        return False, str(e)
    return r.returncode == 0, (r.stderr or r.stdout).strip()


@labnet_bp.post("/v1/labnet/pairs")
def add_pair():
    body = request.get_json(force=True, silent=True) or {}
    a, b = _ip(body.get("a")), _ip(body.get("b"))
    if not a or not b:
        return _problem(400, "Bad Request", "a and b must be IPv4 addresses")
    ok, out = helper("pair", "add", a, b)
    return (jsonify({"paired": [a, b]}), 201) if ok else _problem(400, "Pair Refused", out or "helper failed")


@labnet_bp.delete("/v1/labnet/pairs")
def remove_ip():
    ip = _ip(request.args.get("ip"))
    if not ip:
        return _problem(400, "Bad Request", "ip must be an IPv4 address")
    ok, out = helper("unpair", ip)
    return ("", 204) if ok else _problem(400, "Unpair Failed", out or "helper failed")
