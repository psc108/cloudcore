"""This host's own performance stats — /v1/system/stats. See
api/host_stats.py for what's actually collected and why."""
from __future__ import annotations

from flask import Blueprint, jsonify

import host_stats

stats_bp = Blueprint("stats", __name__)


@stats_bp.get("/v1/system/stats")
def get_system_stats():
    return jsonify(host_stats.collect())
