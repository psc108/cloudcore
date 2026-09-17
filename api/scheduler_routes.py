"""Scheduler API routes — /v1/schedules. See api/scheduler.py for the
execution engine and api/croncalc.py for recurrence handling.
"""
from __future__ import annotations

import os

from flask import Blueprint, jsonify, request

import build_engine
import croncalc
import scheduler
import tofu_engine

scheduler_bp = Blueprint("scheduler", __name__)

API_TOKEN = os.environ.get("CLOUDCORE_API_TOKEN", "dev-token")


def _auth():
    token = request.headers.get("Authorization", "").removeprefix("Bearer ") \
            or request.args.get("token", "")
    if token != API_TOKEN:
        return jsonify({"status": 401, "title": "Unauthorized"}), 401
    return None


@scheduler_bp.get("/v1/schedules/templates")
def list_all_templates():
    err = _auth()
    if err: return err
    items = []
    for t in tofu_engine.list_templates():
        items.append({**t, "engine": "tofu"})
    for t in build_engine.list_templates():
        items.append({**t, "engine": "ansible"})
    return jsonify({"items": items})


@scheduler_bp.get("/v1/schedules")
def list_schedules():
    err = _auth()
    if err: return err
    return jsonify({"items": scheduler.list_schedules()})


@scheduler_bp.post("/v1/schedules")
def create_schedule():
    err = _auth()
    if err: return err
    body = request.get_json(force=True) or {}
    name = (body.get("name") or "").strip()
    kind = body.get("kind")
    if not name:
        return jsonify({"status": 400, "title": "Bad Request", "detail": "name is required"}), 400
    if kind not in ("build", "llm_ingest"):
        return jsonify({"status": 400, "title": "Bad Request",
                         "detail": "kind must be 'build' or 'llm_ingest'"}), 400
    recurrence = body.get("recurrence")
    if not recurrence or "mode" not in recurrence:
        return jsonify({"status": 400, "title": "Bad Request",
                         "detail": "recurrence.mode is required"}), 400

    if kind == "llm_ingest":
        engine, template = "tofu", "distributed-llm"
        pool = (body.get("var_overrides") or {}).get("worker_peer_pool")
        if not pool or not isinstance(pool, list):
            return jsonify({"status": 400, "title": "Bad Request",
                             "detail": "var_overrides.worker_peer_pool (a non-empty list) is required for kind='llm_ingest' — "
                                       "which of these peers actually run each cycle is decided automatically by "
                                       "their current traffic light, not fixed at creation time"}), 400
    else:
        engine = body.get("engine")
        template = body.get("template")
        if engine not in ("tofu", "ansible") or not template:
            return jsonify({"status": 400, "title": "Bad Request",
                             "detail": "engine ('tofu'|'ansible') and template are required for kind='build'"}), 400

    try:
        schedule = scheduler.create_schedule(
            name=name, kind=kind, engine=engine, template=template,
            var_overrides=body.get("var_overrides", {}),
            recurrence=recurrence, created_by="ui")
    except (croncalc.InvalidCron, ValueError) as e:
        return jsonify({"status": 400, "title": "Bad Request", "detail": str(e)}), 400
    return jsonify(schedule), 201


@scheduler_bp.get("/v1/schedules/<schedule_id>")
def get_schedule(schedule_id):
    err = _auth()
    if err: return err
    s = scheduler.get_schedule(schedule_id)
    if not s:
        return jsonify({"status": 404, "title": "Not Found"}), 404
    return jsonify(s)


@scheduler_bp.put("/v1/schedules/<schedule_id>")
def update_schedule(schedule_id):
    err = _auth()
    if err: return err
    if not scheduler.get_schedule(schedule_id):
        return jsonify({"status": 404, "title": "Not Found"}), 404
    body = request.get_json(force=True) or {}
    fields = {}
    if "name" in body:
        fields["name"] = body["name"]
    if "enabled" in body:
        fields["enabled"] = bool(body["enabled"])
    if "var_overrides" in body:
        fields["var_overrides"] = body["var_overrides"]
    if "recurrence" in body:
        fields["recurrence"] = body["recurrence"]
    try:
        s = scheduler.update_schedule(schedule_id, **fields)
    except (croncalc.InvalidCron, ValueError) as e:
        return jsonify({"status": 400, "title": "Bad Request", "detail": str(e)}), 400
    return jsonify(s)


@scheduler_bp.delete("/v1/schedules/<schedule_id>")
def delete_schedule(schedule_id):
    err = _auth()
    if err: return err
    if not scheduler.delete_schedule(schedule_id):
        return jsonify({"status": 404, "title": "Not Found"}), 404
    return "", 204


@scheduler_bp.get("/v1/schedules/<schedule_id>/runs")
def get_runs(schedule_id):
    err = _auth()
    if err: return err
    if not scheduler.get_schedule(schedule_id):
        return jsonify({"status": 404, "title": "Not Found"}), 404
    return jsonify({"items": scheduler.list_runs(schedule_id)})


@scheduler_bp.post("/v1/schedules/<schedule_id>/run-now")
def run_now(schedule_id):
    err = _auth()
    if err: return err
    if not scheduler.run_now(schedule_id):
        return jsonify({"status": 404, "title": "Not Found"}), 404
    return jsonify({"status": "triggered"}), 202
