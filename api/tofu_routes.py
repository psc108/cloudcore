"""OpenTofu Build Manager API routes."""

from __future__ import annotations

import json
import os
from flask import Blueprint, jsonify, request, Response

import tofu_engine

tofu_bp = Blueprint("tofu", __name__)

API_TOKEN = os.environ.get("CLOUDCORE_API_TOKEN", "dev-token")


def _auth():
    token = request.headers.get("Authorization", "").removeprefix("Bearer ") \
            or request.args.get("token", "")
    if token != API_TOKEN:
        return jsonify({"status": 401, "title": "Unauthorized"}), 401
    return None


@tofu_bp.get("/v1/tofu/templates")
def list_templates():
    err = _auth()
    if err: return err
    return jsonify({"items": tofu_engine.list_templates()})


@tofu_bp.get("/v1/tofu/templates/<dir_name>/vars")
def get_template_vars(dir_name):
    err = _auth()
    if err: return err
    try:
        schema = tofu_engine.extract_template_vars(dir_name)
        return jsonify({"vars": schema})
    except FileNotFoundError as e:
        return jsonify({"status": 404, "title": "Not Found", "detail": str(e)}), 404


@tofu_bp.post("/v1/tofu/builds")
def submit_build():
    err = _auth()
    if err: return err
    body = request.get_json(force=True) or {}
    template = body.get("template", "").strip()
    if not template:
        return jsonify({"status": 400, "title": "Bad Request", "detail": "template is required"}), 400
    build = tofu_engine.submit_build(template, body.get("vars", {}), body.get("created_by", "ui"))
    return jsonify(_summary(build)), 202


@tofu_bp.get("/v1/tofu/builds")
def list_builds():
    err = _auth()
    if err: return err
    return jsonify({"items": [_summary(b) for b in tofu_engine.list_builds()]})


@tofu_bp.get("/v1/tofu/builds/<build_id>")
def get_build(build_id):
    err = _auth()
    if err: return err
    build = tofu_engine.get_build(build_id)
    if not build:
        return jsonify({"status": 404, "title": "Not Found", "detail": "Build not found"}), 404
    return jsonify({**_summary(build), "log": build["log"],
                    "var_overrides": build["var_overrides"],
                    "provisioned": build.get("provisioned") or []})


@tofu_bp.get("/v1/tofu/builds/<build_id>/log")
def get_build_log(build_id):
    err = _auth()
    if err: return err
    build = tofu_engine.get_build(build_id)
    if not build:
        return jsonify({"status": 404, "title": "Not Found", "detail": "Build not found"}), 404

    if "text/event-stream" in request.headers.get("Accept", ""):
        def _stream():
            import time
            sent = 0
            while True:
                lines = build["log"]
                while sent < len(lines):
                    yield f"data: {json.dumps(lines[sent])}\n\n"
                    sent += 1
                if build["status"] not in ("pending", "running"):
                    yield f"data: {json.dumps({'__done__': True, 'status': build['status']})}\n\n"
                    break
                time.sleep(0.5)
        return Response(_stream(), mimetype="text/event-stream",
                        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    return jsonify({"log": build["log"], "status": build["status"]})


@tofu_bp.delete("/v1/tofu/builds/<build_id>")
def destroy_build(build_id):
    err = _auth()
    if err: return err
    build = tofu_engine.get_build(build_id)
    if not build:
        return jsonify({"status": 404, "title": "Not Found", "detail": "Build not found"}), 404
    if build["status"] == "destroyed":
        return jsonify({"status": 409, "title": "Already destroyed"}), 409

    try:
        success, logs = tofu_engine.run_tofu_destroy(build_id)
    except Exception as e:
        return jsonify({"status": 500, "title": "Destroy failed", "detail": str(e)}), 500

    http_code = 200 if success else 500
    return jsonify({"success": success, "log": logs}), http_code


def _summary(b: dict) -> dict:
    return {
        "id": b["id"],
        "template": b["template"],
        "status": b["status"],
        "created_at": b["created_at"],
        "started_at": b["started_at"],
        "finished_at": b["finished_at"],
        "created_by": b["created_by"],
        "exit_code": b["exit_code"],
        "log_lines": len(b["log"]),
        "provisioned_count": len(b.get("provisioned") or []),
    }
