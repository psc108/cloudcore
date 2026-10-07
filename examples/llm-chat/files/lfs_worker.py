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
- The controller already enters the task's context (user, chroot, directory): do not su, chroot or cd into the package's directory yourself."""

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
        self.sh(f"rm -f {rcf}; setsid nohup bash -c {shlex.quote(launcher + f' > {logf} 2>&1; echo $? > {rcf}')} "
                f"> /dev/null 2>&1 < /dev/null &")
        t0 = time.monotonic()
        while time.monotonic() - t0 < timeout_s:
            time.sleep(10)
            try:
                code, rc = self.sh(f"cat {rcf} 2>/dev/null")
            except (paramiko.SSHException, OSError):
                continue  # the machine is busy or the link dropped; the step keeps running
            if rc.strip().isdigit():
                _, tail = self.sh(f"tail -c 6000 {logf}")
                return int(rc.strip()), tail, time.monotonic() - t0
        self.sh("pkill -f current-step.sh")
        _, tail = self.sh(f"tail -c 6000 {logf}")
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
        launch = (f"export LFS={LFS}; cp {step} $LFS/tmp/lfs-step.sh && chroot \"$LFS\" /usr/bin/env -i HOME=/root "
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
    """The tarball a package task unpacks, by package and version (or the override)."""
    if not task["package"]:
        return None
    ver = task["version_override"] or task["version"]
    pkg = task["package"].lower()
    for f in manifest["files"]:
        name = f["file"].lower()
        if f["kind"] != "source" or f["set"] in ("books", "blfs-common"):
            continue
        if name.startswith(f"{pkg}-{ver}") or name.startswith(f"{pkg}{ver}") or \
                (pkg == "linux" and name == f"linux-{ver}.tar.xz"):
            return f["file"]
    return None


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

def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", s.replace("\\\n", " ")).strip()


def _next_sections(task: dict, n: int = 3) -> str:
    st, data = api("GET", f"/v1/lfs/builds/{task['build_id']}/tasks?state=waiting")
    later = [t for t in data.get("items", []) if t["seq"] > task["seq"]][:n]
    return "; ".join(f"{t['number']} {t['title']}".strip() for t in later) or "(none)"


def plan(task: dict, build: dict, m: Machine, feedback: str = "") -> tuple[list[dict], str]:
    sec = task["section"] or {}
    cmds = [c for c in sec.get("commands", [])]
    listing = "\n".join(
        f"[{c['index']}] ({c['subsection'] or 'section'}{' / ' + c['note'] if c.get('note') else ''}"
        f"{', as root' if c.get('as_root') else ''})"
        + (f" SKIPPED by the controller: {c['skipped']}" if c.get("skipped") else "") + f"\n{c['text']}"
        for c in cmds) or "(The book gives no commands for this section; its text says what to do.)"
    prose = (sec.get("text") or "")[:2500]
    # The tutor's notes for this section (C5): lessons Claude wrote in the journal.
    notes = [j["text"] for j in task.get("journal", []) if j["who"] == "claude" and j["kind"] == "lesson"]
    ctx = {"host-root": "as root on the build host", "host-lfs": "as the lfs user on the build host, with the book's "
           "environment (LFS, LFS_TGT, PATH, CONFIG_SITE, MAKEFLAGS from ~/.bashrc)",
           "chroot": "inside the chroot, as root, with the book's environment"}[task["context"]]
    user = (f"Section {task['number']} {task['title']} ({task['book'].upper()}). The controller runs your steps "
            f"{ctx}" + (f", in the unpacked source directory ({task['_cwd']})" if task.get("_srcdir") else f", in {task['_cwd']}")
            + ".\n" + (f"This build uses version {task['version_override']} instead of the book's {task['version']}: "
                       "change version-specific names accordingly.\n" if task["version_override"] else "")
            + ("\nYOUR TUTOR'S NOTES FOR THIS SECTION (follow them):\n" + "\n".join(f"- {n}" for n in notes) + "\n"
               if notes else "")
            + f"\nThe next sections, which are NOT yours to do now: {_next_sections(task)}.\n"
            + f"\nFacts about the machine:\n{facts(m)}\n\nThe section's commands:\n{listing}\n\n"
            f"The section's text (start):\n{prose}\n" + (f"\nYour previous answer had problems: {feedback}\n" if feedback else ""))
    reply, who = ask_model(PLAN_SYSTEM.format(lfs=build["lfs_version"], system_disk=SYSTEM_DISK, lfs_disk=LFS_DISK), user,
                           max_tokens=900, schema=PLAN_SCHEMA)
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
    for ch in data["changes"]:
        if not isinstance(ch, dict):
            continue
        idx, why, run = ch.get("book"), str(ch.get("why") or ""), str(ch.get("run") or "")
        if not why:
            problems.append(f"a change ({json.dumps(ch)[:80]}) gives no reason")
        if run and _DANGER.search(run):
            problems.append(f"'{run[:80]}' touches a disk or path outside the LFS disk")
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
            {"model": who, "problems": problems, "raw": reply[:4000]})
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
            journal(task_id, "controller", "command", run, {"step": n + 1, "book": step["book"], "log": name})
            code, tail, secs = m.run_detached(pro + run + "\n", launch, name)
            ends = _CWD_RE.findall(tail)
            tail = _CWD_RE.sub("\n", tail).rstrip("\n")
            journal(task_id, "lab", "result", f"exit {code} after {secs:.0f}s\n{tail[-3000:]}",
                    {"step": n + 1, "exit": code, "seconds": round(secs)})
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
            reply, _ = ask_model(FIX_SYSTEM.format(lfs=build["lfs_version"], system_disk=SYSTEM_DISK),
                                 f"Section {task['number']} {task['title']}, run {task['context']} in {cwd}.\n"
                                 f"Failed step:\n{run}\n\nOutput (end):\n{tail[-3500:]}\n\nFacts:\n{facts(m)}", max_tokens=700,
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
    except (RuntimeError, OSError, paramiko.SSHException) as e:
        log(f"no build machine: {e}")
        return 2
    done = 0
    while True:
        st, nxt = api("GET", f"/v1/lfs/builds/{args.build}/next")
        task = nxt.get("task")
        if not task:
            log("the queue is finished")
            return 0
        if task["state"] in ("stuck", "escalated"):
            log(f"task {task['seq']} ({task['number']} {task['title']}) is {task['state']}: waiting for help")
            return 1
        st, tasks = api("GET", f"/v1/lfs/builds/{args.build}/tasks?state=done")
        done_numbers = {t["number"] for t in tasks.get("items", [])}
        api("POST", f"/v1/lab-vms/{vm_id}/touch")
        if not run_task(task["id"], build, m, manifest, done_numbers, args.plan_only, vm_id):
            return 1
        done += 1
        if args.plan_only or (args.until and task["number"] == args.until) or (args.max_tasks and done >= args.max_tasks):
            log(f"stopping after {task['number']} {task['title']}")
            return 0


def _manifest() -> dict:
    """lfs/manifest.json, as the API host publishes it beside the mirror."""
    with urllib.request.urlopen(f"{REPO}/lfs/manifest.json", timeout=60) as r:
        return json.loads(r.read())


if __name__ == "__main__":
    sys.exit(main())
