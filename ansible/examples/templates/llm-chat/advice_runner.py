"""advice_runner.py -- try a Linux Help answer for real, step by step, in a
fresh disposable microVM (llm-chat-lab-sandbox-Phased-Implementation.md, L4).

Direct request: "we need to be able to install anything the llm might offer
as advice to install and then configure it ... and as a bonus we'll start
collecting a decent corpus of our own that is verified working response or
not and what did/didn't work in advice (for instance, it says to install x
when x doesn't even exist, it says to configure x by using file y when file
y isn't part of the install ...)". Decided: every answer is run
automatically, every step.

The answer is split into steps in the order it gives them:
- shell blocks (bash/sh/shell/console/untagged, and not config-looking)
  are RUN as the student would paste them, as `student` with sudo;
- config blocks are WRITTEN to the path the answer names for them (the
  editor command just before, else the nearest path in the text before),
  appended or replaced according to the wording;
- blocks introduced as example output are skipped;
- editor commands (nano, vim ...) are replaced by those writes.

Each step is classified (package_not_found, command_not_found,
file_missing, service_failed, config_invalid, interactive, timeout,
step_failed, ok), then the run is checked as a whole: packages installed,
services it started are active, config validators pass, and the paths it
relies on exist. The VM is thrown away afterwards; nothing touches the
student's own Terminal.

The runner's own access is the vsock control channel (microvm.py), so a
step that breaks sshd, PAM, sudo or the firewall only fails the steps
after it -- which is exactly the finding -- and never loses the run.
"""
from __future__ import annotations

import io
import re
import shlex
import textwrap
import time
import uuid
from dataclasses import asdict, dataclass, field

# -- Parsing ------------------------------------------------------------------

RUNNABLE_TAGS = {"", "bash", "sh", "shell", "console", "zsh"}
_FENCE_RE = re.compile(r"```([A-Za-z0-9_+-]*)[^\n]*\n(.*?)```", re.DOTALL)
_PATH_RE = re.compile(r"(?<![\w.$-])((?:/etc|/usr|/var|/opt|/srv|/home|/root|/lib|/run)/[\w./@+:-]*[\w@+-])")
_EDITOR_RE = re.compile(r"^\s*(?:sudo\s+(?:-\S+\s+)*)?(?:nano|vim?|vi|emacs|gedit|editor|sensible-editor|sudoedit)"
                        r"(?:\s+-\S+)*\s+(\S+)\s*$")
_OUTPUT_HINT_RE = re.compile(r"(?:you should see|output (?:should|will|similar)|similar to (?:this|the following)"
                             r"|will (?:look|show|display|print)|example output|sample output|returns?:?\s*$)",
                             re.IGNORECASE)
_APPEND_HINT_RE = re.compile(r"\b(?:add|append|insert|include)\b", re.IGNORECASE)
_EDIT_HINT_RE = re.compile(r"\b(?:edit|modify|change|find|locate|uncomment|ensure|update|open)\b", re.IGNORECASE)
_CREATE_HINT_RE = re.compile(r"\b(?:create|new file|with the following content|containing)\b", re.IGNORECASE)
_APT_INSTALL_RE = re.compile(r"\bapt(?:-get)?\s+(?:-\S+\s+)*install\s+([^\n;&|`)]*)")
_SERVICE_RE = re.compile(r"\bsystemctl\s+(?:--now\s+)?(?:start|restart|reload|enable(?:\s+--now)?)\s+"
                         r"(?:--now\s+)?([\w@.-]+)")
_PKG_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9+.-]+$")


def looks_like_config(code: str) -> bool:
    """Server-side twin of the page's looksLikeConfigLine() (F-174)."""
    first = next((ln.strip() for ln in code.splitlines()
                  if ln.strip() and not ln.strip().startswith("#")), "")
    return bool(re.match(r"^(auth|account|password|session|@include)\s+\S", first)
                or re.match(r"^[A-Z][A-Za-z0-9]+\s+\S", first)
                # [section] headers; not "[ -z x ]", a shell test.
                or re.match(r"^\[[\w.:-][\w .:-]*\]$", first)
                # "key = value" with spaces round "=" is never valid shell;
                # "VAR=1 ./run.sh" is.
                or re.match(r"^[A-Za-z_][\w.-]*\s+=\s", first))


@dataclass
class Step:
    n: int
    kind: str            # run | write | append | prepend | edit | skip
    source: str          # the block (or line) from the answer
    target: str = ""     # file path, for write/append
    note: str = ""       # why skipped, or how a write was interpreted
    exit: int | None = None
    output: str = ""
    cls: str = ""
    detail: str = ""
    duration_s: float = 0.0


def _strip_console(code: str) -> str:
    """A `console` block mixes "$ command" lines with output: keep the commands."""
    lines = code.splitlines()
    if not any(ln.lstrip().startswith(("$ ", "# ")) for ln in lines):
        return code
    return "\n".join(ln.lstrip()[2:] for ln in lines if ln.lstrip().startswith(("$ ", "# ")))


def parse_steps(answer: str) -> list[Step]:
    steps: list[Step] = []
    pos = 0
    pending_editor_target = ""
    last_config_target = ""
    for m in _FENCE_RE.finditer(answer):
        before = answer[pos:m.start()]
        pos = m.end()
        tag = m.group(1).lower()
        # Blocks nested in list items carry the list's indentation.
        code = textwrap.dedent(m.group(2)).strip("\n")
        if not code.strip():
            continue
        lead = before[-400:]
        last_sentence = re.split(r"(?<=[.!?:])\s+(?=[A-Z0-9*#`])", lead.strip())[-1] if lead.strip() else ""
        if _OUTPUT_HINT_RE.search(last_sentence):
            steps.append(Step(len(steps) + 1, "skip", code, note="example output, not a step"))
            continue
        if tag in RUNNABLE_TAGS and not looks_like_config(code):
            code = _strip_console(code) if tag == "console" else code
            kept, editors = [], []
            for line in code.splitlines():
                em = _EDITOR_RE.match(line)
                if em:
                    editors.append(em.group(1))
                    continue
                kept.append(line)
            if editors:
                pending_editor_target = editors[-1].replace("~", "/home/student", 1)
            body = "\n".join(kept).strip()
            if body:
                steps.append(Step(len(steps) + 1, "run", body))
            elif editors:
                steps.append(Step(len(steps) + 1, "skip", code,
                                  note=f"opens an editor on {editors[-1]}; the next config block is applied there"))
            continue
        # A config block: where does it go, and how?
        target = pending_editor_target
        if not target:
            paths = _PATH_RE.findall(lead) or _PATH_RE.findall(code.splitlines()[0])
            target = paths[-1] if paths else ""
        if not target and _EDIT_HINT_RE.search(last_sentence):
            # "Ensure that PasswordAuthentication is set to yes" -- still the
            # file the previous config block went into.
            target = last_config_target
        pending_editor_target = ""
        if not target:
            steps.append(Step(len(steps) + 1, "skip", code,
                              note="config block with no file named for it"))
            continue
        if _CREATE_HINT_RE.search(last_sentence) and not _APPEND_HINT_RE.search(last_sentence):
            kind, how = "write", "created with exactly this content"
        elif _APPEND_HINT_RE.search(last_sentence) or not _EDIT_HINT_RE.search(last_sentence):
            kind, how = "append", "added to the end of the file"
        else:
            # "ensure it looks like / set X to" -- an edit in place: set each
            # key where the file already has it (see _edit_config).
            kind, how = "edit", "set in place: existing (or commented-out) lines replaced, missing ones added"
        at_top = re.search(r"\b(?:at the top|beginning|first line)\b", last_sentence, re.IGNORECASE)
        if at_top and kind == "append":
            kind, how = "prepend", "added at the top of the file"
        wants_existing = bool(_EDIT_HINT_RE.search(last_sentence)) and not _CREATE_HINT_RE.search(last_sentence)
        last_config_target = target
        steps.append(Step(len(steps) + 1, kind, code, target=target,
                          note=how + ("|expects-existing" if wants_existing else "")))
    return steps


# -- Classification -------------------------------------------------------------

_CLASSIFIERS = [
    ("package_not_found", re.compile(r"Unable to locate package (\S+)|Package '?([\w.+-]+)'? has no installation candidate"
                                     r"|E: Package '([\w.+-]+)' has no installation candidate")),
    ("service_failed", re.compile(r"Unit ([\w@.-]+) not found|Job for ([\w@.-]+) failed"
                                  r"|Failed to (?:start|restart|reload|enable) ([\w@.-]+)")),
    ("command_not_found", re.compile(r"(?:^|: )([\w.+-]+): command not found|sudo: ([\w.+-]+): command not found", re.MULTILINE)),
    ("interactive", re.compile(r"(?:\(y/n\)|\[y/N\]|\[Y/n\]|[Pp]assword:|Enter [\w ]+:)\s*$")),
]


def classify(exit_code: int | None, output: str, timed_out: bool) -> tuple[str, str]:
    if timed_out:
        return "timeout", "the step didn't finish within its time limit"
    if exit_code == 0:
        return "ok", ""
    tail = output[-3000:]
    for cls, rx in _CLASSIFIERS:
        m = rx.search(tail)
        if m:
            what = next((g for g in m.groups() if g), "")
            return cls, what
    return "step_failed", f"exit {exit_code}"


# -- Validators run at the end, for config the answer touched -------------------

_VALIDATORS = [
    (re.compile(r"sshd_config"), "sshd -t", "sshd config"),
    (re.compile(r"/etc/nginx/"), "nginx -t", "nginx config"),
    (re.compile(r"/etc/apache2/"), "apache2ctl configtest", "apache config"),
    (re.compile(r"/etc/sudoers"), "visudo -c", "sudoers"),
    (re.compile(r"/etc/fail2ban/"), "fail2ban-client -t", "fail2ban config"),
    (re.compile(r"/etc/bind/|named\.conf"), "named-checkconf", "BIND config"),
    (re.compile(r"/etc/postfix/"), "postfix check", "postfix config"),
    (re.compile(r"/etc/haproxy/"), "haproxy -c -f /etc/haproxy/haproxy.cfg", "haproxy config"),
    (re.compile(r"/etc/nftables\.conf"), "nft -c -f /etc/nftables.conf", "nftables ruleset"),
    (re.compile(r"/etc/fstab"), "findmnt --verify", "fstab"),
]


# -- The run ----------------------------------------------------------------------

@dataclass
class RunResult:
    id: str
    started_at: float
    finished_at: float = 0.0
    status: str = "running"      # running | done | error
    vm: dict = field(default_factory=dict)
    steps: list = field(default_factory=list)
    checks: list = field(default_factory=list)
    verdict: str = ""            # lab_verified | failed | partial | not_runnable
    summary: str = ""
    error: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


STEP_TIMEOUT_S = 300
RUN_TIMEOUT_S = 1200
_OUTPUT_KEEP = 4000

_HARNESS_SETUP = r"""set -e
# The runner answers apt's and debconf's questions the way a person
# following the answer would (yes / defaults); nothing else is changed.
printf 'APT::Get::Assume-Yes "true";\n' > /etc/apt/apt.conf.d/99lab-assume-yes
echo 'debconf debconf/frontend select Noninteractive' | debconf-set-selections
"""


def _exec(client, command: str, timeout_s: int) -> tuple[int | None, str, bool]:
    """Run `command` with stdin closed; (exit, combined output, timed out)."""
    wrapped = f"timeout --kill-after=10 {timeout_s} bash -c {shlex.quote(command)} </dev/null 2>&1"
    _, stdout, _ = client.exec_command(wrapped, timeout=timeout_s + 30)
    try:
        out = stdout.read().decode("utf-8", "replace")
        code = stdout.channel.recv_exit_status()
    except OSError as e:
        return None, f"(connection lost: {e})", False
    return code, out, code in (124, 137)


_INI_SECTION_RE = re.compile(r"^\s*\[([^\]]+)\]\s*$")
_INI_KEY_RE = re.compile(r"^\s*#?\s*([A-Za-z0-9_.-]+)\s*[=:]")


def _edit_config(old: str, block: str) -> str:
    """Apply a block the way a person told to "make it look like this" would.
    INI blocks ([section] then key = value): set each key inside that section,
    adding the section if absent. Directive blocks ("Key value", sshd_config
    style): replace the first line for that key, commented out or not, else
    append -- sshd and friends take the FIRST value, so appending alone would
    silently do nothing. Anything else is appended."""
    lines = old.splitlines()
    blines = [b for b in block.splitlines() if b.strip() and not b.strip().startswith("#")]
    if blines and _INI_SECTION_RE.match(blines[0]):
        section = None
        for b in blines:
            sm = _INI_SECTION_RE.match(b)
            if sm:
                section = sm.group(1).strip()
                if not any(_INI_SECTION_RE.match(ln) and _INI_SECTION_RE.match(ln).group(1).strip() == section
                           for ln in lines):
                    lines += ["", f"[{section}]"]
                continue
            km = _INI_KEY_RE.match(b)
            if not km:
                continue
            key = km.group(1)
            start = next(i for i, ln in enumerate(lines)
                         if _INI_SECTION_RE.match(ln) and _INI_SECTION_RE.match(ln).group(1).strip() == section)
            end = next((i for i in range(start + 1, len(lines)) if _INI_SECTION_RE.match(lines[i])), len(lines))
            hit = next((i for i in range(start + 1, end)
                        if _INI_KEY_RE.match(lines[i]) and _INI_KEY_RE.match(lines[i]).group(1) == key), None)
            if hit is not None:
                lines[hit] = b.strip()
            else:
                while end > start + 1 and not lines[end - 1].strip():
                    end -= 1
                lines.insert(end, b.strip())
        return "\n".join(lines) + "\n"
    for b in blines:
        key = b.split()[0]
        rx = re.compile(r"^\s*#?\s*" + re.escape(key) + r"(\s|$)", re.IGNORECASE)
        hit = next((i for i, ln in enumerate(lines) if rx.match(ln)), None)
        if hit is not None and re.match(r"^[A-Za-z][\w-]*\s+\S", b.strip()):
            lines[hit] = b.strip()
        else:
            lines.append(b.strip())
    return "\n".join(lines) + "\n"


def _put_file(root_client, path: str, content: str, mode: str) -> tuple[bool, bool]:
    """Write/append/prepend `content` to `path` as root. Returns
    (existed_before, ok)."""
    sftp = root_client.open_sftp()
    try:
        try:
            with sftp.open(path, "r") as f:
                old = f.read().decode("utf-8", "replace")
            existed = True
        except OSError:
            old, existed = "", False
        parent = path.rsplit("/", 1)[0] or "/"
        root_client.exec_command(f"mkdir -p {shlex.quote(parent)}")[1].channel.recv_exit_status()
        body = content if content.endswith("\n") else content + "\n"
        if mode == "write":
            new = body
        elif mode == "prepend":
            new = body + old
        elif mode == "edit":
            new = _edit_config(old, body)
        else:
            new = old + ("" if not old or old.endswith("\n") else "\n") + body
        with sftp.open(path, "w") as f:
            f.write(new.encode())
        return existed, True
    except OSError:
        return False, False
    finally:
        sftp.close()


def _collect_facts(steps: list[Step]) -> tuple[list[str], list[str], list[str]]:
    """(packages installed, services started, paths the answer relies on)
    from the steps that actually ran."""
    pkgs, services, paths = [], [], []
    for s in steps:
        if s.kind == "run" and s.cls == "ok":
            for m in _APT_INSTALL_RE.finditer(s.source):
                for tok in m.group(1).split():
                    name = tok.split("=", 1)[0].split(":", 1)[0]
                    if not tok.startswith("-") and _PKG_NAME_RE.match(name) and name not in pkgs:
                        pkgs.append(name)
            for m in _SERVICE_RE.finditer(s.source):
                if m.group(1) not in services:
                    services.append(m.group(1))
        if s.target and s.target not in paths:
            paths.append(s.target)
        if s.kind == "run":
            for p in _PATH_RE.findall(s.source):
                if p not in paths:
                    paths.append(p)
    return pkgs, services, paths


def run_advice(answer: str, make_vm, progress=None, run_id: str = "") -> RunResult:
    """Run `answer` in a VM from make_vm() (an un-booted microvm.MicroVM).
    `progress(result)` is called after each step so a caller can publish it."""
    result = RunResult(id=run_id or uuid.uuid4().hex[:12], started_at=time.time())
    steps = parse_steps(answer)
    result.steps = [asdict(s) for s in steps]
    publish = progress or (lambda r: None)
    if not any(s.kind in ("run", "write", "append", "prepend", "edit") for s in steps):
        result.status, result.verdict = "done", "not_runnable"
        result.summary = "Nothing in this answer could be run as a step."
        result.finished_at = time.time()
        publish(result)
        return result

    vm = make_vm()
    student = root = None
    deadline = time.monotonic() + RUN_TIMEOUT_S
    try:
        vm.boot()
        result.vm = {"vcpus": vm.vcpu_count, "mem_mib": vm.mem_size_mib, "scratch_mib": vm.scratch_mib}
        root = vm.ssh_client("root")
        student = vm.ssh_client("student")
        _exec(root, _HARNESS_SETUP, 60)
        publish(result)

        for s in steps:
            if time.monotonic() > deadline:
                s.cls, s.detail = "timeout", "the whole run hit its time limit before this step"
                continue
            t0 = time.monotonic()
            if s.kind == "run":
                code, out, timed_out = _exec(student, s.source, STEP_TIMEOUT_S)
                s.exit, s.output = code, out[-_OUTPUT_KEEP:]
                s.cls, s.detail = classify(code, out, timed_out)
            elif s.kind in ("write", "append", "prepend", "edit"):
                how, _, flag = s.note.partition("|")
                s.note = how
                existed, ok = _put_file(root, s.target, s.source, s.kind)
                s.exit = 0 if ok else 1
                if not ok:
                    s.cls, s.detail = "step_failed", f"could not write {s.target}"
                elif flag == "expects-existing" and not existed:
                    s.cls = "file_missing"
                    s.detail = (f"the answer edits {s.target} as if it already exists, but it didn't at this "
                                f"point (created it so the remaining steps could still be tried)")
                else:
                    s.cls = "ok"
            else:
                s.cls = "skipped"
            s.duration_s = round(time.monotonic() - t0, 1)
            result.steps = [asdict(x) for x in steps]
            publish(result)

        # Whole-run checks, as root so a step that broke sudo can't hide them.
        pkgs, services, paths = _collect_facts(steps)
        checks = []
        for p in pkgs:
            code, out, _ = _exec(root, f"dpkg-query -W -f='${{Status}}' {shlex.quote(p)}", 30)
            checks.append({"kind": "package", "subject": p, "ok": "install ok installed" in out,
                           "detail": out.strip()[:200]})
        for svc in services:
            code, out, _ = _exec(root, f"systemctl is-active {shlex.quote(svc)}", 30)
            checks.append({"kind": "service", "subject": svc, "ok": out.strip() == "active",
                           "detail": out.strip()[:200] if out.strip() != "active" else ""})
        touched = " ".join(paths)
        for rx, cmd, label in _VALIDATORS:
            if rx.search(touched):
                binary = cmd.split()[0]
                code, out, _ = _exec(root, f"command -v {binary} >/dev/null && {cmd}", 60)
                if code is not None and "command -v" not in out:
                    checks.append({"kind": "validator", "subject": label, "ok": code == 0,
                                   "detail": out.strip()[-600:]})
        for p in paths:
            code, _, _ = _exec(root, f"test -e {shlex.quote(p)}", 15)
            checks.append({"kind": "path", "subject": p, "ok": code == 0,
                           "detail": "" if code == 0 else "does not exist after the answer's steps"})
        result.checks = checks
        result.steps = [asdict(x) for x in steps]
        _finish_verdict(result, steps, checks)
    except Exception as e:  # noqa: BLE001 -- any failure is reported as the run's error
        result.status, result.error = "error", f"{type(e).__name__}: {e}"
        result.verdict = result.verdict or "partial"
        result.summary = result.summary or "The lab run could not finish."
    finally:
        for c in (student, root):
            try:
                if c is not None:
                    c.close()
            except Exception:  # noqa: BLE001, S110 -- closing a dead session is not news
                pass
        try:
            vm.teardown()
        except Exception as e:  # noqa: BLE001 -- the run's result stands; say so and move on
            result.error = result.error or f"teardown failed: {e}"
        result.finished_at = time.time()
        if result.status == "running":
            result.status = "done"
        publish(result)
    return result


def _finish_verdict(result: RunResult, steps: list[Step], checks: list[dict]) -> None:
    acted = [s for s in steps if s.kind in ("run", "write", "append", "prepend", "edit")]
    bad = [s for s in acted if s.cls not in ("ok",)]
    failed_checks = [c for c in checks if not c["ok"]]
    changed = any(s.kind in ("write", "append", "prepend", "edit") or _APT_INSTALL_RE.search(s.source)
                  or _SERVICE_RE.search(s.source) for s in acted if s.cls == "ok")
    if not bad and not failed_checks and changed:
        result.verdict = "lab_verified"
        result.summary = f"All {len(acted)} steps worked in a fresh Ubuntu 22.04 sandbox and every check passed."
    elif not bad and not failed_checks:
        result.verdict = "partial"
        result.summary = f"All {len(acted)} steps ran, but none changed the system, so there was nothing to verify."
    else:
        result.verdict = "failed"
        parts = []
        for s in bad:
            parts.append(f"step {s.n}: {s.cls.replace('_', ' ')}" + (f" ({s.detail})" if s.detail else ""))
        for c in failed_checks:
            parts.append(f"{c['kind']} check failed: {c['subject']}")
        result.summary = "; ".join(parts[:6]) + ("; ..." if len(parts) > 6 else "")


def plain_step_line(step: dict) -> str:
    """One human line per step for the page and the corpus."""
    icon = {"ok": "✓", "skipped": "–"}.get(step["cls"], "✗")
    what = step["target"] and f"{step['kind']} {step['target']}" or step["source"].splitlines()[0][:80]
    extra = step["detail"] or step["note"]
    return f"{icon} {step['n']}. {what}" + (f" — {extra}" if extra else "")


def text_report(result: RunResult) -> str:
    out = io.StringIO()
    out.write(f"verdict: {result.verdict} -- {result.summary}\n")
    for s in result.steps:
        out.write(plain_step_line(s) + "\n")
    for c in result.checks:
        out.write(f"{'✓' if c['ok'] else '✗'} {c['kind']}: {c['subject']}"
                  + (f" -- {c['detail'][:160]}" if c["detail"] and not c["ok"] else "") + "\n")
    return out.getvalue()
