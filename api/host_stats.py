"""This host's own current performance stats — CPU load, memory,
disk, and running-instance count. Pure stdlib (/proc, os, shutil) —
same "no new dependency where stdlib already covers it" discipline
peer_client.py already applies (urllib over requests).

Used two ways: GET /v1/system/stats (api/stats_routes.py) for this
host's own numbers, and GET /v1/peers/<id>/stats (api/peers_routes.py,
proxied through the peer-listener bind) for a paired peer's — letting
a user compare hosts before deciding where to place a resource,
per direct request ("we need to be able to collect performance
statistics from each peer in order to understand which is overloaded
for our intended resource placement and which can tolerate it").
"""
from __future__ import annotations

import os
import shutil

import compute
import store
from models import now_iso


def _cpu_stats() -> dict:
    cores = os.cpu_count() or 1
    try:
        load_1m, load_5m, load_15m = os.getloadavg()
    except OSError:
        load_1m = load_5m = load_15m = 0.0
    return {
        "cores": cores,
        "load_1m": round(load_1m, 2),
        "load_5m": round(load_5m, 2),
        "load_15m": round(load_15m, 2),
        # Load average isn't itself a percentage — dividing by core count
        # is the standard normalization (a load of 1.0 on 1 core is "fully
        # busy", same load on 8 cores is "12.5% busy"). Uncapped past 100
        # deliberately (a genuinely overloaded host should show >100%,
        # not be clamped into looking merely full).
        "load_pct_1m": round((load_1m / cores) * 100, 1),
    }


def _memory_stats() -> dict:
    # /proc/meminfo, not psutil — this platform has no existing Python
    # dependency for system metrics, and every number needed here is a
    # single line of a file every Linux host already has.
    info = {}
    try:
        with open("/proc/meminfo") as f:
            for line in f:
                key, _, rest = line.partition(":")
                value_kb = rest.strip().split()[0]
                info[key] = int(value_kb)
    except (OSError, ValueError, IndexError):
        return {"total_mb": 0, "available_mb": 0, "used_pct": 0.0}

    total_kb = info.get("MemTotal", 0)
    # MemAvailable (kernel's own "usable without swapping" estimate,
    # since 3.14) is what actually matters for "can this host take more
    # load" — MemFree alone is misleadingly low on a healthy Linux host
    # that's simply using free RAM for page cache.
    available_kb = info.get("MemAvailable", info.get("MemFree", 0))
    used_pct = ((total_kb - available_kb) / total_kb * 100) if total_kb else 0.0
    return {
        "total_mb": round(total_kb / 1024),
        "available_mb": round(available_kb / 1024),
        "used_pct": round(used_pct, 1),
    }


def _disk_stats() -> dict:
    # Wherever VM disk images actually live (api/instances/), not root —
    # what matters for "can this host take another VM's disk" is free
    # space on THAT filesystem specifically, which may differ from /.
    path = compute.INSTANCES_DIR
    path.mkdir(parents=True, exist_ok=True)
    total, used, free = shutil.disk_usage(path)
    gb = 1024 ** 3
    return {
        "total_gb": round(total / gb, 1),
        "used_gb": round(used / gb, 1),
        "free_gb": round(free / gb, 1),
        "used_pct": round((used / total * 100) if total else 0.0, 1),
    }


def _instance_stats() -> dict:
    instances = store.list_instances()
    running = sum(1 for i in instances if i.status.value == "running")
    return {"count": len(instances), "running": running}


def collect() -> dict:
    """This host's own current stats — safe to call often, every number
    here is a cheap local read (no subprocess, no network)."""
    return {
        "cpu": _cpu_stats(),
        "memory": _memory_stats(),
        "disk": _disk_stats(),
        "instances": _instance_stats(),
        "collected_at": now_iso(),
    }
