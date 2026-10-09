#!/usr/bin/env python3
"""The LFS build's tutor sessions (lfs-os-Phased-Implementation.md, C5 rung 3).

When a build task is stuck and Sentinel has asked for a tutor (a journal
escalation {"rung": 3, "status": "requested"}), this runs one headless Claude
Code session on it and writes the result back to the journal:

  - a lesson for llm-chat (the 14B), which the worker puts in front of the
    model next time, and the task reset to "waiting" -- llm-chat carries on;
  - or, when the tutor finds it can't be taught (a controller bug, a broken
    machine), rung 4: the build paused for Paul, which Sentinel notifies.

Guardrails:
  - no tools: the session gets the journal (each failed step's 16 KB output
    tail, the machine facts the 14B saw, every proposal and lesson), the
    book's section and Sentinel's matches, and can only answer in the
    lesson's JSON schema. It can't touch the build machine, the journal or
    this host;
  - one session at a time (a lock), and a daily cap (LFS_TUTOR_DAILY_CAP);
    at the cap the build pauses for Paul instead;
  - every session is kept (LFS_TUTOR_STATE_DIR/sessions) and its lesson,
    with a finding, goes into the journal, which Sentinel ingests into its
    knowledge base.

Runs from a user timer on the host where Claude Code is signed in. The
LFS API is reached over SSH (LFS_API_SSH): its admin token stays on the API
host. Standard library only.

Usage: lfs/lfs-tutor.py [--dry-run] [--build N]
Exit: 0 nothing to do or a session done; 1 a session failed; 2 bad config.
"""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import shlex
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

API_SSH = os.environ.get("LFS_API_SSH", "")             # user@host of the API host
API_SSH_KEY = os.environ.get("LFS_API_SSH_KEY", str(Path.home() / ".ssh" / "id_ed25519"))
API_TOKEN_FILE = os.environ.get("LFS_API_TOKEN_FILE", "~/.config/cloudcore/api.env")  # on the API host
DAILY_CAP = int(os.environ.get("LFS_TUTOR_DAILY_CAP", "6"))
MODEL = os.environ.get("LFS_TUTOR_MODEL", "opus")
CLAUDE_BIN = os.environ.get("LFS_TUTOR_CLAUDE", "claude")  # a user unit's PATH may not include it
SESSION_TIMEOUT_S = int(os.environ.get("LFS_TUTOR_TIMEOUT_S", "1200"))
STATE = Path(os.environ.get("LFS_TUTOR_STATE_DIR", str(Path.home() / ".local" / "state" / "lfs-tutor")))

LESSON_SCHEMA = {
    "type": "object",
    "required": ["diagnosis", "decision", "lesson", "finding"],
    "properties": {
        "diagnosis": {"type": "string", "description": "what actually went wrong, for the journal"},
        "decision": {"type": "string", "enum": ["retry with the lesson", "needs Paul"]},
        "lesson": {"type": "string", "description": "for llm-chat: what to do in this section and why"},
        "finding": {"type": "object", "required": ["title", "symptom", "root_cause", "fix"], "properties": {
            "title": {"type": "string"}, "symptom": {"type": "string"},
            "root_cause": {"type": "string"}, "fix": {"type": "string"}}},
        "why_paul": {"type": "string", "description": "only with 'needs Paul': what he must decide or fix"},
    },
}

TUTOR_SYSTEM = """You are the tutor for an automated Linux From Scratch build (LFS {lfs}, systemd, 64-bit UEFI). A 14B model (llm-chat) does the work one book section at a time: it plans only differences from the book's commands, a controller runs each step on a build machine, and when a step fails the 14B proposes up to two repairs. It is stuck on the section below, and Sentinel has asked you for help.

Your job is to TEACH, not to build: you have no tools. Work out from the journal what actually went wrong, then write one lesson the 14B will read before its next attempt at this section (it reads it as "your tutor's notes: follow them"). A good lesson:
- names the cause, from the evidence (quote the error);
- says exactly what to run or change, as book command numbers [n] and commands;
- says what NOT to do (repeat a failed repair, delete work, invent reasons);
- is short and plain: the 14B follows concrete instructions well and abstract advice badly.

Choose "needs Paul" only when no lesson to the 14B can fix it: the controller itself is wrong (it runs a step in the wrong place or as the wrong user, loses state between steps, mis-parses the book), the build machine is broken, or the book's own instructions can't work here. Say why in why_paul.

The finding is for the knowledge base: the symptom as it shows in output, the root cause, and the fix, so the next similar problem is matched to it.

The controller's rules, for judging what's possible: steps run non-interactively in the task's context (root on the host, the lfs user with the book's .bashrc, or root in the chroot with the book's environment); each step starts where the previous one ended (cd carries) and runs under `set -e` (in `a; b` a failing `a` ends the step; tolerate one with `a || true`); the book's commands run exactly unless the 14B changes them; repairs that delete directories outside the package's own tree are refused; added steps that copy book commands are refused; the controller times steps itself; for a package section the controller unpacks the package's tarball and starts the first step in its source directory, journalling "unpacked <tarball> into <dir>".

A fault in the controller is "needs Paul" at once, not a workaround for the 14B to carry: for example a package section with no "unpacked" note (nothing to build in), a step run as the wrong user, or state lost between steps. Lessons that work around the controller make the 14B fight its own guards."""


def log(msg: str) -> None:
    print(f"lfs-tutor: {msg}", file=sys.stderr, flush=True)


def api(method: str, path: str, body: dict | None = None) -> tuple[int, dict]:
    """The LFS API on the API host, through SSH: the token is read and used there,
    via a process substitution, so it is never on a command line or sent here."""
    remote = ("bash -c " + shlex.quote(
        f"set -a; . {API_TOKEN_FILE}; set +a; "
        f"curl -s -m 60 -w '\\n%{{http_code}}' -X {method} "
        "-H @<(printf 'Authorization: Bearer %s\\n' \"$CLOUDCORE_API_TOKEN\") "
        "-H 'Content-Type: application/json' "
        + ("--data-binary @- " if body is not None else "")
        + shlex.quote(f"http://127.0.0.1:8080{path}")))
    r = subprocess.run(["ssh", "-o", "IdentityAgent=none", "-o", "IdentitiesOnly=yes", "-i", API_SSH_KEY,
                        "-o", "BatchMode=yes", "-o", "ConnectTimeout=15", API_SSH, remote],
                       input=json.dumps(body) if body is not None else "", capture_output=True, text=True,
                       timeout=120)
    if r.returncode != 0:
        raise RuntimeError(f"ssh to {API_SSH} failed ({r.returncode}): {r.stderr.strip()[:300]}")
    raw, _, code = r.stdout.rpartition("\n")
    try:
        return int(code), (json.loads(raw) if raw.strip()[:1] in ("{", "[") else {})
    except ValueError:
        return 0, {"detail": r.stdout[:300]}


def _data(e: dict) -> dict:
    try:
        return json.loads(e.get("data") or "null") or {}
    except ValueError:
        return {}


def wanted(task: dict) -> bool:
    """Sentinel asked for a tutor in this stuck episode, and nobody has answered."""
    j = task["journal"]
    stuck_at = max((i for i, e in enumerate(j) if e["kind"] == "state"
                    and e["text"].split(":")[0].rstrip().endswith("-> stuck")), default=-1)
    after = [(e, _data(e)) for e in j[stuck_at + 1:] if e["kind"] == "escalation"]
    asked = any(d.get("rung") == 3 and d.get("status") == "requested" for _, d in after)
    answered = any(e["who"] == "claude" and d.get("rung") in (3, 4) for e, d in after)
    return asked and not answered


def sessions_today() -> int:
    day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    return len(list((STATE / "sessions").glob(f"{day}T*.json")))


def earlier(build: dict, task: dict, n: int = 3) -> str:
    """What actually ran in the last n finished sections: a cause can lie a
    section back (LFS-020: 5.4 'done' without copying the headers broke 5.5)."""
    st, done = api("GET", f"/v1/lfs/builds/{build['id']}/tasks?state=done")
    out = []
    for t in sorted((done or {}).get("items", []), key=lambda x: x["seq"])[-n:]:
        st, full = api("GET", f"/v1/lfs/tasks/{t['id']}")
        if st != 200:
            continue
        j = full["journal"]
        start = max((i for i, e in enumerate(j) if e["kind"] == "state" and "-> running" in e["text"]), default=0)
        last_run = [e["text"] for e in j[start:] if e["kind"] == "command"]
        out.append(f"## {t['number']} {t['title']} (done)\n" + "\n".join(f"$ {c}" for c in last_run[-12:]))
    return "\n\n".join(out)


def brief(build: dict, task: dict) -> str:
    """Everything the tutor sees: the section, the task's whole journal, and
    what ran in the sections before it."""
    sec = task.get("section") or {}
    cmds = "\n\n".join(
        f"[{c['index']}]" + (" (as root)" if c.get("as_root") else "")
        + (f" SKIPPED by the controller: {c['skipped']}" if c.get("skipped") else "") + f"\n{c['text']}"
        for c in sec.get("commands", []))
    lines = []
    for e in task["journal"]:
        d = _data(e)
        text = e["text"]
        if e["kind"] == "result" and d.get("tail"):
            text = text.split("\n", 1)[0] + "\n(output tail)\n" + d["tail"][-8000:]
        lines.append(f"--- {e['at']} {e['who']} ({e['kind']})\n{text}")
        if e["kind"] == "proposal" and d.get("facts"):
            lines.append(f"(the facts the 14B was given)\n{d['facts']}")
    journal = "\n".join(lines)
    if len(journal) > 90000:  # keep the start (first plan) and the latest episodes
        journal = journal[:15000] + "\n\n[... earlier attempts cut ...]\n\n" + journal[-75000:]
    return (f"Build {build['id']} ({build['name']}): LFS {build['lfs_version']}, kernel {build['kernel']}.\n"
            f"STUCK SECTION: {task['number']} {task['title']} ({task['book'].upper()}), context {task['context']}"
            + (f", using version {task['version_override']} instead of the book's {task['version']}"
               if task.get("version_override") else "")
            + f". Attempts so far: {task['attempts']}.\n\n"
            f"THE BOOK'S COMMANDS (numbered as the 14B sees them):\n{cmds}\n\n"
            f"THE SECTION'S TEXT:\n{(sec.get('text') or '')[:12000]}\n\n"
            f"THE TASK'S JOURNAL (oldest first):\n{journal}\n\n"
            f"WHAT RAN IN THE SECTIONS BEFORE IT (their last attempt's commands; a cause can lie there):\n"
            f"{earlier(build, task)}\n")


def tutor(build: dict, task: dict) -> dict:
    prompt = brief(build, task)
    with tempfile.TemporaryDirectory(prefix="lfs-tutor-") as cwd:
        r = subprocess.run(
            [CLAUDE_BIN, "-p", prompt, "--tools", "", "--model", MODEL, "--output-format", "json",
             "--no-session-persistence", "--json-schema", json.dumps(LESSON_SCHEMA),
             "--append-system-prompt", TUTOR_SYSTEM.format(lfs=build["lfs_version"])],
            cwd=cwd, capture_output=True, text=True, timeout=SESSION_TIMEOUT_S,
            # The session's own auth: the signed-in Claude Code, never a key from a parent session.
            env={k: v for k, v in os.environ.items() if not k.startswith(("ANTHROPIC_", "CLAUDE_CODE_", "CLAUDECODE"))})
    try:
        out = json.loads(r.stdout)
    except ValueError:
        raise RuntimeError(f"claude -p exit {r.returncode}: {(r.stderr or r.stdout)[-500:]}") from None
    if out.get("is_error") or not isinstance(out.get("structured_output"), dict):
        raise RuntimeError(f"tutor session failed: {out.get('subtype')} {str(out.get('result'))[:300]}")
    return {"prompt_chars": len(prompt), **out}


def post(task: dict, who: str, kind: str, text: str, data: dict | None = None) -> None:
    st, _ = api("POST", f"/v1/lfs/tasks/{task['id']}/journal", {"who": who, "kind": kind, "text": text, "data": data})
    if st != 201:
        raise RuntimeError(f"journal write failed: HTTP {st}")


def pause_for_paul(build: dict, task: dict, why: str, data: dict) -> None:
    post(task, "claude", "escalation", f"rung 4: {why}", {"rung": 4, **data})
    api("POST", f"/v1/lfs/builds/{build['id']}", {"status": "paused"})


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--build", type=int, default=0, help="only this build")
    ap.add_argument("--dry-run", action="store_true", help="show what would be tutored and the brief's size; run nothing")
    args = ap.parse_args()
    if not API_SSH:
        log("LFS_API_SSH is not set (user@host of the CloudCore API host that holds the LFS build)")
        return 2
    (STATE / "sessions").mkdir(parents=True, exist_ok=True)
    lock = open(STATE / "lock", "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        log("a tutor session is already running")
        return 0
    st, builds = api("GET", "/v1/lfs/builds")
    if st != 200:
        log(f"can't list builds: HTTP {st} {builds}")
        return 1
    for b in builds.get("items", []):
        if b.get("status") != "running" or (args.build and b["id"] != args.build):
            continue
        st, nxt = api("GET", f"/v1/lfs/builds/{b['id']}/next")
        t = (nxt or {}).get("task")
        if not t or t["state"] != "stuck":
            continue
        st, task = api("GET", f"/v1/lfs/tasks/{t['id']}")
        if st != 200 or not wanted(task):
            continue
        if args.dry_run:
            log(f"would tutor build {b['id']} task {task['number']} {task['title']} (brief {len(brief(b, task))} chars)")
            continue
        if sessions_today() >= DAILY_CAP:
            pause_for_paul(b, task, f"the tutor's daily cap ({DAILY_CAP} sessions) is reached; "
                                    f"{task['number']} {task['title']} waits for Paul", {"cap": DAILY_CAP})
            log(f"daily cap reached: build {b['id']} paused")
            return 0
        started = datetime.now(timezone.utc)
        record = STATE / "sessions" / f"{started.strftime('%Y-%m-%dT%H%M%S')}-b{b['id']}-t{task['id']}.json"
        log(f"tutoring build {b['id']} task {task['number']} {task['title']}")
        try:
            out = tutor(b, task)
        except (RuntimeError, subprocess.TimeoutExpired, OSError) as e:
            record.write_text(json.dumps({"build": b["id"], "task": task["id"], "error": str(e)}, indent=1))
            pause_for_paul(b, task, f"a tutor session failed ({str(e)[:300]}); {task['number']} waits for Paul",
                           {"session": record.name})
            log(f"session failed: {e}")
            return 1
        ans = out["structured_output"]
        meta = {"session": record.name, "seconds": round(out.get("duration_ms", 0) / 1000),
                "cost_usd": out.get("total_cost_usd"), "model": MODEL}
        record.write_text(json.dumps({"build": b["id"], "task": task["id"], "number": task["number"],
                                      "answer": ans, **meta, "usage": out.get("usage")}, indent=1))
        post(task, "claude", "note", f"tutor's diagnosis: {ans['diagnosis']}", meta)
        if ans["decision"] == "needs Paul":
            pause_for_paul(b, task, f"the tutor says this needs Paul: {ans.get('why_paul') or ans['diagnosis']}", meta)
            log(f"build {b['id']} paused for Paul")
            continue
        post(task, "claude", "lesson", ans["lesson"], {"finding": ans["finding"], **meta})
        post(task, "claude", "escalation", "rung 3: tutor session done; the lesson is in the journal",
             {"rung": 3, "status": "done", **meta})
        api("POST", f"/v1/lfs/tasks/{task['id']}/state",
            {"state": "waiting", "who": "claude", "why": "tutored (rung 3): retry with the lesson"})
        log(f"tutored {task['number']}: lesson posted, task reset")
    return 0


if __name__ == "__main__":
    sys.exit(main())
