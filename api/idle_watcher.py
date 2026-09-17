"""Idle-timeout auto-shutdown for long-running, expensive-to-cold-start
builds (today: examples/llm-chat, examples/distributed-llm — anything
exposing an http_port variable and a load balancer) — per direct
request: "it takes ~4-8 minutes to get the llm ready... [would it
help to] keep it running constantly?" followed by "make it a 60
minute default but allow the user to choose in 30 minute increments."
Rather than run forever (ties up both machines' RAM/CPU indefinitely)
or tear down after every single chat (re-pays the whole cold-start
cost every time), this watches the coordinator's own real client
traffic via its load balancer's HAProxy stats and destroys the build
once nothing has actually used it for the configured window.

Uses HAProxy's own per-server 'lastsess' stat (seconds since the last
real proxied session) rather than polling the coordinator directly —
confirmed live that HAProxy's own active health-check traffic does
NOT count as a session (stot stayed flat across a clean no-traffic
window while lastsess climbed in real time), so this can't mistake
"the health check is still passing" for "someone is using it."

In-memory only, like the rest of this project's own build-tracking —
does not survive a cloudcore-api restart. Left simple deliberately:
making it durable across restarts would need DB persistence and
reconciliation-on-boot (the same shape api/server.py's own reconcile()
already has for VM state), which is more machinery than this feature
asked for. A restart mid-idle-window means the auto-shutdown silently
stops applying to that specific build — it won't be destroyed, but it
also won't be harmed; someone restarting cloudcore-api already knows
they did it (and, since KillMode=process, the build itself keeps
running through that restart regardless — see F-098).
"""
from __future__ import annotations

import socket
import threading
import time
from pathlib import Path

POLL_INTERVAL_S = 60
_LB_DIR = Path(__file__).parent / "lb"

# 30-minute increments, per direct request — the UI never offers
# anything finer-grained than this.
MIN_MINUTES = 30
STEP_MINUTES = 30
DEFAULT_MINUTES = 60


def _read_stat(lb_id: str) -> str | None:
    sock_path = _LB_DIR / f"{lb_id}.sock"
    if not sock_path.exists():
        return None
    try:
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(5)
        s.connect(str(sock_path))
        s.sendall(b"show stat\n")
        data = b""
        while True:
            chunk = s.recv(4096)
            if not chunk:
                break
            data += chunk
        s.close()
        return data.decode()
    except OSError:
        return None


def get_backend_lastsess(lb_id: str, backend_name: str) -> int | None:
    """Seconds since the target group's own coordinator server last
    handled a real client session, or None if it can't be read right
    now (LB not running, socket gone, etc.) — treated by the caller as
    "can't tell, don't act", never as "definitely idle"."""
    raw = _read_stat(lb_id)
    if not raw:
        return None
    lines = raw.split("\n")
    if not lines:
        return None
    header = lines[0].lstrip("#").strip().split(",")
    for line in lines[1:]:
        if not line.strip():
            continue
        row = dict(zip(header, line.split(",")))
        if row.get("pxname") == backend_name and row.get("svname") not in ("FRONTEND", "BACKEND"):
            try:
                val = int(row.get("lastsess", "-1"))
            except ValueError:
                return None
            return val if val >= 0 else None
    return None


def _watch(build_id: str, engine_module, lb_id: str, backend_name: str,
           idle_timeout_minutes: int) -> None:
    threshold_s = idle_timeout_minutes * 60
    print(f"[idle_watcher] armed for build {build_id}: {idle_timeout_minutes}min threshold")
    while True:
        time.sleep(POLL_INTERVAL_S)
        build = engine_module.get_build(build_id)
        if not build or build["status"] != "success":
            print(f"[idle_watcher] build {build_id} no longer active — stopping watch")
            return
        idle_s = get_backend_lastsess(lb_id, backend_name)
        if idle_s is None:
            continue
        if idle_s >= threshold_s:
            print(f"[idle_watcher] build {build_id} idle {idle_s}s >= {threshold_s}s — auto-destroying")
            try:
                engine_module.run_tofu_destroy(build_id)
            except Exception as e:
                print(f"[idle_watcher] WARNING: auto-destroy failed for {build_id}: {e}")
            return


def start(build_id: str, engine_module, lb_id: str, backend_name: str,
          idle_timeout_minutes: int) -> None:
    threading.Thread(
        target=_watch,
        args=(build_id, engine_module, lb_id, backend_name, idle_timeout_minutes),
        daemon=True,
    ).start()
