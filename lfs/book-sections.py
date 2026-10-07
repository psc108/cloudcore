#!/usr/bin/env python3
"""Split the LFS and BLFS books into sections for the build
(lfs-os-Phased-Implementation.md, A4a).

The build reads the book by section, not by search: working on GRUB means
reading section 8.65, and within it the 64-bit UEFI subsection, not the BIOS
one. For each page of each book this writes one record: its number and title,
the package and version it builds, its prose, and its commands in book order,
each with the subsection it belongs to and whether the book runs it as root
(BLFS's "As the root user:" blocks).

Reads the book tarballs mirrored by api/lfs-mirror.py; writes
sections-<book>-<version>.json beside them. Standard library only, as the
mirror scripts.

Usage: python3 lfs/book-sections.py [--books-dir DIR] [--dry-run]
Exit: 0 written; 1 a book couldn't be read; 2 bad usage.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import tarfile
from html import unescape
from html.parser import HTMLParser
from pathlib import Path

BOOKS_DIR = Path(__file__).resolve().parent.parent / "api" / "package-repo" / "jammy" / "artifacts" / "lfs" / "books"
PROSE_LIMIT = 20000  # characters per section: the model reads one section at a time


def log(msg: str) -> None:
    print(f"book-sections: {msg}", file=sys.stderr, flush=True)


class _Page(HTMLParser):
    """Collects a DocBook page's title, subsection headings, commands and prose."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.title = ""
        self.commands: list[dict] = []
        self.prose: list[str] = []
        self._heading: str | None = None   # "h1" or "sub" while inside one
        self._buf: list[str] = []
        self._pre: str | None = None       # "user" or "root" while inside a command block
        self._skip = 0                     # inside navigation, scripts, styles
        self.subsection = ""
        # Admonitions (Warning/Note/Important boxes) have their own heading;
        # it labels the commands inside, it isn't a new subsection (LFS-008).
        self._admon = 0                    # nesting depth inside div.admon
        self._admon_depth: list[int] = []  # div depth at each admonition's start
        self._divs = 0
        self.note = ""

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        cls = dict(attrs).get("class") or ""
        if tag == "div" and not self._skip:
            self._divs += 1
            if cls.split()[:1] == ["admon"]:
                self._admon_depth.append(self._divs)
        if tag in ("script", "style") or (tag == "div" and cls in ("navheader", "navfooter")):
            self._skip += 1
        elif self._skip:
            if tag == "div":
                self._skip += 1
        elif tag == "h1" and not self.title:
            self._heading, self._buf = "h1", []
        elif tag in ("h2", "h3", "h4") and self._pre is None:
            self._heading, self._buf = ("note" if self._admon_depth else "sub"), []
        elif tag == "pre" and cls in ("userinput", "root"):
            # LFS marks some root commands only in the sentence before them
            # ("As the root user, run:", 4.4), not with class="root" (LFS-017).
            lead = " ".join("".join(self.prose[-6:]).split())[-160:]
            said_root = bool(re.search(r"\b[Aa]s (?:the )?root(?: user)?\b[^.]*:\s*$", lead))
            self._pre, self._buf = ("root" if cls == "root" or said_root else "user"), []
        elif tag in ("p", "li", "dt", "dd", "br") and self._pre is None and self._heading is None:
            self.prose.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if self._skip:
            if tag in ("script", "style", "div"):
                self._skip -= 1
            return
        if tag == "div":
            if self._admon_depth and self._admon_depth[-1] == self._divs:
                self._admon_depth.pop()
                self.note = ""
            self._divs -= 1
        if tag == "h1" and self._heading == "h1":
            self.title = " ".join("".join(self._buf).split())
            self._heading = None
        elif tag in ("h2", "h3", "h4") and self._heading == "sub":
            self.subsection = " ".join("".join(self._buf).split())
            self.prose.append(f"\n## {self.subsection}\n")
            self._heading = None
        elif tag in ("h2", "h3", "h4") and self._heading == "note":
            self.note = " ".join("".join(self._buf).split())
            self.prose.append(f"\n[{self.note}]\n")
            self._heading = None
        elif tag == "pre" and self._pre is not None:
            text = "".join(self._buf).strip("\n")
            if text.strip():
                self.commands.append({"subsection": self.subsection, "note": self.note,
                                      "as_root": self._pre == "root", "text": text})
                self.prose.append(f"\n[command {len(self.commands)}]\n")
            self._pre = None

    def handle_data(self, data: str) -> None:
        if self._skip:
            return
        if self._heading is not None or self._pre is not None:
            self._buf.append(data)
        else:
            self.prose.append(data)


def _package(title: str) -> tuple[str, str, str]:
    """('GRUB', '2.14', '') from 'GRUB-2.14', ('Binutils', '2.47', 'Pass 1') from
    'Binutils-2.47 - Pass 1', ('Linux', '7.1.8', 'API Headers') from
    'Linux-7.1.8 API Headers'; empty for a page that isn't a package."""
    m = re.match(r"^(.+?)-(\d[\w.+]*(?:-\d[\w.+]*)?)(?:\s+-\s+(.+)|\s+(\w[\w ]*))?$", title)
    return (m.group(1), m.group(2), m.group(3) or m.group(4) or "") if m else ("", "", "")


def sections(tarball: Path, book: str) -> tuple[str, list[dict]]:
    out, version = [], ""
    with tarfile.open(tarball) as tar:
        for m in sorted(tar.getmembers(), key=lambda m: m.name):
            if not m.isfile() or not m.name.endswith(".html"):
                continue
            path = m.name.split("/", 1)[1] if book == "lfs" else m.name  # LFS's tarball has a version folder
            if book == "lfs" and not version:
                version = m.name.split("/", 1)[0]
            page = _Page()
            page.feed(tar.extractfile(m).read().decode("utf-8", "replace"))
            if not page.title:
                continue
            num = re.match(r"^((?:[A-Z]|\d+)(?:\.\d+)+|\d+)\.?\s+(.*)$", page.title)
            number, title = (num.group(1), num.group(2)) if num else ("", page.title)
            pkg, ver, stage = _package(title)
            prose = re.sub(r"\n\s*\n+", "\n\n", unescape("".join(page.prose))).strip()
            out.append({"book": book, "path": path, "chapter": path.split("/", 1)[0] if "/" in path else "",
                        "number": number, "title": title, "package": pkg, "version": ver, "stage": stage,
                        "commands": page.commands, "text": prose[:PROSE_LIMIT],
                        "truncated": len(prose) > PROSE_LIMIT})
    return version, out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--books-dir", type=Path, default=BOOKS_DIR)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    lfs = sorted(args.books_dir.glob("LFS-BOOK-*.tar.xz"))
    blfs = sorted(args.books_dir.glob("blfs-book-*-html.tar.xz"))
    if not lfs or not blfs:
        log(f"no LFS/BLFS book tarballs in {args.books_dir}: run api/lfs-mirror.py --set books")
        return 2
    for tarball, book in ((lfs[-1], "lfs"), (blfs[-1], "blfs")):
        try:
            version, recs = sections(tarball, book)
        except (OSError, tarfile.TarError) as e:
            log(f"can't read {tarball}: {e}")
            return 1
        version = version or re.search(r"(\d+\.\d+)", tarball.name).group(1)
        n_cmd = sum(len(r["commands"]) for r in recs)
        log(f"{book.upper()} {version}: {len(recs)} sections, {sum(bool(r['package']) for r in recs)} packages, "
            f"{n_cmd} command blocks, {sum(r['truncated'] for r in recs)} truncated")
        if not args.dry_run:
            out = args.books_dir / f"sections-{book}-{version}.json"
            out.write_text(json.dumps({"book": book, "version": version, "source": tarball.name,
                                       "sections": recs}, indent=1) + "\n")
            log(f"wrote {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
