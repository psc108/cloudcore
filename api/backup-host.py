#!/usr/bin/env python3
"""Back this host's CloudCore state up to another host (two-host S6,
cloudcore-two-host-Phased-Implementation.md; restore: restore-from-backup.py).

What is copied, as consistent snapshots:
  cloudcore.db                 the API's database: instances, builds, and the
                               capture record (examples, LLM deployments,
                               student tokens and submissions)
  tfstate/<template>.tfstate   OpenTofu state of each example built here
  sentinel/sentinel.db         Sentinel's database, if Sentinel runs here
  sentinel/models/             its trained models
Databases are copied with SQLite's online backup API (safe while in use) and
integrity-checked. MANIFEST.json lists every file's size and sha256.

Each run makes <staging>/<YYYY-MM-DD>/ (unchanged files hard-linked to the
previous day's), keeps the newest --keep days, and mirrors the whole staging
tree to the target with rsync over SSH. The target's authorized_keys entry
(setup-backup-key.sh) confines the key to one directory with rrsync, so it
can't open a shell or touch anything else there.

Usage: python3 api/backup-host.py --to <user>@<host> [--keep 7] [--dry-run]
Exit: 0 ok; 1 a snapshot or the transfer failed; 2 bad usage.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import socket
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

API_DIR = Path(__file__).resolve().parent
REPO_DIR = API_DIR.parent
STAGING = Path(os.environ.get("CLOUDCORE_BACKUP_STAGING")
               or Path.home() / ".local" / "share" / "cloudcore-backup")
SENTINEL_DIR = Path(os.environ.get("SENTINEL_DATA_DIR") or Path.home() / ".local" / "share" / "sentinel")
KEY = Path(os.environ.get("CLOUDCORE_BACKUP_KEY") or Path.home() / ".ssh" / "cloudcore-backup")


def log(msg: str) -> None:
    print(f"backup-host: {msg}", file=sys.stderr, flush=True)


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        while chunk := f.read(8 * 1024 * 1024):
            h.update(chunk)
    return h.hexdigest()


def snapshot_db(src: Path, dest: Path) -> None:
    """A consistent copy of a live SQLite database, then integrity-checked."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(dest.name + ".tmp")
    tmp.unlink(missing_ok=True)
    with sqlite3.connect(f"file:{src}?mode=ro", uri=True) as s, sqlite3.connect(tmp) as d:
        s.backup(d)
    with sqlite3.connect(tmp) as d:
        ok = d.execute("PRAGMA integrity_check").fetchone()[0]
    if ok != "ok":
        tmp.unlink(missing_ok=True)
        raise RuntimeError(f"{src}: snapshot failed integrity_check: {ok}")
    os.replace(tmp, dest)


def link_or_copy(src: Path, dest: Path, previous: Path | None) -> None:
    """Copy src to dest, hard-linking to yesterday's copy when identical."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    if previous and previous.exists() and previous.stat().st_size == src.stat().st_size \
            and sha256(previous) == sha256(src):
        os.link(previous, dest)
    else:
        shutil.copy2(src, dest)


def dedupe(path: Path, previous: Path | None) -> None:
    """Replace a fresh snapshot with a hard link to yesterday's if identical."""
    if previous and previous.exists() and previous.stat().st_size == path.stat().st_size \
            and sha256(previous) == sha256(path):
        path.unlink()
        os.link(previous, path)


def build_snapshot(day_dir: Path, prev_dir: Path | None) -> dict:
    files: dict[str, dict] = {}

    def prev(rel: str) -> Path | None:
        return prev_dir / rel if prev_dir else None

    snapshot_db(API_DIR / "cloudcore.db", day_dir / "cloudcore.db")
    dedupe(day_dir / "cloudcore.db", prev("cloudcore.db"))
    for state in sorted((REPO_DIR / "examples").glob("*/terraform.tfstate")):
        rel = f"tfstate/{state.parent.name}.tfstate"
        link_or_copy(state, day_dir / rel, prev(rel))
    if (SENTINEL_DIR / "sentinel.db").exists():
        snapshot_db(SENTINEL_DIR / "sentinel.db", day_dir / "sentinel" / "sentinel.db")
        dedupe(day_dir / "sentinel" / "sentinel.db", prev("sentinel/sentinel.db"))
        for f in sorted((SENTINEL_DIR / "models").rglob("*")):
            if f.is_file():
                rel = "sentinel/" + f.relative_to(SENTINEL_DIR).as_posix()
                link_or_copy(f, day_dir / rel, prev(rel))
    for f in sorted(day_dir.rglob("*")):
        if f.is_file() and f.name != "MANIFEST.json":
            files[f.relative_to(day_dir).as_posix()] = {"size": f.stat().st_size, "sha256": sha256(f)}
    manifest = {"host": socket.gethostname(), "created": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "files": files}
    (day_dir / "MANIFEST.json").write_text(json.dumps(manifest, indent=1, sort_keys=True))
    return manifest


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--to", required=True, help="<user>@<host> holding this host's backups")
    ap.add_argument("--keep", type=int, default=7, help="days kept (here and there)")
    ap.add_argument("--dry-run", action="store_true", help="snapshot here, show the transfer, send nothing")
    args = ap.parse_args()
    if "@" not in args.to or args.to.startswith("-") or any(c in args.to for c in " ;:/'\""):
        ap.error("--to must be <user>@<host>")
    if args.keep < 1:
        ap.error("--keep must be at least 1")
    if not KEY.exists():
        log(f"no key at {KEY}: run api/setup-backup-key.sh first")
        return 2

    STAGING.mkdir(parents=True, exist_ok=True)
    os.chmod(STAGING, 0o700)  # the capture record and peer tokens are in here
    today = time.strftime("%Y-%m-%d", time.gmtime())
    days = sorted(p for p in STAGING.iterdir() if p.is_dir() and len(p.name) == 10 and p.name != today)
    prev_dir = days[-1] if days else None
    day_dir = STAGING / today
    if day_dir.exists():
        shutil.rmtree(day_dir)  # a re-run the same day replaces it
    day_dir.mkdir()
    t0 = time.monotonic()
    try:
        manifest = build_snapshot(day_dir, prev_dir)
    except (sqlite3.Error, OSError, RuntimeError) as e:
        log(f"snapshot failed: {e}")
        shutil.rmtree(day_dir, ignore_errors=True)
        return 1
    for old in sorted(p for p in STAGING.iterdir() if p.is_dir() and len(p.name) == 10)[:-args.keep]:
        shutil.rmtree(old)
    size = sum(f["size"] for f in manifest["files"].values())
    log(f"snapshot {today}: {len(manifest['files'])} files, {size / 2**20:.0f} MB "
        f"in {time.monotonic() - t0:.0f}s (integrity ok)")

    # The trailing slash and rrsync's fixed root: this host's tree lands in the
    # one directory the key is allowed to write.
    cmd = ["rsync", "-a", "-H", "--delete", "--partial", "--stats",
           "-e", f"ssh -i {KEY} -o BatchMode=yes -o IdentitiesOnly=yes -o ConnectTimeout=15",
           f"{STAGING}/", f"{args.to}:"]
    if args.dry_run:
        cmd.insert(1, "--dry-run")
    t1 = time.monotonic()
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        log(f"rsync to {args.to} failed ({r.returncode}): {(r.stderr or r.stdout).strip()[-400:]}")
        return 1
    sent = next((ln.split(":", 1)[1].strip() for ln in r.stdout.splitlines()
                 if ln.startswith("Total transferred file size")), "?")
    log(f"{'would send' if args.dry_run else 'sent'} to {args.to} in {time.monotonic() - t1:.0f}s "
        f"(transferred: {sent}); {len(list(STAGING.iterdir()))} day(s) kept")
    return 0


if __name__ == "__main__":
    sys.exit(main())
