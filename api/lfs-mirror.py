#!/usr/bin/env python3
"""Mirror the LFS OS build's sources into the host package repo, verified
(lfs-os-Phased-Implementation.md, A2).

Reads lfs/manifest.json (lfs/build-manifest.py) and downloads every file into
the artifact cache under artifacts/lfs/:
  <lfs-version>/       the LFS systemd book's sources and patches
  kernel/              kernels, each with its detached signature
  blfs-<version>/      BLFS packages (UEFI tools, systemd units)
  books/               the LFS and BLFS books
The lab network is isolated, so the build can only use what is here; every
rebuild uses exactly these bytes.

Verification, per file:
  - the book's MD5 where it publishes one (LFS sources and patches, BLFS
    packages);
  - kernels: the tarball's .tar.sign, a signature over the uncompressed tar,
    by Greg Kroah-Hartman's or Linus Torvalds's key -- their keys fetched from
    kernel.org's key directory (WKD), and accepted only if the signing key's
    fingerprint is one pinned below;
  - files with nothing published (BLFS patches, books, units): their SHA-256
    is recorded at first download over HTTPS and every later run must match.
Every file's SHA-256 and how it was verified go to artifacts/lfs/MIRROR.json.

LFS files come from an LFS mirror that holds the release's whole set, with
upstream as the fallback. Downloads go to <file>.part, resume, and are moved
into place only once verified.

Usage: python3 api/lfs-mirror.py [--set lfs|kernel|blfs-uefi|blfs-common|books]
                                 [--dry-run] [--manifest PATH]
Exit: 0 everything present and verified; 1 a download or verification failed;
2 bad usage or no manifest.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

API_DIR = Path(__file__).resolve().parent
ARTIFACTS = API_DIR / "package-repo" / "jammy" / "artifacts"
OUT = Path(os.environ["LFS_MIRROR_OUT"]) if os.environ.get("LFS_MIRROR_OUT") else ARTIFACTS / "lfs"  # override: tests
LOCK = OUT / "MIRROR.json"
DEFAULT_MANIFEST = API_DIR.parent / "lfs" / "manifest.json"
LFS_MIRROR = "https://ftp.osuosl.org/pub/lfs/lfs-packages/{version}/{file}"
# The kernel's release signers, by primary key fingerprint (kernel.org's
# published keys for Greg Kroah-Hartman and Linus Torvalds).
KERNEL_SIGNERS = {"647F28654894E3BD457199BE38DBBDC86092693E": "Greg Kroah-Hartman",
                  "ABAF11C65A2970B130ABE3C479BE3E4300411886": "Linus Torvalds"}
KERNEL_SIGNER_EMAILS = ["gregkh@kernel.org", "torvalds@kernel.org"]
GNUPG_HOME = Path.home() / ".cache" / "cloudcore" / "lfs-gnupg"
RETRIES = 3


def log(msg: str) -> None:
    print(f"lfs-mirror: {msg}", file=sys.stderr, flush=True)


def digest(path: Path, algo: str) -> str:
    h = hashlib.new(algo)
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def target_dir(entry: dict, manifest: dict) -> Path:
    return OUT / {"lfs": manifest["lfs_version"], "kernel": "kernel", "books": "books"}.get(
        entry["set"], f"blfs-{manifest['blfs_version']}")


def download(urls: list[str], dest: Path) -> str:
    """Into dest.part with resume; returns the URL that worked. Raises on failure."""
    part = dest.with_name(dest.name + ".part")
    last: Exception | None = None
    for url in urls:
        for attempt in range(1, RETRIES + 1):
            have = part.stat().st_size if part.exists() else 0
            req = urllib.request.Request(url, headers={"User-Agent": "cloudcore-lfs-mirror/1",
                                                       **({"Range": f"bytes={have}-"} if have else {})})
            try:
                with urllib.request.urlopen(req, timeout=60) as resp:
                    mode = "ab" if have and resp.status == 206 else "wb"
                    with part.open(mode) as f:
                        shutil.copyfileobj(resp, f, 1 << 20)
                return url
            except urllib.error.HTTPError as e:
                last = e
                if e.code == 416:  # already complete
                    return url
                if e.code == 404:
                    break  # this source doesn't have it; try the next
            except (urllib.error.URLError, TimeoutError, OSError) as e:
                last = e
            log(f"  {dest.name}: attempt {attempt} from {url.split('/')[2]} failed ({last}); retrying")
            time.sleep(3 * attempt)
    raise RuntimeError(f"no source could supply {dest.name}: {last}")


def kernel_signature_ok(tarball: Path, sig: Path) -> tuple[bool, str]:
    """The .tar.sign covers the uncompressed tar: xz -cd | gpg --verify."""
    GNUPG_HOME.mkdir(parents=True, exist_ok=True)
    os.chmod(GNUPG_HOME, 0o700)
    gpg = ["gpg", "--homedir", str(GNUPG_HOME), "--batch", "--quiet"]
    have = subprocess.run(gpg + ["--with-colons", "--list-keys"], capture_output=True, text=True).stdout
    if not all(fp in have for fp in KERNEL_SIGNERS):
        subprocess.run(gpg + ["--auto-key-locate", "clear,wkd", "--locate-keys", *KERNEL_SIGNER_EMAILS],
                       capture_output=True, text=True, timeout=120)
    with subprocess.Popen(["xz", "-cd", str(tarball)], stdout=subprocess.PIPE) as xz:
        res = subprocess.run(gpg + ["--status-fd", "1", "--verify", str(sig), "-"], stdin=xz.stdout,
                             capture_output=True, text=True, timeout=1800)
        xz.stdout.close()
    for line in res.stdout.splitlines():
        parts = line.split()
        # [GNUPG:] VALIDSIG <subkey fpr> ... <primary key fpr> (the last field)
        if len(parts) > 2 and parts[1] == "VALIDSIG" and parts[-1] in KERNEL_SIGNERS:
            return True, f"signed by {KERNEL_SIGNERS[parts[-1]]} ({parts[-1]})"
    return False, (res.stderr.strip() or res.stdout.strip())[-300:] or "no valid signature from a pinned key"


def mirror_one(entry: dict, manifest: dict, lock: dict, dry_run: bool) -> tuple[bool, str]:
    d = target_dir(entry, manifest)
    dest = d / entry["file"]
    key = str(dest.relative_to(OUT))
    known = lock.get(key, {})
    replaced = ""
    if dest.exists() and known.get("sha256"):
        if digest(dest, "sha256") == known["sha256"]:
            return True, "present, verified"
        replaced = "; the copy here didn't match its recorded SHA-256, so it was replaced"
        log(f"  {dest.name}: the copy here doesn't match its recorded SHA-256 -- fetching it again")
    if dry_run:
        return True, "would download" + (f" (and its signature)" if entry["set"] == "kernel" and not entry.get("md5") else "")
    d.mkdir(parents=True, exist_ok=True)
    urls = ([LFS_MIRROR.format(version=manifest["lfs_version"], file=entry["file"])] if entry["set"] == "lfs" else []) \
        + [entry["url"]]
    source = download(urls, dest)
    part = dest.with_name(dest.name + ".part")
    sha = digest(part, "sha256")
    how = ""
    if entry.get("md5"):
        got = digest(part, "md5")
        if got != entry["md5"]:
            part.unlink()
            return False, f"MD5 mismatch: got {got}, the book says {entry['md5']} -- discarded"
        how = f"MD5 matches {entry['checksum_source']}"
    elif entry["set"] == "kernel":
        sig = d / (entry["file"].removesuffix(".xz") + ".sign")
        download([entry["url"].removesuffix(".xz") + ".sign"], sig)
        sig.with_name(sig.name + ".part").replace(sig)
        ok, why = kernel_signature_ok(part, sig)
        if not ok:
            part.unlink()
            return False, f"signature check failed: {why} -- discarded"
        how = f"PGP: {why}"
    elif known.get("sha256"):
        if sha != known["sha256"]:
            part.unlink()
            return False, f"SHA-256 differs from the one recorded at first download ({known['sha256'][:12]}...) -- discarded"
        how = "SHA-256 matches the first download"
    else:
        how = "nothing published: SHA-256 recorded now (first download, over HTTPS)"
    part.replace(dest)
    how += replaced
    lock[key] = {"sha256": sha, "size": dest.stat().st_size, "verified": how, "source": source,
                 "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
    return True, how


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    ap.add_argument("--set", choices=["lfs", "kernel", "blfs-uefi", "blfs-stage1", "blfs-common", "books"])
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    try:
        manifest = json.loads(args.manifest.read_text())
    except (OSError, ValueError) as e:
        log(f"can't read {args.manifest}: {e} -- run lfs/build-manifest.py first")
        return 2
    try:
        lock = json.loads(LOCK.read_text())
    except (OSError, ValueError):
        lock = {}
    entries = [e for e in manifest["files"] if not args.set or e["set"] == args.set]
    log(f"LFS {manifest['lfs_version']}, BLFS {manifest['blfs_version']}, kernel {manifest['kernel']}: "
        f"{len(entries)} files into {OUT}" + (" (dry run)" if args.dry_run else ""))
    failed = []
    for i, e in enumerate(entries, 1):
        try:
            ok, how = mirror_one(e, manifest, lock, args.dry_run)
        except (RuntimeError, OSError, subprocess.SubprocessError) as ex:
            ok, how = False, str(ex)
        log(f"[{i}/{len(entries)}] {e['set']:<11} {e['file']}: {how}")
        if not ok:
            failed.append(e["file"])
        elif not args.dry_run:
            OUT.mkdir(parents=True, exist_ok=True)
            tmp = LOCK.with_name(LOCK.name + ".tmp")
            tmp.write_text(json.dumps(lock, indent=1, sort_keys=True) + "\n")
            tmp.replace(LOCK)
    if failed:
        log(f"{len(failed)} failed: {', '.join(failed)}")
        return 1
    if not args.dry_run:
        total = sum(v["size"] for v in lock.values())
        log(f"all {len(entries)} present and verified ({total / 2**20:.0f} MiB under {OUT}). "
            "Then: python3 api/sync-package-repo.py checksums, so the peer's repo sync picks them up")
    return 0


if __name__ == "__main__":
    sys.exit(main())
