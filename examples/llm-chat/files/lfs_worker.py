#!/usr/bin/env python3
"""The LFS OS build's worker loop (lfs-os-Phased-Implementation.md, C2).

Runs on the llm-chat coordinator, as root, with verify-proxy's environment (the
lab broker's URL and token, the lab model's URL). For each task of a build's
queue (CloudCore's /v1/lfs routes, C1), in book order:

  1. Make sure the build machine is there and reachable: create it through the
     lab broker the first time; after a coordinator rebuild, make a new control
     key and have the broker install it (no private key is stored centrally).
     Restore the mounts the build had (the LFS partitions, then the chroot's
     virtual filesystems) if the machine has rebooted.
  2. For a package, unpack its source where the book expects it.
  3. Ask llm-chat's model for a plan: the section's exact, numbered commands,
     the machine's facts (disks, partitions, cores) and the reasons for any
     skipped command go in; JSON steps come out. Every book command must be
     accounted for, every change explained, and nothing may touch the
     machine's own system disk.
  4. Run each step in the task's context -- root on the host, the lfs user with
     the book's .bashrc, or the chroot with the book's env -i line --
     detached on the machine, so a long compile survives an SSH drop.
  5. On a failed step: a test suite's failures are judged against what the
     book says to expect; anything else gets a repair from the model, a few
     times at most, then the task is marked stuck (C5's escalation starts there).
  6. Journal everything, by who did it.

Section 3.1's wget is replaced by delivery: the controller fetches the verified
sources from the host's repo and copies them in, and each file's SHA-256 is
checked on arrival against MIRROR.json (the lab network can't reach the repo
itself, LFS-006). The book's own md5sum -c then runs as written.

Usage (on the coordinator, as root):
  sudo env $(systemctl show verify-proxy -p Environment --value) \\
      python3 lfs_worker.py --build 1 [--until 5.2] [--max-tasks N] [--plan-only]
Exit: 0 reached --until (or the end); 1 a task got stuck; 2 bad usage or setup.
"""

from __future__ import annotations

import argparse
import difflib
import threading
import hashlib
import json
import os
import re
import shlex
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import paramiko

import model_router  # F-236: the endpoint expected to answer fastest now

API = os.environ.get("LABVM_BROKER_URL", "").rstrip("/")
TOKEN = os.environ.get("LABVM_BROKER_TOKEN", "")
REPO = os.environ.get("LFS_REPO_URL", "http://repo.cloudcore.internal:8090/jammy/artifacts")
LAB_MODELS = [u.strip().rstrip("/") for u in os.environ.get("LAB_READER_URLS", "").split(",") if u.strip()]
OWN_MODEL = "http://127.0.0.1:8721"
MODEL_FILE = os.environ.get("EXAMPLES_MODEL_FILENAME", "")
STATE = Path(os.environ.get("LFS_WORKER_STATE", "/var/lib/lfs-worker"))
CACHE = Path(os.environ.get("LFS_WORKER_CACHE", "/var/cache/lfs-worker"))
LFS = "/mnt/lfs"
LFS_DISK = "/dev/sdb"           # the broker's lfs-build data disk (B4)
SYSTEM_DISK = "/dev/sda"        # the build machine's own system: never touched
STEP_TIMEOUT_S = 6 * 3600
REPAIRS_PER_STEP, REPAIRS_PER_TASK = 2, 4
_DANGER = re.compile(r"/dev/(?:sda|vda)\b|\bdd\b[^\n]*\bof=/dev/(?!sdb)|\bmkfs(?:\.\w+)?\b(?![^\n]*/dev/sdb)[^\n]*/dev/|"
                     r"\brm\s+-rf?\s+/(?:\s|$)|\bwipefs\b(?![^\n]*/dev/sdb)")
# The lab network can't reach the internet; the controller delivers verified
# sources (LFS-006). A plan that adds a download is wrong however it's argued (LFS-019).
_DOWNLOAD = re.compile(r"\b(?:wget|curl)\b[^\n]*\b(?:https?|ftp)://")
# System-level commands: a plan or repair may use one only where the book's own
# section does (2.7 mounts, 10.4 installs GRUB). In 5.5 the 14B added
# grub-install as root and wrote into the build machine's own /boot (LFS-020).
_SYSTEM = re.compile(r"\b(grub-install|grub-mkconfig|efibootmgr|mount|umount|mkfs(?:\.\w+)?|sgdisk|fdisk|parted|"
                     r"mkswap|swapon|sudo|apt-get|apt|dnf|yum|chroot)\b")
# Never, anywhere: the build machine's package manager and sudo (no password).
_NEVER = re.compile(r"\b(sudo|apt-get|apt|dnf|yum)\b")
_CHECK = re.compile(r"\bmake\b[^\n]*\b(?:check|test)s?\b|\bctest\b|\bninja\b[^\n]*\btest\b")
# A step can exit 0 and still report a problem: 2.2's version check prints
# "ERROR: /bin/sh does not point to bash" and carries on.
_REPORTED_PROBLEM = re.compile(r"^\s*(?:ERROR|FAIL(?:ED)?)\b|\bERROR:", re.MULTILINE)


def log(msg: str) -> None:
    print(f"lfs-worker: {msg}", file=sys.stderr, flush=True)


# ── CloudCore API (journal, queue, broker) ────────────────────────────────────

def api(method: str, path: str, body: dict | None = None, timeout: int = 120) -> tuple[int, dict]:
    req = urllib.request.Request(API + path, method=method, data=json.dumps(body).encode() if body is not None else None,
                                 headers={"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read()
            return r.status, (json.loads(raw) if raw[:1] in (b"{", b"[") else {"text": raw.decode()})
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read())
        except ValueError:
            return e.code, {"detail": str(e)}


# ── Heartbeat (C4) ────────────────────────────────────────────────────────────
# What the worker is doing now, posted every minute from a thread, so a long
# model call or a long compile still shows the worker alive. Sentinel judges a
# stall from this and the journal, never from log silence.
HEARTBEAT_S = 60
_hb: dict = {"build": 0, "task_id": None, "number": "", "phase": "starting"}
_hb_lock = threading.Lock()


def phase(text: str, task: dict | None = None) -> None:
    with _hb_lock:
        _hb["phase"] = text[:200]
        _hb["since"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        if task is not None:
            _hb["task_id"], _hb["number"] = task["id"], task["number"]


def beat_now() -> None:
    """One heartbeat at once: the last word before the worker exits."""
    with _hb_lock:
        body = {k: _hb.get(k) for k in ("task_id", "number", "phase", "since")} | {"pid": os.getpid()}
        bid = _hb["build"]
    try:
        api("POST", f"/v1/lfs/builds/{bid}/heartbeat", body, timeout=30)
    except (urllib.error.URLError, OSError, ValueError):
        pass


def _beat_forever() -> None:
    while True:
        with _hb_lock:
            body = {k: _hb.get(k) for k in ("task_id", "number", "phase", "since")} | {"pid": os.getpid()}
            bid = _hb["build"]
        try:
            api("POST", f"/v1/lfs/builds/{bid}/heartbeat", body, timeout=30)
        except (urllib.error.URLError, OSError, ValueError):
            pass  # the next beat tries again; a missed one isn't a stall
        time.sleep(HEARTBEAT_S)


def journal(task_id: int, who: str, kind: str, text: str, data=None) -> None:
    st, _ = api("POST", f"/v1/lfs/tasks/{task_id}/journal", {"who": who, "kind": kind, "text": text[:20000],
                                                              **({"data": data} if data is not None else {})})
    if st != 201:
        log(f"journal write failed ({st}): {kind} {text[:80]}")


def set_state(task_id: int, state: str, why: str = "") -> None:
    api("POST", f"/v1/lfs/tasks/{task_id}/state", {"state": state, "who": "controller", "why": why})


# ── The model ─────────────────────────────────────────────────────────────────

# JSON schemas for each kind of call: llama-server constrains its output to
# them, so a reply is always the JSON asked for (LFS-011).
PLAN_SCHEMA = {"type": "object", "required": ["changes", "expect"], "properties": {
    "changes": {"type": "array", "items": {"type": "object", "required": ["why"], "properties": {
        "book": {"type": "integer"}, "after": {"type": "integer"}, "run": {"type": "string"},
        "omit": {"type": "boolean"}, "as": {"type": "string", "enum": ["root", "lfs"]}, "why": {"type": "string"}}}},
    "expect": {"type": "string"}}}
# LFS-012: with free-text fields the 14B wrote the right fix in its reason and
# left the command field empty. Now the fix's commands are a required,
# non-empty list, separate from the explanation.
FIX_SCHEMA = {"type": "object", "required": ["cause", "commands", "then"], "properties": {
    "cause": {"type": "string"},
    # Empty only with "the step's work is already done" (LFS-015).
    "commands": {"type": "array", "items": {"type": "string", "minLength": 1}},
    "then": {"type": "string", "enum": ["rerun the step", "run this instead", "the step's work is already done"]},
    "instead": {"type": "string"}}}
JUDGE_SCHEMA = {"type": "object", "required": ["acceptable", "why"], "properties": {
    "acceptable": {"type": "boolean"}, "why": {"type": "string"}}}
OUTPUT_SCHEMA = {"type": "object", "required": ["ok", "why"], "properties": {
    "ok": {"type": "boolean"}, "why": {"type": "string"}}}


def ask_model(system: str, user: str, max_tokens: int = 1500, schema: dict | None = None) -> tuple[str, str]:
    """Returns (reply, which endpoint answered): the capable endpoint expected to
    answer fastest now, by live measured speeds (model_router, F-236; LFS-010).
    Only a free endpoint: the build waits rather than queue ahead of a student."""
    payload = {"messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
               "max_tokens": max_tokens, "temperature": 0.2, "stream": False}
    if schema:
        payload["response_format"] = {"type": "json_object", "schema": schema}
    urls = [u for u in LAB_MODELS + [OWN_MODEL] if model_router.capable(u, MODEL_FILE)] or [OWN_MODEL]
    last = ""
    for _ in range(240):  # up to an hour waiting for a free one
        for base in [u for u in model_router.order(urls, len(system) + len(user), max_tokens) if model_router.free(u)]:
            req = urllib.request.Request(base + "/v1/chat/completions", data=json.dumps(payload).encode(),
                                         headers={"Content-Type": "application/json"})
            try:
                with urllib.request.urlopen(req, timeout=3600) as r:
                    data = json.loads(r.read())
                model_router.record(base, data.get("timings"))
                return data["choices"][0]["message"]["content"], base
            except (urllib.error.URLError, TimeoutError, KeyError, ValueError) as e:
                last = f"{base}: {e}"
                log(f"model at {base} failed ({e}); trying the next")
        time.sleep(15)
    raise RuntimeError(f"no free model answered in an hour ({last})")


def _json_reply(text: str) -> dict | None:
    s, e = text.find("{"), text.rfind("}")
    if s < 0 or e <= s:
        return None
    try:
        return json.loads(text[s:e + 1])
    except ValueError:
        return None


PLAN_SYSTEM = """You are building Linux From Scratch {lfs} (systemd) for a 64-bit UEFI computer, one section of the book at a time, on a build machine. You get the section's commands, numbered, and facts about the machine. Every numbered command runs EXACTLY as the book has it unless you say otherwise -- so list ONLY the commands you change, leave out or add. Reply with ONLY a JSON object, no prose, no code fences:
{{"changes": [
  {{"book": <number>, "run": "<the command as it must run here>", "why": "<reason>"}},
  {{"book": <number>, "omit": true, "why": "<reason>"}},
  {{"after": <the command number it follows, or -1 to go first>, "run": "<an added step>", "why": "<reason>"}},
  {{"book": <number>, "as": "root|lfs", "why": "<reason>"}}
], "expect": "<what success looks like>"}}
An empty "changes" list means: run the section exactly as the book has it.
Rules:
- Follow the book. Change only what the facts require.
- A command with a placeholder (like /dev/<xxx>) MUST be changed: fill it from the facts.
- Leave a command out only if the facts or the book's own text say it doesn't apply here (an alternative for BIOS, swap that isn't wanted ...).
- Add a step only when the facts require it -- for example a section whose text says what to do but gives no command.
- "as" moves one command to another user, when the book says to run it as that user.
- Do ONLY this section's work. Later sections (named in the request) do theirs: don't format, mount or build anything that belongs to them.
- {system_disk} is the build machine's own system disk: never touch it. The LFS disk is {lfs_disk}.
- Nothing interactive: no editors, cfdisk, fdisk prompts, menuconfig or password prompts. Use non-interactive equivalents (sgdisk, scripts/config, here-documents).
- The controller already enters the task's context (user, chroot, directory): do not su, chroot or cd into the package's directory yourself.
- The controller times every step and records it in the journal: never add `time` or SBU measurements.
- Never add a copy of the book's own commands: they already run. To change one, give its "book" number."""

FIX_SYSTEM = """You are building Linux From Scratch {lfs} (systemd) for a 64-bit UEFI computer. A step from the book's section failed on the build machine. Reply with ONLY a JSON object:
{{"cause": "<what went wrong, briefly>", "commands": ["<a shell command that fixes the cause>", "..."], "then": "rerun the step" | "run this instead" | "the step's work is already done", "instead": "<only with 'run this instead': the step to run in its place>"}}
If the step failed only because its result already exists (a directory, a user, a mount), its work is already done: say so, with no commands. NEVER delete files or directories to make a step pass -- that destroys work.
Put every command to run in "commands" -- the controller runs exactly those, in the same place as the step (same user, chroot and directory), and nothing written in "cause". Fix the cause; don't hide the failure (no '|| true', no skipping tests the book runs). {system_disk} is the build machine's own system disk: never touch it."""

OUTPUT_JUDGE_SYSTEM = """You are building Linux From Scratch (systemd). A step exited 0 but its output reports problems. Decide whether the step met the book's requirement. Reply with ONLY a JSON object:
{"ok": true|false, "why": "<what the output shows, against what the book requires>"}"""

JUDGE_SYSTEM = """You are building Linux From Scratch (systemd). A test-suite step exited non-zero. The book says which test failures are known and acceptable. Reply with ONLY a JSON object:
{"acceptable": true|false, "why": "<which failures, and what the book says about them>"}
Acceptable only if every failure shown is one the book's text says to expect."""


# ── The build machine ─────────────────────────────────────────────────────────

class Machine:
    def __init__(self, ip: str, key: Path) -> None:
        self.ip, self.key = ip, key
        self.client: paramiko.SSHClient | None = None

    def connect(self) -> None:
        c = paramiko.SSHClient()
        c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        c.connect(self.ip, port=1022, username="root", key_filename=str(self.key), timeout=15,
                  banner_timeout=20, auth_timeout=20, look_for_keys=False, allow_agent=False)
        c.get_transport().set_keepalive(30)
        self.client = c

    def sh(self, cmd: str, timeout: int = 300) -> tuple[int, str]:
        """A short command as root on the build host. Returns (exit, output)."""
        for attempt in (1, 2):
            try:
                if self.client is None or not self.client.get_transport() or not self.client.get_transport().is_active():
                    self.connect()
                _, out, _ = self.client.exec_command(f"bash -c {shlex.quote(cmd)} 2>&1", timeout=timeout)
                text = out.read().decode("utf-8", "replace")
                return out.channel.recv_exit_status(), text
            except (paramiko.SSHException, OSError, EOFError):
                if attempt == 2:
                    raise
                self.client = None
                time.sleep(5)
        return 1, ""

    def put(self, data: bytes, path: str, mode: int = 0o644) -> None:
        if self.client is None:
            self.connect()
        sftp = self.client.open_sftp()
        try:
            with sftp.open(path, "wb") as f:
                f.write(data)
            sftp.chmod(path, mode)
        finally:
            sftp.close()

    def run_detached(self, script: str, launcher: str, name: str, timeout_s: int = STEP_TIMEOUT_S) -> tuple[int, str, float]:
        """Run a step so it survives an SSH drop: launch it with setsid, poll
        for its exit code. Returns (exit, output tail, seconds)."""
        logf, rcf = f"/var/log/lfs-build/{name}.log", f"/var/log/lfs-build/{name}.rc"
        self.sh("mkdir -p /var/log/lfs-build")
        self.put(script.encode(), "/var/log/lfs-build/current-step.sh", 0o755)
        # The whole launcher writes to the log: a failure before the step itself
        # (a copy into the chroot, say) left no log and no clue (LFS-027).
        self.sh(f"rm -f {rcf}; setsid nohup bash -c {shlex.quote('{ ' + launcher + f'; }} > {logf} 2>&1; echo $? > {rcf}')} "
                f"> /dev/null 2>&1 < /dev/null &")
        t0 = time.monotonic()
        while time.monotonic() - t0 < timeout_s:
            time.sleep(10)
            try:
                code, rc = self.sh(f"cat {rcf} 2>/dev/null")
            except (paramiko.SSHException, OSError):
                continue  # the machine is busy or the link dropped; the step keeps running
            if rc.strip().isdigit():
                _, tail = self.sh(f"tail -c 16000 {logf}")
                return int(rc.strip()), tail, time.monotonic() - t0
        self.sh("pkill -f current-step.sh")
        _, tail = self.sh(f"tail -c 16000 {logf}")
        return 124, tail + f"\n[the controller stopped the step after {timeout_s}s]", time.monotonic() - t0


def _keygen(path: Path) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", "lfs-worker", "-f", str(path)], check=True)
    return path.with_suffix(".pub").read_text().strip() if path.with_suffix(".pub").exists() else \
        Path(str(path) + ".pub").read_text().strip()


def ensure_machine(build: dict) -> tuple[Machine, str]:
    """The build machine, reachable with this controller's key."""
    key = STATE / f"build-{build['id']}.key"
    pub = _keygen(key)
    vm_id = build.get("build_vm_id") or ""
    if vm_id:
        st, vm = api("GET", f"/v1/lab-vms/{vm_id}")
        if st != 200 or vm.get("deleted_at"):
            raise RuntimeError(f"build machine {vm_id} is gone ({st}): its work is lost; start a new build")
    else:
        st, vm = api("POST", "/v1/lab-vms", {"purpose": "lfs-build", "run_id": f"lfs-{build['id']}", "public_key": pub},
                     timeout=300)
        if st not in (200, 201, 202):  # 202: accepted, being created
            raise RuntimeError(f"the broker refused a build machine: {st} {vm.get('detail', vm)}")
        vm_id = vm["id"]
        api("POST", f"/v1/lfs/builds/{build['id']}", {"build_vm_id": vm_id, "status": "running"})
        log(f"build machine {vm_id} ({vm.get('flavor')}) requested")
    for _ in range(90):
        st, vm = api("GET", f"/v1/lab-vms/{vm_id}")
        if vm.get("ip"):
            break
        time.sleep(10)
    m = Machine(vm["ip"], key)
    for attempt in range(60):
        try:
            m.connect()
            break
        except paramiko.AuthenticationException:
            # A rebuilt coordinator: have the broker install this controller's key.
            st, r = api("POST", f"/v1/lab-vms/{vm_id}/control-key", {"public_key": pub})
            log(f"control key re-installed through the broker ({st})")
            time.sleep(3)
        except (paramiko.SSHException, OSError):
            time.sleep(10)
    else:
        raise RuntimeError(f"can't reach the build machine at {vm['ip']}:1022")
    m.sh("cloud-init status --wait >/dev/null 2>&1 || true", timeout=900)
    api("POST", f"/v1/lab-vms/{vm_id}/touch")
    return m, vm_id


def facts(m: Machine) -> str:
    _, cores = m.sh("nproc")
    _, disks = m.sh(f"lsblk -nrpo NAME,SIZE,TYPE,FSTYPE,PARTLABEL,MOUNTPOINT {LFS_DISK} 2>&1")
    _, mounted = m.sh(f"findmnt -rno TARGET,SOURCE,FSTYPE | grep '^{LFS}' || true")
    # What already exists under $LFS, so a plan can't claim otherwise (LFS-016).
    _, top = m.sh(f"ls -A {LFS} 2>/dev/null | grep -vx 'lost+found' | tr '\\n' ' '")
    return (f"- LFS={LFS}. The LFS disk is {LFS_DISK}; {SYSTEM_DISK} is the build machine's own system: never touch it.\n"
            f"- The machine boots the finished system by 64-bit UEFI: GPT, with an EFI system partition (FAT32, "
            f"mounted at /boot/efi in the new system, so $LFS/boot/efi during the build) and an ext4 root.\n"
            f"- Cores: {cores.strip()}. Swap: none (not needed).\n"
            f"- {LFS_DISK} now (NAME SIZE TYPE FSTYPE PARTLABEL MOUNTPOINT):\n{disks.strip() or '(empty)'}\n"
            f"- Mounted under {LFS}:\n{mounted.strip() or '(nothing)'}\n"
            f"- In {LFS} right now: {top.strip() or '(nothing yet)'}. Nothing else has been done for you: "
            f"the controller only delivers the sources (3.1) and enters each task's context.")


# ── Contexts, mounts and sources ──────────────────────────────────────────────

_VFS = ("mountpoint -q $LFS/dev || mount -v --bind /dev $LFS/dev; "
        "mountpoint -q $LFS/dev/pts || mount -vt devpts devpts -o gid=5,mode=0620 $LFS/dev/pts; "
        "mountpoint -q $LFS/proc || mount -vt proc proc $LFS/proc; "
        "mountpoint -q $LFS/sys || mount -vt sysfs sysfs $LFS/sys; "
        "mountpoint -q $LFS/run || mount -vt tmpfs tmpfs $LFS/run; "
        "if [ -h $LFS/dev/shm ]; then install -v -d -m 1777 $LFS$(realpath /dev/shm); "
        "else mountpoint -q $LFS/dev/shm || mount -vt tmpfs -o nosuid,nodev tmpfs $LFS/dev/shm; fi")


SWAP_FILE, SWAP_GB = "/swapfile", 8


def ensure_swap(m: Machine) -> None:
    """Swap on the machine's own system disk (LFS-019): a standard.large has
    3.9 GB and no swap, and GCC's final links at -j4 ran it out of memory. The
    controller's set-up, like the mounts -- not the book's, not the model's."""
    code, _ = m.sh(f"swapon --show=NAME --noheadings | grep -qx {SWAP_FILE}")
    if code == 0:
        return
    code, out = m.sh(f"[ -f {SWAP_FILE} ] || {{ fallocate -l {SWAP_GB}G {SWAP_FILE} && chmod 600 {SWAP_FILE} "
                     f"&& mkswap {SWAP_FILE} >/dev/null; }}; swapon {SWAP_FILE} && "
                     f"{{ grep -q '^{SWAP_FILE} ' /etc/fstab || echo '{SWAP_FILE} none swap sw 0 0' >> /etc/fstab; }}")
    log(f"swap {SWAP_FILE} ({SWAP_GB} GB): " + ("on" if code == 0 else f"FAILED: {out[-300:]}"))


def ensure_mounts(m: Machine, task: dict, done_numbers: set[str]) -> None:
    """After a reboot (or a snapshot restore) the build's mounts are gone; put
    back what the book had set up by this point: the partitions (2.7), the
    chroot's virtual filesystems (7.3)."""
    if "2.7" in done_numbers:
        _, fstab = m.sh("cat /var/lib/lfs-build/mounts 2>/dev/null")
        for line in fstab.splitlines():
            dev, target = line.split()[:2]
            m.sh(f"mkdir -p {target} && (mountpoint -q {target} || mount {dev} {target})")
    if "7.3" in done_numbers and task["context"] == "chroot":
        m.sh(f"export LFS={LFS}; {_VFS}")


def record_mounts(m: Machine) -> None:
    """After 2.7: remember which devices are mounted where under $LFS."""
    m.sh(f"mkdir -p /var/lib/lfs-build && findmnt -rno SOURCE,TARGET | awk '$2 ~ \"^{LFS}(/|$)\"' "
         f"| sort -k2 > /var/lib/lfs-build/mounts")


# Each step reports the directory it ends in, so the book's `cd build` carries
# into its next command as it would in one shell (LFS-018).
_CWD_MARK = "@@lfs-cwd="
_CWD_TRAP = f"trap 'printf \"\\n{_CWD_MARK}%s\\n\" \"$PWD\"' EXIT\n"
_CWD_RE = re.compile(r"\n?" + re.escape(_CWD_MARK) + r"(\S[^\n]*)\n?")


def launcher(context: str, as_user: str | None, cwd: str) -> tuple[str, str]:
    """(script prologue, launcher command) for a step in its context."""
    step = "/var/log/lfs-build/current-step.sh"
    if context == "chroot":
        # The book's 7.4 environment, run non-interactively; the script is copied into the chroot.
        pro = f"set -e\ncd {shlex.quote(cwd)}\n{_CWD_TRAP}"
        # $LFS/tmp exists only once 7.5 has run; 7.5 itself runs in the chroot (LFS-027).
        launch = (f"export LFS={LFS}; install -d -m 1777 $LFS/tmp && cp {step} $LFS/tmp/lfs-step.sh && chroot \"$LFS\" /usr/bin/env -i HOME=/root "
                  "TERM=xterm PATH=/usr/bin:/usr/sbin MAKEFLAGS=\"-j$(nproc)\" TESTSUITEFLAGS=\"-j$(nproc)\" "
                  "/bin/bash -e /tmp/lfs-step.sh")
        return pro, launch
    user = as_user or ("lfs" if context == "host-lfs" else "root")
    if user == "lfs":
        # The lfs user's environment from the book's 4.4 .bashrc (exported), plus set +h.
        pro = f"set -e\nset +h\ncd {shlex.quote(cwd)}\n{_CWD_TRAP}"
        return pro, f"chmod 755 {step}; su lfs -s /bin/bash -c 'source ~/.bashrc 2>/dev/null; bash -e {step}'"
    pro = f"set -e\nexport LFS={LFS}\numask 022\ncd {shlex.quote(cwd)}\n{_CWD_TRAP}"
    return pro, f"bash -e {step}"


def source_file(task: dict, manifest: dict) -> str | None:
    """The tarball a package task unpacks, by package and version (or the override).

    Names are matched loosely (LFS-023): "Libstdc++ from GCC" is GCC's tarball,
    "D-Bus" is dbus-*, "Flit-Core" is flit_core-*, and "Sqlite" is
    sqlite-autoconf-*. Before that, 5.6 ran with nothing unpacked."""
    if not task["package"]:
        return None
    ver = task["version_override"] or task["version"]
    pkg = task["package"].lower()
    if " from " in pkg:  # "libstdc++ from gcc": the section builds part of another package
        pkg = pkg.split(" from ", 1)[1].strip()

    def flat(s: str) -> str:
        return re.sub(r"[^a-z0-9]", "", s)

    best = None
    for f in manifest["files"]:
        name = f["file"].lower()
        if f["kind"] != "source" or f["set"] in ("books", "blfs-common") or not re.search(r"\.tar(\.\w+)?$|\.tgz$", name):
            continue
        if name.startswith(f"{pkg}-{ver}") or name.startswith(f"{pkg}{ver}") or \
                (pkg == "linux" and name == f"linux-{ver}.tar.xz"):
            return f["file"]
        stem = flat(name.split(ver, 1)[0]) if ver in name else ""
        if stem and (stem == flat(pkg) or (stem.startswith(flat(pkg)) and len(stem) - len(flat(pkg)) <= 9)):
            best = best or f["file"]  # e.g. sqlite + "autoconf"
    return best


def deliver_sources(m: Machine, manifest: dict, task_id: int) -> None:
    """3.1 without wget (LFS-006): verified on the coordinator against
    MIRROR.json, copied in, and checked again by SHA-256 on the machine."""
    m.sh(f"mkdir -p {LFS}/sources && chmod a+wt {LFS}/sources")  # the book's 3.1 mode, in case its mkdir was left out
    with urllib.request.urlopen(f"{REPO}/lfs/MIRROR.json", timeout=60) as r:
        mirror = json.loads(r.read())
    wanted = [f for f in manifest["files"] if f["set"] in ("lfs", "blfs-uefi", "blfs-stage1", "blfs-common")
              or (f["set"] == "kernel" and f.get("role", "").startswith("latest"))]
    sums, md5s = [], []
    sent = 0
    for f in wanted:
        rel = next(k for k in mirror if k.endswith("/" + f["file"]))
        local = CACHE / rel
        if not local.exists() or hashlib.sha256(local.read_bytes()).hexdigest() != mirror[rel]["sha256"]:
            local.parent.mkdir(parents=True, exist_ok=True)
            urllib.request.urlretrieve(f"{REPO}/lfs/{rel}", local)
            if hashlib.sha256(local.read_bytes()).hexdigest() != mirror[rel]["sha256"]:
                raise RuntimeError(f"{rel} doesn't match MIRROR.json after download")
        m.put(local.read_bytes(), f"{LFS}/sources/{f['file']}")
        sent += local.stat().st_size
        sums.append(f"{mirror[rel]['sha256']}  {f['file']}")
        if f["set"] == "lfs" and f.get("md5"):
            md5s.append(f"{f['md5']}  {f['file']}")
    m.put(("\n".join(sums) + "\n").encode(), f"{LFS}/sources/SHA256SUMS")
    m.put(("\n".join(md5s) + "\n").encode(), f"{LFS}/sources/md5sums")
    code, out = m.sh(f"cd {LFS}/sources && sha256sum -c --quiet SHA256SUMS", timeout=900)
    if code != 0:
        raise RuntimeError(f"sources failed their SHA-256 check on the machine: {out[-500:]}")
    journal(task_id, "controller", "note",
            f"Delivered {len(wanted)} source files ({sent / 2**20:.0f} MiB) from the host's repo instead of wget "
            f"(LFS-006); every file's SHA-256 checked on the machine against MIRROR.json. md5sums written from "
            f"the book's own list ({len(md5s)} files) for its md5sum -c.")


# ── Checkpoints (C3) ──────────────────────────────────────────────────────────

def checkpoint(m: Machine, vm_id: str, task: dict, when: str) -> bool:
    """A snapshot of both disks, through the broker (the machine shuts down
    cleanly and starts again), then reconnect and restore the mounts."""
    slug = re.sub(r"[^A-Za-z0-9]+", "-", f"{task['number']}-{task['title']}").strip("-").lower()[:40]
    name = f"{when}-{task['seq']:03d}-{slug}"
    # A retried task keeps its first "before" checkpoint: that's the state to go back to.
    st0, have = api("GET", f"/v1/lab-vms/{vm_id}/snapshots")
    if st0 == 200 and any(s.get("name") == name for s in have.get("items", [])):
        journal(task["id"], "controller", "checkpoint", f"checkpoint {name} already taken; kept")
        return True
    phase(f"checkpoint {name}", task)
    m.sh("sync")
    st, out = api("POST", f"/v1/lab-vms/{vm_id}/snapshots",
                  {"name": name, "description": f"{when} {task['number']} {task['title']}"}, timeout=700)
    if st != 201:
        journal(task["id"], "controller", "checkpoint", f"checkpoint {name} FAILED ({st}): {out.get('detail', out)}")
        return False
    m.client = None
    for _ in range(60):
        try:
            m.connect()
            break
        except (paramiko.SSHException, OSError):
            time.sleep(5)
    st2, tasks = api("GET", f"/v1/lfs/builds/{task['build_id']}/tasks?state=done")
    ensure_mounts(m, {**task, "context": "chroot" if task["context"] == "chroot" else task["context"]},
                  {t["number"] for t in tasks.get("items", [])})
    journal(task["id"], "controller", "checkpoint",
            f"checkpoint {name}: both disks snapshotted in {out.get('seconds')} s; machine back, mounts restored",
            {"name": name})
    return True


def _really_done(m: Machine, run: str, output: str) -> tuple[bool, str]:
    """The model says a failed step's work is already done. Accept that only for
    an 'already exists' failure, and only if what the step makes is there."""
    if not re.search(r"File exists|already exists|already mounted", output):
        return False, "the failure isn't an 'already exists' one"
    targets = re.findall(r"\bmkdir\s+(?:-\w+\s+)*(\S+)", run)
    for tgt in targets:
        code, _ = m.sh(f"export LFS={LFS}; test -d {tgt}")
        if code != 0:
            return False, f"{tgt} isn't there"
    return True, ("what the step makes is already there: " + ", ".join(targets)) if targets else "the output says it exists"


# ── One task ──────────────────────────────────────────────────────────────────

def _dropped_lines(book: str, run: str) -> list[str]:
    """The book command's lines missing from a changed version (LFS-020: in 5.4
    the 14B changed `make headers; find ...; cp -rv usr/include $LFS/usr` to
    just `make headers`, and the headers never reached $LFS). A line counts as
    kept if a line of the change is mostly the same: a version, a placeholder
    filled, a variable written out, an option added."""
    def key(line: str) -> str:
        return re.sub(r"\d+(?:\.\d+)*", "N", _norm(line))
    have = [key(x) for x in run.replace("\\\n", " ").splitlines() if x.strip()]
    out = []
    for line in book.replace("\\\n", " ").splitlines():
        k = key(line)
        if k and not any(k in h or difflib.SequenceMatcher(None, k, h).ratio() >= 0.6 for h in have):
            out.append(_norm(line)[:60])
    return out


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", s.replace("\\\n", " ")).strip()


def _next_sections(task: dict, n: int = 3) -> str:
    st, data = api("GET", f"/v1/lfs/builds/{task['build_id']}/tasks?state=waiting")
    later = [t for t in data.get("items", []) if t["seq"] > task["seq"]][:n]
    return "; ".join(f"{t['number']} {t['title']}".strip() for t in later) or "(none)"


def _version_note(task: dict, cmds: list[dict]) -> str:
    """The version override, said only where it matters (LFS-021): told to
    "change version-specific names" in 5.4, whose commands name no version,
    the 14B rewrote `make headers` three times to have something to change."""
    if not task["version_override"]:
        return ""
    named = [c["index"] for c in cmds if task["version"] in c["text"] and not c.get("skipped")]
    if not named:
        return (f"This build uses version {task['version_override']} instead of the book's {task['version']}. "
                "None of this section's commands names a version, so the version needs NO change to them.\n")
    return (f"This build uses version {task['version_override']} instead of the book's {task['version']}: "
            f"change '{task['version']}' to '{task['version_override']}' in command(s) {named} and nothing else.\n")


def tutor_notes(task: dict) -> list[str]:
    """The section's lessons from the ladder (C5): the tutor's (Claude) and
    Sentinel's knowledge-base nudges, oldest first. A lesson marked
    {"supersedes": true} retires every lesson before it (LFS-024: after the
    controller was fixed, 5.6's old workaround lessons kept steering the 14B)."""
    who = {"claude": "tutor", "sentinel": "Sentinel, from the knowledge base"}
    notes = []
    for j in task.get("journal", []):
        if j["who"] not in who or j["kind"] != "lesson":
            continue
        try:
            if (json.loads(j.get("data") or "null") or {}).get("supersedes"):
                notes = []
        except ValueError:
            pass
        notes.append(f"({who[j['who']]}) {j['text']}")
    return notes


def plan(task: dict, build: dict, m: Machine, feedback: str = "") -> tuple[list[dict], str]:
    sec = task["section"] or {}
    cmds = [c for c in sec.get("commands", [])]
    listing = "\n".join(
        f"[{c['index']}] ({c['subsection'] or 'section'}{' / ' + c['note'] if c.get('note') else ''}"
        f"{', as root' if c.get('as_root') else ''})"
        + (f" SKIPPED by the controller: {c['skipped']}" if c.get("skipped") else "") + f"\n{c['text']}"
        for c in cmds) or "(The book gives no commands for this section; its text says what to do.)"
    prose = (sec.get("text") or "")[:2500]
    notes = tutor_notes(task)
    fx = facts(m)
    ctx = {"host-root": "as root on the build host", "host-lfs": "as the lfs user on the build host, with the book's "
           "environment (LFS, LFS_TGT, PATH, CONFIG_SITE, MAKEFLAGS from ~/.bashrc)",
           "chroot": "inside the chroot, as root, with the book's environment"}[task["context"]]
    user = (f"Section {task['number']} {task['title']} ({task['book'].upper()}). The controller runs your steps "
            f"{ctx}" + (f", in the unpacked source directory ({task['_cwd']})" if task.get("_srcdir") else f", in {task['_cwd']}")
            + ".\n" + _version_note(task, cmds)
            + ("\nYOUR TUTOR'S NOTES FOR THIS SECTION (follow them):\n" + "\n".join(f"- {n}" for n in notes) + "\n"
               if notes else "")
            + f"\nThe next sections, which are NOT yours to do now: {_next_sections(task)}.\n"
            + f"\nFacts about the machine:\n{fx}\n\nThe section's commands:\n{listing}\n\n"
            f"The section's text (start):\n{prose}\n" + (f"\nYour previous answer had problems: {feedback}\n" if feedback else ""))
    reply, who = ask_model(PLAN_SYSTEM.format(lfs=build["lfs_version"], system_disk=SYSTEM_DISK, lfs_disk=LFS_DISK), user,
                           # Long sections (7.6's /etc/passwd and /etc/group) need room for a
                           # changed command written out whole; 900 cut 7.6's replies off (LFS-028).
                           max_tokens=900 if len(listing) < 3000 else 2000, schema=PLAN_SCHEMA)
    data = _json_reply(reply)
    if not data or not isinstance(data.get("changes"), list):
        journal(task["id"], "llm-chat", "proposal", f"(unusable reply)\n{reply[:3000]}",
                {"model": who, "problems": ["the reply wasn't the JSON asked for"]})
        return [], "the reply wasn't the JSON asked for"
    problems = []
    by_index = {c["index"]: c for c in cmds}
    # The book's commands, exactly, in order (LFS-009); then the model's changes.
    # A book command the book runs as root stays root in an lfs-user section (LFS-017).
    steps = [{"book": i, "run": c["text"], "as": "root" if c.get("as_root") and task["context"] == "host-lfs" else None,
              "why": "", "changed": False}
             for i, c in by_index.items() if not c.get("skipped")]
    book_system = {w for c in by_index.values() for w in _SYSTEM.findall(c["text"])}
    for ch in data["changes"]:
        if not isinstance(ch, dict):
            continue
        idx, why, run = ch.get("book"), str(ch.get("why") or ""), str(ch.get("run") or "")
        # A reason alone ("the commands need no changes") is a remark, not a
        # change: in 5.4 the 14B said so four times and was refused (LFS-022).
        if idx is None and not run and not ch.get("omit") and not ch.get("as") and ch.get("after") is None:
            continue
        # LFS-020: system-level commands only where the book's section has them.
        # A section with no book commands (2.4) must write its own; _DANGER still guards the disks.
        foreign = sorted({w for w in _SYSTEM.findall(run)
                          if _NEVER.fullmatch(w) or (by_index and w not in book_system)})
        if foreign:
            problems.append(f"'{run[:80]}' uses {', '.join(foreign)}, which this section's book commands don't: "
                            "that belongs to another section (or never, for sudo and the package manager)")
            continue
        # LFS-020: in an lfs-user section only the book's own root commands run as root.
        if ch.get("as") == "root" and task["context"] == "host-lfs" and not (idx in by_index and by_index[idx].get("as_root")):
            problems.append(f"a change runs {'command [' + str(idx) + ']' if idx is not None else 'an added step'} as root: "
                            "in this section only the book's own root commands run as root")
            continue
        if not why:
            problems.append(f"a change ({json.dumps(ch)[:80]}) gives no reason")
        if run and _DANGER.search(run):
            problems.append(f"'{run[:80]}' touches a disk or path outside the LFS disk")
            continue
        if run and _DOWNLOAD.search(run):
            problems.append(f"'{run[:80]}' downloads from the internet: the build machine can't, and every source is "
                            "already in $LFS/sources (the controller delivered and verified them)")
            continue
        if idx is None:
            # An added step. Its place: after the book command it names; at the
            # start for -1; otherwise -- no book commands in the section, or no
            # position given -- after the steps added so far, in the order given
            # (LFS-013: in a commandless section the 14B numbered its own steps).
            if not run:
                problems.append("an added step has no 'run'")
                continue
            # LFS-018: the 14B added a copy of the book's configure/make/install
            # (wrapped in `time`) ahead of the book's own `mkdir build; cd build`.
            dup = [i for i, c in by_index.items() if not c.get("skipped") and len(_norm(c["text"])) >= 12
                   and _norm(c["text"]) in _norm(run)]
            if dup:
                problems.append(f"an added step repeats book command(s) {dup}: the book's commands already run; "
                                "to change one, give a change with its 'book' number instead of adding a copy")
                continue
            after = ch.get("after")
            if after == -1:
                pos = 0
            elif after in by_index:
                pos = next((n + 1 for n, s in enumerate(steps) if s["book"] == after), len(steps))
            else:
                after, pos = "end", len(steps)
            # Several steps added at the same place keep the order given.
            while pos < len(steps) and steps[pos].get("added_after") == after:
                pos += 1
            steps.insert(pos, {"book": None, "run": run, "as": ch.get("as") if ch.get("as") in ("root", "lfs") else None,
                               "why": why, "changed": True, "added_after": after})
            continue
        if idx not in by_index:
            problems.append(f"a change refers to command [{idx}], which isn't in the list: the commands are "
                            f"{sorted(by_index)} (a multi-line block such as a 'case' or 'for' is ONE command)")
            continue
        if by_index[idx].get("skipped"):
            continue  # the controller already skips it
        s = next(s for s in steps if s["book"] == idx)
        if ch.get("omit"):
            s.update(run="", omitted=True, why=why)
        if run:
            dropped = _dropped_lines(by_index[idx]["text"], run)
            if dropped:
                problems.append(f"your change to command [{idx}] drops the book's line(s) {dropped}. Command [{idx}] is ONE "
                                f"command of {len([x for x in by_index[idx]['text'].splitlines() if x.strip()])} lines: "
                                "a change gives the WHOLE command in 'run', with every line. If nothing in it must "
                                "change, leave it out of 'changes' and it runs as the book has it")
                continue
            s.update(run=run, changed=_norm(run) != _norm(by_index[idx]["text"]), why=why)
        if ch.get("as") in ("root", "lfs"):
            s.update(**{"as": ch["as"]}, why=why or s["why"])
    left = [s["book"] for s in steps if not s.get("omitted") and re.search(r"/dev/<|<[a-z]{2,10}>", s["run"])]
    if left:
        problems.append(f"command(s) {left} still have a placeholder like /dev/<xxx>: change them")
    if not steps or all(s.get("omitted") for s in steps):
        problems.append("nothing would run: a section with no commands needs added steps")
    journal(task["id"], "llm-chat", "proposal",
            (f"{len(data['changes'])} change(s) to the book's commands" if data["changes"] else
             "runs the section exactly as the book has it") + "\n\n"
            + "\n\n".join(f"[{s['book'] if s['book'] is not None else '+'}]"
                          + (" OMIT" if s.get("omitted") else " CHANGED" if s.get("changed") else " as the book")
                          + (f" as {s['as']}" if s.get("as") else "")
                          + (f" -- {s['why']}" if s["why"] else "")
                          + (f"\n{s['run']}" if s.get("changed") or s["book"] is None else "")
                          for s in steps) + (f"\n\nexpect: {data.get('expect', '')}" if data.get("expect") else ""),
            {"model": who, "problems": problems, "raw": reply[:4000], "facts": fx})
    return (steps if not problems else []), "; ".join(problems)


def run_task(task_id: int, build: dict, m: Machine, manifest: dict, done_numbers: set[str],
             plan_only: bool = False, vm_id: str = "") -> bool:
    st, task = api("GET", f"/v1/lfs/tasks/{task_id}")
    log(f"task {task['seq']}: {task['number']} {task['title']} ({task['context']})")
    set_state(task_id, "running")
    ensure_mounts(m, task, done_numbers)
    if task["checkpoint_before"] and task["attempts"] <= 1 and not plan_only and vm_id:
        if not checkpoint(m, vm_id, task, "before"):
            set_state(task_id, "stuck", "couldn't take the checkpoint before a high-risk section")
            return False
    srcdir, cwd = "", {"chroot": "/", "host-lfs": f"{LFS}/sources", "host-root": "/root"}[task["context"]]
    src = source_file(task, manifest)
    if src:
        base = "/sources" if task["context"] == "chroot" else f"{LFS}/sources"
        code, top = m.sh(f"cd {LFS}/sources && tar -tf {shlex.quote(src)} | head -1 | cut -d/ -f1")
        srcdir = top.strip()
        own = "lfs:lfs" if task["context"] == "host-lfs" else "root:root"
        code, out = m.sh(f"cd {LFS}/sources && rm -rf {shlex.quote(srcdir)} && tar -xf {shlex.quote(src)} "
                         f"&& chown -R {own} {shlex.quote(srcdir)}", timeout=1800)
        if code != 0:
            journal(task_id, "controller", "result", f"unpacking {src} failed:\n{out[-1500:]}")
            set_state(task_id, "stuck", f"couldn't unpack {src}")
            return False
        cwd = f"{base}/{srcdir}"
        journal(task_id, "controller", "note", f"unpacked {src} into {cwd}")
    task["_cwd"], task["_srcdir"] = cwd, srcdir
    # LFS-026: every command skipped by the controller (7.4: the chroot it
    # enters itself) leaves nothing to plan; the section is done by the
    # controller, and asking the model only makes it refuse a correct "nothing".
    cmds = (task["section"] or {}).get("commands", [])
    if cmds and all(c.get("skipped") for c in cmds):
        journal(task_id, "controller", "note", "every command in this section is the controller's own: "
                + "; ".join(f"[{c['index']}] {c['skipped']}" for c in cmds))
        set_state(task_id, "done", "the controller does this section itself")
        if task["checkpoint_after"] and vm_id:
            checkpoint(m, vm_id, task, "after")
        return True
    phase("planning", task)
    steps, why = plan(task, build, m)
    if not steps:
        steps, why2 = plan(task, build, m, feedback=why)
        if not steps:
            journal(task_id, "controller", "escalation", f"no usable plan after two tries: {why2 or why}")
            set_state(task_id, "stuck", "no usable plan")
            return False
    if plan_only:
        set_state(task_id, "waiting", "plan only")
        return True
    repairs = 0
    # 3.1: the controller's delivery stands where the book's wget is: after the
    # book's own mkdir and chmod, before the md5sum check (LFS-006, LFS-015).
    wget_at = next((c["index"] for c in (task["section"] or {}).get("commands", [])
                    if c.get("skipped") and "wget" in c["text"]), None) if task["number"] == "3.1" else None
    delivered = wget_at is None
    for n, step in enumerate(steps):
        if not delivered and (step["book"] is None or step["book"] > wget_at):
            deliver_sources(m, manifest, task_id)
            delivered = True
        if step.get("omitted"):
            continue
        attempt, run = 0, step["run"]
        ends: list[str] = []
        while True:
            pro, launch = launcher(task["context"], step.get("as"), cwd)
            name = f"task{task['seq']:03d}-step{n + 1}-try{attempt + 1}"
            phase(f"step {n + 1}/{len(steps)} try {attempt + 1}: {run.strip().splitlines()[0][:120]}", task)
            journal(task_id, "controller", "command", run, {"step": n + 1, "book": step["book"], "log": name})
            code, tail, secs = m.run_detached(pro + run + "\n", launch, name)
            ends = _CWD_RE.findall(tail)
            tail = _CWD_RE.sub("\n", tail).rstrip("\n")
            # The longer tail is for the tutor (C5), who reads the journal, not the machine.
            journal(task_id, "lab", "result", f"exit {code} after {secs:.0f}s\n{tail[-3000:]}",
                    {"step": n + 1, "exit": code, "seconds": round(secs), "tail": tail[-16000:] if code else ""})
            if code == 0 and _REPORTED_PROBLEM.search(tail):
                reply, _ = ask_model(OUTPUT_JUDGE_SYSTEM, f"Section {task['number']} {task['title']}.\nStep:\n{run[:1500]}\n\n"
                                     f"Output (end):\n{tail[-3500:]}\n\nThe book's text:\n"
                                     f"{(task['section'] or {}).get('text', '')[:3000]}", max_tokens=300,
                                     schema=OUTPUT_SCHEMA)
                verdict = _json_reply(reply) or {}
                journal(task_id, "llm-chat", "lesson", f"exit 0, but the output reports a problem: "
                        f"{'met the requirement' if verdict.get('ok') else 'NOT met'} -- {verdict.get('why', reply[:300])}")
                if verdict.get("ok"):
                    break
                code = 1  # treat as a failure: repair it
            elif code == 0:
                break
            if _CHECK.search(run):
                reply, _ = ask_model(JUDGE_SYSTEM, f"Section {task['number']} {task['title']}.\nStep:\n{run}\n\n"
                                     f"Output (end):\n{tail[-3500:]}\n\nThe book's text:\n{(task['section'] or {}).get('text', '')[:4000]}",
                                     max_tokens=400, schema=JUDGE_SCHEMA)
                verdict = _json_reply(reply) or {}
                journal(task_id, "llm-chat", "lesson", f"test result: {'acceptable' if verdict.get('acceptable') else 'NOT acceptable'}"
                        f" -- {verdict.get('why', reply[:300])}")
                if verdict.get("acceptable"):
                    break
            attempt += 1
            repairs += 1
            if attempt > REPAIRS_PER_STEP or repairs > REPAIRS_PER_TASK:
                journal(task_id, "controller", "escalation",
                        f"step {n + 1} still fails after {attempt - 1} repair(s); stopping here for help (C5)")
                set_state(task_id, "stuck", f"step {n + 1} fails")
                return False
            phase(f"asking for a repair of step {n + 1}", task)
            reply, _ = ask_model(FIX_SYSTEM.format(lfs=build["lfs_version"], system_disk=SYSTEM_DISK),
                                 f"Section {task['number']} {task['title']}, run {task['context']} in {cwd}.\n"
                                 f"Failed step:\n{run}\n\nOutput (end):\n{tail[-3500:]}\n\nFacts:\n{facts(m)}"
                                 + ("\n\nYOUR TUTOR'S NOTES FOR THIS SECTION (follow them):\n"
                                    + "\n".join(f"- {n}" for n in tutor_notes(task)) if tutor_notes(task) else ""),
                                 max_tokens=700,
                                 schema=FIX_SCHEMA)
            fix = _json_reply(reply) or {}
            before = "\n".join(str(c) for c in (fix.get("commands") or []) if str(c).strip())
            replace = str(fix.get("instead") or "") if fix.get("then") == "run this instead" else ""
            if fix.get("then") == "the step's work is already done" and not before:
                journal(task_id, "llm-chat", "proposal", f"repair: {fix.get('cause', '')} -- the step's work is already done")
                ok, why = _really_done(m, run, tail)
                journal(task_id, "controller", "note", ("accepted: " if ok else "not accepted: ") + why)
                if ok:
                    break
                fix["then"], before = "rerun the step", ""
            # LFS-015: a repair that deletes directories destroyed delivered work once.
            # Only inside the package's own unpacked tree is that allowed.
            if re.search(r"\brm\s+(?:-\w*[rR]\w*|--recursive)\b", before + "\n" + replace) and \
                    not (srcdir and all(srcdir in seg or "build" in seg for seg in
                                        re.findall(r"\brm\s+-\w*[rR]\w*\s+([^;&|\n]+)", before + "\n" + replace))):
                journal(task_id, "controller", "note", f"refused a repair that deletes directories: {before or replace}")
                before, replace = "", ""
            book_system = {w for c in (task["section"] or {}).get("commands", []) for w in _SYSTEM.findall(c["text"])}
            bad = sorted({w for w in _SYSTEM.findall(before + "\n" + replace) if w not in book_system or _NEVER.fullmatch(w)})
            if bad:
                journal(task_id, "controller", "note", f"refused a repair using {', '.join(bad)} (not this section's; "
                        f"LFS-020): {before or replace}")
                before, replace = "", ""
            if _DOWNLOAD.search(before + "\n" + replace):
                journal(task_id, "controller", "note", f"refused a repair that downloads from the internet: {before or replace}")
                before, replace = "", ""
            if _DANGER.search(before + "\n" + replace):
                journal(task_id, "controller", "note", f"refused a repair that touches a disk outside the LFS disk: {reply[:500]}")
                before, replace = "", ""
            journal(task_id, "llm-chat", "proposal", f"repair: {fix.get('cause', reply[:300])}"
                    + (f"\nfirst: {before}" if before else "") + (f"\ninstead: {replace}" if replace else ""))
            if before:
                pro2, launch2 = launcher(task["context"], step.get("as"), cwd)
                c2, t2, _ = m.run_detached(pro2 + before + "\n", launch2, name + "-fix")
                journal(task_id, "lab", "result", f"fix exit {c2}\n{t2[-1500:]}")
            if replace:
                run = replace
        if ends and ends[-1] != cwd:
            cwd = ends[-1]  # the step's own `cd`, as in one shell (LFS-018)
    if not delivered:
        deliver_sources(m, manifest, task_id)
    if task["number"] == "2.7":
        record_mounts(m)
    if srcdir:
        m.sh(f"cd {LFS}/sources && rm -rf {shlex.quote(srcdir)}")
    set_state(task_id, "done", f"{len([s for s in steps if not s.get('omitted')])} step(s) ran cleanly")
    if task["checkpoint_after"] and vm_id:
        checkpoint(m, vm_id, task, "after")  # a failed one is journalled; the work itself is done
    return True


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--build", type=int, required=True)
    ap.add_argument("--until", default="", help="stop after the task with this section number (e.g. 5.2)")
    ap.add_argument("--max-tasks", type=int, default=0)
    ap.add_argument("--plan-only", action="store_true", help="ask for the next task's plan and journal it; run nothing")
    ap.add_argument("--follow", action="store_true",
                    help="when a task is stuck, wait for it to be reset (by Sentinel, the tutor or Paul) and carry on")
    args = ap.parse_args()
    if not API or not TOKEN:
        log("needs LABVM_BROKER_URL and LABVM_BROKER_TOKEN (verify-proxy's environment)")
        return 2
    st, build = api("GET", f"/v1/lfs/builds/{args.build}")
    if st != 200:
        log(f"no build {args.build}: {st}")
        return 2
    try:
        manifest = _manifest()
    except (urllib.error.URLError, ValueError) as e:
        log(f"can't read the manifest from the repo ({REPO}/lfs/manifest.json): {e}")
        return 2
    try:
        m, vm_id = ensure_machine(build)
        ensure_swap(m)
    except (RuntimeError, OSError, paramiko.SSHException) as e:
        log(f"no build machine: {e}")
        return 2
    _hb["build"] = args.build
    threading.Thread(target=_beat_forever, daemon=True, name="heartbeat").start()
    done = 0
    while True:
        st, nxt = api("GET", f"/v1/lfs/builds/{args.build}/next")
        task = nxt.get("task")
        if not task:
            phase("finished")
            log("the queue is finished")
            return 0
        st, b = api("GET", f"/v1/lfs/builds/{args.build}")
        if task["state"] in ("stuck", "escalated") or b.get("status") == "paused":
            if not args.follow:
                log(f"task {task['seq']} ({task['number']} {task['title']}) is {task['state']}: waiting for help")
                return 1
            # C5: the ladder works on it (Sentinel's nudge, the tutor, Paul); a
            # reset to waiting, or the build resumed, lets the worker carry on.
            phase(f"waiting for help: {task['number']} is {task['state']}"
                  + (" (build paused)" if b.get("status") == "paused" else ""), task)
            time.sleep(60)
            continue
        st, tasks = api("GET", f"/v1/lfs/builds/{args.build}/tasks?state=done")
        done_numbers = {t["number"] for t in tasks.get("items", [])}
        api("POST", f"/v1/lab-vms/{vm_id}/touch")
        if not run_task(task["id"], build, m, manifest, done_numbers, args.plan_only, vm_id):
            if args.follow:
                continue  # the loop above waits for help
            return 1
        done += 1
        if args.plan_only or (args.until and task["number"] == args.until) or (args.max_tasks and done >= args.max_tasks):
            log(f"stopping after {task['number']} {task['title']}")
            # Sentinel reads "stopped" as a deliberate stop, not a lost worker.
            phase(f"stopped: ran to {task['number']} as asked")
            beat_now()
            return 0


def _manifest() -> dict:
    """lfs/manifest.json, as the API host publishes it beside the mirror."""
    with urllib.request.urlopen(f"{REPO}/lfs/manifest.json", timeout=60) as r:
        return json.loads(r.read())


if __name__ == "__main__":
    sys.exit(main())
