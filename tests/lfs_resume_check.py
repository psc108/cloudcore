#!/usr/bin/env python3
"""Offline simulation of the LFS worker resuming a task from where it failed
(CC-95), against a fake build machine: the real run_task(), with the API, the
model and the machine stubbed.

Cases:
  1. attempt 1 dies at step 3 -> attempt 2 resumes at step 3, in the same tree,
     without unpacking again;
  2. a lesson changes step 2, which already ran -> attempt 2 starts afresh;
  3. a first attempt behaves as before: unpack, then every step;
  4. the tree has gone (say, a checkpoint restore) -> start afresh;
  5. a task that completes removes its marker.

Usage: python3 tests/lfs_resume_check.py
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


class Died(Exception):
    """The worker process dying mid-step."""


class FakeMachine:
    def __init__(self) -> None:
        self.files: dict[str, str] = {}
        self.tree = False
        self.unpacks = 0
        self.ran: list[int] = []
        self.die_at: int | None = None

    def sh(self, cmd: str, timeout: int = 120) -> tuple[int, str]:
        if cmd.startswith("cat ") and ".progress" in cmd:
            path = cmd.split()[1]
            return (0, self.files[path]) if path in self.files else (1, "")
        if cmd.startswith("rm -f ") and ".progress" in cmd:
            self.files.pop(cmd.split()[2], None)
            return 0, ""
        if "tar -tf" in cmd:
            return 0, "pkg-1.0\n"
        if cmd.startswith("test -d"):
            return (0 if self.tree else 1), ""
        if "tar -xf" in cmd:
            self.tree, self.unpacks = True, self.unpacks + 1
            return 0, ""
        if "rm -rf" in cmd:
            self.tree = False
            return 0, ""
        return 0, ""

    def put(self, data: bytes, path: str, mode: int = 0o644) -> None:
        self.files[path] = data.decode()

    def run_detached(self, script: str, launcher: str, name: str, timeout_s: int = 0) -> tuple[int, str, float]:
        step = int(name.split("-step")[1].split("-")[0])
        if self.die_at == step:
            raise Died()
        self.ran.append(step)
        return 0, f"ok\n@@lfs-cwd=/sources/pkg-1.0/build{step}\n", 1.0


def make_task(attempts: int) -> dict:
    return {"id": 9, "seq": 9, "build_id": 1, "number": "8.99", "title": "Pkg-1.0", "book": "lfs", "context": "chroot",
            "package": "Pkg", "version": "1.0", "version_override": "", "attempts": attempts,
            "checkpoint_before": 0, "checkpoint_after": 0, "journal": [],
            "section": {"text": "", "commands": [{"index": i, "text": f"cmd{i}", "subsection": ""} for i in range(5)]}}


def run(m: FakeMachine, attempts: int, steps: list[str]) -> tuple[bool | str, list[str]]:
    notes: list[str] = []
    w.api = lambda method, path, body=None, timeout=0: (200, make_task(attempts))
    w.set_state = lambda *a, **k: None
    w.journal = lambda tid, who, kind, text, data=None: notes.append(text)
    w.ensure_mounts = lambda *a, **k: None
    w.phase = lambda *a, **k: None
    w.plan = lambda task, build, m, feedback="": ([{"book": i, "run": r, "as": None, "why": "", "changed": False}
                                                    for i, r in enumerate(steps)], "")
    manifest = {"files": [{"kind": "source", "set": "lfs", "file": "pkg-1.0.tar.xz"}]}
    try:
        ok: bool | str = w.run_task(9, {"lfs_version": "13.1"}, m, manifest, set())
    except Died:
        ok = "died"
    return ok, notes


def main() -> int:
    failed = 0
    steps = ["./configure", "make", "make check", "make install", "ldconfig"]

    def check(name: str, cond: bool, detail: str = "") -> None:
        nonlocal failed
        failed += not cond
        print(f"{'ok  ' if cond else 'FAIL'} {name}" + ("" if cond else f": {detail}"))

    # 1. dies at step 3, then resumes there
    m = FakeMachine()
    m.die_at = 3
    ok, _ = run(m, 0, steps)
    check("1a attempt 1 unpacks once and runs steps 1-2, then dies at 3", ok == "died" and m.unpacks == 1 and m.ran == [1, 2],
          f"{ok} unpacks={m.unpacks} ran={m.ran}")
    marker = json.loads(m.files[w._progress_path(make_task(1))])
    check("1b the marker says 2 done, with step 2's directory", marker["done"] == 2 and marker["cwd"].endswith("build2"), str(marker))
    m.die_at, m.ran = None, []
    ok, notes = run(m, 1, steps)
    check("1c attempt 2 resumes at step 3 without unpacking", ok is True and m.ran == [3, 4, 5] and m.unpacks == 1,
          f"{ok} ran={m.ran} unpacks={m.unpacks}")
    check("1d it journals the resume", any("resuming at step 3 of 5" in n for n in notes), str(notes[-3:]))
    check("5  a completed task removes its marker", w._progress_path(make_task(1)) not in m.files, str(m.files))

    # 2. a lesson changed step 2, which already ran
    m = FakeMachine()
    m.die_at = 3
    run(m, 0, steps)
    m.die_at, m.ran = None, []
    changed = ["./configure", "make -j1", "make check", "make install", "ldconfig"]
    ok, notes = run(m, 1, changed)
    check("2  a changed earlier step starts afresh: unpacks again, runs all", ok is True and m.ran == [1, 2, 3, 4, 5] and m.unpacks == 2,
          f"{ok} ran={m.ran} unpacks={m.unpacks}")
    check("2b it journals why", any("starting afresh" in n for n in notes), str(notes[-3:]))

    # 3. first attempt, as before
    m = FakeMachine()
    ok, _ = run(m, 0, steps)
    check("3  a first attempt unpacks and runs every step", ok is True and m.ran == [1, 2, 3, 4, 5] and m.unpacks == 1,
          f"{ok} ran={m.ran}")

    # 4. the tree has gone (a checkpoint restore rewound it; here, just deleted)
    m = FakeMachine()
    m.die_at = 3
    run(m, 0, steps)
    m.tree, m.die_at, m.ran = False, None, []
    ok, _ = run(m, 1, steps)
    check("4  no tree: start afresh even with a marker", ok is True and m.ran == [1, 2, 3, 4, 5] and m.unpacks == 2,
          f"{ok} ran={m.ran} unpacks={m.unpacks}")

    total = 8
    print(f"{total - failed}/{total} as expected")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
