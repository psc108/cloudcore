"""Nightly backup of this host to another -- the scheduler's `host_backup`
job kind (two-host S6). A thin wrapper round api/backup-host.py, so runs and
failures show in the dashboard's schedule history. The target is
configuration (var_overrides.to = "<user>@<host>"), never a host named here.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent / "backup-host.py"
TIMEOUT_S = 6 * 3600
_TARGET = re.compile(r"^[a-z_][a-z0-9_-]*@[A-Za-z0-9.-]+$")


def validate(options: dict) -> str | None:
    if not _TARGET.match(str(options.get("to") or "")):
        return "var_overrides.to must be <user>@<host> (the host that keeps this host's backups)"
    keep = options.get("keep", 7)
    if not isinstance(keep, int) or not 1 <= keep <= 60:
        return "var_overrides.keep must be a whole number of days, 1-60"
    return None


def run(options: dict | None = None) -> tuple[str, str, list[str]]:
    opts = options or {}
    err = validate(opts)
    if err:
        return "failed", err, [err]
    cmd = [sys.executable, str(SCRIPT), "--to", opts["to"], "--keep", str(opts.get("keep", 7))]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=TIMEOUT_S)
    except subprocess.TimeoutExpired:
        return "failed", f"backup to {opts['to']} still running after {TIMEOUT_S // 3600}h", []
    lines = [ln.removeprefix("backup-host: ") for ln in (proc.stderr + proc.stdout).splitlines()]
    summary = next((ln for ln in reversed(lines) if ln.strip()), "(no output)")
    return ("success" if proc.returncode == 0 else "failed"), summary, lines[-200:]
