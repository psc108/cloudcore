#!/usr/bin/env python3
"""Download the ZIM archives listed in a kiwix manifest into the host
artifact cache (api/package-repo/jammy/artifacts/), verify each against
Kiwix's own published .sha256, and write the verified checksum back into
the manifest -- which examples/llm-chat then uses as-is (K2 of
llm-chat-kiwix-expansion-Phased-Implementation.md).

Idempotent and resumable: a file that already matches its recorded
checksum is skipped; a partial file is resumed with an HTTP Range request
(F-138: a retry that silently restarts a multi-GB download from byte 0 can
exhaust its retries and never finish). Runs as the normal user; mutates
only the artifact cache and the manifest.

With `libzim` installed, it also records each archive's internal Name,
UUID and whether it carries a full-text index (kiwix search finds nothing
in a ZIM without one) -- informational only. Note kiwix-serve's search
filter identifies a book by its filename minus ".zim", not by this Name.

Usage:
  python3 api/fetch-kiwix-zims.py examples/llm-chat/kiwix-zims.json [--jobs N] [--dry-run] [--only NAME ...]

--jobs: Kiwix mirrors throttle each connection (~1MB/s measured), so
several downloads at once finish far sooner. Manifest writes are
serialised with a lock, so concurrent jobs never lose each other's
checksums.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import httpx

ARTIFACTS = Path(__file__).resolve().parent / "package-repo" / "jammy" / "artifacts"
_CHUNK = 4 * 1024 * 1024
_RETRIES = 8


def log(msg: str) -> None:
    print(f"fetch-kiwix-zims: {msg}", file=sys.stderr, flush=True)


def sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(_CHUNK), b""):
            h.update(chunk)
    return h.hexdigest()


def published_sha256(client: httpx.Client, url: str) -> str:
    r = client.get(url + ".sha256", timeout=30, follow_redirects=True)
    r.raise_for_status()
    value = r.text.split()[0].strip().lower()
    if len(value) != 64:
        raise ValueError(f"unexpected .sha256 content for {url}: {r.text[:80]!r}")
    return value


def download(client: httpx.Client, url: str, dest: Path, expected_size: int | None) -> None:
    part = dest.with_suffix(dest.suffix + ".part")
    for attempt in range(1, _RETRIES + 1):
        have = part.stat().st_size if part.exists() else 0
        headers = {"Range": f"bytes={have}-"} if have else {}
        try:
            with client.stream("GET", url, headers=headers, timeout=httpx.Timeout(30, read=120),
                               follow_redirects=True) as r:
                if r.status_code == 416 and expected_size and have >= expected_size:
                    break                                # already complete
                if have and r.status_code == 200:
                    log(f"{dest.name}: server ignored Range, restarting from 0")
                    have = 0
                    part.unlink(missing_ok=True)
                r.raise_for_status()
                with part.open("ab") as f:
                    last = time.monotonic()
                    for chunk in r.iter_bytes(_CHUNK):
                        f.write(chunk)
                        have += len(chunk)
                        if time.monotonic() - last > 30:
                            pct = f" ({have * 100 // expected_size}%)" if expected_size else ""
                            log(f"{dest.name}: {have // 2**20}MB{pct}")
                            last = time.monotonic()
            break
        except (httpx.HTTPError, OSError) as e:
            log(f"{dest.name}: attempt {attempt}/{_RETRIES} failed at {have // 2**20}MB: {e}")
            if attempt == _RETRIES:
                raise
            time.sleep(min(60, 5 * attempt))
    part.rename(dest)


def record_metadata(entry: dict, path: Path) -> None:
    """Fill book_name / uuid / fulltext from the ZIM itself. Best-effort:
    skipped (with a note) when libzim isn't installed."""
    try:
        from libzim.reader import Archive
    except ImportError:
        log("libzim not installed -- book names not recorded (pip install libzim)")
        return
    a = Archive(path)
    if "Name" in a.metadata_keys:
        entry["book_name"] = a.get_metadata("Name").decode()
    entry["uuid"] = str(a.uuid)
    entry["fulltext"] = bool(a.has_fulltext_index)
    entry["size_bytes"] = path.stat().st_size


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("manifest", type=Path)
    ap.add_argument("--dry-run", action="store_true", help="list what would be downloaded; change nothing")
    ap.add_argument("--only", nargs="*", default=None, help="restrict to these manifest names")
    ap.add_argument("--jobs", type=int, default=1, help="concurrent downloads (default 1)")
    args = ap.parse_args()

    manifest = json.loads(args.manifest.read_text())
    entries = [e for e in manifest["zims"] if args.only is None or e["name"] in args.only]
    ARTIFACTS.mkdir(parents=True, exist_ok=True)
    lock = threading.Lock()
    failures = []

    def save() -> None:
        with lock:
            args.manifest.write_text(json.dumps(manifest, indent=2) + "\n")

    def one(client: httpx.Client, e: dict) -> None:
        dest = ARTIFACTS / e["filename"]
        if not e.get("source_url"):
            # Already in the cache (pre-manifest ZIMs): just record metadata.
            if dest.exists() and not e.get("book_name") and not args.dry_run:
                with lock:
                    record_metadata(e, dest)
                save()
            return
        if dest.exists() and e.get("sha256") and (e.get("book_name") or sha256_of(dest) == e["sha256"]):
            if not e.get("book_name") and not args.dry_run:
                with lock:
                    record_metadata(e, dest)
                save()
            log(f"{e['filename']}: present, skipping")
            return
        if args.dry_run:
            log(f"would download {e['source_url']} ({e.get('size_bytes', 0) // 2**20}MB)")
            return
        try:
            expected = published_sha256(client, e["source_url"])
            if not dest.exists():
                log(f"{e['filename']}: downloading ({e.get('size_bytes', 0) // 2**20}MB)")
                download(client, e["source_url"], dest, e.get("size_bytes"))
            actual = sha256_of(dest)
            if actual != expected:
                dest.rename(dest.with_suffix(dest.suffix + ".bad"))
                raise ValueError(f"checksum mismatch: got {actual}, Kiwix publishes {expected}")
            # Entry updates under the same lock as the serialisation in save(),
            # or json.dumps can see a dict change size mid-iteration.
            with lock:
                e["sha256"] = actual
                record_metadata(e, dest)
            save()
            log(f"{e['filename']}: verified {actual[:16]}...")
        except (httpx.HTTPError, OSError, ValueError) as err:
            with lock:
                failures.append(e["filename"])
            log(f"{e['filename']}: FAILED: {err}")

    # Largest first, so the long downloads overlap instead of queueing
    # at the end behind the small ones.
    entries.sort(key=lambda e: -(e.get("size_bytes") or 0))
    with httpx.Client(headers={"User-Agent": "cloudcore-fetch-kiwix-zims"},
                      limits=httpx.Limits(max_connections=max(4, args.jobs * 2))) as client:
        with ThreadPoolExecutor(max_workers=max(1, args.jobs)) as pool:
            list(pool.map(lambda e: one(client, e), entries))
    failures = len(failures)
    if failures:
        log(f"{failures} ZIM(s) failed -- re-run to resume")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
