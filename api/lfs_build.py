"""The LFS OS build's journal and task queue (lfs-os-Phased-Implementation.md, C1).

One build is an ordered queue of tasks, one per book section that has commands,
generated from the section files lfs/book-sections.py writes (A4a), plus the
BLFS packages stage 1 needs (UEFI tools, OpenSSH). Each task knows where its
commands run, which subsections to leave out, any version the build changes,
how risky it is, and whether a checkpoint comes before or after it.

The journal is the build's record "between the two of us": every proposal,
command, result, lesson and escalation, by whoever made it -- llm-chat, Claude,
the lab, Sentinel, Paul or the controller. It lives here, in CloudCore's own
database on the host (backed up nightly), not on the llm-chat coordinator,
which is rebuilt often while a build runs for weeks.

Routes (admin or the lab token; the coordinator reaches them on the guest
listener, 8083):
  POST /v1/lfs/builds                     create a build and its queue
  GET  /v1/lfs/builds                     list builds
  GET  /v1/lfs/builds/<id>                one build, with its task counts
  GET  /v1/lfs/builds/<id>/tasks          the queue (?state= to filter)
  GET  /v1/lfs/builds/<id>/next           the next task to work on
  GET  /v1/lfs/tasks/<id>                 one task, with its section and journal
  POST /v1/lfs/tasks/<id>/state           move a task on (waiting/running/done/stuck/escalated/skipped)
  POST /v1/lfs/tasks/<id>/journal         add a journal entry
  GET  /v1/lfs/builds/<id>/journal.md     the whole journal, readable
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path

from flask import Blueprint, Response, jsonify, request

import db

lfs_bp = Blueprint("lfs", __name__)

LFS_ENDPOINTS = {
    "lfs.create_build", "lfs.list_builds", "lfs.get_build", "lfs.update_build", "lfs.list_tasks", "lfs.next_task",
    "lfs.get_task", "lfs.set_task_state", "lfs.add_journal", "lfs.journal_markdown", "lfs.heartbeat",
}

API_DIR = Path(__file__).resolve().parent
BOOKS = API_DIR / "package-repo" / "jammy" / "artifacts" / "lfs" / "books"
MANIFEST = API_DIR.parent / "lfs" / "manifest.json"

STATES = ("waiting", "running", "done", "stuck", "escalated", "skipped")
WHO = ("llm-chat", "claude", "lab", "sentinel", "paul", "controller")
KINDS = ("proposal", "command", "result", "lesson", "escalation", "note", "state", "checkpoint")

# Where a section's commands run, by the book's own structure: chapters 2-4
# prepare the host as root (4.4 onwards sets up the lfs user), 5-6 build the
# cross-toolchain as the lfs user, 7 enters the chroot (7.2-7.4 still on the
# host, as root), 8 onwards inside it.
_HOST_ROOT_SECTIONS = {"7.2", "7.3", "7.4"}
# Sections where a mistake costs the most: a checkpoint is taken before each.
_HIGH_RISK = re.compile(r"^(?:Binutils|GCC|Glibc|Linux|GRUB)\b|Using GRUB|Linux-[\d.]+ API Headers")
# GRUB's pages build or install one boot method per subsection; this build is
# 64-bit UEFI.
_SKIP_SUBSECTION = re.compile(r"\bBIOS\b|32-bit")
# Single commands the build leaves out, and why (recorded with the task).
_SKIP_COMMANDS = [
    (re.compile(r"grub-mkrescue|xorriso"), "an optional rescue CD; the lab machine has no CD writer"),
    (re.compile(r"^\s*passwd lfs\s*$"), "the controller switches to the lfs user itself; no password is needed"),
    (re.compile(r"^\s*su - lfs\s*$"), "the controller runs the lfs user's tasks as lfs itself (an interactive shell can't be driven)"),
    (re.compile(r'^\s*chroot "\$LFS"'), "the controller enters the chroot for every step itself, with the book's "
                                          "env -i settings (an interactive login shell can't be driven)"),
    (re.compile(r"^\s*source ~/\.bash_profile\s*$"),
     "its exec env -i /bin/bash starts an interactive shell; the controller gives every lfs step a clean "
     "environment from the lfs user's .bashrc itself (LFS-017)"),
    (re.compile(r"^\s*(?:make -j32|export MAKEFLAGS=-j32)\s*$"),
     "the book's illustration for a 32-core CPU; its next command sets MAKEFLAGS from nproc for this machine (LFS-017)"),
    # Chapter 7 (LFS-025): an interactive login shell, and 7.15's backup, which
    # leaves the chroot. Each step runs in a fresh shell already, and the
    # checkpoint after chapter 7 snapshots both disks: that is the backup.
    (re.compile(r"^\s*exec /usr/bin/bash --login\s*$"),
     "it starts an interactive login shell; the controller runs every step in a fresh shell in the chroot (LFS-025)"),
    (re.compile(r"^\s*exit\s*$|umount \$LFS/\{sys,proc,run,dev\}|tar -cJpf \$HOME/lfs-temp-tools"),
     "the book's backup leaves the chroot; the controller's checkpoint after chapter 7 snapshots both disks instead (LFS-025)"),
    (re.compile(r"^\s*passwd root\s*$"),
     "the root password is Paul's to set, not the build's: the controller locks root's password instead, "
     "and Paul sets it himself (LFS-035)"),
    # LFS-041: util-linux's root test suite, in 8.81's Warning box: the book says
    # to run it only after booting the finished system (with scsi_debug).
    (re.compile(r"^\s*bash tests/run\.sh --srcdir=\$PWD --builddir=\$PWD\s*$"),
     "the book's Warning: run this root test suite only after booting the finished LFS system; it is in the "
     "boot checks (D6), not the build (LFS-041)"),
    # LFS-043: chapter 10 and BLFS stage 1.
    (re.compile(r"^\s*make menuconfig\s*$"),
     "interactive (a menu); the kernel is configured non-interactively with make defconfig and the kernel's own "
     "scripts/config instead, as the tutor's note for this section says (LFS-043)"),
    (re.compile(r"efivarfs|efibootmgr -c|umount -v /sys/firmware/efi/efivars"),
     "writes a boot entry into the firmware; in the chroot that would be the build machine's own firmware, and "
     "grub-install --removable (EFI/BOOT/BOOTX64.EFI) needs no entry (LFS-043)"),
    (re.compile(r"^\s*mount /boot\s*$"),
     "the book's caution: only for a separate /boot partition, and this build's disk has none (LFS-043)"),
    (re.compile(r"\bdoxygen\b"), "documentation built with doxygen, which isn't part of this build (LFS-043)"),
    (re.compile(r"ssh-copy-id -i ~/\.ssh/id_ed25519\.pub REMOTE_USERNAME@REMOTE_HOSTNAME"),
     "an example of copying a key to another machine; Paul adds his own key at first boot (LFS-043)"),
    (re.compile(r"sed 's@d/login@d/sshd@g' /etc/pam\.d/login"),
     "for Linux-PAM, which isn't part of this build (LFS-043)"),
    (re.compile(r"^\s*wget --input-file"), "the lab network can't reach the internet's mirrors this way; the controller "
                                          "delivers the verified sources from the host's repo (LFS-006)"),
]
# Sections the build needs although they have no commands: the book leaves the
# step to the reader (2.4: partition with cfdisk), so the model must write it.
_COMMANDLESS = {"2.4"}
# Reading sections: their commands illustrate, they aren't steps (LFS-029). 8.2
# shows package-management styles with a made-up libfoo; the 14B's first plan
# ("omit them all") was right and the controller refused it.
_EXAMPLE_DOTS = re.compile(r"(?:^|\s)\.\.\.(?:\s|$)")
_READING_SECTIONS = {"8.2": "a reading section: the book illustrates package-management styles with a made-up "
                            "libfoo; LFS installs each package directly, so there is nothing to run (LFS-029)",
                     # LFS-040: chapter 9 sections whose commands are all examples or
                     # alternatives that don't apply to this build (Paul, 2026-10-10).
                     "11.3": "logging out of the chroot and unmounting: the controller does this itself when it "
                             "makes the disk image for the first boot (D6) (LFS-043)",
                     "9.4": "examples for duplicate devices (a webcam, a TV tuner); this machine has none (LFS-040)",
                     "9.5": "for a hardware clock kept in local time, or set by hand: a VM's clock is UTC, the time "
                            "zone is already set (8.5, Europe/London), and systemd-timesyncd stays on (LFS-040)"}
# BLFS packages for stage 1, in build order (dependencies first), and where
# they go: the UEFI tools before LFS's GRUB set-up (10.4), OpenSSH last.
_BLFS_STAGE1 = [("general/popt.html", "10.4"), ("postlfs/efivar.html", "10.4"),
                ("postlfs/efibootmgr.html", "10.4"), ("postlfs/openssh.html", "end")]

_SCHEMA = """
CREATE TABLE IF NOT EXISTS lfs_builds (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    name         TEXT NOT NULL,
    lfs_version  TEXT NOT NULL,
    blfs_version TEXT NOT NULL,
    kernel       TEXT NOT NULL,
    stage        INTEGER NOT NULL DEFAULT 1,
    status       TEXT NOT NULL DEFAULT 'planned',
    build_vm_id  TEXT,
    created_at   TEXT NOT NULL,
    updated_at   TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS lfs_tasks (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    build_id         INTEGER NOT NULL REFERENCES lfs_builds(id),
    seq              INTEGER NOT NULL,
    book             TEXT NOT NULL,
    path             TEXT NOT NULL,
    number           TEXT NOT NULL,
    title            TEXT NOT NULL,
    package          TEXT NOT NULL,
    version          TEXT NOT NULL,
    stage_note       TEXT NOT NULL,
    chapter          TEXT NOT NULL,
    context          TEXT NOT NULL,
    skip_subsections TEXT NOT NULL,
    skip_commands    TEXT NOT NULL,
    version_override TEXT NOT NULL,
    risk             TEXT NOT NULL,
    checkpoint_before INTEGER NOT NULL,
    checkpoint_after INTEGER NOT NULL,
    n_commands       INTEGER NOT NULL,
    state            TEXT NOT NULL DEFAULT 'waiting',
    attempts         INTEGER NOT NULL DEFAULT 0,
    started_at       TEXT,
    finished_at      TEXT,
    updated_at       TEXT NOT NULL,
    UNIQUE (build_id, seq)
);
CREATE TABLE IF NOT EXISTS lfs_journal (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    build_id  INTEGER NOT NULL REFERENCES lfs_builds(id),
    task_id   INTEGER REFERENCES lfs_tasks(id),
    at        TEXT NOT NULL,
    who       TEXT NOT NULL,
    kind      TEXT NOT NULL,
    text      TEXT NOT NULL,
    data      TEXT
);
CREATE INDEX IF NOT EXISTS lfs_tasks_build ON lfs_tasks(build_id, seq);
CREATE INDEX IF NOT EXISTS lfs_journal_task ON lfs_journal(task_id, id);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _conn():
    conn = db.get_db()
    conn.executescript(_SCHEMA)
    # C4: the worker's heartbeat -- what it's doing now, for stall detection.
    cols = {r[1] for r in conn.execute("PRAGMA table_info(lfs_builds)")}
    for col in ("heartbeat_at", "heartbeat"):
        if col not in cols:
            conn.execute(f"ALTER TABLE lfs_builds ADD COLUMN {col} TEXT")
    return conn


def _problem(status: int, title: str, detail: str):
    return jsonify({"status": status, "title": title, "detail": detail}), status


def _num_key(number: str) -> tuple:
    return tuple(int(p) if p.isdigit() else 0 for p in number.split("."))


def _sections(book: str, version: str) -> list[dict]:
    return json.loads((BOOKS / f"sections-{book}-{version}.json").read_text())["sections"]


def _context(sec: dict) -> str:
    ch = int(sec["chapter"].removeprefix("chapter") or 0) if sec["chapter"].startswith("chapter") else 0
    if sec["book"] == "blfs":
        return "chroot"
    if ch <= 3 or (ch == 4 and _num_key(sec["number"]) < (4, 4)):
        return "host-root"
    if ch <= 6 or ch == 4:
        return "host-lfs"
    if sec["number"] in _HOST_ROOT_SECTIONS:
        return "host-root"
    return "chroot"


def plan_queue(lfs_version: str, blfs_version: str, kernel: str) -> list[dict]:
    """The ordered task list for stage 1, from the books' sections."""
    lfs = [s for s in _sections("lfs", lfs_version)
           if (s["commands"] or s["number"] in _COMMANDLESS) and re.fullmatch(r"chapter(0[2-9]|1[01])", s["chapter"] or "")]
    lfs.sort(key=lambda s: _num_key(s["number"]))
    blfs = {s["path"]: s for s in _sections("blfs", blfs_version)}
    extras = {}
    for path, before in _BLFS_STAGE1:
        if path not in blfs:
            raise ValueError(f"BLFS {blfs_version} has no {path}")
        extras.setdefault(before, []).append(blfs[path])
    ordered: list[dict] = []
    for s in lfs:
        if s["number"] in extras:
            ordered += extras.pop(s["number"])
        ordered.append(s)
    ordered += extras.pop("end", [])
    if extras:
        raise ValueError(f"no LFS section {', '.join(extras)} to place BLFS tasks before")
    out = []
    for i, s in enumerate(ordered):
        title = s["title"]
        skip = sorted({c["subsection"] for c in s["commands"] if _SKIP_SUBSECTION.search(c["subsection"] or "")})
        skip_cmds = [{"index": n, "why": why} for n, c in enumerate(s["commands"])
                     for rx, why in _SKIP_COMMANDS if rx.search(c["text"]) and c["subsection"] not in skip]
        override = kernel if s["package"] == "Linux" and s["version"] != kernel else ""
        high = bool(_HIGH_RISK.search(title))
        nxt = ordered[i + 1] if i + 1 < len(ordered) else None
        # BLFS packages come in one group however BLFS files them: one checkpoint after it.
        group = (lambda x: ("blfs",) if x["book"] == "blfs" else ("lfs", x["chapter"]))
        last_in_chapter = nxt is None or group(nxt) != group(s)
        out.append({"seq": i + 1, "book": s["book"], "path": s["path"], "number": s["number"], "title": title,
                    "package": s["package"], "version": s["version"], "stage_note": s.get("stage", ""),
                    "chapter": s["chapter"], "context": _context(s), "skip_subsections": json.dumps(skip),
                    "skip_commands": json.dumps(skip_cmds),
                    "version_override": override, "risk": "high" if high else "normal",
                    "checkpoint_before": int(high), "checkpoint_after": int(last_in_chapter),
                    "n_commands": sum(1 for c in s["commands"] if c["subsection"] not in skip) - len(skip_cmds)})
    return out


def _task_dict(row) -> dict:
    d = dict(row)
    d["skip_subsections"] = json.loads(d["skip_subsections"])
    d["skip_commands"] = json.loads(d["skip_commands"])
    d["checkpoint_before"], d["checkpoint_after"] = bool(d["checkpoint_before"]), bool(d["checkpoint_after"])
    return d


def _journal(conn, build_id: int, task_id: int | None, who: str, kind: str, text: str, data=None) -> int:
    cur = conn.execute("INSERT INTO lfs_journal (build_id, task_id, at, who, kind, text, data) VALUES (?,?,?,?,?,?,?)",
                       (build_id, task_id, _now(), who, kind, text[:20000],
                        json.dumps(data)[:200000] if data is not None else None))
    return cur.lastrowid


@lfs_bp.post("/v1/lfs/builds")
def create_build():
    body = request.get_json(force=True, silent=True) or {}
    name = str(body.get("name") or "").strip()[:80]
    if not name:
        return _problem(400, "Bad Request", "name is required")
    try:
        m = json.loads(MANIFEST.read_text())
        tasks = plan_queue(m["lfs_version"], m["blfs_version"], m["kernel"])
    except (OSError, ValueError, KeyError) as e:
        return _problem(409, "Conflict", f"can't plan the queue: {e} (run lfs/build-manifest.py, "
                                         "api/lfs-mirror.py and lfs/book-sections.py first)")
    conn = _conn()
    now = _now()
    cur = conn.execute("INSERT INTO lfs_builds (name, lfs_version, blfs_version, kernel, created_at, updated_at) "
                       "VALUES (?,?,?,?,?,?)", (name, m["lfs_version"], m["blfs_version"], m["kernel"], now, now))
    bid = cur.lastrowid
    cols = list(tasks[0]) + ["build_id", "updated_at"]
    conn.executemany(f"INSERT INTO lfs_tasks ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})",
                     [tuple(t[c] for c in tasks[0]) + (bid, now) for t in tasks])
    _journal(conn, bid, None, "controller", "note",
             f"Build '{name}' planned: LFS {m['lfs_version']} (systemd), kernel {m['kernel']}, "
             f"{len(tasks)} tasks, {sum(t['checkpoint_before'] or t['checkpoint_after'] for t in tasks)} checkpoints.")
    conn.commit()
    return jsonify(_build_dict(conn, bid)), 201


def _build_dict(conn, bid: int) -> dict:
    b = conn.execute("SELECT * FROM lfs_builds WHERE id=?", (bid,)).fetchone()
    if not b:
        return {}
    counts = {r["state"]: r["n"] for r in conn.execute(
        "SELECT state, COUNT(*) AS n FROM lfs_tasks WHERE build_id=? GROUP BY state", (bid,))}
    d = dict(b)
    try:
        d["heartbeat"] = json.loads(d.get("heartbeat") or "null")
    except ValueError:
        d["heartbeat"] = None
    return {**d, "tasks": counts, "total": sum(counts.values())}


@lfs_bp.get("/v1/lfs/builds")
def list_builds():
    conn = _conn()
    return jsonify({"items": [_build_dict(conn, r["id"]) for r in
                              conn.execute("SELECT id FROM lfs_builds ORDER BY id DESC")]})


@lfs_bp.get("/v1/lfs/builds/<int:bid>")
def get_build(bid):
    d = _build_dict(_conn(), bid)
    return (jsonify(d), 200) if d else _problem(404, "Not Found", f"no build {bid}")


@lfs_bp.post("/v1/lfs/builds/<int:bid>")
def update_build(bid):
    """Body: {status?, build_vm_id?} -- the controller records its build machine."""
    body = request.get_json(force=True, silent=True) or {}
    sets = {k: str(body[k])[:80] for k in ("status", "build_vm_id") if k in body}
    if "status" in sets and sets["status"] not in ("planned", "running", "paused", "done", "failed"):
        return _problem(400, "Bad Request", "status: planned, running, paused, done or failed")
    if not sets:
        return _problem(400, "Bad Request", "nothing to change")
    conn = _conn()
    if not conn.execute("SELECT 1 FROM lfs_builds WHERE id=?", (bid,)).fetchone():
        return _problem(404, "Not Found", f"no build {bid}")
    sets["updated_at"] = _now()
    conn.execute(f"UPDATE lfs_builds SET {', '.join(k + '=?' for k in sets)} WHERE id=?", (*sets.values(), bid))
    _journal(conn, bid, None, "controller", "note", "build " + ", ".join(f"{k}={v}" for k, v in sets.items()
                                                                          if k != "updated_at"))
    conn.commit()
    return jsonify(_build_dict(conn, bid))


@lfs_bp.post("/v1/lfs/builds/<int:bid>/heartbeat")
def heartbeat(bid):
    """The worker says it's alive and what it's doing (C4): {task_id, number,
    phase, pid}. Sentinel spots a stall from this and the journal, never from
    log silence -- GCC's build is quiet for long stretches."""
    body = request.get_json(force=True, silent=True) or {}
    hb = {k: body[k] for k in ("task_id", "number", "phase", "pid", "since") if k in body}
    conn = _conn()
    cur = conn.execute("UPDATE lfs_builds SET heartbeat_at=?, heartbeat=? WHERE id=?",
                       (_now(), json.dumps(hb)[:2000], bid))
    if not cur.rowcount:
        return _problem(404, "Not Found", f"no build {bid}")
    conn.commit()
    return "", 204


@lfs_bp.get("/v1/lfs/builds/<int:bid>/tasks")
def list_tasks(bid):
    state = request.args.get("state")
    q, args = "SELECT * FROM lfs_tasks WHERE build_id=?", [bid]
    if state:
        q, args = q + " AND state=?", args + [state]
    return jsonify({"items": [_task_dict(r) for r in _conn().execute(q + " ORDER BY seq", args)]})


@lfs_bp.get("/v1/lfs/builds/<int:bid>/next")
def next_task(bid):
    """The first task not done or skipped, in book order. A stuck or
    escalated task stops the queue: the book's order matters."""
    r = _conn().execute("SELECT * FROM lfs_tasks WHERE build_id=? AND state NOT IN ('done','skipped') "
                        "ORDER BY seq LIMIT 1", (bid,)).fetchone()
    return jsonify({"task": _task_dict(r) if r else None})


@lfs_bp.get("/v1/lfs/tasks/<int:tid>")
def get_task(tid):
    conn = _conn()
    r = conn.execute("SELECT * FROM lfs_tasks WHERE id=?", (tid,)).fetchone()
    if not r:
        return _problem(404, "Not Found", f"no task {tid}")
    t = _task_dict(r)
    b = conn.execute("SELECT * FROM lfs_builds WHERE id=?", (t["build_id"],)).fetchone()
    version = b["lfs_version"] if t["book"] == "lfs" else b["blfs_version"]
    sec = next((s for s in _sections(t["book"], version) if s["path"] == t["path"]), None)
    if sec:
        # Numbered as in the book, so a skipped command's index means the same everywhere.
        skipped = {s["index"]: s["why"] for s in t["skip_commands"]}
        # Rules added since the build was planned apply too (LFS-017).
        for n, c in enumerate(sec["commands"]):
            if t["number"] in _READING_SECTIONS and t["book"] == "lfs":
                skipped.setdefault(n, _READING_SECTIONS[t["number"]])
            # LFS-034: a command inside one of the book's notes, with a bare `...`,
            # is an example, not a step (8.23 GMP: `ABI=32 ./configure ...` for 32-bit x86).
            if c.get("note") and _EXAMPLE_DOTS.search(c["text"]):
                skipped.setdefault(n, f"an example in the book's note ({c['note']}): its '...' stands for the rest "
                                      "of a command, and the note's case doesn't apply to this build (LFS-034)")
            if n not in skipped and c["subsection"] not in t["skip_subsections"]:
                why = next((w for rx, w in _SKIP_COMMANDS if rx.search(c["text"])), None)
                if why:
                    skipped[n] = why
        sec = {**sec, "commands": [{**c, "index": n, **({"skipped": skipped[n]} if n in skipped else {})}
                                   for n, c in enumerate(sec["commands"])
                                   if c["subsection"] not in t["skip_subsections"]]}
    journal = [dict(j) for j in conn.execute("SELECT * FROM lfs_journal WHERE task_id=? ORDER BY id", (tid,))]
    return jsonify({**t, "section": sec, "journal": journal})


@lfs_bp.post("/v1/lfs/tasks/<int:tid>/state")
def set_task_state(tid):
    body = request.get_json(force=True, silent=True) or {}
    state, who, why = body.get("state"), body.get("who", "controller"), str(body.get("why") or "")
    if state not in STATES or who not in WHO:
        return _problem(400, "Bad Request", f"state is one of {STATES}; who is one of {WHO}")
    conn = _conn()
    r = conn.execute("SELECT * FROM lfs_tasks WHERE id=?", (tid,)).fetchone()
    if not r:
        return _problem(404, "Not Found", f"no task {tid}")
    now = _now()
    sets = {"state": state, "updated_at": now}
    if state == "running":
        sets.update(started_at=r["started_at"] or now, attempts=r["attempts"] + 1)
    if state in ("done", "skipped"):
        sets["finished_at"] = now
    conn.execute(f"UPDATE lfs_tasks SET {', '.join(k + '=?' for k in sets)} WHERE id=?", (*sets.values(), tid))
    conn.execute("UPDATE lfs_builds SET updated_at=?, status=CASE WHEN status='planned' THEN 'running' ELSE status END "
                 "WHERE id=?", (now, r["build_id"]))
    _journal(conn, r["build_id"], tid, who, "state", f"{r['state']} -> {state}" + (f": {why}" if why else ""))
    conn.commit()
    return jsonify(_task_dict(conn.execute("SELECT * FROM lfs_tasks WHERE id=?", (tid,)).fetchone()))


@lfs_bp.post("/v1/lfs/tasks/<int:tid>/journal")
def add_journal(tid):
    body = request.get_json(force=True, silent=True) or {}
    who, kind, text = body.get("who"), body.get("kind"), str(body.get("text") or "").strip()
    if who not in WHO or kind not in KINDS or not text:
        return _problem(400, "Bad Request", f"who is one of {WHO}; kind one of {KINDS}; text is required")
    conn = _conn()
    r = conn.execute("SELECT build_id FROM lfs_tasks WHERE id=?", (tid,)).fetchone()
    if not r:
        return _problem(404, "Not Found", f"no task {tid}")
    jid = _journal(conn, r["build_id"], tid, who, kind, text, body.get("data"))
    conn.commit()
    return jsonify({"id": jid}), 201


@lfs_bp.get("/v1/lfs/builds/<int:bid>/journal.md")
def journal_markdown(bid):
    """The journal as a document: tasks in book order, each with its entries."""
    conn = _conn()
    b = conn.execute("SELECT * FROM lfs_builds WHERE id=?", (bid,)).fetchone()
    if not b:
        return _problem(404, "Not Found", f"no build {bid}")
    lines = [f"# LFS build journal: {b['name']}", "",
             f"LFS {b['lfs_version']} (systemd), BLFS {b['blfs_version']}, kernel {b['kernel']}. "
             f"Status: {b['status']}. Created {b['created_at']}.", ""]
    for j in conn.execute("SELECT * FROM lfs_journal WHERE build_id=? AND task_id IS NULL ORDER BY id", (bid,)):
        lines.append(f"- {j['at']} **{j['who']}** ({j['kind']}): {j['text']}")
    for t in conn.execute("SELECT * FROM lfs_tasks WHERE build_id=? ORDER BY seq", (bid,)):
        entries = list(conn.execute("SELECT * FROM lfs_journal WHERE task_id=? ORDER BY id", (t["id"],)))
        if not entries and t["state"] == "waiting":
            continue
        lines += ["", f"## {t['seq']}. {t['number'] + ' ' if t['number'] else ''}{t['title']} ({t['book'].upper()})", "",
                  f"State: **{t['state']}**, attempts {t['attempts']}, runs in `{t['context']}`."]
        for j in entries:
            text = j["text"] if "\n" not in j["text"] else "\n\n```\n" + j["text"] + "\n```"
            lines.append(f"- {j['at']} **{j['who']}** ({j['kind']}): {text}")
    return Response("\n".join(lines) + "\n", mimetype="text/markdown")
