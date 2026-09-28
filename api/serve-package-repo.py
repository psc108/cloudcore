#!/usr/bin/env python3
"""Serves the host-managed package repo + artifact cache over plain HTTP.

Bound to the bridge gateway address (192.168.<octet>.1, octet from the
network.bridge_subnet_octet setting, default 100 — set up by
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

F-162: this used to be the stdlib's single-threaded HTTPServer, so one
client streaming a large artifact (the 50GB Wikipedia ZIM, the 8.5GB
model) blocked every other client for the whole transfer. A guest booting
alongside it saw its apt-get update time out, which is what put two
llm-chat builds in a row into cloud-init `status: error`. It is now
threaded, with a deeper listen queue, and answers HTTP Range requests,
which every cloud-init download relies on through `curl -C -` to resume
after a stall. Each completed transfer is also logged with its size,
duration and the number of transfers in flight, so a slow build can be
read straight from `journalctl -u cloudcore-repo`.

REPO_BIND_ADDR / REPO_PORT / REPO_DIR override the defaults, for testing
on a scratch port without touching the live service.
"""
import http.server
import mimetypes
import os
import re
import threading
import time
from email.utils import formatdate
from pathlib import Path

REPO_DIR = Path(os.environ.get("REPO_DIR") or Path(__file__).parent / "package-repo")
BIND_PORT = int(os.environ.get("REPO_PORT", "8090"))
_CHUNK = 1024 * 1024
_RANGE_RE = re.compile(r"bytes=(\d*)-(\d*)")

_active = 0
_active_lock = threading.Lock()


def _bind_addr() -> str:
    """192.168.<octet>.1 — the bridge gateway address, whatever octet
    this host is actually using (network.bridge_subnet_octet, default
    100, same setting compute.py's own bridge_cidr() reads). This runs
    as a separate long-lived process from cloudcore-api, so the setting
    is read straight from the shared SQLite DB rather than in-process —
    falls back to the unchanged default if the DB/table isn't there yet
    (e.g. this service started before cloudcore-api has ever run once).

    db.init() must be called before this (get_db() raises otherwise,
    not silently falls back) — confirmed live as a real bug: without
    it, this always hit the except branch and silently kept binding
    192.168.100.1 regardless of the actual configured octet.
    """
    if os.environ.get("REPO_BIND_ADDR"):
        return os.environ["REPO_BIND_ADDR"]
    try:
        import db
        db.init()
        import settings_store
        return f"192.168.{settings_store.get('network.bridge_subnet_octet', 100)}.1"
    except Exception:
        return "192.168.100.1"


class Handler(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        self._range = None
        self._sent = 0
        super().__init__(*args, directory=str(REPO_DIR), **kwargs)

    def _serve(self, method) -> None:
        global _active
        with _active_lock:
            _active += 1
        t0 = time.monotonic()
        try:
            method()
        finally:
            with _active_lock:
                _active -= 1
                still = _active
            # Logged at completion, unlike the stdlib's own line (written
            # when the response starts), so a slow transfer and whatever
            # queued behind it are both visible.
            self.log_message('done "%s" %s bytes in %.1fs (%d other transfer(s) active)',
                             self.requestline, self._sent, time.monotonic() - t0, still)

    def do_GET(self):
        self._serve(super().do_GET)

    def do_HEAD(self):
        self._serve(super().do_HEAD)

    def send_head(self):
        """Adds single-range `Range: bytes=a-b` support (206/416) on top of
        the stdlib's plain 200 responses. Anything it doesn't handle
        (no Range header, directories, multi-range) falls through to the
        unchanged stdlib behaviour, which serves the whole file."""
        self._range = None
        header = self.headers.get("Range")
        path = self.translate_path(self.path)
        m = _RANGE_RE.fullmatch(header.strip()) if header else None
        if not m or not (m.group(1) or m.group(2)) or not os.path.isfile(path):
            head = super().send_head()
            return head
        try:
            f = open(path, "rb")
        except OSError:
            self.send_error(404, "File not found")
            return None
        st = os.fstat(f.fileno())
        size = st.st_size
        if m.group(1) == "":                       # suffix range: last N bytes
            start, end = max(0, size - int(m.group(2))), size - 1
        else:
            start = int(m.group(1))
            end = int(m.group(2)) if m.group(2) else size - 1
        if start >= size or start > end:
            f.close()
            # 416 with the real size lets a resuming client see the file
            # is already complete rather than retrying forever.
            self.send_response(416)
            self.send_header("Content-Range", f"bytes */{size}")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return None
        end = min(end, size - 1)
        self.send_response(206)
        self.send_header("Content-Type", mimetypes.guess_type(path)[0] or "application/octet-stream")
        self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.send_header("Content-Length", str(end - start + 1))
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Last-Modified", formatdate(st.st_mtime, usegmt=True))
        self.end_headers()
        f.seek(start)
        self._range = (start, end)
        return f

    def end_headers(self):
        # Advertise range support on plain 200s too, so clients know they
        # can resume later.
        if self._range is None and getattr(self, "_headers_buffer", None) is not None:
            self.send_header("Accept-Ranges", "bytes")
        super().end_headers()

    def copyfile(self, source, outputfile):
        remaining = None if self._range is None else self._range[1] - self._range[0] + 1
        while remaining is None or remaining > 0:
            chunk = source.read(_CHUNK if remaining is None else min(_CHUNK, remaining))
            if not chunk:
                break
            outputfile.write(chunk)
            self._sent += len(chunk)
            if remaining is not None:
                remaining -= len(chunk)


class Server(http.server.ThreadingHTTPServer):
    daemon_threads = True
    # The stdlib default backlog is 5. A full backlog means dropped SYNs,
    # which a client sees as "connection timed out" (one of F-162's two
    # symptoms), so leave generous room for a burst of guests booting.
    request_queue_size = 128


if __name__ == "__main__":
    REPO_DIR.mkdir(exist_ok=True)
    Server((_bind_addr(), BIND_PORT), Handler).serve_forever()
