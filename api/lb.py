from __future__ import annotations

import os
import signal
import subprocess
import textwrap
from pathlib import Path
from typing import Optional

from models import LoadBalancer

_LB_DIR = Path(__file__).parent / "lb"
_LB_DIR.mkdir(exist_ok=True)

# Port range for LB listeners
_LB_PORT_START = 8200
_LB_PORT_END = 8299


def _cfg_path(lb_id: str) -> Path:
    return _LB_DIR / f"{lb_id}.cfg"


def _pid_path(lb_id: str) -> Path:
    return _LB_DIR / f"{lb_id}.pid"


def _sock_path(lb_id: str) -> Path:
    return _LB_DIR / f"{lb_id}.sock"


def _free_port(start: int, end: int) -> int:
    import socket
    for port in range(start, end + 1):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            if s.connect_ex(("127.0.0.1", port)) != 0:
                return port
    raise RuntimeError(f"No free port in range {start}-{end}")


def _write_config(lb: LoadBalancer, listen_port: int, vpc_instances=None) -> Path:
    mode = "http" if lb.type == "application" else "tcp"

    # Use explicit backends first; fall back to auto-discovering running SLIRP instances
    # in the same VPC when no backends have been registered via the API.
    effective_backends = lb.backends
    # Auto-discover backends only for HTTP/application LBs.
    # TCP/network LBs require explicit backend registration because the service
    # port is workload-specific — there is no sensible default to assume.
    if not effective_backends and vpc_instances and lb.type == "application":
        effective_backends = []
        for inst in vpc_instances:
            if inst.http_host_port:
                # SLIRP: host port forwarding is set up by the QEMU process at launch
                # and stays live for the process lifetime regardless of libvirt state.
                effective_backends.append({
                    "name": inst.name,
                    "address": "127.0.0.1",
                    "port": inst.http_host_port,
                })
            elif inst.status.value == "running" and inst.private_ip:
                # Bridge networking: guest IP must be confirmed reachable.
                effective_backends.append({
                    "name": inst.name,
                    "address": inst.private_ip,
                    "port": 80,
                })

    backends_cfg = "\n".join(
        f"    server {b['name']} {b['address']}:{b['port']} check"
        for b in effective_backends
    ) if effective_backends else "    # no backends configured yet"

    # Health check options for backend
    hc = lb.health_check or {}
    hc_opts = ""
    if hc and mode == "http":
        path     = hc.get("path", "/")
        interval = hc.get("interval", 30)
        rise     = hc.get("healthy_threshold", 2)
        fall     = hc.get("unhealthy_threshold", 3)
        hc_opts  = f"    option httpchk GET {path}\n    default-server inter {interval}s rise {rise} fall {fall}\n"

    # Build frontend blocks — one per listener, plus the legacy listen_port if no listeners
    frontends = ""
    if lb.listeners:
        for lst in lb.listeners:
            lst_port = lst["port"]
            lst_id   = lst["id"].replace("-", "")[:8]
            frontends += f"""
frontend {lb.name}-{lst_id}
    bind 127.0.0.1:{lst_port}
    default_backend {lb.name}-back
"""
    else:
        frontends = f"""
frontend {lb.name}-front
    bind 127.0.0.1:{listen_port}
    default_backend {lb.name}-back
"""

    http_opts = "    option  forwardfor\n    option  http-server-close\n" if mode == "http" else ""
    cfg = textwrap.dedent(f"""\
        global
            daemon
            pidfile {_pid_path(lb.id)}
            stats socket {_sock_path(lb.id)} mode 660 level admin

        defaults
            mode {mode}
            timeout connect 5s
            timeout client  30s
            timeout server  30s
    """) + http_opts + frontends + f"""
backend {lb.name}-back
    balance roundrobin
{hc_opts}{backends_cfg}
"""
    path = _cfg_path(lb.id)
    path.write_text(cfg)
    return path


def start(lb: LoadBalancer, vpc_instances=None) -> int:
    """Start HAProxy for this LB. Returns the listen port."""
    listen_port = lb.listen_port or _free_port(_LB_PORT_START, _LB_PORT_END)
    cfg = _write_config(lb, listen_port, vpc_instances=vpc_instances)
    subprocess.run(["haproxy", "-f", str(cfg), "-D"], check=True)
    return listen_port


def reload(lb: LoadBalancer, vpc_instances=None) -> None:
    """Reload HAProxy config without dropping connections (if running)."""
    pid_path = _pid_path(lb.id)
    cfg = _write_config(lb, lb.listen_port or _free_port(_LB_PORT_START, _LB_PORT_END),
                        vpc_instances=vpc_instances)
    if pid_path.exists():
        try:
            pid = int(pid_path.read_text().strip())
            subprocess.run(
                ["haproxy", "-f", str(cfg), "-D", "-sf", str(pid)], check=True
            )
            return
        except (ValueError, subprocess.CalledProcessError):
            pass
    subprocess.run(["haproxy", "-f", str(cfg), "-D"], check=True)


def stop(lb_id: str) -> None:
    """Stop HAProxy for this LB."""
    pid_path = _pid_path(lb_id)
    if pid_path.exists():
        try:
            pid = int(pid_path.read_text().strip())
            os.kill(pid, signal.SIGTERM)
        except (ValueError, ProcessLookupError):
            pass
        pid_path.unlink(missing_ok=True)
    _cfg_path(lb_id).unlink(missing_ok=True)
    _sock_path(lb_id).unlink(missing_ok=True)


def is_running(lb_id: str) -> bool:
    pid_path = _pid_path(lb_id)
    if not pid_path.exists():
        return False
    try:
        pid = int(pid_path.read_text().strip())
        os.kill(pid, 0)
        return True
    except (ValueError, ProcessLookupError, PermissionError):
        return False
