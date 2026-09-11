"""Settings API routes. Scoped by category (currently just 'tofu') — the
underlying store (settings_store.py) is a generic key/value table, so a new
category is just a new route + validation function, no schema change."""

from __future__ import annotations

import os
from flask import Blueprint, jsonify, request

import settings_store

settings_bp = Blueprint("settings", __name__)

API_TOKEN = os.environ.get("CLOUDCORE_API_TOKEN", "dev-token")


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
