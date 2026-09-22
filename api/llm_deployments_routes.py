"""LLM Performance page — "live deployments" registry API.

Same two-trust-domain split as llm_examples_routes.py:

- POST /v1/llm-deployments/register is the only guest-reachable route —
  reachable from a coordinator VM via the dedicated, always-on
  examples_listener.py bind, gated there to exactly this endpoint by
  name (server.py's own before_request gate), same mechanism
  llm_examples_routes.py's own EXAMPLES_REACHABLE_ENDPOINTS already
  uses. A leaked/guessed ingestion token still can't reach anything
  else through that bind.
- GET /v1/llm-deployments and DELETE .../<id> are dashboard-only
  (127.0.0.1:8080), gated by the normal admin cloudcore_api_token.

Ingestion auth is intentionally separate from require_auth (same
circular-import reasoning peers_routes.py's own _peer_inbound_auth
docstring documents — this module is imported before server.py defines
require_auth).
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request

from flask import Blueprint, jsonify, request

import llm_deployments_store
import store

llm_deployments_bp = Blueprint("llm_deployments", __name__)

API_TOKEN = os.environ.get("CLOUDCORE_API_TOKEN", "dev-token")

# Named here (not inline in server.py) so server.py's own before_request
# gate can import a stable set, same convention as
# llm_examples_routes.py's own EXAMPLES_REACHABLE_ENDPOINTS.
LLM_DEPLOYMENTS_REACHABLE_ENDPOINTS = {
    "llm_deployments.register_deployment_endpoint",
}

# A registered deployment that's mid-rebuild or was torn down without
# ever unregistering must never hang the whole page waiting on it —
# short enough that a handful of stale/unreachable rows still costs a
# real page load well under a second.
_POLL_TIMEOUT_S = 3


def _admin_auth():
    token = request.headers.get("Authorization", "").removeprefix("Bearer ") \
            or request.args.get("token", "")
    if token != API_TOKEN:
        return jsonify({"status": 401, "title": "Unauthorized"}), 401
    return None


def _register_auth() -> bool:
    """Coordinator guests authenticate with the same shared token every
    template's cloudcore_api_token variable already carries — not a new
    secret to provision. Same as llm_examples_routes.py's own
    _ingest_auth."""
    auth = request.headers.get("Authorization", "")
    return auth == f"Bearer {API_TOKEN}"


def _poll_live(deployment: dict) -> dict:
    """Resolves `deployment["name"]` to its current CloudCore instance
    and, if it's running with a known address, fetches its live stats.
    Never raises — an unreachable/torn-down/rebuilding deployment just
    comes back {"live": False, ...} so one bad row can't blank the rest
    of the page."""
    inst = store.find_instance_by_name(deployment["name"])
    if not inst or inst.status.value != "running" or not inst.private_ip:
        return {**deployment, "live": False, "reachable": False, "stats": None}
    url = f"http://{inst.private_ip}:{deployment['port']}{deployment['stats_path']}"
    try:
        req = urllib.request.Request(url, method="GET")
        with urllib.request.urlopen(req, timeout=_POLL_TIMEOUT_S) as resp:
            stats = json.loads(resp.read())
        return {**deployment, "live": True, "reachable": True,
                "instance_id": inst.id, "private_ip": inst.private_ip, "stats": stats}
    except (urllib.error.URLError, OSError, ValueError):
        return {**deployment, "live": True, "reachable": False,
                "instance_id": inst.id, "private_ip": inst.private_ip, "stats": None}


@llm_deployments_bp.post("/v1/llm-deployments/register")
def register_deployment_endpoint():
    if not _register_auth():
        return jsonify({"status": 401, "title": "Unauthorized"}), 401
    body = request.get_json(force=True) or {}
    name = (body.get("name") or "").strip()
    example = (body.get("example") or "").strip()
    port = body.get("port")
    stats_path = (body.get("stats_path") or "").strip()
    if not name or not example or not port or not stats_path:
        return jsonify({"status": 400, "title": "Bad Request",
                         "detail": "name, example, port and stats_path are all required"}), 400
    deployment = llm_deployments_store.register_deployment(name, example, int(port), stats_path)
    return jsonify(deployment), 201


@llm_deployments_bp.get("/v1/llm-deployments")
def list_deployments():
    err = _admin_auth()
    if err: return err
    items = [_poll_live(d) for d in llm_deployments_store.list_deployments()]
    return jsonify({"items": items})


@llm_deployments_bp.delete("/v1/llm-deployments/<deployment_id>")
def delete_deployment(deployment_id):
    err = _admin_auth()
    if err: return err
    if not llm_deployments_store.delete_deployment(deployment_id):
        return jsonify({"status": 404, "title": "Not Found"}), 404
    return "", 204
