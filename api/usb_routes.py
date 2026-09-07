"""USB device discovery API routes — /v1/usb-devices

Read-only: devices aren't created or destroyed through this API, they're
discovered live from the host (see usb.py). Attachment happens through
the instances endpoints (usb_device_ids on POST/PUT /v1/instances).
"""
from __future__ import annotations

import functools
import os

from flask import Blueprint, request, jsonify, abort

import usb

usb_bp = Blueprint("usb", __name__)

# Duplicated locally rather than imported from server.py: server.py imports
# this blueprint before require_auth is defined in its own namespace, so
# `from server import require_auth` here would be a circular import at
# module-load time. Every other blueprint in this app duplicates its own
# small helpers (_problem, etc.) for the same reason — same convention.
#
# Note: nfs_routes.py and sg_routes.py (and build_manager_routes.py,
# editor_routes.py, about_routes.py, tofu_routes.py) don't apply auth at
# all today — a pre-existing gap, not something to replicate here.
_API_TOKEN = os.environ.get("CLOUDCORE_API_TOKEN", "dev-token")


def require_auth(f):
    @functools.wraps(f)
    def wrapper(*args, **kwargs):
        auth = request.headers.get("Authorization", "")
        if auth != f"Bearer {_API_TOKEN}":
            abort(401)
        return f(*args, **kwargs)
    return wrapper


@usb_bp.get("/v1/usb-devices")
@require_auth
def list_usb_devices():
    return jsonify({"items": usb.list_usb_devices()})
