"""Keep two hosts' package repos in step -- the scheduler's `repo_sync` job kind
(cloudcore-two-host-Phased-Implementation.md, S3).

A thin wrapper round api/sync-package-repo.py, so the work shows up in the
dashboard's schedule history like any other job:

  mode "index"  on a host others copy from: refresh <codename>/sync-index.json
                (only files whose size or mtime changed are re-hashed, so a
                daily run is cheap). Schedule it after anything that changes
                the repo, e.g. the weekly kiwix_update.
  mode "pull"   on a host that copies: fetch what changed from `source`
                (that host's repo URL), checksum-verified. `prune` deletes
                files the source no longer has. Off by default: a kiwix VM
                reading the library over NFS may still have a superseded ZIM
                open, and kiwix_update keeps those for the same reason.

The source is configuration (var_overrides), never a host named in code.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent / "sync-package-repo.py"
# A first pull of a full repo is a few hundred GB.
TIMEOUT_S = 24 * 3600


def validate(options: dict) -> str | None:
    """An error message for bad var_overrides, or None."""
    mode = options.get("mode")
    if mode not in ("index", "pull"):
        return "var_overrides.mode must be 'index' or 'pull'"
    if mode == "pull" and not str(options.get("source") or "").startswith(("http://", "https://")):
        return "var_overrides.source (the source host's repo URL, e.g. http://192.168.100.1:8090) is required for mode 'pull'"
    return None


def run(options: dict | None = None) -> tuple[str, str, list[str]]:
    """One pass. Returns (status, summary, log) for the scheduler."""
    opts = options or {}
    err = validate(opts)
    if err:
        return "failed", err, [err]
    codename = str(opts.get("codename") or "jammy")
    if opts["mode"] == "index":
        cmd = [sys.executable, str(SCRIPT), "checksums", "--codename", codename]
    else:
        cmd = [sys.executable, str(SCRIPT), "pull", "--from", opts["source"], "--codename", codename]
        if opts.get("prune"):
            cmd.append("--prune")
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=TIMEOUT_S)
    except subprocess.TimeoutExpired:
        return "failed", f"{opts['mode']}: still running after {TIMEOUT_S // 3600}h; the next run resumes", []
    lines = [ln.removeprefix("sync-package-repo: ") for ln in (proc.stderr + proc.stdout).splitlines()]
    log = lines[-300:]
    summary = next((ln for ln in reversed(lines) if ln.strip()), "(no output)")
    return ("success" if proc.returncode == 0 else "failed"), f"{opts['mode']}: {summary}", log
