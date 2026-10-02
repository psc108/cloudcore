"""Hardware inventory API route — GET /v1/hardware. See hw_info.py's
own module docstring for what this covers and why it's a separate
concern from host_stats.py's live performance numbers."""
from __future__ import annotations

import os
from flask import Blueprint, jsonify, request

import hw_info
import cc_token

hw_bp = Blueprint("hw", __name__)

API_TOKEN = cc_token.master_token()


def _auth():
    token = request.headers.get("Authorization", "").removeprefix("Bearer ") \
            or request.args.get("token", "")
    if token != API_TOKEN:
        return jsonify({"status": 401, "title": "Unauthorized"}), 401
    return None


@hw_bp.get("/v1/hardware")
def get_hardware():
    err = _auth()
    if err: return err
    return jsonify(hw_info.collect())
