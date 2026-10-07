#!/usr/bin/env python3
"""Build the LFS OS build's source manifest (lfs-os-Phased-Implementation.md, A1)
from the books' own published data, so a new release is re-read, not re-typed.

Sets, each entry with its file, version, URL, checksum and where that came from:
  lfs        the LFS systemd book's sources and patches (its md5sums is the
             authoritative systemd list; its wget-list also carries SysVinit-only
             files, which are left out)
  kernel     the latest stable kernel (kernel.org releases.json), checked later
             by kernel.org's signed sha256sums.asc -- plus the book's own kernel
             as a fallback
  blfs-uefi  efibootmgr and what it needs, from the matching BLFS book (LFS
             builds UEFI GRUB itself)
  blfs-stage1  OpenSSH, so D6's proof can log in from another machine
  blfs-common  BLFS's systemd unit files, which its packages install
  books      the LFS and BLFS books, for llm-chat's corpus (A4)
The Wayland set waits for the compositor decision (E1).

Uses only the standard library: it runs on the hosts' system Python, as
api/llm-bench.py does, with no venv to install httpx into.

Usage: python3 lfs/build-manifest.py [--out lfs/manifest.json] [--dry-run]
Exit: 0 written; 1 a source page couldn't be read or parsed; 2 bad usage.
"""

from __future__ import annotations

import argparse
import html
import json
import re
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

LFS = "https://www.linuxfromscratch.org/lfs"
BLFS = "https://www.linuxfromscratch.org/blfs"
KERNEL_RELEASES = "https://www.kernel.org/releases.json"
# BLFS packages stage 1 needs, by set and page; their required dependencies
# are followed from the pages themselves. blfs-uefi manages UEFI boot entries;
# blfs-stage1 adds what D6's proof needs beyond LFS (another machine can SSH in).
BLFS_SETS = {"blfs-uefi": ["postlfs/efibootmgr.html"], "blfs-stage1": ["postlfs/openssh.html"]}
BLFS_PAGE_FOR = {"efivar": "postlfs/efivar.html", "popt": "general/popt.html"}


def log(msg: str) -> None:
    print(f"build-manifest: {msg}", file=sys.stderr, flush=True)


def fetch(url: str) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": "cloudcore-lfs-manifest/1"})
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            return resp.read().decode("utf-8", "replace")
    except (urllib.error.URLError, TimeoutError) as e:
        log(f"could not read {url}: {e}")
        raise


def text_of(page: str) -> str:
    return html.unescape(re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", page)))


def split_version(name: str) -> tuple[str, str]:
    """('gcc', '15.2.0') from 'gcc-15.2.0.tar.xz'; patches keep their name."""
    stem = re.sub(r"\.(tar\.(?:xz|gz|bz2|zst)|tgz|zip|patch)$", "", name)
    m = re.match(r"^(.+?)-(\d[\w.+-]*)$", stem)
    return (m.group(1), m.group(2)) if m else (stem, "")


def lfs_set() -> tuple[str, list[dict]]:
    base = f"{LFS}/downloads/stable-systemd"
    listing = fetch(base + "/")
    version = re.search(r"LFS-BOOK-([\d.]+)\.tar\.xz", listing).group(1)
    urls = {u.rsplit("/", 1)[1]: u for u in fetch(base + "/wget-list").split() if u.startswith("http")}
    entries = []
    for line in fetch(base + "/md5sums").splitlines():
        if not line.strip():
            continue
        md5, name = line.split()
        if name not in urls:
            raise ValueError(f"{name} has a checksum but no URL in wget-list")
        pkg, ver = split_version(name)
        entries.append({"set": "lfs", "file": name, "package": pkg, "version": ver, "url": urls[name],
                        "md5": md5, "checksum_source": f"LFS {version} systemd md5sums",
                        "kind": "patch" if name.endswith(".patch") else "source"})
    left_out = sorted(set(urls) - {e["file"] for e in entries})
    log(f"LFS {version} (systemd): {len(entries)} files; left out as SysVinit-only: {', '.join(left_out)}")
    return version, entries


def kernel_set(book_kernel: dict | None) -> list[dict]:
    rel = json.loads(fetch(KERNEL_RELEASES))
    ver = rel["latest_stable"]["version"]
    src = next(r["source"] for r in rel["releases"] if r["version"] == ver)
    major = ver.split(".")[0]
    out = [{"set": "kernel", "file": src.rsplit("/", 1)[1], "package": "linux", "version": ver, "url": src,
            "sha256": None, "checksum_source": f"kernel.org v{major}.x sha256sums.asc, PGP-verified at download",
            "sums_url": f"https://cdn.kernel.org/pub/linux/kernel/v{major}.x/sha256sums.asc",
            "kind": "source", "role": "latest stable: the one to build"}]
    if book_kernel and book_kernel["version"] != ver:
        out.append({**book_kernel, "set": "kernel", "role": "the book's own kernel: a fallback"})
    log(f"kernel: latest stable {ver}" + (f"; the book's {book_kernel['version']} kept as a fallback"
                                          if book_kernel and book_kernel["version"] != ver else ""))
    return out


def blfs_page(page: str) -> dict:
    t = text_of(fetch(f"{BLFS}/view/stable-systemd/{page}"))
    url = re.search(r"Download \(HTTP\): (\S+)", t).group(1)
    md5 = re.search(r"Download MD5 sum: ([0-9a-f]{32})", t).group(1)
    # The dependency list, not "Required patch:" above it.
    req = re.search(r"Dependencies Required (.*?)(?: Recommended| Optional| Installation of)", t)
    required = [] if not req or req.group(1).strip() == "None" else re.findall(r"([A-Za-z][\w+.]*?)-\d", req.group(1))
    patches = re.findall(r"Required patch: (\S+\.patch)", t)
    return {"url": url, "md5": md5, "required": required, "patches": patches}


def blfs_set(name_: str, pages: list[str]) -> tuple[str, list[dict]]:
    version = re.search(r"Version ([\d.]+)", text_of(fetch(f"{BLFS}/view/stable-systemd/index.html"))).group(1)
    todo, seen, entries = list(pages), set(), []
    while todo:
        page = todo.pop(0)
        if page in seen:
            continue
        seen.add(page)
        info = blfs_page(page)
        name = info["url"].rsplit("/", 1)[1]
        pkg, ver = split_version(name)
        entries.append({"set": name_, "file": name, "package": pkg, "version": ver, "url": info["url"],
                        "md5": info["md5"], "checksum_source": f"BLFS {version} systemd, {page}",
                        "kind": "source", "book_page": page, "requires": info["required"]})
        for p in info["patches"]:
            entries.append({"set": name_, "file": p.rsplit("/", 1)[1], "package": pkg, "version": ver,
                            "url": p, "md5": None, "kind": "patch", "book_page": page,
                            "checksum_source": "none published: SHA-256 recorded at first download over HTTPS"})
        for dep in info["required"]:
            dep_page = BLFS_PAGE_FOR.get(dep.lower())
            if not dep_page:
                raise ValueError(f"{page} requires {dep}, which has no known BLFS page here")
            todo.append(dep_page)
    log(f"BLFS {version} (systemd), {name_}: {', '.join(e['file'] for e in entries)}")
    return version, entries


def books_set(lfs_version: str, blfs_version: str) -> list[dict]:
    blfs_dl = fetch(f"{BLFS}/downloads/stable-systemd/")
    blfs_book = re.search(r'href="(blfs-book-[\w.-]+-html\.tar\.xz)"', blfs_dl, re.IGNORECASE)
    units = re.search(r'href="(blfs-systemd-units-(\d+)\.tar\.xz)"', blfs_dl)
    out = [{"set": "books", "file": f"LFS-BOOK-{lfs_version}.tar.xz", "package": "lfs-book", "version": lfs_version,
            "url": f"{LFS}/downloads/stable-systemd/LFS-BOOK-{lfs_version}.tar.xz", "md5": None, "kind": "book",
            "checksum_source": "none published: SHA-256 recorded at first download over HTTPS"}]
    if blfs_book:
        out.append({"set": "books", "file": blfs_book.group(1), "package": "blfs-book", "version": blfs_version,
                    "url": f"{BLFS}/downloads/stable-systemd/{blfs_book.group(1)}", "md5": None, "kind": "book",
                    "checksum_source": "none published: SHA-256 recorded at first download over HTTPS"})
    else:
        log("no BLFS book tarball found in its downloads directory; the corpus will need the HTML pages")
    if units:
        # The systemd unit files BLFS packages install for their services.
        out.append({"set": "blfs-common", "file": units.group(1), "package": "blfs-systemd-units",
                    "version": units.group(2), "url": f"{BLFS}/downloads/stable-systemd/{units.group(1)}",
                    "md5": None, "kind": "source",
                    "checksum_source": "none published: SHA-256 recorded at first download over HTTPS"})
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--out", type=Path, default=Path(__file__).resolve().parent / "manifest.json")
    ap.add_argument("--dry-run", action="store_true", help="print the summary, write nothing")
    args = ap.parse_args()
    try:
        lfs_version, lfs = lfs_set()
        book_kernel = next((e for e in lfs if e["package"] == "linux"), None)
        lfs = [e for e in lfs if e["package"] != "linux"]  # the kernel set owns the kernel
        kernel = kernel_set(book_kernel)
        blfs_version, uefi = blfs_set("blfs-uefi", BLFS_SETS["blfs-uefi"])
        uefi += blfs_set("blfs-stage1", BLFS_SETS["blfs-stage1"])[1]
        books = books_set(lfs_version, blfs_version)
    except (urllib.error.URLError, TimeoutError, AttributeError, ValueError, KeyError, StopIteration) as e:
        log(f"failed: {type(e).__name__}: {e}")
        return 1
    files = lfs + kernel + uefi + books
    manifest = {"generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "lfs_version": lfs_version, "blfs_version": blfs_version, "init": "systemd",
                "kernel": kernel[0]["version"], "files": files,
                "pending": ["wayland: waits for the compositor decision (E1)"]}
    log(f"{len(files)} files: " + ", ".join(f"{s} {sum(f['set'] == s for f in files)}"
                                             for s in ("lfs", "kernel", "blfs-uefi", "blfs-stage1", "blfs-common", "books")))
    if args.dry_run:
        return 0
    args.out.write_text(json.dumps(manifest, indent=1) + "\n")
    log(f"wrote {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
