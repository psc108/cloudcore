#!/usr/bin/env python3
"""Serves the host-managed package repo + artifact cache over plain HTTP.

Bound to the bridge gateway address (192.168.100.1, set up by
setup-network.sh — every bridged instance's default gateway, so it's
always reachable regardless of which VPC/subnet a consuming guest
belongs to), not loopback — this is the whole point: guests reach it
directly, without needing any per-project NFS server or repo-builder VM
of their own (haFullStack-LLD.md §6, F-037's original motivation).

Layout served, one directory per Ubuntu release codename so multiple
guest OS versions can coexist without clobbering each other:
    api/package-repo/<codename>/apt-repo/    (dpkg-scanpackages index)
    api/package-repo/<codename>/artifacts/   (pinned .debs)

Run via the cloudcore-repo systemd service (setup-package-repo.sh
installs it) — not meant to be started by hand except for debugging.
"""
import http.server
from pathlib import Path

REPO_DIR = Path(__file__).parent / "package-repo"
BIND_ADDR = "192.168.100.1"
BIND_PORT = 8090


class Handler(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(REPO_DIR), **kwargs)


if __name__ == "__main__":
    REPO_DIR.mkdir(exist_ok=True)
    http.server.HTTPServer((BIND_ADDR, BIND_PORT), Handler).serve_forever()
