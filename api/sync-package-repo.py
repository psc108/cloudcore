#!/usr/bin/env python3
"""Copy one host's package repo + artifact cache to another, checksum-verified
(cloudcore-two-host-Phased-Implementation.md, S3).

Two halves, both run as the repo's owner (no root):

  checksums   on the SOURCE host: write <codename>/sync-index.json (path,
              size, sha256 of every served file) and a plain SHA256SUMS
              beside it, for `sha256sum -c` by hand.
  pull        on the DESTINATION host: fetch the source's index over the
              repo's own HTTP (serve-package-repo.py, which answers Range
              requests), download what's missing or different into
              <file>.part with resume, verify each file's sha256, and only
              then move it into place. Re-running it is how two hosts stay
              in step: files already verified are skipped.

Hashing 200+ GB is slow, so both halves keep a cache (<codename>/.sync-cache.json:
path -> size, mtime, sha256) and only re-hash a file whose size or mtime
changed.

Usage:
  python3 api/sync-package-repo.py checksums [--codename jammy]
  python3 api/sync-package-repo.py pull --from http://repo.cloudcore.internal:8090 \\
      [--codename jammy] [--dry-run] [--prune]

Exit codes: 0 in step; 1 a file failed (download or checksum); 2 bad usage
or the source can't be reached; 3 not enough free space.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

# REPO_DIR overrides it, as for serve-package-repo.py (testing).
REPO_ROOT = Path(os.environ.get("REPO_DIR") or Path(__file__).resolve().parent / "package-repo")
SERVED_DIRS = ("apt-repo", "artifacts")
INDEX = "sync-index.json"
SUMS = "SHA256SUMS"
CACHE = ".sync-cache.json"
CHUNK = 8 * 1024 * 1024
# Free space to leave on the destination after the copy, so filling the
# repo can't take the host's disk to zero.
HEADROOM = 20 * 1024**3


def log(msg: str) -> None:
    print(f"sync-package-repo: {msg}", file=sys.stderr, flush=True)


def human(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.1f}{unit}"
        n /= 1024
    return f"{n:.1f}TB"


def load_cache(base: Path) -> dict:
    try:
        return json.loads((base / CACHE).read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_cache(base: Path, cache: dict) -> None:
    tmp = base / (CACHE + ".tmp")
    tmp.write_text(json.dumps(cache, indent=0, sort_keys=True))
    os.replace(tmp, base / CACHE)


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        while chunk := f.read(CHUNK):
            h.update(chunk)
    return h.hexdigest()


def cached_sha(base: Path, rel: str, cache: dict) -> str:
    """The file's sha256, from the cache when its size and mtime are unchanged."""
    st = (base / rel).stat()
    hit = cache.get(rel)
    if hit and hit["size"] == st.st_size and hit["mtime_ns"] == st.st_mtime_ns:
        return hit["sha256"]
    if st.st_size > 1024**3:
        log(f"hashing {rel} ({human(st.st_size)})")
    digest = sha256_file(base / rel)
    cache[rel] = {"size": st.st_size, "mtime_ns": st.st_mtime_ns, "sha256": digest}
    return digest


def served_files(base: Path) -> list[str]:
    out = []
    for d in SERVED_DIRS:
        for p in sorted((base / d).rglob("*")):
            if p.is_file() and not p.name.endswith(".part"):
                out.append(p.relative_to(base).as_posix())
    return out


def cmd_checksums(args: argparse.Namespace) -> int:
    base = REPO_ROOT / args.codename
    if not all((base / d).is_dir() for d in SERVED_DIRS):
        log(f"{base} has no {' and '.join(SERVED_DIRS)}: nothing to index")
        return 2
    cache = load_cache(base)
    files, started = {}, time.monotonic()
    for i, rel in enumerate(served_files(base), 1):
        files[rel] = {"size": (base / rel).stat().st_size, "sha256": cached_sha(base, rel, cache)}
        if i % 50 == 0:
            save_cache(base, cache)  # a long first run keeps its progress if interrupted
    save_cache(base, {k: v for k, v in cache.items() if k in files})
    index = {"codename": args.codename, "generated": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
             "files": files}
    for name, text in ((INDEX, json.dumps(index, indent=1, sort_keys=True)),
                       (SUMS, "".join(f"{v['sha256']}  {k}\n" for k, v in sorted(files.items())))):
        tmp = base / (name + ".tmp")
        tmp.write_text(text)
        os.replace(tmp, base / name)
    total = sum(v["size"] for v in files.values())
    log(f"indexed {len(files)} files, {human(total)}, in {time.monotonic() - started:.0f}s -> {base / INDEX}")
    return 0


def fetch_index(source: str, codename: str) -> dict:
    url = f"{source.rstrip('/')}/{codename}/{INDEX}"
    with urllib.request.urlopen(url, timeout=30) as r:
        return json.load(r)


def download(url: str, dest: Path, size: int, sha: str) -> None:
    """Into dest.part, resuming what's there; verified before it replaces dest."""
    part = dest.with_name(dest.name + ".part")
    have = part.stat().st_size if part.exists() else 0
    if have > size:
        part.unlink()
        have = 0
    h = hashlib.sha256()
    if have:
        with part.open("rb") as f:
            while chunk := f.read(CHUNK):
                h.update(chunk)
    part.touch()  # a zero-byte file (e.g. .build-complete) never enters the loop
    attempts = 0
    while have < size:
        req = urllib.request.Request(url, headers={"Range": f"bytes={have}-"} if have else {})
        try:
            with urllib.request.urlopen(req, timeout=60) as r, part.open("ab") as out:
                if have and r.status != 206:
                    raise OSError(f"source ignored the Range request (HTTP {r.status})")
                last = time.monotonic()
                while chunk := r.read(CHUNK):
                    out.write(chunk)
                    h.update(chunk)
                    have += len(chunk)
                    if size > 1024**3 and time.monotonic() - last > 60:
                        log(f"  {dest.name}: {human(have)} of {human(size)} ({100 * have // size}%)")
                        last = time.monotonic()
        except (urllib.error.URLError, OSError, TimeoutError) as e:
            error = str(e)
        else:
            error = f"connection closed at {have} of {size} bytes"
        if have < size:
            attempts += 1
            if attempts > 5:
                raise OSError(f"giving up after 5 retries: {error}")
            log(f"  {dest.name}: {error}; resuming at {human(have)} (retry {attempts}/5)")
            time.sleep(min(60, 5 * attempts))
    if have != size or h.hexdigest() != sha:
        part.unlink(missing_ok=True)  # corrupt: start this file over next run
        raise OSError(f"checksum mismatch (got {have} bytes, sha256 {h.hexdigest()[:16]}...)")
    os.replace(part, dest)


def cmd_pull(args: argparse.Namespace) -> int:
    base = REPO_ROOT / args.codename
    try:
        index = fetch_index(args.source, args.codename)
    except (urllib.error.URLError, OSError, json.JSONDecodeError) as e:
        log(f"can't read {args.source}/{args.codename}/{INDEX}: {e} "
            f"(run 'sync-package-repo.py checksums' on the source first)")
        return 2
    files: dict = index["files"]
    for rel in files:
        if rel.startswith("/") or ".." in Path(rel).parts or Path(rel).parts[0] not in SERVED_DIRS:
            log(f"refusing index entry outside the repo: {rel!r}")
            return 2
    for d in SERVED_DIRS:
        (base / d).mkdir(parents=True, exist_ok=True)
    cache = load_cache(base)

    todo, ok = [], 0
    for rel, meta in sorted(files.items()):
        p = base / rel
        if p.exists() and p.stat().st_size == meta["size"] and cached_sha(base, rel, cache) == meta["sha256"]:
            ok += 1
        else:
            todo.append(rel)
    save_cache(base, cache)
    extras = [rel for rel in served_files(base) if rel not in files]

    need = sum(files[r]["size"] for r in todo)
    # .part files already on disk count towards what's needed.
    need -= sum((base / (r + ".part")).stat().st_size for r in todo if (base / (r + ".part")).exists())
    free = shutil.disk_usage(base).free
    log(f"source {index.get('generated', '?')}: {len(files)} files; {ok} already verified here, "
        f"{len(todo)} to fetch ({human(need)}); {len(extras)} here but not on the source; "
        f"{human(free)} free")
    if need + HEADROOM > free:
        log(f"not enough space: need {human(need)} plus {human(HEADROOM)} headroom")
        return 3
    if args.dry_run:
        for rel in todo:
            print(f"fetch  {rel}  {human(files[rel]['size'])}")
        for rel in extras:
            print(f"{'prune' if args.prune else 'extra'}  {rel}")
        return 0

    failed, started, done_bytes = [], time.monotonic(), 0
    # Small files first: the apt repo is usable long before the big models land.
    for i, rel in enumerate(sorted(todo, key=lambda r: files[r]["size"]), 1):
        meta = files[rel]
        url = f"{args.source.rstrip('/')}/{args.codename}/{urllib.parse.quote(rel)}"
        if meta["size"] > 100 * 1024**2:
            log(f"[{i}/{len(todo)}] {rel} ({human(meta['size'])})")
        try:
            download(url, base / rel, meta["size"], meta["sha256"])
        except (urllib.error.URLError, OSError, TimeoutError) as e:
            log(f"FAILED {rel}: {e}")
            failed.append(rel)
            continue
        st = (base / rel).stat()
        cache[rel] = {"size": st.st_size, "mtime_ns": st.st_mtime_ns, "sha256": meta["sha256"]}
        save_cache(base, cache)
        done_bytes += meta["size"]
    if args.prune:
        for rel in extras:
            log(f"pruning {rel} (not on the source)")
            (base / rel).unlink()
            cache.pop(rel, None)
        save_cache(base, cache)
    elapsed = max(time.monotonic() - started, 1)
    log(f"fetched {len(todo) - len(failed)} files, {human(done_bytes)} in {elapsed:.0f}s "
        f"({human(done_bytes / elapsed)}/s); {len(failed)} failed")
    if failed:
        log("re-run to resume; partial downloads are kept as .part")
        return 1
    log(f"IN STEP: all {len(files)} files match the source's checksums")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0],
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("checksums", help="index this host's repo (run on the source)")
    c.add_argument("--codename", default="jammy")
    p = sub.add_parser("pull", help="copy a source repo here, checksum-verified")
    p.add_argument("--from", dest="source", required=True, help="e.g. http://192.168.100.1:8090")
    p.add_argument("--codename", default="jammy")
    p.add_argument("--dry-run", action="store_true", help="show what would be fetched or pruned")
    p.add_argument("--prune", action="store_true", help="delete local files the source no longer has")
    args = ap.parse_args()
    if args.cmd == "pull" and not args.source.startswith(("http://", "https://")):
        ap.error("--from must be an http(s) URL")
    return cmd_checksums(args) if args.cmd == "checksums" else cmd_pull(args)


if __name__ == "__main__":
    sys.exit(main())
