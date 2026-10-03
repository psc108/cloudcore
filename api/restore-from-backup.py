#!/usr/bin/env python3
"""Use the backups another host sends here (two-host S6; backup-host.py makes
them, cloudcore-two-host-Phased-Implementation.md has the runbook).

  list                       backups held here, newest first
  verify  <host> [--day D]   check a backup against its MANIFEST.json
  sentinel <host> [--day D]  put that host's Sentinel database and models in
                             place here, to run Sentinel on this host
  capture <host> [--day D]   merge that host's capture record (examples,
                             LLM deployments, student tokens and
                             submissions) into this host's CloudCore
                             database, to make this host capture's home

Every command verifies the backup first. sentinel refuses to overwrite an
existing Sentinel database without --force (the old one is kept as
.before-restore). capture writes nothing with --dry-run; the rows merged
have UUID keys, so this host's own rows are never touched. Stop the
service that owns a database before restoring into it.

Exit: 0 done; 1 the backup failed verification or the restore failed; 2 bad usage.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sqlite3
import sys
from pathlib import Path

API_DIR = Path(__file__).resolve().parent
BACKUPS = Path(os.environ.get("CLOUDCORE_BACKUPS_DIR") or Path.home() / "cloudcore-backups")
SENTINEL_DIR = Path(os.environ.get("SENTINEL_DATA_DIR") or Path.home() / ".local" / "share" / "sentinel")
CAPTURE_TABLES = ("llm_verification_examples", "llm_deployments", "llm_client_tokens", "llm_client_submissions")


def log(msg: str) -> None:
    print(f"restore-from-backup: {msg}", file=sys.stderr, flush=True)


def day_dir(host: str, day: str | None) -> Path:
    root = BACKUPS / host
    days = sorted(p for p in root.iterdir() if p.is_dir() and (p / "MANIFEST.json").exists()) if root.is_dir() else []
    if not days:
        raise SystemExit(log(f"no backups from {host!r} under {BACKUPS}") or 1)
    if day:
        match = [p for p in days if p.name == day]
        if not match:
            raise SystemExit(log(f"no backup {day} from {host}; have {', '.join(p.name for p in days)}") or 1)
        return match[0]
    return days[-1]


def verify(d: Path) -> bool:
    manifest = json.loads((d / "MANIFEST.json").read_text())
    bad = []
    for rel, f in manifest["files"].items():
        p = d / rel
        if not p.is_file() or p.stat().st_size != f["size"]:
            bad.append(rel)
            continue
        h = hashlib.sha256()
        with p.open("rb") as fh:
            while chunk := fh.read(8 * 1024 * 1024):
                h.update(chunk)
        if h.hexdigest() != f["sha256"]:
            bad.append(rel)
    for db in ("cloudcore.db", "sentinel/sentinel.db"):
        if (d / db).exists() and db not in bad:
            with sqlite3.connect(f"file:{d / db}?mode=ro", uri=True) as c:
                if c.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                    bad.append(db + " (integrity_check)")
    if bad:
        log(f"{d}: FAILED verification: {', '.join(bad[:10])}")
        return False
    log(f"{d}: {len(manifest['files'])} files verified (from {manifest['host']}, made {manifest['created']})")
    return True


def cmd_list(_args) -> int:
    if not BACKUPS.is_dir():
        log(f"nothing under {BACKUPS}")
        return 0
    for host in sorted(p for p in BACKUPS.iterdir() if p.is_dir()):
        for d in sorted((p for p in host.iterdir() if (p / "MANIFEST.json").exists()), reverse=True):
            m = json.loads((d / "MANIFEST.json").read_text())
            size = sum(f["size"] for f in m["files"].values())
            has = [n for n, test in (("cloudcore", "cloudcore.db"), ("sentinel", "sentinel/sentinel.db"),
                                     ("tfstate", "tfstate/")) if any(k.startswith(test) for k in m["files"])]
            print(f"{host.name:20} {d.name}  {size / 2**20:8.0f} MB  {', '.join(has)}")
    return 0


def cmd_verify(args) -> int:
    return 0 if verify(day_dir(args.host, args.day)) else 1


def cmd_sentinel(args) -> int:
    d = day_dir(args.host, args.day)
    if not (d / "sentinel" / "sentinel.db").exists():
        log(f"{d} has no Sentinel database ({args.host} doesn't run Sentinel)")
        return 1
    if not verify(d):
        return 1
    target = SENTINEL_DIR / "sentinel.db"
    if target.exists() and not args.force:
        log(f"{target} exists; stop Sentinel here and re-run with --force to replace it")
        return 2
    SENTINEL_DIR.mkdir(parents=True, exist_ok=True)
    if target.exists():
        os.replace(target, target.with_name("sentinel.db.before-restore"))
        for suffix in ("-wal", "-shm"):
            Path(str(target) + suffix).unlink(missing_ok=True)
    shutil.copy2(d / "sentinel" / "sentinel.db", target)
    if (d / "sentinel" / "models").is_dir():
        # Set the old models aside like the database: copying symlinks over
        # existing ones fails (EEXIST), and stale files mustn't mix in.
        models, aside = SENTINEL_DIR / "models", SENTINEL_DIR / "models.before-restore"
        if models.exists():
            shutil.rmtree(aside, ignore_errors=True)
            os.replace(models, aside)
        shutil.copytree(d / "sentinel" / "models", models, symlinks=True)
    log(f"Sentinel database and models from {args.host} {d.name} are in {SENTINEL_DIR}")
    return 0


def cmd_capture(args) -> int:
    d = day_dir(args.host, args.day)
    if not verify(d):
        return 1
    target = API_DIR / "cloudcore.db"
    with sqlite3.connect(target) as dst:
        dst.execute("ATTACH DATABASE ? AS bk", (f"file:{d / 'cloudcore.db'}?mode=ro",))
        total = 0
        for table in CAPTURE_TABLES:
            have_src = dst.execute("SELECT 1 FROM bk.sqlite_master WHERE type='table' AND name=?", (table,)).fetchone()
            have_dst = dst.execute("SELECT 1 FROM main.sqlite_master WHERE type='table' AND name=?", (table,)).fetchone()
            if not have_src or not have_dst:
                log(f"{table}: not in {'the backup' if not have_src else 'this database'}; skipped")
                continue
            # Only the columns both sides have: the two hosts may be a release apart.
            cols = [r[1] for r in dst.execute(f"PRAGMA bk.table_info({table})")]
            cols = [c for c in cols if c in {r[1] for r in dst.execute(f"PRAGMA main.table_info({table})")}]
            n_src = dst.execute(f"SELECT count(*) FROM bk.{table}").fetchone()[0]
            n_new = dst.execute(f"SELECT count(*) FROM bk.{table} WHERE id NOT IN (SELECT id FROM main.{table})").fetchone()[0]
            print(f"{table:28} {n_src:6} in backup, {n_new:6} new here")
            if not args.dry_run:
                col_list = ", ".join(cols)
                dst.execute(f"INSERT OR REPLACE INTO main.{table} ({col_list}) SELECT {col_list} FROM bk.{table}")
                total += n_src
        if args.dry_run:
            dst.rollback()
            log("dry run: nothing written")
        else:
            dst.commit()
            log(f"merged {total} capture rows from {args.host} {d.name} into {target}")
        dst.execute("DETACH DATABASE bk")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0],
                                 formatter_class=argparse.RawDescriptionHelpFormatter, epilog=__doc__)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list")
    for name in ("verify", "sentinel", "capture"):
        p = sub.add_parser(name)
        p.add_argument("host", help="the host whose backup to use, as listed")
        p.add_argument("--day", help="YYYY-MM-DD (default: newest)")
        if name == "sentinel":
            p.add_argument("--force", action="store_true", help="replace an existing Sentinel database")
        if name == "capture":
            p.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    if getattr(args, "host", "") and ("/" in args.host or args.host.startswith(".")):
        ap.error("host is a name from 'list', not a path")
    return {"list": cmd_list, "verify": cmd_verify, "sentinel": cmd_sentinel, "capture": cmd_capture}[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
