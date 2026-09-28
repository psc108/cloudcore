"""Regression suite for api/serve-package-repo.py (F-162).

Starts the real server script on a free loopback port with throwaway test
data, so it needs neither the CloudCore API nor a VM. It covers the
two failures that put llm-chat builds into cloud-init `status: error`:
- one large download in progress must not block other clients (the old
  single-threaded server made every other request wait for the whole
  50GB ZIM transfer);
- `curl -C -`-style resumes need HTTP Range support.
Plus the behaviours apt depends on, so the fix can't quietly break them.
"""
from __future__ import annotations

import atexit
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

SERVER = Path(__file__).resolve().parents[2] / "api" / "serve-package-repo.py"
BIG_BYTES = 64 * 1024 * 1024


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class _Repo:
    """One server process + test data for the whole suite."""
    _instance = None

    def __init__(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="repo-test-")
        root = Path(self.tmp.name)
        art = root / "jammy" / "artifacts"
        art.mkdir(parents=True)
        (root / "jammy" / "apt-repo").mkdir()
        self.big = os.urandom(BIG_BYTES)
        (art / "big.bin").write_bytes(self.big)
        (art / "Packages.gz").write_bytes(b"tiny index\n")
        self.port = _free_port()
        env = dict(os.environ, REPO_BIND_ADDR="127.0.0.1", REPO_PORT=str(self.port), REPO_DIR=str(root))
        self.proc = subprocess.Popen([sys.executable, str(SERVER)], env=env,
                                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.base = f"http://127.0.0.1:{self.port}/jammy"
        deadline = time.monotonic() + 10
        while True:
            try:
                socket.create_connection(("127.0.0.1", self.port), timeout=0.5).close()
                break
            except OSError:
                if time.monotonic() > deadline:
                    raise RuntimeError("package repo server did not start")
                time.sleep(0.1)
        atexit.register(self.close)

    def close(self):
        self.proc.kill()
        self.tmp.cleanup()

    @classmethod
    def get(cls) -> "_Repo":
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance


def _get(url: str, headers: dict | None = None, timeout: float = 5.0):
    """(status, body, headers); HTTP errors are returned, not raised."""
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers=headers or {}), timeout=timeout) as r:
            return r.status, r.read(), r.headers
    except urllib.error.HTTPError as e:
        return e.code, e.read(), e.headers


class _StalledDownload:
    """Starts a big download, reads one chunk, then stops reading, so the
    server is stuck mid-transfer exactly like a slow guest."""

    def __init__(self, url: str):
        self.sock = socket.create_connection(("127.0.0.1", int(url.split(":")[2].split("/")[0])))
        path = "/" + url.split("/", 3)[3]
        self.sock.sendall(f"GET {path} HTTP/1.1\r\nHost: x\r\n\r\n".encode())
        self.sock.recv(65536)
        time.sleep(0.5)   # let the server fill the socket buffers and block

    def close(self):
        self.sock.close()


class TestPackageRepo:
    def setUp(self):
        self.repo = _Repo.get()

    def test_small_request_not_blocked_by_stalled_download(self):
        stalled = _StalledDownload(f"{self.repo.base}/artifacts/big.bin")
        try:
            t0 = time.monotonic()
            status, body, _ = _get(f"{self.repo.base}/artifacts/Packages.gz", timeout=5)
            took = time.monotonic() - t0
        finally:
            stalled.close()
        if status != 200 or body != b"tiny index\n" or took > 2:
            raise AssertionError(f"small request during a stalled download: status={status}, {took:.1f}s")

    def test_burst_of_requests_during_stalled_download(self):
        stalled = _StalledDownload(f"{self.repo.base}/artifacts/big.bin")
        try:
            with ThreadPoolExecutor(20) as ex:
                results = list(ex.map(lambda _: _get(f"{self.repo.base}/artifacts/Packages.gz", timeout=5)[0],
                                      range(20)))
        finally:
            stalled.close()
        if results.count(200) != 20:
            raise AssertionError(f"only {results.count(200)}/20 succeeded during a stalled download")

    def test_range_resume_returns_206_with_exact_bytes(self):
        status, body, headers = _get(f"{self.repo.base}/artifacts/big.bin",
                                     {"Range": f"bytes={BIG_BYTES - 1000}-"})
        if status != 206 or body != self.repo.big[-1000:]:
            raise AssertionError(f"resume from offset: status={status}, {len(body)} bytes")
        expected = f"bytes {BIG_BYTES - 1000}-{BIG_BYTES - 1}/{BIG_BYTES}"
        if headers.get("Content-Range") != expected:
            raise AssertionError(f"Content-Range {headers.get('Content-Range')!r}, want {expected!r}")

    def test_bounded_and_suffix_ranges(self):
        status, body, _ = _get(f"{self.repo.base}/artifacts/big.bin", {"Range": "bytes=10-19"})
        if status != 206 or body != self.repo.big[10:20]:
            raise AssertionError(f"bytes=10-19: status={status}, body={body!r}")
        status, body, _ = _get(f"{self.repo.base}/artifacts/big.bin", {"Range": "bytes=-5"})
        if status != 206 or body != self.repo.big[-5:]:
            raise AssertionError(f"bytes=-5: status={status}, body={body!r}")

    def test_range_past_end_is_416_with_size(self):
        status, _, headers = _get(f"{self.repo.base}/artifacts/big.bin", {"Range": f"bytes={BIG_BYTES}-"})
        if status != 416 or headers.get("Content-Range") != f"bytes */{BIG_BYTES}":
            raise AssertionError(f"range past end: status={status}, Content-Range={headers.get('Content-Range')!r}")

    def test_full_download_still_200_and_advertises_ranges(self):
        status, body, headers = _get(f"{self.repo.base}/artifacts/big.bin", timeout=30)
        if status != 200 or body != self.repo.big or headers.get("Accept-Ranges") != "bytes":
            raise AssertionError(f"full GET: status={status}, {len(body)} bytes, Accept-Ranges={headers.get('Accept-Ranges')!r}")

    def test_apt_probe_404_and_not_modified_304(self):
        status, _, _ = _get(f"{self.repo.base}/apt-repo/InRelease")
        if status != 404:
            raise AssertionError(f"missing InRelease should 404, got {status}")
        _, _, headers = _get(f"{self.repo.base}/artifacts/Packages.gz")
        status, _, _ = _get(f"{self.repo.base}/artifacts/Packages.gz",
                            {"If-Modified-Since": headers["Last-Modified"]})
        if status != 304:
            raise AssertionError(f"If-Modified-Since should 304, got {status}")

    def test_path_traversal_refused(self):
        with socket.create_connection(("127.0.0.1", self.repo.port), timeout=5) as s:
            s.sendall(b"GET /../../../../etc/passwd HTTP/1.1\r\nHost: x\r\n\r\n")
            reply = s.recv(4096)
        if b"root:" in reply or not reply.startswith(b"HTTP/1.0 404"):
            raise AssertionError(f"path traversal: {reply[:60]!r}")


if __name__ == "__main__":
    # Standalone: python3 tests/suites/test_package_repo.py
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    from tests.lib.framework import get_results, run_suite
    run_suite(TestPackageRepo)
    sys.exit(0 if all(r["status"] == "PASS" for r in get_results()) else 1)
