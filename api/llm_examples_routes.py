"""llm-chat Phase 3 — central learning corpus API.

Two trust domains on these routes, mirroring peers_routes.py's own
split between dashboard-only and peer-reachable endpoints:

- POST /v1/llm-chat/examples and GET /v1/llm-chat/examples/published
  are the only two guest-reachable routes — reachable from a
  coordinator VM (local or peer-placed) via the dedicated, always-on
  examples_listener.py bind, gated there to exactly these two by
  endpoint name (server.py's own before_request gate), same
  SERVER_PORT-based mechanism peer_listener.py already uses. A
  leaked/guessed ingestion token still can't reach anything else
  through that bind.
- GET /v1/llm-chat/examples, PUT .../<id>, GET .../export are
  dashboard-only (127.0.0.1:8080), gated by the normal admin
  cloudcore_api_token.

Ingestion auth is intentionally separate from require_auth (same
circular-import reasoning peers_routes.py's own _peer_inbound_auth
docstring documents — this module is imported before server.py
defines require_auth).
"""
from __future__ import annotations

import os

from flask import Blueprint, Response, jsonify, request

import llm_examples_store

examples_bp = Blueprint("llm_examples", __name__)

API_TOKEN = os.environ.get("CLOUDCORE_API_TOKEN", "dev-token")

# Named here (not inline in server.py) so server.py's own before_request
# gate can import a stable set, same convention as peers_routes.py's
# PEER_REACHABLE_ENDPOINTS.
EXAMPLES_REACHABLE_ENDPOINTS = {
    "llm_examples.ingest_example",
    "llm_examples.list_published",
}


def _admin_auth():
    token = request.headers.get("Authorization", "").removeprefix("Bearer ") \
            or request.args.get("token", "")
    if token != API_TOKEN:
        return jsonify({"status": 401, "title": "Unauthorized"}), 401
    return None


def _ingest_auth() -> bool:
    """Coordinator guests authenticate with the same shared token every
    template's cloudcore_api_token variable already carries (threaded
    into verify_proxy.py's own environment at cloud-init time) — not a
    new secret to provision."""
    auth = request.headers.get("Authorization", "")
    return auth == f"Bearer {API_TOKEN}"


@examples_bp.post("/v1/llm-chat/examples")
def ingest_example():
    if not _ingest_auth():
        return jsonify({"status": 401, "title": "Unauthorized"}), 401
    body = request.get_json(force=True) or {}
    for required in ("model_filename", "prompt", "generated_code"):
        if not body.get(required):
            return jsonify({"status": 400, "title": "Bad Request",
                             "detail": f"'{required}' is required"}), 400
    example_id = llm_examples_store.record_example(
        source=body.get("source") or "llm-chat-coordinator",
        build_id=body.get("build_id") or "",
        model_filename=body["model_filename"],
        prompt=body["prompt"],
        generated_code=body["generated_code"],
        exec_stdout=body.get("exec_stdout") or "",
        exec_stderr=body.get("exec_stderr") or "",
        exec_exit_code=body.get("exec_exit_code"),
        passed=bool(body.get("passed")),
        fix_explanation=body.get("fix_explanation") or "",
        fixed_code=body.get("fixed_code") or "",
        fix_exec_stdout=body.get("fix_exec_stdout") or "",
        fix_exec_stderr=body.get("fix_exec_stderr") or "",
        fix_passed=body.get("fix_passed"),
    )
    return jsonify({"id": example_id}), 201


@examples_bp.get("/v1/llm-chat/examples/published")
def list_published():
    """The one unauthenticated route in this codebase, by design —
    published-only, read-only, never exposes pending/hidden rows or
    anything beyond what an instructor has explicitly curated for
    students."""
    limit = min(int(request.args.get("limit", 100)), 500)
    return jsonify({"items": llm_examples_store.list_examples("published", limit)})


@examples_bp.get("/v1/llm-chat/examples")
def list_all_examples():
    err = _admin_auth()
    if err: return err
    status = request.args.get("status")
    limit = min(int(request.args.get("limit", 200)), 1000)
    return jsonify({"items": llm_examples_store.list_examples(status, limit)})


@examples_bp.put("/v1/llm-chat/examples/<example_id>")
def update_example_status(example_id):
    err = _admin_auth()
    if err: return err
    body = request.get_json(force=True) or {}
    status = body.get("status")
    if status not in llm_examples_store.VALID_STATUSES:
        return jsonify({"status": 400, "title": "Bad Request",
                         "detail": f"status must be one of {llm_examples_store.VALID_STATUSES}"}), 400
    if not llm_examples_store.set_status(example_id, status):
        return jsonify({"status": 404, "title": "Not Found"}), 404
    return jsonify({"id": example_id, "status": status})


@examples_bp.get("/v1/llm-chat/examples/export")
def export_examples():
    err = _admin_auth()
    if err: return err
    return Response(llm_examples_store.export_jsonl(), mimetype="application/x-ndjson",
                     headers={"Content-Disposition": "attachment; filename=llm-verification-examples.jsonl"})
