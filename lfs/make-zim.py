#!/usr/bin/env python3
"""Turn the mirrored LFS and BLFS books into ZIM files for llm-chat's Kiwix
tier (lfs-os-Phased-Implementation.md, A4b).

One ZIM per book, full-text indexed, from the HTML tarball api/lfs-mirror.py
fetched: every page, stylesheet and image under its own path, so kiwix-serve
shows the book as it is online and an answer can link the exact page.
Written to the artifact cache's top level, beside the other ZIMs, where the
Kiwix VM's NFS mount sees them.

Needs python-libzim, which the hosts' system Python doesn't have:
    uv venv ~/.cache/cloudcore/zim-venv
    uv pip install --python ~/.cache/cloudcore/zim-venv/bin/python libzim
    ~/.cache/cloudcore/zim-venv/bin/python lfs/make-zim.py

Usage: make-zim.py [--books-dir DIR] [--out-dir DIR] [--dry-run]
Exit: 0 written; 1 a book couldn't be read or written; 2 bad usage.
"""

from __future__ import annotations

import argparse
import mimetypes
import re
import sys
import tarfile
import time
from pathlib import Path

try:
    from libzim.writer import Creator, Hint, Item, StringProvider
except ImportError:
    print("make-zim: python-libzim is missing -- see the usage in this file's docstring", file=sys.stderr)
    sys.exit(2)

ARTIFACTS = Path(__file__).resolve().parent.parent / "api" / "package-repo" / "jammy" / "artifacts"
BOOKS_DIR = ARTIFACTS / "lfs" / "books"


def log(msg: str) -> None:
    print(f"make-zim: {msg}", file=sys.stderr, flush=True)


class _Entry(Item):
    def __init__(self, path: str, title: str, mime: str, data: bytes, front: bool) -> None:
        super().__init__()
        self._path, self._title, self._mime, self._data, self._front = path, title, mime, data, front

    def get_path(self) -> str:
        return self._path

    def get_title(self) -> str:
        return self._title

    def get_mimetype(self) -> str:
        return self._mime

    def get_contentprovider(self) -> StringProvider:
        return StringProvider(self._data)

    def get_hints(self) -> dict:
        return {Hint.FRONT_ARTICLE: self._front, Hint.COMPRESS: not self._mime.startswith("image/")}


def build(tarball: Path, out: Path, book: str, version: str) -> int:
    """Returns the number of pages written."""
    pages = 0
    tmp = out.with_name(out.name + ".part")
    tmp.unlink(missing_ok=True)
    with tarfile.open(tarball) as tar, Creator(str(tmp)).config_indexing(True, "eng") as zim:
        zim.set_mainpath("index.html")
        name = "Linux From Scratch" if book == "lfs" else "Beyond Linux From Scratch"
        meta = {"Name": f"{book}_en_systemd", "Title": f"{name} {version} (systemd)",
                "Description": f"The {name} book, version {version}, systemd edition, for llm-chat's LFS build",
                "Language": "eng", "Creator": "The Linux From Scratch project", "Publisher": "CloudCore",
                "Date": time.strftime("%Y-%m-%d"), "Tags": "lfs;linux;_ftindex:yes",
                "License": "CC BY-NC-SA 2.0 (text); MIT (instructions)"}
        for k, v in meta.items():
            zim.add_metadata(k, v)
        for m in tar.getmembers():
            if not m.isfile():
                continue
            # LFS's tarball has a version folder; the ZIM's paths start below it.
            path = m.name.split("/", 1)[1] if book == "lfs" and "/" in m.name else m.name
            data = tar.extractfile(m).read()
            mime = mimetypes.guess_type(path)[0] or "application/octet-stream"
            title = path
            front = mime == "text/html"
            if front:
                t = re.search(rb"<title>(.*?)</title>", data, re.S)
                title = " ".join(t.group(1).decode("utf-8", "replace").split()) if t else path
                pages += 1
            zim.add_item(_Entry(path, title, mime, data, front))
    tmp.replace(out)
    return pages


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--books-dir", type=Path, default=BOOKS_DIR)
    ap.add_argument("--out-dir", type=Path, default=ARTIFACTS)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    found = [(p, "lfs") for p in sorted(args.books_dir.glob("LFS-BOOK-*.tar.xz"))[-1:]] + \
            [(p, "blfs") for p in sorted(args.books_dir.glob("blfs-book-*-html.tar.xz"))[-1:]]
    if len(found) != 2:
        log(f"need both book tarballs in {args.books_dir}: run api/lfs-mirror.py --set books")
        return 2
    for tarball, book in found:
        version = re.search(r"(\d+\.\d+)", tarball.name).group(1)
        out = args.out_dir / f"{book}_en_systemd_{version}.zim"
        if args.dry_run:
            log(f"would write {out} from {tarball.name}")
            continue
        try:
            pages = build(tarball, out, book, version)
        except (OSError, tarfile.TarError, RuntimeError) as e:
            log(f"{tarball.name}: {e}")
            return 1
        log(f"{out.name}: {pages} pages, {out.stat().st_size / 2**20:.1f} MiB")
    return 0


if __name__ == "__main__":
    sys.exit(main())
