#!/usr/bin/env python3
"""Offline checks for the LFS worker's plan checker (examples/llm-chat/files/
lfs_worker.py, plan()): the 14B's real replies from build 1, each with the
verdict the checker must give.

The checker has been tightened and then over-tightened, one live build stop
at a time (LFS-020 guards, LFS-021, LFS-022). Every reply that stopped the
build, or should have, is a case here. Add one with each new finding. Runs
with no network and no build machine: the model, the machine facts and the
journal are stubbed.

Usage: python3 tests/lfs_plan_check.py
Exit: 0 every case as expected; 1 otherwise.
"""

from __future__ import annotations

import json
import sys
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.modules.setdefault("paramiko", types.SimpleNamespace(SSHException=Exception, AuthenticationException=Exception))
sys.path.insert(0, str(ROOT / "examples" / "llm-chat" / "files"))
import lfs_worker as w  # noqa: E402

# The book's commands for the sections in the cases (LFS 13.1, as numbered).
SECTIONS = {
    "2.4": [],
    "2.7": ["mkdir -pv $LFS\nmount -v -t ext4 /dev/<xxx> $LFS", "mkdir -v $LFS/home\nmount -v -t ext4 /dev/<yyy> $LFS/home",
            "/sbin/swapon -v /dev/<zzz>"],
    "4.2": ["mkdir -pv $LFS/{etc,var} $LFS/usr/{bin,lib,sbin}\n\nfor i in bin lib sbin; do\n  ln -sv usr/$i $LFS/$i\n"
            "done\n\ncase $(uname -m) in\n  x86_64) mkdir -pv $LFS/lib64 ;;\nesac", "mkdir -pv $LFS/tools"],
    "5.2": ["mkdir -v build\ncd       build",
            "../configure --prefix=$LFS/tools \\\n             --with-sysroot=$LFS \\\n             --target=$LFS_TGT   "
            "\\\n             --disable-nls       \\\n             --enable-gprofng=no \\\n             --disable-werror    "
            "\\\n             --enable-new-dtags  \\\n             --enable-default-hash-style=gnu", "make", "make install"],
    "5.3": ["tar -xf ../mpfr-4.2.2.tar.xz\nmv -v mpfr-4.2.2 mpfr", "mkdir -v build\ncd       build", "make", "make install"],
    "5.4": ["make mrproper", "make headers\nfind usr/include -type f ! -name '*.h' -delete\ncp -rv usr/include $LFS/usr"],
    "5.5": ["case $(uname -m) in\n    x86_64) ln -sfv ../lib/ld-linux-x86-64.so.2 $LFS/lib64\n    ;;\nesac",
            "mkdir -v build\ncd       build", "../configure --prefix=/usr --host=$LFS_TGT --disable-nscd", "make"],
}
CONTEXT = {"2.4": "host-root", "2.7": "host-root", "4.2": "host-root"}
OVERRIDE = {"5.4": ("7.1.8", "7.2.9")}

# (name, section, reply's "changes", expect accepted?, a word the refusal must contain)
CASES = [
    # LFS-022: a reason alone is a remark; the plan is "as the book".
    ("5.4 remark only (LFS-022)", "5.4", [{"why": "The section's commands do not need any changes."}], True, ""),
    ("5.4 no changes", "5.4", [], True, ""),
    # LFS-020: a change that drops the book command's lines.
    ("5.4 make headers only (LFS-020)", "5.4", [{"book": 1, "run": "make headers", "why": "version"}], False, "drops"),
    # LFS-021: the same lines split over invented command numbers.
    ("5.4 split into [1],[2],[3] (LFS-021)", "5.4",
     [{"book": 1, "run": "make headers", "why": "v"}, {"book": 2, "run": "find usr/include -type f ! -name '*.h' -delete", "why": "v"},
      {"book": 3, "run": "cp -rv usr/include $LFS/usr", "why": "v"}], False, "isn't in the list"),
    # LFS-020: bootloader work as root in an lfs-user section.
    ("5.5 grub-install as root (LFS-020)", "5.5",
     [{"after": -1, "run": "grub-install --target=x86_64-efi --efi-directory=$LFS/boot/efi", "as": "root", "why": "UEFI"}],
     False, "grub-install"),
    ("5.5 mount added (LFS-020)", "5.5", [{"after": -1, "run": "mount -v /dev/sdb1 $LFS/boot/efi", "why": "UEFI"}], False, "mount"),
    ("5.5 sudo apt-get (LFS-020)", "5.5", [{"after": 2, "run": "sudo apt-get install linux-libc-dev", "why": "headers"}], False, "sudo"),
    ("5.5 option added to configure", "5.5",
     [{"book": 2, "run": "../configure --prefix=/usr --host=$LFS_TGT --disable-nscd --enable-kernel=5.10", "why": "x"}], True, ""),
    # LFS-019: an internet download.
    ("5.3 wget added (LFS-019)", "5.3", [{"after": -1, "run": "wget https://ftp.gnu.org/gnu/gcc/gcc-16.2.0/gcc-16.2.0.tar.xz", "why": "x"}],
     False, "internet"),
    # LFS-019: the tutor's own change must pass.
    ("5.3 tutor's make || make -j1 (LFS-019)", "5.3",
     [{"book": 2, "run": "make || { rm -f gcc/cc1 gcc/cc1plus gcc/lto1 gcc/lto-dump; make -j1; }", "why": "memory"}], True, ""),
    # LFS-018: a copy of the book's commands, timed.
    ("5.2 timed copy of the build (LFS-018)", "5.2",
     [{"after": -1, "run": "time { ../configure --prefix=$LFS/tools --with-sysroot=$LFS --target=$LFS_TGT --disable-nls "
       "--enable-gprofng=no --disable-werror --enable-new-dtags --enable-default-hash-style=gnu && make && make install; }",
       "why": "SBU"}], False, "repeats"),
    ("5.2 target written out", "5.2",
     [{"book": 1, "run": "../configure --prefix=$LFS/tools --with-sysroot=$LFS --target=x86_64-lfs-linux-gnu --disable-nls "
       "--enable-gprofng=no --disable-werror --enable-new-dtags --enable-default-hash-style=gnu", "why": "target"}], True, ""),
    # LFS-016: an invented command number; leaving everything out.
    ("4.2 omit command [3] (LFS-016)", "4.2", [{"book": 3, "omit": True, "why": "editors' note"}], False, "isn't in the list"),
    ("4.2 omit everything (LFS-016)", "4.2", [{"book": 0, "omit": True, "why": "done"}, {"book": 1, "omit": True, "why": "done"}],
     False, "nothing would run"),
    # LFS-014: 2.7 done right; dropping the book's mkdir.
    ("2.7 as tutored (LFS-014)", "2.7",
     [{"book": 0, "run": "mkdir -pv $LFS\nmount -v -t ext4 /dev/sdb2 $LFS", "why": "partition"},
      {"after": 0, "run": "mkdir -pv $LFS/boot/efi && mount -v -t vfat /dev/sdb1 $LFS/boot/efi", "why": "ESP"},
      {"book": 1, "omit": True, "why": "no /home partition"}, {"book": 2, "omit": True, "why": "no swap"}], True, ""),
    ("2.7 mkdir dropped", "2.7", [{"book": 0, "run": "mount -v -t ext4 /dev/sdb2 $LFS", "why": "partition"},
                                  {"book": 1, "omit": True, "why": "x"}, {"book": 2, "omit": True, "why": "x"}], False, "drops"),
    ("2.7 placeholder left", "2.7", [{"book": 1, "omit": True, "why": "x"}, {"book": 2, "omit": True, "why": "x"}], False, "placeholder"),
    # 2.4: a commandless section writes its own sgdisk steps.
    ("2.4 sgdisk steps", "2.4", [{"run": "sgdisk -og /dev/sdb", "why": "GPT"},
                                 {"run": "sgdisk -n 1:0:+512M -t 1:ef00 /dev/sdb", "why": "ESP"}], True, ""),
    ("2.4 the system disk", "2.4", [{"run": "sgdisk -og /dev/sda", "why": "GPT"}], False, "outside the LFS disk"),
]


def task_for(number: str) -> dict:
    book_v, override = OVERRIDE.get(number, ("1.0", ""))
    return {"id": 1, "build_id": 1, "seq": 1, "number": number, "title": f"section {number}", "book": "lfs",
            "context": CONTEXT.get(number, "host-lfs"), "version": book_v, "version_override": override,
            "journal": [], "_cwd": "/mnt/lfs/sources/pkg", "_srcdir": "pkg",
            "section": {"text": "", "commands": [{"index": i, "text": c, "subsection": "", "as_root": False}
                                                 for i, c in enumerate(SECTIONS[number])]}}


def main() -> int:
    w.facts = lambda m: "LFS disk /dev/sdb; system disk /dev/sda"
    w.journal = lambda *a, **k: None
    w._next_sections = lambda task: "(none)"
    build = {"lfs_version": "13.1"}
    failed = 0
    for name, number, changes, ok, word in CASES:
        reply = json.dumps({"changes": changes, "expect": "x"})
        w.ask_model = lambda *a, _r=reply, **k: (_r, "test")
        steps, why = w.plan(task_for(number), build, None)
        accepted = bool(steps)
        good = accepted == ok and (ok or word in why)
        failed += not good
        print(f"{'ok  ' if good else 'FAIL'} {name}: {'accepted' if accepted else 'refused'}"
              + ("" if good else f" (expected {'accepted' if ok else 'refused, mentioning ' + repr(word)}): {why[:200]}"))
    print(f"{len(CASES) - failed}/{len(CASES)} as expected")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
