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


def _resolve_tg_servers(tg: dict, inst_map: dict) -> list[dict]:
    """Resolve a target group's targets to HAProxy server entries."""
    servers = []
    for t in (tg.get("targets") or []):
        inst = inst_map.get(t["instance_id"])
        if not inst:
            continue
        tg_port = t.get("port") or tg.get("port", 80)
        if inst.http_host_port:
            servers.append({"name": inst.name, "address": "127.0.0.1", "port": inst.http_host_port})
        elif inst.private_ip:
            servers.append({"name": inst.name, "address": inst.private_ip, "port": tg_port})
    return servers


def _servers_cfg(servers: list[dict], sticky: bool, cookie: str) -> str:
    if not servers:
        return "    # no targets registered"
    if sticky:
        return "\n".join(
            f"    server {s['name']} {s['address']}:{s['port']} check cookie {s['name']}"
            for s in servers
        )
    return "\n".join(
        f"    server {s['name']} {s['address']}:{s['port']} check"
        for s in servers
    )


def _write_config(lb: LoadBalancer, listen_port: int, vpc_instances=None) -> Path:
    mode = "http" if lb.type == "application" else "tcp"
    sticky = lb.sticky_sessions and mode == "http"
    cookie = lb.cookie_name or "SERVERID"
    sticky_line = f"    cookie {cookie} insert indirect nocache\n" if sticky else ""

    inst_map = {i.id: i for i in (vpc_instances or [])}

    # --- Resolve target groups → backend sections ---
    tg_sections: dict[str, tuple[str, str]] = {}  # tg_id -> (backend_name, cfg_block)
    for tg in lb.target_groups:
        tg_id = tg["id"]
        back_name = f"tg-{tg_id[:8]}-back"
        servers = _resolve_tg_servers(tg, inst_map)
        hc = tg.get("health_check", {})
        hc_opts = ""
        if hc and mode == "http" and hc.get("path"):
            hc_opts = (
                f"    option httpchk GET {hc.get('path', '/')}\n"
                f"    default-server inter {hc.get('interval', 30)}s "
                f"rise {hc.get('healthy_threshold', 2)} "
                f"fall {hc.get('unhealthy_threshold', 3)}\n"
            )
        tg_sections[tg_id] = (back_name, (
            f"\nbackend {back_name}\n"
            f"    balance roundrobin\n"
            f"{sticky_line}{hc_opts}"
            f"{_servers_cfg(servers, sticky, cookie)}\n"
        ))

    # --- Build frontend blocks ---
    referenced_tgs: set[str] = set()
    frontend_blocks = ""
    if lb.listeners:
        for lst in lb.listeners:
            lst_port = lst["port"]
            lst_short = lst["id"].replace("-", "")[:8]
            default_tg_id = lst.get("target_group_id", "")
            # `or []`, not `.get(..., [])`: this key can be present with an
            # explicit None (Terraform sends null for an unset
            # Optional+Computed list), which a plain default wouldn't catch.
            routing_rules = sorted(lst.get("routing_rules") or [], key=lambda r: r.get("priority", 999))

            acl_lines: list[str] = []
            use_lines: list[str] = []
            for idx, rule in enumerate(routing_rules):
                conds = rule.get("conditions", {})
                path_pat = conds.get("path_pattern", "")
                host_hdr = conds.get("host_header", "")
                rtg_id = rule.get("target_group_id", "")
                if not rtg_id or rtg_id not in tg_sections:
                    continue
                back_name = tg_sections[rtg_id][0]
                acl_parts: list[str] = []
                if path_pat:
                    aname = f"r{idx}p"
                    if path_pat.endswith("*"):
                        acl_lines.append(f"    acl {aname} path_beg {path_pat[:-1]}")
                    else:
                        acl_lines.append(f"    acl {aname} path {path_pat}")
                    acl_parts.append(aname)
                if host_hdr:
                    aname = f"r{idx}h"
                    acl_lines.append(f"    acl {aname} hdr(host) -i {host_hdr}")
                    acl_parts.append(aname)
                if acl_parts:
                    use_lines.append(f"    use_backend {back_name} if {' '.join(acl_parts)}")
                    referenced_tgs.add(rtg_id)

            default_back = f"{lb.name}-back"
            if default_tg_id and default_tg_id in tg_sections:
                default_back = tg_sections[default_tg_id][0]
                referenced_tgs.add(default_tg_id)

            acl_block = ("\n".join(acl_lines) + "\n") if acl_lines else ""
            use_block = ("\n".join(use_lines) + "\n") if use_lines else ""
            frontend_blocks += (
                f"\nfrontend {lb.name}-{lst_short}\n"
                f"    bind 127.0.0.1:{lst_port}\n"
                f"{acl_block}{use_block}"
                f"    default_backend {default_back}\n"
            )
    else:
        frontend_blocks = (
            f"\nfrontend {lb.name}-front\n"
            f"    bind 127.0.0.1:{listen_port}\n"
            f"    default_backend {lb.name}-back\n"
        )

    # --- Default backend (auto-discover or explicit) ---
    effective_backends = lb.backends
    if not effective_backends and vpc_instances and lb.type == "application":
        effective_backends = []
        for inst in vpc_instances:
            if inst.http_host_port:
                effective_backends.append({"name": inst.name, "address": "127.0.0.1", "port": inst.http_host_port})
            elif inst.status.value == "running" and inst.private_ip:
                effective_backends.append({"name": inst.name, "address": inst.private_ip, "port": 80})

    hc = lb.health_check or {}
    hc_opts = ""
    if hc and mode == "http":
        hc_opts = (
            f"    option httpchk GET {hc.get('path', '/')}\n"
            f"    default-server inter {hc.get('interval', 30)}s "
            f"rise {hc.get('healthy_threshold', 2)} "
            f"fall {hc.get('unhealthy_threshold', 3)}\n"
        )

    http_opts = "    option  forwardfor\n    option  http-server-close\n" if mode == "http" else ""

    # Extra backend sections for all TGs referenced by routing rules
    extra_backends = "".join(
        block for tg_id, (_, block) in tg_sections.items() if tg_id in referenced_tgs
    )

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
    """) + http_opts + frontend_blocks + (
        f"\nbackend {lb.name}-back\n"
        f"    balance roundrobin\n"
        f"{sticky_line}{hc_opts}"
        f"{_servers_cfg(effective_backends, sticky, cookie)}\n"
    ) + extra_backends

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


def get_health(lb_id: str) -> list[dict]:
    """Query the HAProxy stats socket for backend health. Returns [] if not running."""
    import csv
    import io
    import socket as _socket

    sock_path = _sock_path(lb_id)
    if not sock_path.exists():
        return []
    try:
        with _socket.socket(_socket.AF_UNIX, _socket.SOCK_STREAM) as s:
            s.settimeout(2.0)
            s.connect(str(sock_path))
            s.sendall(b"show stat\n")
            buf = b""
            while True:
                chunk = s.recv(65536)
                if not chunk:
                    break
                buf += chunk
        raw = buf.decode(errors="replace").lstrip("# ")
        results = []
        for row in csv.DictReader(io.StringIO(raw)):
            svname = row.get("svname", "")
            if svname in ("FRONTEND", "BACKEND", ""):
                continue
            status = row.get("status", "UNKNOWN")
            results.append({
                "name":         svname,
                "status":       status,
                "healthy":      status == "UP",
                "check_status": row.get("check_status", ""),
                "last_chk":     row.get("last_chk", ""),
                "connections":  int(row.get("scur", 0) or 0),
                "requests":     int(row.get("req_tot", 0) or 0),
                "bytes_in":     int(row.get("bin", 0) or 0),
                "bytes_out":    int(row.get("bout", 0) or 0),
            })
        return results
    except Exception:
        return []


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
