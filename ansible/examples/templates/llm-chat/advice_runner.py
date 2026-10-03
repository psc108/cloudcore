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

import base64
import hashlib
import hmac
import io
import ipaddress
import json
import re
import secrets
import struct
import shlex
import textwrap
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field

# -- Parsing ------------------------------------------------------------------

RUNNABLE_TAGS = {"", "bash", "sh", "shell", "console", "zsh"}
_FENCE_RE = re.compile(r"```([A-Za-z0-9_+-]*)[^\n]*\n(.*?)```", re.DOTALL)
_PATH_RE = re.compile(r"(?<![\w.$-])((?:/etc|/usr|/var|/opt|/srv|/home|/root|/lib|/run)/[\w./@+:-]*[\w@+-])")
_EDITOR_RE = re.compile(r"^\s*(?:sudo\s+(?:-\S+\s+)*)?(?:nano|vim?|vi|emacs|gedit|editor|sensible-editor|sudoedit)"
                        r"(?:\s+-\S+)*\s+(\S+)\s*$")
_SUDO_E_RE = re.compile(r"^\s*sudo\s+-e\s+(\S+)\s*$")
_CRONTAB_E_RE = re.compile(r"^\s*(sudo\s+)?crontab\s+(?:-u\s+(\S+)\s+)?-e\s*$")
_VISUDO_RE = re.compile(r"^\s*(?:sudo\s+)?visudo(?:\s+-f\s+(\S+))?\s*$")
_SYSTEMCTL_EDIT_RE = re.compile(r"^\s*(?:sudo\s+)?systemctl\s+edit\s+(--full\s+)?([\w@.-]+)\s*$")


def _editor_target(line: str) -> str:
    """The file an editor command opens -- "" if the line isn't one. L10a:
    beyond nano/vim, `crontab -e` (as "crontab:<user>"), `visudo`,
    `systemctl edit` and `sudo -e` (found in L10: `crontab -e` failed)."""
    m = _EDITOR_RE.match(line) or _SUDO_E_RE.match(line)
    if m:
        return m.group(1).replace("~", "/home/student", 1)
    m = _CRONTAB_E_RE.match(line)
    if m:
        return f"crontab:{m.group(2) or ('root' if m.group(1) else 'student')}"
    m = _VISUDO_RE.match(line)
    if m:
        return m.group(1) or "/etc/sudoers"
    m = _SYSTEMCTL_EDIT_RE.match(line)
    if m:
        unit = m.group(2) if "." in m.group(2) else m.group(2) + ".service"
        return f"/etc/systemd/system/{unit}" if m.group(1) else f"/etc/systemd/system/{unit}.d/override.conf"
    return ""


# Edits given in prose rather than a block (found in L10: "Change `#Port 22`
# to `Port 2222`" was never applied, and the run still passed).
_PROSE_CHANGE_RE = re.compile(r"\b[Cc]hange\s+(?:the\s+line\s+|it\s+from\s+)?`([^`\n]+)`\s+to\s+`([^`\n]+)`")
_PROSE_SET_RE = re.compile(r"\b[Ss]et\s+`([A-Za-z_][\w.-]*)`\s+to\s+`([^`\n]+)`")
_PROSE_UNCOMMENT_RE = re.compile(r"\b[Uu]ncomment\s+(?:the\s+line\s+)?`([^`\n]+)`")


def _prose_edits(text: str) -> list[list[str]]:
    ops = [["change", a, b] for a, b in _PROSE_CHANGE_RE.findall(text)]
    ops += [["set", k, v] for k, v in _PROSE_SET_RE.findall(text)]
    ops += [["uncomment", a, ""] for a in _PROSE_UNCOMMENT_RE.findall(text)]
    return ops
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
# Placeholders the student is meant to fill in. L10a: the lab fills them in
# the way a person would -- with a real user, group, path or address it
# creates -- and says so, instead of skipping the step or running it
# literally (found in L10: `sudo passwd -l username`, `/path/to/directory`).
# What still matches _PLACEHOLDER_RE afterwards is skipped as before.
_PLACEHOLDER_RE = re.compile(r"\byour[_-][a-z_]+|\b(?:username|user|youruser)@|"
                             r"<(?:your[\w -]*|[\w-]*(?:user|name|server|ip|file|path|dir|domain|host|group|"
                             r"password|key|port)[\w-]*)>|\bYOUR_[A-Z_]+\b|\bserver_ip\b", re.IGNORECASE)
LAB_USER, LAB_GROUP, LAB_DIR = "labuser", "labgroup", "/srv/lab"
_MICROVM_TARGET_IP = "172.30.0.2"
TARGET_PAIR_IP = _MICROVM_TARGET_IP
_SUBSTITUTIONS = [
    # (pattern, replacement or callable, what the lab must create first)
    (re.compile(r"/home/(?:user|username|your[_-]?user(?:name)?|youruser|<user(?:name)?>)/"), "/home/student/", ""),
    (re.compile(r"(?<![\w/.-])/path/to/([\w./-]*[\w-])"), lambda m: f"{LAB_DIR}/{m.group(1).split('/')[-1]}", "path"),
    # Not a key in key=value (F-200 #26: the cifs mount option username=).
    (re.compile(r"\b(?:your[_-]?user(?:name)?|user[_-]name|new[_-]?user(?:name)?|youruser|username)\b(?!=)|<user(?:name)?>",
                re.IGNORECASE), LAB_USER, "user"),
    (re.compile(r"\b(?:your[_-]?group(?:name)?|group[_-]?name)\b|<group(?:name)?>", re.IGNORECASE), LAB_GROUP, "group"),
    (re.compile(r"\b(?:your[_-]?server[_-]?ip|server[_-]?ip|your[_-]?ip(?:[_-]?address)?|server[_-]?address|"
                r"remote[_-]?(?:host|server)|server\.example\.com)\b|<(?:server|host|ip)[\w-]*>", re.IGNORECASE),
     lambda m: TARGET_PAIR_IP, ""),
]


# Set per answer by parse_steps: the answer's placeholder home is /home/user,
# so a bare account named "user" (User=user, chown user:user) is the same
# placeholder (found in L10: a unit with User=user failed with 217/USER).
_placeholder_account = False
# Set per run: a full VM has a bootloader and loads modules (L18).
_full_vm_run = False
_ACCOUNT_USER_RE = re.compile(r"(?<=\bUser=)user\b|(?<=\bGroup=)user\b|\buser:user\b|(?<=-u )user\b")


def _substitute(text: str) -> tuple[str, list[str], set[str]]:
    """(text with placeholders filled in, what changed, what to create)."""
    changes, needs = [], set()
    if _placeholder_account:
        new = _ACCOUNT_USER_RE.sub(lambda m: "student:student" if m.group(0) == "user:user" else "student", text)
        if new != text:
            changes.append("user -> student (the answer's placeholder account)")
            text = new
    for rx, repl, need in _SUBSTITUTIONS:
        def sub(m, _repl=repl, _need=need):
            new = _repl(m) if callable(_repl) else _repl
            if m.group(0) != new:
                changes.append(f"{m.group(0)} -> {new}")
                if _need == "path":
                    needs.add("path:" + new)
                elif _need:
                    needs.add(_need)
            return new
        text = rx.sub(sub, text)
    # A private example address given as "your DNS server" (nslookup/dig
    # server argument, a resolv.conf nameserver) means this machine here.
    out_lines = []
    for line in text.split("\n"):
        if re.search(r"\b(?:nslookup|dig|host)\b|^\s*nameserver\s", line):
            new = re.sub(r"\b(?:192\.168\.[01]\.\d{1,3}|10\.0\.0\.\d{1,3})\b", "127.0.0.1", line)
            if new != line:
                changes.append("example DNS server address -> 127.0.0.1 (this machine)")
            line = new
        # F-200 #11: `newgrp G` alone opens a new interactive shell (and asks
        # for a group password without a terminal). It only makes the group
        # active now; the lab's later logins and checks are fresh, so they
        # have it already.
        mg = re.match(r"^\s*(?:sudo\s+)?newgrp\s+([\w-]+)\s*$", line)
        if mg:
            line = "true"
            changes.append(f"newgrp {mg.group(1)}: skipped -- it only opens a new shell with the group; "
                           "the lab's next logins have it")
        # ping runs until Ctrl-C, which is what a person presses.
        m2 = re.match(r"^(\s*(?:sudo\s+)?ping6?\s+)", line)
        if m2 and not re.search(r"(?:^|\s)-[a-zA-Z]*c\s*\d", line[m2.end():]):
            line = m2.group(1) + "-c 4 " + line[m2.end():]
            changes.append("ping -> ping -c 4 (a person stops it with Ctrl-C)")
        # L13: an interactive fdisk/gdisk session, its keys described in
        # prose. The lab types the usual ones: one partition, whole disk.
        m = re.match(r"^(\s*)((?:sudo\s+)?(fdisk|gdisk)\s+(/dev/\w+))\s*$", line)
        if m:
            keys = "n\\np\\n1\\n\\n\\nw\\n" if m.group(3) == "fdisk" else "n\\n1\\n\\n\\n\\nw\\ny\\n"
            # udevadm settle: the new partition's device appears a moment
            # after fdisk exits -- unnoticeable typing by hand, but the lab's
            # next command ran first (found in L13: mkfs on /dev/sdb1 "No
            # such file").
            line = f"{m.group(1)}printf '{keys}' | {m.group(2)} && sudo udevadm settle"
            changes.append(f"{m.group(3)} {m.group(4)}: typed the usual keys for one partition using the whole disk "
                           + ("(n, p, 1, Enter, Enter, w)" if m.group(3) == "fdisk" else "(n, 1, Enter x3, w, y)")
                           + " and waited for the new partition to appear")
        out_lines.append(line)
    return "\n".join(out_lines), list(dict.fromkeys(changes)), needs


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
                or re.match(r"^[A-Za-z_][\w.-]*\s+=\s", first)
                # an fstab line: device, mount point, type (found in L13: it ran as a command)
                or re.match(r"^(?:/dev/\S+|UUID=\S+|LABEL=\S+|PARTUUID=\S+|[\w.-]+:/\S*|//[\w.-]+/\S+|tmpfs|proc)"
                            r"\s+(?:/\S*|none|swap)\s+[\w.,-]+(?:\s|$)", first)
                # a crontab line: five schedule fields (or @daily ...) then a command
                or re.match(r"^(?:@(?:reboot|yearly|annually|monthly|weekly|daily|midnight|hourly)|"
                            r"(?:[\d*/,-]+\s+){4}[\d*/,-]+)\s+\S", first))


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
    # L10a: placeholders the lab filled in, and what it had to create for them.
    subs: list = field(default_factory=list)
    needs: list = field(default_factory=list)
    # L10a: edits given in prose ([op, a, b]), applied to `target`.
    edit_ops: list = field(default_factory=list)
    # L10a/L10b: every attempt at this step (the answer's own, then any repair).
    attempts: list = field(default_factory=list)
    repair: str = ""      # L10b: what the lab changed to make this step work
    final: str = ""       # L10b: the commands that worked (for the repaired procedure)


# F-200 #20: example output pasted into a command block (blkid's
# "/dev/sdb1: UUID=... TYPE=...", ls -l's "drwxr-xr-x 2 root ...").
_OUTPUT_LINE_RE = re.compile(r"^\s*(?:/dev/\S+:\s+[A-Z_]+=|[-dlcbps][rwxsStT-]{9}[.+@]?\s+\d+\s)")


def _strip_console(code: str) -> str:
    """A `console` block mixes "$ command" lines with output: keep the commands."""
    code = "\n".join(ln for ln in code.splitlines() if not _OUTPUT_LINE_RE.match(ln))
    lines = code.splitlines()
    if not any(ln.lstrip().startswith(("$ ", "# ")) for ln in lines):
        return code
    return "\n".join(ln.lstrip()[2:] for ln in lines if ln.lstrip().startswith(("$ ", "# ")))


def parse_steps(answer: str) -> list[Step]:
    global _placeholder_account
    _placeholder_account = "/home/user/" in answer
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
            # Prose edits for the file an earlier editor step opened.
            if pending_editor_target:
                ops = _prose_edits(before)
                if ops:
                    steps.append(Step(len(steps) + 1, "prose", before.strip()[-300:], target=pending_editor_target,
                                      edit_ops=ops, note="edit described in the text, applied to the file"))
                    last_config_target, pending_editor_target = pending_editor_target, ""
            kept, editors = [], []
            for line in code.splitlines():
                et = _editor_target(line)
                if et:
                    editors.append(et)
                    continue
                kept.append(line)
            if editors:
                pending_editor_target = editors[-1]
            body, subs, needs = _substitute("\n".join(kept).strip())
            placeholder = _PLACEHOLDER_RE.search(body) or re.search(
                r"#\s*(?:replace|change|substitute)\b[^\n]*\b(?:with|to)\s+(?:your|the)\b[^\n]*", body, re.IGNORECASE)
            if body and placeholder:
                steps.append(Step(len(steps) + 1, "skip", body,
                                  note=f"example with a placeholder ({placeholder.group(0)}) for you to fill in; "
                                       "logins are tested from the lab's prober machine instead"))
            elif body:
                steps.append(Step(len(steps) + 1, "run", body, subs=subs, needs=sorted(needs),
                                  note=("the lab filled in: " + ", ".join(subs)) if subs else ""))
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
        code, subs, needs = _substitute(code)
        target, tsubs, tneeds = _substitute(target)
        if target == "/etc/exports":
            # The answer's example client network means "your clients": in the
            # lab that's the prober's private link (found in L10a: an export for
            # 192.168.1.0/24 refused the lab's own mount).
            new = re.sub(r"\b(?:192\.168|10\.\d{1,3}|172\.(?:1[6-9]|2\d|3[01]))\.\d{1,3}\.\d{1,3}(?:/\d{1,2})?(?=\()"
                         r"|\b(?:client[_-]?ip|client[_-]?address|your[_-]?client\w*)\b(?=\()|<client[\w-]*>(?=\()",
                         "172.30.0.0/24", code, flags=re.IGNORECASE)
            if new != code:
                subs.append("example client network in /etc/exports -> 172.30.0.0/24 (the lab's clients)")
                code = new
        steps.append(Step(len(steps) + 1, kind, code, target=target, subs=subs + tsubs, needs=sorted(needs | tneeds),
                          note=how + (f"; the lab filled in: {', '.join(subs + tsubs)}" if subs or tsubs else "")
                          + ("|expects-existing" if wants_existing else "")))
    # Prose edits after the last block, for a file still open in an editor.
    if pending_editor_target:
        tail = answer[pos:]
        ops = _prose_edits(tail)
        if ops:
            steps.append(Step(len(steps) + 1, "prose", tail.strip()[:300], target=pending_editor_target,
                              edit_ops=ops, note="edit described in the text, applied to the file"))
    _feed_clients(steps)
    _sessions(steps)
    _disk_needs(steps)
    return steps


_PART_RE = re.compile(r"/dev/(?:sd|xvd)([bc])(\d+)\b")


def _disk_needs(steps: list[Step]) -> None:
    """A partition on a spare disk the answer uses but never creates (found
    in F-193: "check a filesystem for errors" on /dev/sdb1) is prepared by
    the lab first -- a partition, with ext4 unless the answer makes its own
    filesystem -- and said so."""
    runs = [s for s in steps if s.kind == "run"]
    for s in runs:
        for disk, num in dict.fromkeys(_PART_RE.findall(s.source)):
            dev = f"/dev/sd{disk}{num}"
            before = "\n".join(x.source for x in runs if x.n <= s.n)
            if re.search(r"\b(?:fdisk|gdisk|cfdisk|sfdisk|parted)\b[^\n]*/dev/(?:sd|xvd)" + disk + r"\b", before):
                continue
            if any(n.startswith(f"disk:{dev}") for x in runs for n in x.needs):
                continue
            fs = not re.search(r"\bmkfs(?:\.\w+)?\b[^\n]*" + re.escape(dev), before)
            # "umount it first": the answer expects it to be mounted.
            mounted = fs and bool(re.search(r"\bumount\s+" + re.escape(dev), s.source))
            s.needs = sorted(set(s.needs) | {f"disk:{dev}:{'ext4' if fs else ''}:{'mounted' if mounted else ''}"})
            s.subs.append(f"prepared {dev}" + (" with an ext4 filesystem" if fs else "")
                          + (", mounted at /mnt/labdisk" if mounted else "") + " (the answer assumes it exists)")
            s.note = "the lab filled in: " + ", ".join(s.subs)


# -- Interactive sessions in answers (L13) ---------------------------------------------
#
# Found in L13 (PostgreSQL): "sudo -i -u postgres", then "psql", then SQL to
# type, then "\q" and "exit" -- a person's interactive session, which the lab
# ran as separate commands as the wrong user and skipped the SQL.

_CLIENT_RE = re.compile(r"^\s*(?:sudo\s+(?:-u\s+[\w-]+\s+)?)?(?:psql|mysql|mariadb)\b(?![^\n]*(?:\s-[cef]\b|\s--command|<|\|))"
                        r"[^\n;&|]*$")
_SQL_RE = re.compile(r"^\s*(?:CREATE|GRANT|ALTER|DROP|INSERT|UPDATE|DELETE|SELECT|FLUSH|USE|SHOW|SET|REVOKE|"
                     r"\\[a-z]+)\b", re.IGNORECASE)
_SQL_QUIT_RE = re.compile(r"^\s*(?:\\q|exit;?|quit;?)\s*$", re.IGNORECASE)


def _feed_clients(steps: list[Step]) -> None:
    """A bare psql/mysql step followed by SQL blocks: the SQL is typed into
    it (a heredoc), and the SQL blocks are marked as part of that step."""
    for i, s in enumerate(steps):
        lines = s.source.rstrip().splitlines()
        if s.kind != "run" or not lines or not _CLIENT_RE.match(lines[-1]):
            continue
        sql, used = [], []
        for nxt in steps[i + 1:]:
            if nxt.kind not in ("skip", "run") or not _SQL_RE.match(nxt.source.lstrip().splitlines()[0]):
                break
            sql += [ln for ln in nxt.source.splitlines() if not _SQL_QUIT_RE.match(ln)]
            used.append(nxt)
        if not sql:
            continue
        s.source = "\n".join(lines[:-1] + [f"{lines[-1].strip()} <<'LABSQL'"] + sql + ["LABSQL"])
        s.subs.append(f"typed the SQL from step{'s' if len(used) > 1 else ''} "
                      + ", ".join(str(u.n) for u in used) + f" into {lines[-1].split()[-1]}")
        s.note = "the lab filled in: " + ", ".join(s.subs)
        for u in used:
            u.kind, u.note = "skip", f"typed into {lines[-1].strip()} in step {s.n}"


def _switch_user(line: str) -> str:
    """The account an interactive "become another user" line switches to
    (sudo -i -u X, sudo -iu X, sudo -u X -i/-s, sudo su - X, su - X,
    sudo -i -> root), or ""."""
    try:
        words = shlex.split(line)
    except ValueError:
        return ""
    sudo = bool(words) and words[0] == "sudo"
    words = words[1:] if sudo else words
    if words and words[0] == "su":
        rest = [w for w in words[1:] if w not in ("-", "-l", "--login")]
        return "root" if not rest else (rest[0] if len(rest) == 1 and not rest[0].startswith("-") else "")
    if not sudo or not words or any(not w.startswith("-") for w in words if w not in _sudo_values(words)):
        return ""
    flags = "".join(w.lstrip("-") for w in words if w.startswith("-") and not w.startswith("--"))
    if "i" not in flags and "s" not in flags:
        return ""
    vals = _sudo_values(words)
    return vals[0] if vals else "root"


def _sudo_values(words: list) -> list:
    out = []
    for a, b in zip(words, words[1:], strict=False):
        if a in ("-u", "-iu", "-ui", "-su", "-us"):
            out.append(b)
    return out


def _sessions(steps: list[Step]) -> None:
    """Run steps after an interactive user switch run as that user, until
    the answer's `exit`."""
    user = ""
    for s in steps:
        if s.kind != "run":
            continue
        end = False  # found in the held-out run: unset when a switch line had more after it
        lines = s.source.strip().splitlines()
        if lines and _switch_user(lines[0]):
            user = _switch_user(lines[0])
            rest = "\n".join(lines[1:]).strip()
            if not rest:
                s.kind, s.note = "skip", f"switches to the {user} account; the steps after it run as {user}"
                continue
            s.source = rest
        elif user and lines and re.fullmatch(r"\s*(?:exit|logout)\s*", lines[-1]):
            body = "\n".join(lines[:-1]).strip()
            if not body:
                s.kind, s.note = "skip", f"leaves the {user} session"
                user = ""
                continue
            s.source, end = body, True
        if user:
            # Not `sudo -i`: it backslash-escapes the command's newlines,
            # which breaks a multi-line step (a heredoc of SQL).
            s.source = f"sudo -u {user} -H bash -lc {shlex.quote('cd ~ && ' + s.source)}"
            s.subs.append(f"ran as {user} (the answer switched to that account)")
            s.note = "the lab filled in: " + ", ".join(s.subs)
            if end:
                user = ""


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
    wording = {
        "package_not_found": "no package called '{}' in Ubuntu 22.04",
        "service_failed": "the service '{}' failed or doesn't exist",
        "command_not_found": "'{}' is not a command here",
        "interactive": "it waited for input nobody could sensibly type",
    }
    for cls, rx in _CLASSIFIERS:
        m = rx.search(tail)
        if m:
            what = next((g for g in m.groups() if g), "")
            return cls, wording[cls].format(what)
    last = next((ln.strip() for ln in reversed(tail.splitlines()) if ln.strip()), "")
    return "step_failed", (last[:160] if last else f"exit {exit_code}")


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


# -- Goal probes (L8) --------------------------------------------------------------
#
# "It ran" says nothing about whether the answer did what was asked: an MFA
# answer can install, configure and restart cleanly and still never ask for
# a code. So after the steps, a second, small prober VM -- joined to the
# target by a private two-VM bridge -- tests the goal from outside: logs in
# for real, recording every prompt; checks which ports another machine can
# reach; fetches what web servers serve. Probes of something the answer
# changed decide the verdict; the rest are reported as information.
# TARGET_PAIR_IP (defined above) is how the prober reaches the target: the
# microVM pair bridge's fixed 172.30.0.2, or a full VM's lab address (F4).
_MFA_RE = re.compile(r"\b(?:mfa|2fa|two[- ]factor|multi[- ]factor|totp|otp|one[- ]time|authenticator|yubikey|"
                     r"pam_google|pam_oath|pam_u2f|verification code)\b", re.I)
_FIREWALL_RE = re.compile(r"\b(?:ufw|iptables|ip6tables|nft|firewall-cmd)\b")
_WEB_PROCS = ("nginx", "apache2", "httpd", "caddy", "lighttpd", "node", "python", "gunicorn", "uvicorn", "php-fpm")
_CODE_WORDS = ("code", "verification", "token", "otp")

_PROBER_SSH = r"""
# Logs in the way the server asks: its key first (as students do), then any
# further method the server requires -- a second factor arrives as a
# keyboard-interactive prompt -- recording every prompt and method used.
import json, socket, sys
import paramiko
host, user, pw, code, keyfile = sys.argv[1:6]
port = int(sys.argv[6]) if len(sys.argv) > 6 else 22
use_key = keyfile != "-"
res = {"ok": False, "prompts": [], "methods": [], "used": [], "error": ""}
def handler(title, instructions, fields):
    out = []
    for prompt, echo in fields:
        res["prompts"].append(prompt.strip())
        p = prompt.lower()
        out.append(code if any(w in p for w in ("code", "verification", "token", "otp")) else pw)
    return out
try:
    sock = socket.create_connection((host, port), timeout=8)
    t = paramiko.Transport(sock)
    t.banner_timeout = 10
    t.start_client(timeout=10)
    try:
        t.auth_none(user)
    except paramiko.BadAuthenticationType as e:
        res["methods"] = list(e.allowed_types)
    remaining = list(res["methods"])
    if use_key and "publickey" in remaining:
        more = t.auth_publickey(user, paramiko.Ed25519Key.from_private_key_file(keyfile))
        res["used"].append("publickey")
        remaining = list(more or [])
    if not t.is_authenticated() and "keyboard-interactive" in remaining:
        t.auth_interactive(user, handler)
        res["used"].append("keyboard-interactive")
    elif not t.is_authenticated() and "password" in remaining:
        res["prompts"].append("(password)")
        t.auth_password(user, pw)
        res["used"].append("password")
    res["ok"] = t.is_authenticated()
    t.close()
except paramiko.AuthenticationException as e:
    res["error"] = "authentication failed" + (f": {e}" if str(e) else "")
except Exception as e:
    res["error"] = f"{type(e).__name__}: {e}"
print(json.dumps(res))
"""


# The target's clock minus ours, measured at setup: codes must match the
# clock the target checks them with. A microVM's clock starts from a
# whole-second RTC (found live: up to ~1s behind, so codes computed from the
# coordinator's clock were rejected near a 30s boundary).
_target_clock_offset = 0.0


def totp(secret_b32: str, at: float | None = None, step: int = 30, digits: int = 6) -> str:
    """RFC 6238 code for a base32 secret (google-authenticator's first line),
    by the target's clock unless `at` is given."""
    key = base64.b32decode(secret_b32.strip().upper() + "=" * (-len(secret_b32.strip()) % 8))
    counter = int(((time.time() + _target_clock_offset) if at is None else at) // step)
    digest = hmac.new(key, struct.pack(">Q", counter), hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    value = struct.unpack(">I", digest[offset:offset + 4])[0] & 0x7FFFFFFF
    return str(value % 10 ** digits).zfill(digits)


def _check(kind: str, subject: str, ok: bool, detail: str = "", decisive: bool = True) -> dict:
    return {"kind": kind, "subject": subject, "ok": ok, "detail": detail, "decisive": decisive}


def _pam_services_touched(steps: list[Step]) -> set[str]:
    touched = set()
    for s in steps:
        for path in [s.target] + _PATH_RE.findall(s.source if s.kind == "run" else ""):
            m = re.match(r"/etc/pam\.d/([\w.-]+)$", path or "")
            if m:
                touched.add(m.group(1))
    return touched


class _LoginProber:
    """SSH (from the prober) and PAM (pamtester, on the target) logins as
    `student`, with a test password and a prober key the harness installs --
    said so in the results. Used for a baseline before the answer's steps
    and again after them."""

    def __init__(self, root, prober_root, services: list[str]):
        self.root, self.prober_root, self.services = root, prober_root, services
        self.pw = _lab_password or ("Lab-" + secrets.token_hex(6))
        self.attempts = 0
        self.secret = ""
        _exec(prober_root, "test -s /tmp/probe_key || ssh-keygen -q -t ed25519 -N '' -f /tmp/probe_key", 20)
        self.pubkey = _exec(prober_root, "cat /tmp/probe_key.pub", 10)[1].strip()

    def prepare(self) -> None:
        # Re-applied each time: a reboot rewrites authorized_keys from MMDS.
        _exec(self.root, f"echo 'student:{self.pw}' | chpasswd; install -d -o student -g student -m 700 "
                         f"/home/student/.ssh; echo {shlex.quote(self.pubkey)} >> /home/student/.ssh/authorized_keys; "
                         "chown student:student /home/student/.ssh/authorized_keys", 30)
        head = _exec(self.root, "head -1 /home/student/.google_authenticator 2>/dev/null", 15)[1].strip()
        self.secret = head if re.fullmatch(r"[A-Z2-7]{16,}", head) else ""

    def _code(self, wrong: bool = False) -> str:
        if wrong or not self.secret:
            return "000000"
        # A distinct time step per attempt: "disallow reuse" is common.
        return totp(self.secret, time.time() + _target_clock_offset + 30 * ((self.attempts % 3) - 1))

    def _pace(self) -> None:
        # pam_google_authenticator's default rate limit is 3 logins per 30s.
        self.attempts += 1
        if self.secret and self.attempts % 3 == 0:
            time.sleep(31)

    def ssh(self, wrong: bool = False, key: bool = True) -> dict:
        # The port sshd really listens on now (found in L10: an answer that
        # moved SSH to 2222 was reported as a lockout because the prober
        # still knocked on 22).
        _, ports, _ = _exec(self.root, "sshd -T 2>/dev/null | awk '$1==\"port\"{print $2; exit}'", 15)
        port = ports.strip() if ports.strip().isdigit() else "22"
        _, out, _ = _exec(self.prober_root, f"python3 /tmp/probe_ssh.py {TARGET_PAIR_IP} student "
                                            f"{shlex.quote(self.pw)} {self._code(wrong)} "
                                            f"{'/tmp/probe_key' if key else '-'} {port}", 60)
        self._pace()
        try:
            return json.loads(out.strip().splitlines()[-1])
        except (ValueError, IndexError):
            return {"ok": False, "prompts": [], "methods": [], "used": [], "error": out.strip()[-300:]}

    def pam(self, service: str, wrong: bool = False) -> dict:
        """Authenticate through `service` with pamtester, answering each prompt
        by what it asks for -- a code at a code prompt, the password at a
        password prompt -- in whatever order the stack asks. (Feeding them
        in a fixed order reported a false lockout, found live, for a correct
        answer that put the code first.)"""
        code = self._code(wrong)
        chan = self.root.get_transport().open_session()
        chan.set_combine_stderr(True)
        chan.exec_command(f"timeout 30 pamtester -v {shlex.quote(service)} student authenticate")
        out, answered = "", 0
        last = time.monotonic()
        deadline = last + 40
        try:
            while time.monotonic() < deadline:
                if chan.recv_ready():
                    out += chan.recv(65536).decode("utf-8", "replace")
                    last = time.monotonic()
                    continue
                if chan.exit_status_ready():
                    break
                tail = out.rsplit("\n", 1)[-1]
                if tail.rstrip().endswith(":") and time.monotonic() - last > 0.3 and answered < 6:
                    reply = code if any(w in tail.lower() for w in _CODE_WORDS) else self.pw
                    chan.sendall((reply + "\n").encode())
                    answered += 1
                    out += "\n"  # this prompt is answered; don't answer it again
                    last = time.monotonic()
                time.sleep(0.05)
            while chan.recv_ready():
                out += chan.recv(65536).decode("utf-8", "replace")
        finally:
            chan.close()
        self._pace()
        ok = "successfully authenticated" in out
        last_line = (out.strip().splitlines() or [""])[-1]
        error = re.sub(r"^(?:(?:[A-Z][a-z]+ )*(?:[Pp]assword|[Cc]ode):\s*)+", "", last_line).strip()
        if "Module is unknown" in out:
            error = ("PAM cannot load a module named in this service's stack (not installed?), "
                     "so this login is refused for everyone")
        return {"ok": ok,
                "prompts": re.findall(r"\b((?:[A-Z][a-z]+ )*(?:[Pp]assword|[Cc]ode))\s*:", out),
                "error": "" if ok else error[:200]}

def _describe(r: dict) -> str:
    bits = [f"prompts: {', '.join(r.get('prompts') or []) or 'none'}"]
    if r.get("methods") and not r.get("used"):
        bits.append(f"server offers only: {', '.join(r['methods'])}")
    if r.get("used"):
        bits.append(f"methods used: {', '.join(r['used'])}")
    if r.get("error"):
        bits.append(r["error"])
    return "; ".join(bits)


def _asks_code(r: dict) -> bool:
    return any(any(w in p.lower() for w in _CODE_WORDS) for p in r.get("prompts") or [])


def _login_probes(steps, prober: _LoginProber, baseline: dict, question: str, answer: str,
                  sshd_changed: bool) -> list[dict]:
    """Can the student still log in -- over SSH from another machine and
    through each PAM service the answer edited -- compared with before the
    answer ran; and for an MFA answer, is a second factor asked for,
    accepted when right and refused when wrong?"""
    mfa = bool(_MFA_RE.search(question + "\n" + answer))
    checks = []
    prober.prepare()
    if mfa and not prober.secret:
        checks.append(_check("login", "authenticator secret for student", False,
                             "no ~/.google_authenticator was created, so no code could ever be right"))
    note = " (logging in as student with the prober's key and a test password the lab set)"
    good = prober.ssh()
    before = baseline.get("ssh", {}).get("ok")
    checks.append(_check("login", "SSH login from another machine still works" + note, good["ok"],
                         _describe(good) + ("" if before else "; it didn't work before the answer either"),
                         decisive=bool(before)))
    if mfa:
        # Keys skip PAM's auth stack, so a key login and a password login can
        # behave differently; both are tried.
        nokey = prober.ssh(key=False)
        key_asked, nokey_asked = _asks_code(good), _asks_code(nokey)
        checks.append(_check("login", "SSH login without a key asks for a verification code", nokey_asked,
                             _describe(nokey), decisive=False))
        checks.append(_check("login", "SSH login with a key asks for a verification code", key_asked,
                             _describe(good) + ("" if key_asked else
                                                "; key logins skip PAM's auth stack -- "
                                                "'AuthenticationMethods publickey,keyboard-interactive' in "
                                                "sshd_config would require the code as well"),
                             decisive=False))
        checks.append(_check("login", "SSH login asks for a verification code", key_asked or nokey_asked,
                             "asked with a key" if key_asked else ("asked without a key" if nokey_asked
                                                                  else "neither kind of login asked"),
                             decisive=sshd_changed))
        if key_asked or nokey_asked:
            bad = prober.ssh(wrong=True, key=key_asked)
            checks.append(_check("login", "SSH login refuses a wrong code", not bad["ok"],
                                 "" if not bad["ok"] else "a wrong code was accepted", decisive=sshd_changed))
    for svc in prober.services:
        r = prober.pam(svc)
        was = baseline.get(f"pam:{svc}", {}).get("ok")
        checks.append(_check("login", f"'{svc}' login (PAM) still works", r["ok"],
                             _describe(r) + ("" if was else "; it didn't work before the answer either"),
                             decisive=bool(was)))
        if mfa:
            asked = _asks_code(r)
            checks.append(_check("login", f"'{svc}' login asks for a verification code", asked, _describe(r)))
            if asked:
                bad = prober.pam(svc, wrong=True)
                checks.append(_check("login", f"'{svc}' login refuses a wrong code", not bad["ok"],
                                     "" if not bad["ok"] else "a wrong code was accepted"))
    if mfa and not any(c["subject"].endswith("asks for a verification code") and c["ok"] for c in checks):
        checks.append(_check("login", "some login path asks for a second factor", False,
                             "none of the login paths tested asked for a verification code"))
    return checks


def _network_probes(steps, root, prober_root, answer: str, services: list[str], pkgs: list[str]) -> list[dict]:
    """Which listening ports another machine can reach, and what web servers
    return. Decisive for services the answer set up and ports it opened."""
    _, out, _ = _exec(root, "ss -ltnpH", 15)
    listening = {}
    for line in out.splitlines():
        cols = line.split()
        if len(cols) < 4:
            continue
        addr = cols[3]
        if addr.startswith(("127.", "[::1]", "::1")):
            continue
        port = addr.rsplit(":", 1)[-1]
        proc = (re.search(r'users:\(\("([^"]+)"', line) or [None, "?"])[1]
        if port.isdigit():
            listening.setdefault(int(port), proc)
    if not listening:
        return []
    ports = sorted(listening)
    probe = "; ".join(f"timeout 3 bash -c '</dev/tcp/{TARGET_PAIR_IP}/{p}' 2>/dev/null && echo {p}:open || echo {p}:closed"
                      for p in ports)
    reach = dict(x.split(":") for x in _exec(prober_root, probe, 60)[1].split() if ":" in x)
    fw = any(s.cls == "ok" and _FIREWALL_RE.search(s.source) for s in steps if s.kind == "run")
    started = {x.split(".")[0] for x in services} | set(pkgs)
    checks = []
    for port in ports:
        proc = listening[port]
        is_open = reach.get(str(port)) == "open"
        ours = any(proc.startswith(x) or x.startswith(proc) for x in started if x)
        checks.append(_check("reachable", f"port {port} ({proc}) from another machine", is_open if ours else True,
                             "reachable" if is_open else ("blocked" + (" by the firewall the answer set up" if fw else "")),
                             decisive=ours))
        if is_open and (proc.startswith(_WEB_PROCS) or port in (80, 443, 8080, 8000, 8443)):
            scheme = "https" if port in (443, 8443) else "http"
            # The site the answer configured, not whatever the default server
            # answers for a bare IP.
            names = [n for n in re.findall(r"server_name\s+([^;\s]+)", answer) if n not in ("_", "localhost")]
            host_hdr = f"-H {shlex.quote('Host: ' + names[0])} " if names else ""
            code = _exec(prober_root, f"curl -sk -o /dev/null -m 8 {host_hdr}-w '%{{http_code}}' "
                                      f"{scheme}://{TARGET_PAIR_IP}:{port}/", 20)[1].strip()
            proxied = "proxy_pass" in answer and code in ("502", "503", "504")
            good = code not in ("", "000") and (code < "500" or proxied)
            checks.append(_check("http", f"{scheme}://{names[0] if names else '…'}:{port}/ ({proc})", good,
                                 f"HTTP {code or 'no response'}"
                                 + ("; the proxy works but the upstream app the answer assumes isn't running here" if proxied else ""),
                                 decisive=ours))
    return checks


def _suggest_package(root, name: str, is_command: bool = False) -> str:
    """The real package for a name the answer got wrong, from the sandbox's
    own data: Ubuntu's command-not-found database for a command, else a
    package whose name contains it (google-authenticator ->
    libpam-google-authenticator). "" when there's no clear answer."""
    _, out, _ = _exec(root, f"/usr/lib/command-not-found --ignore-installed {shlex.quote(name)} 2>&1", 20)
    m = re.search(r"sudo apt install ([a-z0-9][a-z0-9+.-]+)", out)
    if m:
        return m.group(1)
    if is_command:
        # A package named exactly like the command is the usual answer
        # (found in L10: htop was "corrected" to bashtop).
        _, pol, _ = _exec(root, f"apt-cache policy {shlex.quote(name)} 2>/dev/null", 15)
        if re.search(r"Candidate:\s*(?!\(none\))\S", pol):
            return name
    # The command-not-found database isn't always built in a fresh sandbox;
    # a package named after the command is the usual case either way.
    _, out, _ = _exec(root, f"apt-cache search --names-only {shlex.quote(re.escape(name))} 2>/dev/null", 20)
    names = [ln.split(" - ", 1)[0].strip() for ln in out.splitlines() if " - " in ln]
    names = [n for n in names if n != name and name in n]
    return min(names, key=len) if names else ""


def _add_corrections(steps: list[Step], root) -> None:
    """Say what the right package is, not only what doesn't exist -- a small
    model follows a correction better than a prohibition (found live: told
    'google-authenticator is not a package', it kept installing it)."""
    for s in steps:
        if s.cls not in ("package_not_found", "command_not_found"):
            continue
        m = re.search(r"'([^']+)'", s.detail)
        if not m:
            continue
        name = m.group(1)
        right = _suggest_package(root, name, s.cls == "command_not_found")
        if s.cls == "package_not_found":
            multi = any(len([t for t in im.group(1).split() if not t.startswith("-")]) > 1
                        for im in _APT_INSTALL_RE.finditer(s.source))
            if right:
                s.detail += f" -- did you mean '{right}'?"
            if multi:
                s.detail += "; apt then installed none of the packages on that line"
        elif right:
            s.detail += f" -- it comes from the package '{right}'"


_OR_INSTALL_RE = re.compile(r"\|\|\s*(?:sudo\s+)?apt(?:-get)?\s+(?:-\S+\s+)*install\b")
_GUARDED_INSTALL_RE = re.compile(r"(?:command\s+-v|which|type|dpkg\s+-s)\s+(\S+)[^|;&\n]*\|\|\s*(?:sudo\s+)?"
                                 r"apt(?:-get)?\s+(?:-\S+\s+)*install\s+([^\n;&|]*)")


def _explain_missing_packages(steps: list[Step], checks: list[dict], root) -> None:
    """An install step that exited 0 but left its package missing did
    nothing: say which step and why, instead of a bare package failure.
    Found live: `apt-get update || apt-get install X` -- the install only
    runs if the update FAILS. The install-if-missing idiom
    (`command -v X || apt-get install X`) is not a mistake when X exists."""
    for c in checks:
        if c["kind"] != "package" or c["ok"]:
            continue
        pkg = c["subject"]
        for s in steps:
            if s.kind != "run" or s.cls != "ok" or pkg not in s.source:
                continue
            guard = next((m for m in _GUARDED_INSTALL_RE.finditer(s.source) if pkg in m.group(2).split()), None)
            if guard and _exec(root, f"command -v {shlex.quote(guard.group(1))}", 10)[0] == 0:
                c["ok"] = True
                c["detail"] = f"not installed, but '{guard.group(1)}' already existed, so the answer's check skipped it"
                break
            s.cls = "no_effect"
            if _OR_INSTALL_RE.search(s.source):
                s.detail = (f"exited 0, but '{pkg}' was never installed: in 'A || B', B only runs when A "
                            "fails, and A succeeded -- chain steps that must all happen with '&&'")
            else:
                s.detail = f"exited 0, but '{pkg}' is not installed afterwards"
            break


# -- Goal checks from the question (L12) ---------------------------------------------
#
# "It ran" is not "it did what was asked" (found in L10: a ufw answer that
# opened only SSH when the question asked for HTTP/HTTPS too, a static IP
# never checked). Each check below is triggered by what the QUESTION asks,
# takes its parameters from the answer, and tests the resulting state --
# from the prober where "from another machine" matters.

_PORT_WORDS = {"ssh": 22, "http": 80, "https": 443, "dns": 53, "smtp": 25, "mysql": 3306, "mariadb": 3306,
               "postgres": 5432, "postgresql": 5432, "ftp": 21, "imap": 143, "rdp": 3389}


def _port_state(prober_root, port: int) -> str:
    """open / refused (the host answered: the firewall let it through) /
    filtered (no answer: dropped), seen from the prober."""
    _, out, _ = _exec(prober_root, f"timeout 3 bash -c '</dev/tcp/{TARGET_PAIR_IP}/{port}' 2>&1; echo rc=$?", 10)
    rc = re.search(r"rc=(\d+)", out)
    code = int(rc.group(1)) if rc else 1
    return "open" if code == 0 else ("refused" if "refused" in out.lower() else "filtered")


def _goal(subject: str, ok: bool, detail: str = "") -> dict:
    return {"kind": "goal", "subject": subject, "ok": ok, "detail": detail, "decisive": True}


# L19: questions where this machine is the client, not the server.
_CLIENT_Q_RE = re.compile(r"\b(?:connect|log ?in|ssh|copy|mount)\b[^?]*\b(?:to|into|on|from)\b[^?]*\b(?:server|machine|host)\b",
                          re.IGNORECASE)
_SERVER_Q_RE = re.compile(r"\b(?:change|set|make|configure|run|listen|move|enable)\b", re.IGNORECASE)


def _goal_checks(question: str, answer: str, steps: list, root, prober_root, prober_ip: str = "") -> list[dict]:
    q, a = question.lower(), answer
    ran = "\n".join(s.final or s.source for s in steps if s.cls in ("ok", "repaired"))
    checks = []

    def sh(cmd, t=30):
        return _exec(root, cmd, t)

    if prober_root is not None and re.search(r"\b(?:ufw|firewall|iptables|nft)\b", q):
        wanted = {p for w, p in _PORT_WORDS.items() if re.search(r"\b" + w + r"\b", q)}
        wanted |= {int(n) for n in re.findall(r"\bport\s+(\d{2,5})\b", q)}
        # L19 (F-200 #12): a rule allowing a port only from some network is
        # checked as such -- the prober should get in if its address is in
        # that network, and be kept out if it isn't.
        src = re.search(r"\bfrom\s+(?:the\s+)?(\d{1,3}(?:\.\d{1,3}){3}/\d{1,2})", q)
        inside = True
        if src and prober_ip:
            try:
                inside = ipaddress.ip_address(prober_ip) in ipaddress.ip_network(src.group(1), strict=False)
            except ValueError:
                src = None
        for port in sorted(wanted):
            st = _port_state(prober_root, port)
            if src and prober_ip and not inside:
                checks.append(_goal(f"port {port} is closed to a machine outside {src.group(1)} ({prober_ip})",
                                    st == "filtered", f"connection {st}"))
            else:
                where = f" (from {prober_ip}, inside {src.group(1)})" if src and prober_ip else " (from another machine)"
                checks.append(_goal(f"port {port} is allowed through the firewall{where}", st != "filtered",
                                    f"connection {st}"))
        if wanted and re.search(r"\bonly\b", q):
            st = _port_state(prober_root, 8081)
            checks.append(_goal("a port the question didn't ask for (8081) is blocked", st == "filtered",
                                f"connection {st}" + ("" if st == "filtered" else ": the firewall let it through")))
    m = re.search(r"\bssh\b.*?\bport\b.*?\b(\d{2,5})\b", q)
    if m and _CLIENT_Q_RE.search(question) and not _SERVER_Q_RE.search(question):
        m = None  # L19 (F-200 #39): this machine is the client; there's no server here to check
    if m and prober_root is not None:
        port = m.group(1)
        _, banner, _ = _exec(prober_root, f"timeout 4 bash -c 'exec 3<>/dev/tcp/{TARGET_PAIR_IP}/{port}; "
                                          f"head -c 4 <&3' 2>&1", 10)
        checks.append(_goal(f"SSH answers on port {port} (from another machine)", banner.startswith("SSH-"),
                            "" if banner.startswith("SSH-") else banner.strip()[:120] or "no answer"))
    if re.search(r"\bpassword\b", q) and re.search(r"\b(?:disable|no|without|only)\b", q) and "ssh" in q:
        _, eff, _ = sh("sshd -T 2>/dev/null | grep -E '^(passwordauthentication|kbdinteractiveauthentication) '")
        on = [ln for ln in eff.splitlines() if ln.split()[-1:] == ["yes"]]
        checks.append(_goal("SSH no longer accepts passwords", not on, "; ".join(on)))
    if re.search(r"\b(?:static ip|ip address|second ip|another ip|add(?:ing)? an? ip)\b", q):
        addrs = sorted({f"{ip}/{pl}" for ip, pl in re.findall(r"\b(\d{1,3}(?:\.\d{1,3}){3})/(\d{1,2})\b", ran)
                        if not ip.startswith(("0.", "255."))})
        _, have, _ = sh("ip -4 -o addr show")
        for addr in addrs[:3]:
            checks.append(_goal(f"{addr} is assigned to an interface", f" {addr} " in have))
    if "hostname" in q:
        m = re.search(r"hostnamectl\s+set-hostname\s+(\S+)", ran)
        if m:
            name = m.group(1).strip("'\"")
            _, now, _ = sh("hostnamectl --static 2>/dev/null || cat /etc/hostname")
            checks.append(_goal(f"the hostname is now '{name}'", now.strip() == name, f"it is '{now.strip()}'"))
    if "timezone" in q or "time zone" in q:
        m = re.search(r"\b([A-Z][a-z]+/[A-Z][A-Za-z_]+)\b", question) or re.search(r"\b([A-Z][a-z]+/[A-Z][A-Za-z_]+)\b", ran)
        if m:
            _, tz, _ = sh("timedatectl show -p Timezone --value")
            checks.append(_goal(f"the timezone is {m.group(1)}", tz.strip() == m.group(1), f"it is {tz.strip()}"))
    if "sudo" in q and re.search(r"\b(?:user|account)\b", q):
        m = re.search(r"\b(?:called|named)\s+['\"`]?([a-z_][a-z0-9_-]*)", q) or \
            re.search(r"\b(?:useradd|adduser)\s+(?:-\S+\s+)*([a-z_][a-z0-9_-]*)", ran)
        if m:
            user = m.group(1)
            _, groups, _ = sh(f"id -nG {shlex.quote(user)} 2>&1")
            _, rights, _ = sh(f"sudo -l -U {shlex.quote(user)} 2>&1")
            ok = "sudo" in groups.split() or "(ALL" in rights
            checks.append(_goal(f"user '{user}' exists and can use sudo", ok, groups.strip()[:120]))
    if re.search(r"\binherit", q) and "group" in q:
        m = re.search(r"chmod\s+(?:-R\s+)?[0-7]*g\+[rwx]*s[rwx]*\s+(\S+)|chmod\s+2[0-7]{3}\s+(\S+)", ran)
        if m:
            d = m.group(1) or m.group(2)
            _, mode, _ = sh(f"stat -c %A {shlex.quote(d)}")
            ok = len(mode.strip()) == 10 and mode.strip()[6] in "sS"
            checks.append(_goal(f"new files in {d} inherit its group (setgid)", ok, mode.strip()))
    if re.search(r"\b(?:systemd|service)\b", q) and re.search(r"\bboot\b", q):
        for unit in dict.fromkeys(re.findall(r"/etc/systemd/system/([\w@.-]+\.service)", a)):
            _, en, _ = sh(f"systemctl is-enabled {unit}")
            checks.append(_goal(f"{unit} will start at boot", en.strip() == "enabled", en.strip()))
            if re.search(r"\b(?:restart|crash)", q):
                _, rs, _ = sh(f"systemctl show -p Restart --value {unit}")
                checks.append(_goal(f"{unit} restarts if it crashes", rs.strip() not in ("", "no"), f"Restart={rs.strip()}"))
    if "fail2ban" in q:
        code, out, _ = sh("for i in 1 2 3; do fail2ban-client status sshd && exit 0; sleep 4; done; exit 1")
        checks.append(_goal("fail2ban is protecting SSH (sshd jail active)", code == 0, out.strip()[-160:]))
    if re.search(r"\blog ?rotat", q):
        m = re.search(r"(/var/log/[\w./*-]+)", question) or re.search(r"(/var/log/[\w./*-]+)", ran)
        if m:
            code, out, _ = sh(f"logrotate -d /etc/logrotate.conf 2>&1 | grep -F {shlex.quote(m.group(1).rstrip('/*'))} | head -3")
            checks.append(_goal(f"logrotate covers {m.group(1)}", bool(out.strip())))
    # L13: disk advice runs on the spare disks (/dev/sdb, /dev/sdc).
    m = re.search(r"\bmount(?:ed)?\b.*?\bat\s+(/[\w./-]+)", q)
    if m:
        path = m.group(1).rstrip(".")
        code, out, _ = sh(f"findmnt -n -o SOURCE,FSTYPE {shlex.quote(path)}")
        checks.append(_goal(f"a filesystem is mounted at {path}", code == 0 and bool(out.strip()), out.strip()))
        if re.search(r"\b(?:permanent|boot|fstab|persist|automatic)", q):
            _, line, _ = sh(f"awk '$2 == \"{path}\"' /etc/fstab")
            vcode, verify, _ = sh("findmnt --verify --tab-file /etc/fstab 2>&1 | tail -3; exit ${PIPESTATUS[0]}")
            listed = any(ln.split()[1:2] == [path] for ln in line.splitlines())
            checks.append(_goal(f"{path} is in /etc/fstab, so it mounts at boot", listed and vcode == 0,
                                ("not listed" if not listed else " ".join(verify.split())[:160])))
    if re.search(r"\blvm\b|logical volume", q):
        _, out, _ = sh("lvs --noheadings -o vg_name,lv_name,lv_size 2>&1")
        checks.append(_goal("an LVM logical volume exists", bool(out.strip()) and "No " not in out, out.strip()[:120]))
    if re.search(r"\braid\s*1\b|\bmirror", q) and "mdadm" in q + a:
        _, out, _ = sh("cat /proc/mdstat")
        ok = bool(re.search(r"\bactive raid1\b", out))
        checks.append(_goal("a RAID1 array is active", ok, " ".join(out.split())[:160]))
    if "wireguard" in q:
        code, out, _ = sh("wg show 2>&1 | head -4; ip -br link show type wireguard")
        up = "interface:" in out and bool(re.search(r"^\S+\s+(?:UP|UNKNOWN)\b", out, re.MULTILINE))
        checks.append(_goal("a WireGuard interface is up", up, " ".join(out.split())[:160]))
    if re.search(r"\bswap\b", q):  # L19 (F-200 #28): not "swappiness"
        _, sw, _ = sh("swapon --show --noheadings")
        checks.append(_goal("swap is active", bool(sw.strip()), sw.strip()[:120]))
    # L19 (F-200 #11, #40): a question about something inside a container
    # isn't checked on the host; "without sudo" is checked as that user.
    if "docker" in q and not re.search(r"\b(?:inside|in|within)\s+(?:a|the|my)?\s*(?:docker\s+)?container", q):
        user = ""
        if re.search(r"without\s+(?:using\s+)?sudo|non-?root|as (?:a )?(?:normal|regular) user", q):
            mu = re.search(r"usermod\s+(?:-\S+\s+)*-a?G\s+\S*docker\S*\s+(\S+)|gpasswd\s+-a\s+(\S+)\s+docker", ran)
            user = (mu.group(1) or mu.group(2)).strip("'\"") if mu else ""
            user = user if _NAME_OK.match(user) else ""
        run = (f"su - {user} -c 'docker run --rm hello-world' 2>&1" if user else "docker run --rm hello-world 2>&1")
        code, out, _ = sh(f"{run} | grep -m1 -e 'Hello from Docker' -e rror -e denied", 180)
        checks.append(_goal(f"a Docker container runs{' as ' + user + ' (no sudo)' if user else ''}",
                            "Hello from Docker" in out, out.strip()[-160:]))
    if "nfs" in q and prober_root is not None:
        _, exports, _ = sh("exportfs -v 2>/dev/null | awk 'NR==1{print $1}'")
        path = exports.strip()
        if path:
            code, out, _ = _exec(prober_root, f"mkdir -p /mnt/labnfs && timeout 30 mount -t nfs {TARGET_PAIR_IP}:{path} "
                                              f"/mnt/labnfs && ls /mnt/labnfs >/dev/null && umount /mnt/labnfs", 45)
            checks.append(_goal(f"another machine can mount {path} over NFS", code == 0, out.strip()[-160:]))
    if re.search(r"\bdns\b|\bbind9?\b", q) and prober_root is not None:
        zones = [z for z in re.findall(r'zone\s+"([\w.-]+)"', a) if not z.endswith((".arpa", "."))]
        for z in zones[:2]:
            _, out, _ = _exec(prober_root, f"dig +short @{TARGET_PAIR_IP} {z} SOA 2>&1", 20)
            checks.append(_goal(f"another machine can resolve {z} from this DNS server", bool(out.strip()) and
                                "timed out" not in out and "SERVFAIL" not in out, out.strip()[:120]))
    if re.search(r"\bpostgres", q):
        for db in dict.fromkeys(re.findall(r"CREATE DATABASE\s+\"?(\w+)", a, re.IGNORECASE)):
            _, out, _ = sh(f"cd /tmp && sudo -u postgres psql -tAc \"select 1 from pg_database where datname='{db}'\" 2>&1")
            checks.append(_goal(f"database '{db}' exists", out.strip() == "1", out.strip()[:120]))
        for user in dict.fromkeys(re.findall(r"CREATE (?:USER|ROLE)\s+\"?(\w+)", a, re.IGNORECASE)):
            _, out, _ = sh(f"cd /tmp && sudo -u postgres psql -tAc \"select 1 from pg_roles where rolname='{user}'\" 2>&1")
            checks.append(_goal(f"database user '{user}' exists", out.strip() == "1", out.strip()[:120]))
    return checks


# sshd -T prints canonical, lower-case names; these are the old spellings
# answers still use.
_SSHD_ALIASES = {"challengeresponseauthentication": "kbdinteractiveauthentication",
                 "skeyauthentication": "kbdinteractiveauthentication"}


def _sshd_effective_checks(steps: list[Step], root) -> list[dict]:
    """Did each directive the answer wrote to sshd_config take effect? sshd
    uses the FIRST value it reads (sshd_config.d is included at the top), so
    a line added at the end is silently ignored when an earlier one sets the
    same key -- found live with an MFA answer's ChallengeResponseAuthentication."""
    wrote = []
    for s in steps:
        if s.cls == "ok" and s.kind == "prose" and s.target.endswith("sshd_config"):
            for op, a, b in s.edit_ops:
                m = re.match(r"^\s*([A-Za-z][A-Za-z0-9]+)\s+(\S.*?)\s*$", b if op == "change" else f"{a} {b}")
                if op in ("change", "set") and m:
                    wrote.append((m.group(1), m.group(2)))
        if s.cls == "ok" and s.kind in ("write", "append", "prepend", "edit") and s.target.endswith("sshd_config"):
            for line in s.source.splitlines():
                m = re.match(r"^\s*([A-Za-z][A-Za-z0-9]+)\s+(\S.*?)\s*$", line)
                if m and not line.lstrip().startswith("#") and m.group(1).lower() not in ("match", "include"):
                    wrote.append((m.group(1), m.group(2)))
    if not wrote:
        return []
    code, out, _ = _exec(root, "sshd -T 2>&1", 30)
    if code != 0:
        return [_check("validator", "sshd effective configuration", False, out.strip()[-300:])]
    eff = {}
    for line in out.splitlines():
        k, _, v = line.partition(" ")
        eff.setdefault(k.lower(), []).append(v.strip().lower())
    checks = []
    for key, value in wrote:
        k = _SSHD_ALIASES.get(key.lower(), key.lower())
        now = eff.get(k)
        if now is None:
            continue
        took = value.lower() in now
        checks.append(_check("effective", f"sshd uses '{key} {value}'", took,
                             "" if took else f"sshd is using '{k} {now[0]}': the answer's line did not take "
                                             "effect (sshd keeps the first value it reads; an earlier line, "
                                             "or a file in sshd_config.d, already sets it)"))
    return checks


def _judge(code, out, timed_out, marks, fullscreen) -> tuple[str, str, str]:
    """(class, detail, benign note) for one execution of a run step."""
    bad = [(st, cmd) for st, cmd in marks if not _benign(cmd, st)]
    benign = [_benign(cmd, st) for st, cmd in marks if _benign(cmd, st)]
    if bad and all(re.search(r"\bumount\b", cmd) for _, cmd in bad) and re.search(r"\bnot mounted\b", out):
        # "umount it first" when it wasn't mounted: a person reads that and goes on.
        bad, benign = [], benign + ["it wasn't mounted, so there was nothing to unmount"]
    if fullscreen and not bad:
        return "ok", "", "full-screen tool: shown for a few seconds, then quit with q"
    if timed_out:
        cls, detail = classify(code, out, True)
        return cls, detail, ""
    if bad:
        cls, detail = classify(bad[0][0] or 1, out, False)
        if cls == "step_failed":
            detail = (f"`{bad[0][1][:100]}` failed (exit {bad[0][0]})"
                      + (f": {detail}" if detail and not detail.startswith("exit ") else ""))
        return cls, detail, ""
    if code and not benign:
        cls, detail = classify(code, out, False)
        return cls, detail, ""
    return "ok", "", (benign[0] if benign else "")


# -- What this lab can't be ----------------------------------------------------------
#
# Found in the L10-L13 re-run (F-193): GPU drivers, GRUB and kernel modules
# "failed" because a microVM has no GPU, no bootloader and its modules built
# in -- advice that is right on a real machine. Such a step is a lab_limit,
# not a failure: it isn't repaired, never becomes a "doesn't work" fact, and
# a run whose only problems are limits is not_testable.

# Limits of a microVM only: a full VM (F4/F5) has a bootloader and loads
# modules, so there these are real failures.
_MICROVM_LIMITS = [
    (re.compile(r"\b(?:modprobe|insmod|rmmod|depmod|dkms)\b|FATAL: Module|Module \S+ not found|"
                r"/lib/modules/\S+: No such file"),
     "loading kernel modules: the lab's kernel has the common ones built in and can't load others"),
    (re.compile(r"\b(?:update-grub|grub-install|grub2?-mkconfig|efibootmgr|update-initramfs|bootctl)\b|"
                r"/etc/default/grub|/boot/grub"),
     "bootloader changes: the lab machine starts its kernel directly, with no GRUB or initramfs"),
]
# L18: limits of any virtual lab machine -- physical hardware it doesn't
# have, and identities on the public internet it can't hold. Matched on the
# failing command and its output, by category, not on particular answers.
_LAB_LIMITS = [
    (re.compile(r"\bnvidia|NVIDIA|\bubuntu-drivers\b|\bcuda\b|\bnouveau\b", re.IGNORECASE),
     "GPU drivers: the lab machine has no GPU or other physical hardware"),
    (re.compile(r"\bsmartctl\b|\bnvme\s+smart-log\b|SMART support is:\s*Unavailable|"
                r"does not support (?:Self Test|SMART)", re.IGNORECASE),
     "disk health (SMART): the lab's disks are virtual and report no SMART data"),
    (re.compile(r"\bsensors(?:-detect)?\b|No sensors found|\bfancontrol\b|/sys/class/(?:thermal|hwmon)"),
     "temperature and fan sensors: the lab machine has none"),
    (re.compile(r"\b(?:iw|iwconfig|iwlist|wpa_cli|bluetoothctl|hciconfig|rfkill)\b|\bwlan\d|\bwlp\w+|"
                r"nmcli\s+(?:dev(?:ice)?\s+)?wifi"),
     "Wi-Fi and Bluetooth: the lab machine has no radios"),
    (re.compile(r"\bcertbot\b|\bacme\.sh\b|Some challenges have failed|urn:ietf:params:acme|"
                r"Let'?s ?Encrypt", re.IGNORECASE),
     "a certificate from a public CA: that needs a real domain whose DNS points at this machine from the "
     "internet, which a lab machine can't have"),
]
# Questions that are about such things outright: the lab says so before
# booting anything (and never asks a public CA for a certificate for a
# domain it doesn't own).
_QUESTION_LIMITS = [
    (re.compile(r"\bSMART\b|\b(?:disk|drive|ssd|hdd|nvme)\b[^?]*\b(?:health|wear|bad sectors)\b", re.IGNORECASE),
     "disk health (SMART): the lab's disks are virtual and report no SMART data"),
    (re.compile(r"\b(?:fans?|temperatures?|overheat\w*|thermal)\b", re.IGNORECASE),
     "temperature and fan sensors: the lab machine has none"),
    (re.compile(r"\b(?:wi-?fi|wireless|wlan|bluetooth)\b", re.IGNORECASE),
     "Wi-Fi and Bluetooth: the lab machine has no radios"),
    (re.compile(r"\b(?:graphics (?:card|driver)s?|gpus?|nvidia)\b", re.IGNORECASE),
     "graphics hardware: the lab machine has no GPU"),
    (re.compile(r"let'?s ?encrypt|\bcertbot\b", re.IGNORECASE),
     "a certificate from a public CA: that needs a real domain whose DNS points at this machine from the "
     "internet, which a lab machine can't have"),
]


def question_limit(question: str) -> str:
    """L18: why the question as a whole can't be tried in a lab, or ""."""
    return next((why for rx, why in _QUESTION_LIMITS if rx.search(question)), "")


def _lab_limit(s: Step) -> str:
    """Why a failed step can't be tested here, or ""."""
    # The failing command and what it printed -- not the whole step, which
    # may mention modprobe in a line that worked (found live: a WireGuard
    # step failing at `wg setconf` was blamed on its modprobe line).
    m = re.search(r"`([^`]+)` failed", s.detail or "")
    failing = m.group(1) if m else (s.source if s.kind == "run" else "")
    text = "\n".join((failing, s.target or "", (s.output or "")[-2000:]))
    rules = _LAB_LIMITS + ([] if _full_vm_run else _MICROVM_LIMITS)
    return next((why for rx, why in rules if rx.search(text)), "")


# -- Step-level repair (L10b) --------------------------------------------------------
#
# Direct request: "a failure to install a package becomes multiple searches
# and download/install attempts etc". When a step fails, the lab tries to fix
# *that step* before moving on -- its own strategies first (fast, no model),
# then one-line fixes from the model -- bounded, every attempt in the log.
# The answer keeps its honest verdict; the repaired procedure gets its own.

REPAIRS_PER_STEP = 3
REPAIRS_PER_RUN = 8
MODEL_FIXES_PER_RUN = 2
_RISKY_RE = re.compile(r"add-apt-repository|ppa:|/etc/apt/sources\.list|curl[^|\n]*\|\s*(?:sudo\s+)?(?:ba)?sh|"
                       r"wget[^|\n]*\|\s*(?:sudo\s+)?(?:ba)?sh|apt-key|signed-by=", re.IGNORECASE)
_APT_TRANSIENT_RE = re.compile(r"404\s+Not Found|Unable to fetch|Failed to fetch|Hash Sum mismatch|Could not get lock|"
                               r"Unable to acquire the dpkg")
_NOT_READY_RE = re.compile(r"does not exist|Failed to access socket|Connection refused|could not connect to server|"
                           r"\.sock[^\n]*No such file|is not running|Is the server running", re.IGNORECASE)
_MISSING_KEY_RE = re.compile(r"\.ssh/id_(rsa|ed25519|ecdsa)(?:\.pub)?['\"]?:? No such file")
_MISSING_PARENT_RE = re.compile(r"(?:cannot create|Can't open|can't create|cannot open|No such file or directory)"
                                r"[^\n]*?['\"]?((?:/etc|/var|/opt|/srv|/home|/usr/local)/[\w./-]+)")


_SHELL_WORDS = {"cd", "echo", "export", "source", ".", "set", "test", "[", "[[", "true", "false", "if", "for",
                "while", "then", "do", "exit", "read", "printf", "eval", "exec", "ulimit", "umask", "alias"}


def _program_of(command: str) -> str:
    """The program a shell command line runs, past sudo/env/VAR=... ("" for
    shell builtins and anything unclear)."""
    try:
        words = shlex.split(command.split("|")[0].split("&&")[0].split(";")[0])
    except ValueError:
        return ""
    while words and (words[0] in ("sudo", "env", "nohup", "time", "command") or "=" in words[0]
                     or (words[0].startswith("-") and len(words) > 1)):
        # sudo options that take a value (-u postgres, -g staff, ...)
        if words[0] in ("-u", "-g", "-h", "-p", "-C", "-D", "-r", "-t", "-U", "-T") and len(words) > 2:
            words.pop(0)
        words.pop(0)
    prog = words[0] if words else ""
    return "" if not re.fullmatch(r"[\w.+-]+", prog) or prog in _SHELL_WORDS else prog


def _repair_candidates(s: Step, out: str, root, already: set, model_fix) -> list[dict]:
    """Fixes worth trying for a failed run step, most specific first. Each is
    {label, pre (commands run first), source (the step, possibly changed),
    by, risky}."""
    cands = []
    quoted = re.findall(r"'([^']+)'", s.detail)
    if s.cls == "package_not_found" and quoted:
        wrong = quoted[0]
        right = _suggest_package(root, wrong)
        if right:
            cands.append({"label": f"installed '{right}' instead of '{wrong}'", "pre": "",
                          "source": re.sub(r"(?<![\w.+-])" + re.escape(wrong) + r"(?![\w.+-])", right, s.source)})
    if s.cls == "command_not_found" and quoted:
        pkg = _suggest_package(root, quoted[0], True)
        if pkg:
            cands.append({"label": f"installed '{pkg}', which provides '{quoted[0]}'",
                          "pre": f"sudo apt-get install -y {pkg}", "source": s.source})
    elif s.cls != "command_not_found":
        # A failing program that isn't installed, whatever was printed (found
        # in L13: `sudo nginx -t` with nginx never installed failed with no
        # output at all, so "command not found" was never seen).
        m = re.search(r"`([^`]+)` failed", s.detail)
        prog = _program_of(m.group(1) if m else s.source.strip().splitlines()[0] if s.source.strip() else "")
        if prog and _exec(root, f"command -v {shlex.quote(prog)}", 10)[0] != 0:
            pkg = _suggest_package(root, prog, True)
            if pkg:
                cands.append({"label": f"installed '{pkg}': '{prog}' wasn't installed",
                              "pre": f"sudo apt-get install -y {pkg}", "source": s.source})
    if _APT_TRANSIENT_RE.search(out):
        cands.append({"label": "refreshed the package index and tried again",
                      "pre": "while sudo fuser /var/lib/dpkg/lock-frontend >/dev/null 2>&1; do sleep 1; done; "
                             "sudo apt-get update", "source": s.source})
    for path in dict.fromkeys(_MISSING_PARENT_RE.findall(out)):
        parent = path.rsplit("/", 1)[0]
        if parent and _exec(root, f"test -d {shlex.quote(parent)}", 10)[0] != 0:
            cands.append({"label": f"created the missing directory {parent}",
                          "pre": f"sudo mkdir -p {shlex.quote(parent)}", "source": s.source})
            break
    if "Permission denied" in out and not s.source.lstrip().startswith("sudo") and "/home/student" not in s.source \
            and "~" not in s.source:
        cands.append({"label": "ran it as root (the answer left sudo off)", "pre": "",
                      "source": f"sudo bash -c {shlex.quote(s.source)}"})
    m = _MISSING_KEY_RE.search(out)
    if m:
        kt = m.group(1)
        cands.append({"label": f"created the SSH key pair the answer assumes you have (id_{kt})",
                      "pre": f"test -f ~/.ssh/id_{kt} || ssh-keygen -q -t {kt} -N '' -f ~/.ssh/id_{kt}",
                      "source": s.source})
    if re.search(r"/dev/[\w/-]+[^\n]*(?:No such file|does not exist)", out):
        cands.append({"label": "waited for the new device to appear (udevadm settle) and tried again",
                      "pre": "sudo udevadm settle; sleep 1", "source": s.source})
    if _NOT_READY_RE.search(out):
        cands.append({"label": "waited for the service to finish starting and asked again", "pre": "sleep 6",
                      "source": s.source})
    if model_fix is not None:
        cands.append({"label": "model", "pre": None, "source": s.source, "by": "model"})
    return [c for c in cands if c["label"] not in already]


def _repair_step(s: Step, student, root, say, budget: dict, model_fix, question: str) -> None:
    """Try _repair_candidates on a failed run step until one works or the
    budgets run out. Updates s in place (cls "repaired" on success)."""
    tried = set()
    for _ in range(REPAIRS_PER_STEP):
        if budget["run"] <= 0:
            return
        cands = _repair_candidates(s, s.output, root, tried, model_fix if budget["model"] > 0 else None)
        if not cands:
            return
        c = cands[0]
        tried.add(c["label"])
        budget["run"] -= 1
        if c.get("by") == "model":
            budget["model"] -= 1
            say("# asking the model for a one-line fix for this step...\n", True)
            fix = (model_fix(question, s.source, s.output[-1500:]) or "").strip()
            if not fix or fix.upper().startswith("NONE"):
                s.attempts.append({"by": "model", "action": "no fix offered"})
                say("# the model had no fix\n")
                continue
            c = {"label": f"the model suggested: {fix[:160]}", "pre": fix, "source": s.source, "by": "model"}
            tried.add(c["label"])
        risky = bool(_RISKY_RE.search((c["pre"] or "") + "\n" + c["source"]))
        say(f"# repair: {c['label']}" + (" [adds a third-party source or pipes a download into a shell]" if risky else "")
            + "\n", True)
        if c["pre"]:
            pre_log = _StepLog(say)
            _exec_step(student, c["pre"], STEP_TIMEOUT_S, on_output=pre_log)
            pre_log.close()
        say("$ " + c["source"].replace("\n", "\n> ") + "\n")
        log = _StepLog(say)
        code, out, timed_out, _, marks, fullscreen = _exec_step(student, c["source"], STEP_TIMEOUT_S, on_output=log)
        log.close()
        cls, detail, _ = _judge(code, out, timed_out, marks, fullscreen)
        s.attempts.append({"by": c.get("by", "lab"), "action": c["label"], "pre": c["pre"], "source": c["source"],
                           "exit": code, "cls": cls, "risky": risky})
        if cls == "ok":
            s.cls, s.repair = "repaired", c["label"] + (" [risky]" if risky else "")
            s.final = ((c["pre"] + "\n") if c["pre"] else "") + c["source"]
            say(f"# repaired: {c['label']}\n")
            return
        s.output, s.detail = out[-_OUTPUT_KEEP:], detail or s.detail
        say(f"# still failing: {detail}\n")


def _procedure(steps: list[Step]) -> str:
    """The repaired procedure as an answer a person could follow: each step
    as it finally worked, the lab's changes marked."""
    parts = []
    for s in steps:
        if s.kind == "run" and s.cls in ("ok", "repaired"):
            note = f"# lab: {s.repair}\n" if s.repair else ""
            parts.append(f"```bash\n{note}{s.final or s.source}\n```")
        elif s.kind in ("write", "append", "prepend", "edit") and s.cls == "ok":
            how = {"write": "Create", "append": "Add to the end of", "prepend": "Add at the top of",
                   "edit": "Set in"}[s.kind]
            parts.append(f"{how} `{s.target}`:\n```text\n{s.source}\n```")
        elif s.kind == "prose" and s.cls == "ok":
            parts.append(f"In `{s.target}`: {s.note}.")
    return "\n\n".join(parts)


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
    verdict: str = ""            # goal_verified | ran_clean | failed | partial | not_testable | not_runnable
    summary: str = ""
    error: str = ""
    # Everything that happened on the lab machine, as a read-only terminal
    # log for the page (tail kept); and, once the run ends, how to reach the
    # machine if the caller kept it.
    transcript: str = ""
    kept: dict = field(default_factory=dict)
    # L10b: when the lab repaired steps, the verdict of the *repaired*
    # procedure, what it changed, and the procedure itself. `verdict` above
    # stays the honest verdict on the answer as written.
    repaired: dict = field(default_factory=dict)
    # L11: on a failed run, what the machine itself said about why (service
    # journals, validators), gathered before the VM goes -- for the next
    # attempt and for whoever reads the run.
    diagnosis: str = ""
    # L16/L17: what the lab set up because the question presumes it, which
    # placeholders it filled (and couldn't), and what the model suggested
    # that the lab refused.
    setup: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)


STEP_TIMEOUT_S = 300
RUN_TIMEOUT_S = 1200
_OUTPUT_KEEP = 4000
_TRANSCRIPT_KEEP = 150_000
# Per step in the log: one noisy step (found live: `journalctl -xe`) must not
# push boot, installs and config writes out of it.
_LOG_HEAD_LINES = 40
_LOG_TAIL_LINES = 15


class _StepLog:
    """Streams a step's output into the transcript, keeping its first
    _LOG_HEAD_LINES lines live, then only the last _LOG_TAIL_LINES, and a
    count of what was left out."""

    def __init__(self, say):
        self.say = say
        self.lines = 0
        self.tail: list[str] = []
        self.partial = ""

    def __call__(self, chunk: str) -> None:
        text = self.partial + chunk
        parts = text.split("\n")
        self.partial = parts.pop()
        for line in parts:
            self.lines += 1
            if self.lines <= _LOG_HEAD_LINES:
                self.say(line + "\n")
            else:
                self.tail = (self.tail + [line])[-_LOG_TAIL_LINES:]
        if self.lines <= _LOG_HEAD_LINES and self.partial and _PROMPT_TAIL_RE.search(self.partial):
            self.say(self.partial)  # a prompt waiting on this line
            self.partial = ""

    def close(self) -> None:
        if self.partial and self.lines < _LOG_HEAD_LINES:
            self.say(self.partial + "\n")
        elif self.partial:
            self.tail = (self.tail + [self.partial])[-_LOG_TAIL_LINES:]
            self.lines += 1
        omitted = self.lines - _LOG_HEAD_LINES - len(self.tail)
        if omitted > 0:
            self.say(f"# ... {omitted} lines omitted ...\n")
        for line in self.tail:
            self.say(line + "\n")

_HARNESS_SETUP = r"""set -e
# The runner answers apt's and debconf's questions the way a person
# following the answer would (yes / defaults); nothing else is changed.
printf 'APT::Get::Assume-Yes "true";\n' > /etc/apt/apt.conf.d/99lab-assume-yes
# Steps run in a real terminal, where systemctl/journalctl open a pager; sudo
# would drop the variables that turn pagers off, so keep them through sudo.
printf 'Defaults env_keep += "PAGER SYSTEMD_PAGER GIT_PAGER LESS SYSTEMD_COLORS MANPAGER"\n' > /etc/sudoers.d/99-lab-env
chmod 440 /etc/sudoers.d/99-lab-env
echo 'debconf debconf/frontend select Noninteractive' | debconf-set-selections
# The lab kernel has its modules built in and no /lib/modules, so the real
# modprobe fails even for a built-in one -- where a real Ubuntu's succeeds
# quietly (found live: `modprobe wireguard` stopped a WireGuard answer whose
# module is built in). This stand-in succeeds for built-ins and defers to
# the real modprobe otherwise.
cat > /usr/local/sbin/modprobe <<'EOS'
#!/bin/sh
# Stand-in installed by the lab: see advice_runner.py _HARNESS_SETUP.
for a in "$@"; do case "$a" in -*) ;; *) m=$(echo "$a" | tr - _); break ;; esac; done
[ -n "$m" ] && [ -e "/sys/module/$m" ] && exit 0
case "$m" in
  tun) c=TUN ;; br_netfilter) c=BRIDGE_NETFILTER ;; i2c_dev) c=I2C_CHARDEV ;; nf_tables) c=NF_TABLES ;;
  loop) c=BLK_DEV_LOOP ;; fuse) c=FUSE_FS ;; bridge) c=BRIDGE ;; 8021q) c=VLAN_8021Q ;; bonding) c=BONDING ;;
  ip_tables) c=IP_NF_IPTABLES ;; iptable_nat) c=IP_NF_NAT ;; nf_nat) c=NF_NAT ;; veth) c=VETH ;; vxlan) c=VXLAN ;;
  *) c= ;;
esac
[ -n "$c" ] && zcat /proc/config.gz 2>/dev/null | grep -qx "CONFIG_$c=y" && exit 0
exec /sbin/modprobe "$@"
EOS
chmod 755 /usr/local/sbin/modprobe
"""


_REBOOT_RE = re.compile(r"\b(?:reboot|shutdown\s+(?:-\S+\s+)*-r|systemctl\s+reboot|init\s+6)\b")


_PROMPT_TAIL_RE = re.compile(r"(?:[?:>]|\(y/n\)|\[y/n\]|\[Y/n\]|\[y/N\])\s*$", re.IGNORECASE)
# Also "(y|n)" -- ufw's own form (found on full VMs: `ufw enable` asked
# "Proceed with operation (y|n)?", got Enter, printed "Aborted" and stayed
# inactive, while the step looked fine).
_YES_NO_RE = re.compile(r"\(y\s*[/|]\s*n\)|\[y\s*[/|]\s*n\]|\byes\s*[/|]\s*no\b", re.IGNORECASE)
_NEW_SECRET_RE = re.compile(r"secret key is:?\s*([A-Z2-7]{16,})")


# The password the lab uses wherever an answer's step asks for one (passwd,
# adduser ...), per run; reported with the kept machine's login details.
_lab_password = ""
_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b[()][A-Z0-9]|\x1b[=>78]")
_ERR_MARK = "__LABERR__"
# Pagers off: systemctl/journalctl/git would otherwise wait in `less`.
_STEP_ENV = "export PAGER=cat SYSTEMD_PAGER=cat GIT_PAGER=cat SYSTEMD_COLORS=0 LESS=FRX MANPAGER=cat"
# Full-screen tools a person looks at and quits (found in L10: `top` failed
# with no terminal). Shown for a few seconds, then quit with q.
_FULLSCREEN_RE = re.compile(r"(?m)^\s*(?:sudo\s+)?(top|htop|atop|btop|iotop|iftop|nethogs|nload|watch|less|more|"
                            r"man|ncdu|mc|nmtui|glances|bmon)\b(?![^\n]*\s-b\b)")
# Commands whose non-zero exit means "found nothing" or "partly readable",
# not "failed" (found in L10: grep with no match, find / with permission
# noise, `ss | grep :80` with nothing listening).
_SEARCH_CMDS = {"grep", "egrep", "fgrep", "zgrep", "rg", "pgrep", "pidof", "lsof", "diff", "cmp", "test", "[", "[[",
                "which", "type", "command", "find", "locate", "whereis"}


def _clean_tty(text: str) -> str:
    """Terminal output as a person would read it: no escape codes, and a
    progress line redrawn with bare \\r shows only its final state."""
    text = _ANSI_RE.sub("", text).replace("\r\n", "\n")
    return "\n".join(seg.rsplit("\r", 1)[-1] for seg in text.split("\n"))


def _benign(cmd: str, status: int) -> str:
    """Why a failing command isn't a failure ("" if it is one)."""
    words = [w for w in cmd.split("|")[-1].split() if "=" not in w or w.startswith("-")]
    while words and words[0] in ("sudo", "env", "time", "nice", "nohup"):
        words = words[1:]
    head = words[0].rsplit("/", 1)[-1] if words else ""
    if head == "find" and status == 1:
        return "find printed its results, with some paths it couldn't read"
    if head in _SEARCH_CMDS and status == 1:
        return f"{head} found nothing to match (exit 1 means no match, not an error)"
    if head == "systemctl" and len(words) > 1 and words[1] in ("is-active", "is-enabled", "is-failed", "status") \
            and status in (1, 3, 4):
        return f"systemctl {words[1]} reports a state; it isn't an error"
    return ""


def _reply_for(prompt: str, output: str) -> str | None:
    """What a person following the answer types at `prompt`: yes to yes/no
    questions, the current code when a tool has just shown a new TOTP
    secret (as they would from their app), the lab's password at a password
    prompt, Enter for defaults; None when there is no sensible reply."""
    low = prompt.lower()
    secrets_seen = _NEW_SECRET_RE.findall(output)
    if "code" in low and secrets_seen:
        return totp(secrets_seen[-1])
    if "-1 to skip" in low:
        return "-1"
    if re.search(r"\byes/no\b", prompt, re.IGNORECASE):
        return "yes"  # ssh's host-key question wants the whole word
    if _YES_NO_RE.search(prompt):
        return "y"
    if any(w in low for w in ("password", "passphrase")):
        return _lab_password or None
    if "pin" in low.split():
        return None
    return ""


def _exec_step(client, command: str, timeout_s: int, on_output=None):
    """Run one of the answer's steps the way a person at a terminal would.
    L10a: in a real pseudo-terminal (TERM=xterm, pagers off), with every
    failing command reported by an ERR trap -- a multi-line block fails if
    any line fails, not only the last (found in L10: an mdadm failure hid
    inside a block that 'passed'). Returns (exit, output, timed out,
    replies given, failing commands [(status, command)], full-screen tool)."""
    script = f"{_STEP_ENV}\ntrap 'echo \"{_ERR_MARK} $? $BASH_COMMAND\"' ERR\n{command}"
    fullscreen = _FULLSCREEN_RE.search(command)
    chan = client.get_transport().open_session()
    chan.get_pty(term="xterm", width=160, height=48)
    # --foreground: without it the command can't read the terminal at all.
    chan.exec_command(f"timeout --foreground --kill-after=10 {timeout_s} bash -c {shlex.quote(script)}")
    raw, replies = "", []
    started = last_data = time.monotonic()
    deadline = started + timeout_s + 30
    stdin_open, quit_sent = True, 0
    shown = ""

    def emit(text: str) -> None:
        nonlocal shown
        clean = _clean_tty(text)
        visible = "\n".join(ln for ln in clean.split("\n") if _ERR_MARK not in ln)
        if on_output and visible:
            on_output(visible)
        shown += visible

    try:
        while time.monotonic() < deadline:
            if chan.recv_ready():
                chunk = chan.recv(65536).decode("utf-8", "replace")
                raw += chunk
                emit(chunk)
                last_data = time.monotonic()
                continue
            if chan.exit_status_ready():
                break
            now = time.monotonic()
            if fullscreen and quit_sent < 2 and now - started > (5 if quit_sent == 0 else 8):
                chan.sendall(b"q" if quit_sent == 0 else b"\x03")
                quit_sent += 1
                continue
            idle = now - last_data
            tail = _clean_tty(raw).rsplit("\n", 1)[-1]
            if idle > 1 and re.search(r"(?:lines \d+-\d+.*|\(END\))\s*$", tail):
                chan.sendall(b"q")  # a pager left waiting: quit it, as a person would
                last_data = time.monotonic()
                continue
            waiting = bool(tail.strip()) and _PROMPT_TAIL_RE.search(tail)
            if stdin_open and waiting and idle > 0.7 and not fullscreen:
                reply = _reply_for(tail, _clean_tty(raw)) if len(replies) < 40 else None
                if reply is None:
                    if idle > 5:
                        chan.sendall(b"\x04")  # nothing sensible to type: end of input
                        stdin_open = False
                else:
                    chan.sendall((reply + "\n").encode())  # the terminal echoes it
                    replies.append("the lab's password" if reply == _lab_password and reply else (reply or "Enter"))
                    last_data = time.monotonic()
            elif stdin_open and idle > 30 and not fullscreen:
                chan.sendall(b"\x04")  # silent and not asking: whatever reads input gets EOF
                stdin_open = False
            time.sleep(0.05)
        # With a terminal, a fast command's output can arrive after its exit
        # status (found live: `sudo nginx -t` failing in a few ms recorded no
        # output, so "command not found" was never seen). Read to end of data.
        drain_until = time.monotonic() + 3
        while time.monotonic() < drain_until:
            if chan.recv_ready():
                chunk = chan.recv(65536).decode("utf-8", "replace")
                raw += chunk
                emit(chunk)
            elif chan.eof_received or chan.closed:
                break
            else:
                time.sleep(0.02)
        code = chan.recv_exit_status() if chan.exit_status_ready() else None
    except OSError as e:
        return None, shown + f"\n(connection lost: {e})", False, replies, [], bool(fullscreen)
    finally:
        chan.close()
    marks = []
    for line in _clean_tty(raw).splitlines():
        m = re.search(_ERR_MARK + r" (\d+) (.*)$", line)
        if m and "echo \"" + _ERR_MARK not in line:
            marks.append((int(m.group(1)), m.group(2).strip()))
    timed_out = (code in (124, 137) or code is None) and not fullscreen
    return code, shown, timed_out, replies, marks, bool(fullscreen)


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


_STUB_SCRIPT_RE = re.compile(r"(/(?:home|opt|srv|usr/local)/[\w./-]+\.(?:py|sh))\b")
# Scripts any unit in the answer runs (ExecStart=): their stand-ins must keep
# running, or systemd sees a service that exits and restarts it forever.
_unit_scripts: set[str] = set()


def _prepare_needs(root_client, s: Step) -> list[str]:
    """Create what the lab's placeholder values refer to, and stand-ins for
    scripts the answer assumes already exist. Returns what was made."""
    made = []
    needs = set(s.needs)
    if "user" in needs:
        _exec(root_client, f"id {LAB_USER} >/dev/null 2>&1 || useradd -m -s /bin/bash {LAB_USER}; "
                           f"echo '{LAB_USER}:{_lab_password}' | chpasswd", 30)
        made.append(f"user '{LAB_USER}'")
    if "group" in needs:
        _exec(root_client, f"getent group {LAB_GROUP} >/dev/null || groupadd {LAB_GROUP}; "
                           f"usermod -aG {LAB_GROUP} student; id {LAB_USER} >/dev/null 2>&1 && "
                           f"usermod -aG {LAB_GROUP} {LAB_USER}; true", 30)
        made.append(f"group '{LAB_GROUP}'")
    for n in sorted(x for x in needs if x.startswith("path:")):
        path = n[5:]
        if re.search(rf"\b(?:mkdir|install\s+-d)\b[^\n]*{re.escape(path)}(?:/|\s|$)", s.source):
            continue  # F-200 #10: the answer makes it; pre-creating it made its mkdir fail
        if "." in path.rsplit("/", 1)[-1]:
            _exec(root_client, f"mkdir -p {shlex.quote(path.rsplit('/', 1)[0])}; test -e {shlex.quote(path)} || "
                               f"echo 'stand-in file created by the lab' > {shlex.quote(path)}", 15)
        else:
            _exec(root_client, f"mkdir -p {shlex.quote(path)}", 15)
        made.append(path)
    for n in sorted(x for x in needs if x.startswith("disk:")):
        _, dev, fs, mounted = n.split(":")
        disk = dev.rstrip("0123456789")
        _exec(root_client, f"test -e {dev} || {{ echo ',,L' | sfdisk -q {disk}; udevadm settle; }}; "
                           # blkid alone succeeds on any partition (its PARTUUID); ask for a filesystem type.
                           + (f"blkid -s TYPE -o value {dev} | grep -q . || mkfs.ext4 -q {dev}; " if fs else "")
                           + (f"mkdir -p /mnt/labdisk && mount {dev} /mnt/labdisk" if mounted else "true"), 60)
        made.append(dev + (" (partition with ext4" + (", mounted" if mounted else "") + ")" if fs else " (partition)"))
    long_running = "ExecStart" in s.source or any(
        path in _unit_scripts for path in _STUB_SCRIPT_RE.findall(s.source + "\n" + s.target))
    for path in dict.fromkeys(_STUB_SCRIPT_RE.findall(s.source + "\n" + s.target)):
        if _exec(root_client, f"test -e {shlex.quote(path)}", 10)[0] == 0:
            continue
        if path.endswith(".py"):
            body = ("#!/usr/bin/env python3\n# stand-in created by the lab: the answer assumes this script exists\n"
                    + ("import time\nwhile True:\n    time.sleep(60)\n" if long_running else "print('stand-in script')\n"))
        else:
            body = ("#!/bin/bash\n# stand-in created by the lab: the answer assumes this script exists\n"
                    + ("exec sleep infinity\n" if long_running else "echo stand-in script\n"))
        owner = "student:student" if path.startswith("/home/student/") else "root:root"
        _exec(root_client, f"mkdir -p {shlex.quote(path.rsplit('/', 1)[0])} && printf %s {shlex.quote(body)} > "
                           f"{shlex.quote(path)} && chmod 755 {shlex.quote(path)} && chown {owner} {shlex.quote(path)}", 15)
        made.append(f"a stand-in for {path}")
    return made


def _apply_prose(root_client, s: Step) -> tuple[bool, str]:
    """Apply edits described in prose to s.target. (ok, what was done)."""
    sftp = root_client.open_sftp()
    try:
        try:
            with sftp.open(s.target, "r") as f:
                lines = f.read().decode("utf-8", "replace").splitlines()
        except OSError:
            return False, f"{s.target} doesn't exist"
        done = []
        for op, a, b in s.edit_ops:
            if op == "change":
                hit = next((i for i, ln in enumerate(lines) if ln.strip() == a.strip()), None)
                if hit is None:
                    hit = next((i for i, ln in enumerate(lines) if a.strip() in ln), None)
                if hit is not None:
                    lines[hit] = b
                    done.append(f"changed '{a}' to '{b}'")
                else:
                    lines = _edit_config("\n".join(lines), b).splitlines()
                    done.append(f"'{a}' wasn't in the file; set '{b}'")
            elif op == "set":
                rx = re.compile(r"^\s*#?\s*" + re.escape(a) + r"\b\s*(=|\s)")
                hit = next((i for i, ln in enumerate(lines) if rx.match(ln)), None)
                sep = "=" if hit is not None and "=" in lines[hit].split(a, 1)[-1][:3] else " "
                if hit is not None:
                    lines[hit] = f"{a}{sep}{b}"
                else:
                    lines.append(f"{a} {b}")
                done.append(f"set {a} to {b}")
            elif op == "uncomment":
                hit = next((i for i, ln in enumerate(lines) if ln.lstrip().startswith("#") and a.strip().lstrip("#").strip()
                            in ln), None)
                if hit is not None:
                    lines[hit] = re.sub(r"^(\s*)#\s?", r"\1", lines[hit])
                    done.append(f"uncommented '{a}'")
                else:
                    done.append(f"no commented-out '{a}' to uncomment")
        with sftp.open(s.target, "w") as f:
            f.write(("\n".join(lines) + "\n").encode())
        return True, "; ".join(done)
    except OSError as e:
        return False, f"could not edit {s.target}: {e}"
    finally:
        sftp.close()


def _put_crontab(root_client, target: str, content: str, mode: str) -> tuple[bool, bool]:
    user = target.split(":", 1)[1] or "student"
    _, old, _ = _exec(root_client, f"crontab -u {shlex.quote(user)} -l 2>/dev/null", 15)
    body = content if content.endswith("\n") else content + "\n"
    new = body if mode == "write" else (old + ("" if not old or old.endswith("\n") else "\n") + body)
    code, _, _ = _exec(root_client, f"printf %s {shlex.quote(new)} | crontab -u {shlex.quote(user)} -", 15)
    return bool(old.strip()), code == 0


_REAL_UUID_RE = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}|"
                           r"[0-9a-fA-F]{4}-[0-9a-fA-F]{4}")


def _fill_fstab_uuids(root, s: Step, steps: list) -> str:
    """An fstab line with a placeholder UUID (UUID=<YOUR_UUID>, UUID=your-uuid)
    gets the real UUID of what the answer mounted there (found in L13: the
    LVM answer's own `blkid` step printed it, then the line kept the
    placeholder). Recorded as a substitution."""
    out = []
    for line in s.source.split("\n"):
        m = re.match(r"(\s*UUID=)(\S+)(\s+)(/\S*)", line)
        if m and not _REAL_UUID_RE.fullmatch(m.group(2)):
            mp = m.group(4)
            _, uuid, _ = _exec(root, f"findmnt -n -o UUID {shlex.quote(mp)}", 10)
            uuid = uuid.strip()
            if not uuid:
                earlier = "\n".join(x.final or x.source for x in steps if x.kind == "run" and x.n < s.n)
                dev = re.findall(r"\bmount\s+(?:-\S+\s+)*(/dev/\S+)\s+" + re.escape(mp) + r"\b", earlier)
                if dev:
                    _, uuid, _ = _exec(root, f"blkid -s UUID -o value {shlex.quote(dev[-1])}", 10)
                    uuid = uuid.strip()
            if uuid and _REAL_UUID_RE.fullmatch(uuid):
                s.subs.append(f"UUID={m.group(2)} -> UUID={uuid} (the real UUID of what is mounted at {mp})")
                line = m.group(1) + uuid + line[m.end(2):]
        out.append(line)
    return "\n".join(out)


_KEY_PLACEHOLDER_RE = re.compile(r"^(\s*(PrivateKey|PublicKey)\s*=\s*)(\S*(?:YOUR|your|<|\.\.\.)\S*)\s*$",
                                 re.MULTILINE)


def _fill_wg_keys(root, s: Step, steps: list) -> str:
    """WireGuard config placeholders, filled as a person would: PrivateKey
    with the key file an earlier step generated, a peer's PublicKey with a
    throwaway key (the lab has no real peer). Recorded as substitutions."""
    earlier = "\n".join(x.final or x.source for x in steps if x.kind == "run" and x.n < s.n)
    files = re.findall(r"(?:tee|>)\s*(/etc/wireguard/[\w.-]*priv[\w.-]*)", earlier)

    def fill(m):
        if m.group(2) == "PrivateKey" and files:
            _, key, _ = _exec(root, f"cat {shlex.quote(files[0])}", 10)
            if key.strip():
                s.subs.append(f"PrivateKey = {m.group(3)} -> the key in {files[0]} (generated by an earlier step)")
                return m.group(1) + key.strip()
        if m.group(2) == "PublicKey":
            _, key, _ = _exec(root, "wg genkey | wg pubkey", 10)
            if key.strip():
                s.subs.append(f"PublicKey = {m.group(3)} -> a throwaway peer key (the lab has no real peer)")
                return m.group(1) + key.strip()
        return m.group(0)
    return _KEY_PLACEHOLDER_RE.sub(fill, s.source)


def _put_file(root_client, path: str, content: str, mode: str) -> tuple[bool, bool]:
    """Write/append/prepend `content` to `path` as root. Returns
    (existed_before, ok)."""
    if path.startswith("crontab:"):
        return _put_crontab(root_client, path, content, mode)
    sftp = root_client.open_sftp()
    try:
        import stat as _stat
        try:
            if _stat.S_ISDIR(sftp.stat(path).st_mode):
                # A block aimed at a directory (found in L10: "/etc/netplan")
                # goes into a file inside it, as a person would name one.
                path = path.rstrip("/") + ("/99-lab.yaml" if "netplan" in path else "/lab.conf")
        except OSError:
            pass
        try:
            with sftp.open(path, "r") as f:
                old = f.read().decode("utf-8", "replace")
            existed = True
        except OSError:
            old, existed = "", False
        first = next((ln for ln in content.splitlines() if ln.strip() and not ln.strip().startswith("#")), "")
        sm = _INI_SECTION_RE.match(first)
        if mode == "append" and sm and any(_INI_SECTION_RE.match(ln) and _INI_SECTION_RE.match(ln).group(1).strip()
                                           == sm.group(1).strip() for ln in old.splitlines()):
            # The section already exists: a person with the file open edits
            # it rather than adding a duplicate (found in L10: fail2ban
            # refused a jail.local with two [sshd] sections).
            mode = "edit"
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
        if s.target and s.target not in paths and not s.target.startswith("crontab:"):
            paths.append(s.target)
        if s.kind == "run":
            for p in _PATH_RE.findall(s.source):
                if p not in paths:
                    paths.append(p)
    return pkgs, services, paths


# -- L16/L17: the setup stage and placeholders by kind ---------------------------------
#
# F-200 class A: a question often presumes things a fresh machine doesn't
# have -- user bob, nginx already installed, /srv/reports, a server to
# connect to. Class C: answers leave placeholders (ZOMBIE_PID,
# server_ip_address, your-uuid-here) that ran literally. The fixed rules in
# _SUBSTITUTIONS and _prepare_needs only knew the cases already seen, so
# they didn't generalise. Here the model reads the question and answer once
# (while the lab VM boots) and says what is presumed and which tokens are
# placeholders; the lab acts only through a fixed menu, never running
# anything the model wrote, and never sets up what the answer itself is
# meant to do.

_SETUP_ACTIONS = ("user", "group", "package", "service", "dir", "file")
_PLACEHOLDER_KINDS = ("user", "group", "this_machine_ip", "other_machine_ip", "uuid", "pid", "service",
                      "package", "path", "domain")
_NAME_OK = re.compile(r"^[a-z_][a-z0-9_.+-]{0,62}$")
_PATH_OK = re.compile(r"^/(?:[\w.@+-]+/)*[\w.@+-]+/?$")
_PATH_NEVER = re.compile(r"^/(?:proc|sys|dev|boot|run|usr|bin|sbin|lib\w*|snap)(?:/|$)|^/etc/?$|^/root/?$|/\.\.")
# A token only counts as a placeholder if it looks like one: the model
# sometimes calls a real name (nginx, eth0) a placeholder.
_PLACEHOLDER_SHAPE = re.compile(
    r"^[<$\[{]|your|_here\b|^[A-Z][A-Z0-9]*_[A-Z0-9_]+$|^/path/to/|^(?:a\.b\.c\.d|x\.x\.x\.x|1\.2\.3\.4)$|"
    r"(?:^|[_-])(?:name|ip|addr|address|id|path|dir|file|pid|uuid|user|host|hostname|domain|server)$|"
    r"^(?:user|username|hostname|server|ip|ipaddress|ip_address|domain|example\.com|uuid)$", re.IGNORECASE)
_PUBLIC_DOMAIN_STANDIN = "lab.example"
_PACKAGE_STANDIN = "tree"
_SERVICE_STANDIN = "labapp"


_USERADD_ARG_OPTS = {"-s", "--shell", "-d", "--home", "--home-dir", "-g", "--gid", "-G", "--groups", "-c",
                     "--comment", "--gecos", "-u", "--uid", "-e", "--expiredate", "-k", "--skel", "-p",
                     "--password", "--ingroup", "-f", "--inactive"}


def _plain_args(text: str, arg_opts: set[str]) -> list[str]:
    """Positional arguments of a command, skipping options and their values."""
    try:
        words = shlex.split(text)
    except ValueError:
        words = text.split()
    out, skip = [], False
    for w in words:
        if skip:
            skip = False
        elif w in arg_opts:
            skip = True
        elif not w.startswith("-"):
            out.append(w)
    return out


def _creates(answer: str) -> dict[str, set[str]]:
    """What the answer itself creates or installs, so setup never does it."""
    made: dict[str, set[str]] = {k: set() for k in ("user", "group", "package", "path", "member")}
    for m in re.finditer(r"\b(useradd|adduser)\b([^\n;&|]*)", answer):
        words = _plain_args(m.group(2), _USERADD_ARG_OPTS)
        if m.group(1) == "adduser" and len(words) == 2:  # adduser USER GROUP adds a member
            made["member"].add(f"{words[0]}:{words[1]}")
        elif words:
            made["user"].add(words[-1])
    for m in re.finditer(r"\b(?:groupadd|addgroup)\b([^\n;&|]*)", answer):
        made["group"].update(w for w in m.group(1).split() if not w.startswith("-"))
    for m in re.finditer(r"\busermod\s+[^\n;&|]*-a?G\s+(\S+)\s+(\S+)", answer):
        for g in m.group(1).split(","):
            made["member"].add(f"{m.group(2)}:{g}")
    for m in re.finditer(r"\bgpasswd\s+-a\s+(\S+)\s+(\S+)", answer):
        made["member"].add(f"{m.group(1)}:{m.group(2)}")
    for m in re.finditer(r"\b(?:apt-get|apt|snap|dnf|yum)\s+(?:-\S+\s+)*install\b([^\n;&|]*)", answer):
        made["package"].update(w for w in m.group(1).split() if not w.startswith("-"))
    for m in re.finditer(r"\b(?:mkdir|touch|install\s+-d)\b([^\n;&|]*)", answer):
        made["path"].update(w.rstrip("/") for w in m.group(1).split() if w.startswith("/"))
    for m in re.finditer(r"(?:>>?|\btee(?:\s+-a)?)\s+(/[\w./@+-]+)", answer):
        made["path"].add(m.group(1).rstrip("/"))
    return made


def _asked_to_create(question: str, name: str) -> bool:
    """The question itself asks for this to be created or installed."""
    q, n = question.lower(), re.escape(name.lower())
    return bool(re.search(rf"\b(?:create|add|set up|setup)\s+(?:a\s+|an\s+)?(?:new\s+)?"
                          rf"(?:user|group|account|directory|folder|file)?\s*(?:called\s+|named\s+)?['\"]?{n}\b", q)
                or re.search(rf"\binstall\s+(?:and\s+\w+\s+)?(?:the\s+)?{n}\b", q))


def plan_setup(raw: dict, question: str, answer: str) -> tuple[list[dict], list[dict], list[str]]:
    """Validate the model's reply against the menu. Returns (setup actions,
    placeholders, what was dropped and why)."""
    setup, placeholders, dropped = [], [], []
    made = _creates(answer)
    for item in (raw.get("setup") or [])[:12]:
        if not isinstance(item, dict) or item.get("action") not in _SETUP_ACTIONS:
            dropped.append(f"{item!r}: not a setup action the lab has")
            continue
        a = item["action"]
        name = str(item.get("path" if a in ("dir", "file") else "name") or "").strip()
        ok = _PATH_OK.match(name) and not _PATH_NEVER.search(name) if a in ("dir", "file") else _NAME_OK.match(name)
        if not ok:
            dropped.append(f"{a} {name!r}: not a safe name")
            continue
        if (a in made and name in made[a]) or (a in ("dir", "file") and name.rstrip("/") in made["path"]):
            dropped.append(f"{a} {name}: the answer creates it")
            continue
        if _asked_to_create(question, name.rsplit("/", 1)[-1]):
            dropped.append(f"{a} {name}: the question asks for it")
            continue
        # F-200 #26/#38: the model sometimes lists a placeholder ("username",
        # "your_group") as a real user or group; it is the lab's own.
        if a == "user" and _PLACEHOLDER_SHAPE.search(name):
            name = LAB_USER
        if a == "group" and _PLACEHOLDER_SHAPE.search(name):
            name = LAB_GROUP
        clean = {"action": a, ("path" if a in ("dir", "file") else "name"): name.rstrip("/") if a != "dir" else name}
        if a == "group":
            members = [LAB_USER if _PLACEHOLDER_SHAPE.search(str(m)) else str(m) for m in (item.get("members") or [])]
            members = [m for m in members if _NAME_OK.match(m)]
            members = [m for m in members if f"{m}:{name}" not in made["member"]]
            clean["members"] = members[:5]
        if a in ("package", "service"):
            clean["running"] = bool(item.get("running", a == "service"))
        if a in ("dir", "file") and item.get("owner") and _NAME_OK.match(str(item["owner"])):
            clean["owner"] = str(item["owner"])
        setup.append(clean)
    for item in (raw.get("placeholders") or [])[:12]:
        if not isinstance(item, dict):
            continue
        token, kind = str(item.get("token") or "").strip(), item.get("kind")
        if kind not in _PLACEHOLDER_KINDS or len(token) < 3 or token not in answer:
            continue
        if not _PLACEHOLDER_SHAPE.search(token.strip("<>{}[]$")) and not _PLACEHOLDER_SHAPE.search(token):
            dropped.append(f"placeholder {token!r}: doesn't look like one")
            continue
        placeholders.append({"token": token, "kind": kind})
    return setup, placeholders, dropped


def apply_setup(root_client, setup: list[dict], say) -> list[str]:
    """Carry out validated setup actions. Returns what was done, in words."""
    done = []
    order = {"package": 0, "user": 1, "group": 2, "service": 3, "dir": 4, "file": 5}
    for item in sorted(setup, key=lambda i: order[i["action"]]):
        a = item["action"]
        if a == "user":
            n = item["name"]
            code, _, _ = _exec(root_client, f"id {n} >/dev/null 2>&1 || useradd -m -s /bin/bash {n} && "
                                            f"echo '{n}:{_lab_password}' | chpasswd", 30)
            if code == 0:
                done.append(f"user {n}")
        elif a == "group":
            n = item["name"]
            cmd = f"getent group {n} >/dev/null || groupadd {n}"
            for m in item.get("members", []):
                cmd += f"; id {m} >/dev/null 2>&1 || useradd -m -s /bin/bash {m}; usermod -aG {n} {m}"
            code, _, _ = _exec(root_client, cmd, 30)
            if code == 0:
                done.append(f"group {n}" + (f" with {', '.join(item['members'])} in it" if item.get("members") else ""))
        elif a == "package":
            n = item["name"]
            say(f"# setup: installing {n} (the question assumes it is installed)\n", now=True)
            code, _, _ = _exec(root_client, f"DEBIAN_FRONTEND=noninteractive apt-get install -y -q {n} >/tmp/setup-{n}.log 2>&1", 300)
            if code == 0:
                if item.get("running"):
                    _exec(root_client, f"systemctl enable --now {n} >/dev/null 2>&1 || true", 60)
                done.append(f"{n} installed" + (" and running" if item.get("running") else ""))
            else:
                say(f"# setup: couldn't install {n}; the answer runs without it\n")
        elif a == "service":
            n = item["name"]
            code, _, _ = _exec(root_client, f"systemctl cat {n} >/dev/null 2>&1", 15)
            if code != 0:
                unit = (f"[Unit]\nDescription=Stand-in service created by the lab\n[Service]\n"
                        f"ExecStart=/bin/sleep infinity\n[Install]\nWantedBy=multi-user.target\n")
                _exec(root_client, f"printf %s {shlex.quote(unit)} > /etc/systemd/system/{n}.service && "
                                   "systemctl daemon-reload", 30)
            if item.get("running"):
                _exec(root_client, f"systemctl enable --now {n} >/dev/null 2>&1 || true", 60)
            done.append(f"service {n}" + (" (a stand-in)" if code != 0 else "") + (" running" if item.get("running") else ""))
        elif a in ("dir", "file"):
            path = item["path"]
            q = shlex.quote(path)
            cmd = (f"mkdir -p {q}" if a == "dir" else
                   f"mkdir -p $(dirname {q}) && {{ test -e {q} || echo 'stand-in file created by the lab' > {q}; }}")
            if item.get("owner"):
                cmd += f" && chown -R {item['owner']}: {q}"
            code, _, _ = _exec(root_client, cmd, 30)
            if code == 0:
                done.append(f"{'directory' if a == 'dir' else 'file'} {path}")
    return done


def lab_facts(root_client, kinds: set[str], target_ip: str, other_ip: str) -> dict[str, str]:
    """Values for placeholder kinds, from the lab itself."""
    facts = {"user": LAB_USER, "group": LAB_GROUP, "this_machine_ip": target_ip, "domain": _PUBLIC_DOMAIN_STANDIN,
             "package": _PACKAGE_STANDIN, "service": _SERVICE_STANDIN, "path": LAB_DIR + "/example.txt"}
    if other_ip:
        facts["other_machine_ip"] = other_ip
    if "user" in kinds:
        _exec(root_client, f"id {LAB_USER} >/dev/null 2>&1 || useradd -m -s /bin/bash {LAB_USER}; "
                           f"echo '{LAB_USER}:{_lab_password}' | chpasswd", 30)
    if "group" in kinds:
        _exec(root_client, f"getent group {LAB_GROUP} >/dev/null || groupadd {LAB_GROUP}", 15)
    if "path" in kinds:
        _exec(root_client, f"mkdir -p {LAB_DIR} && echo 'stand-in file created by the lab' > {LAB_DIR}/example.txt", 15)
    if "service" in kinds:
        apply_setup(root_client, [{"action": "service", "name": _SERVICE_STANDIN, "running": True}], lambda *a, **k: None)
    if "pid" in kinds:
        # The student's own process: the answer's kill runs as the student (F-200 #33).
        _, out, _ = _exec(root_client, "su student -c 'nohup sleep 3600 >/dev/null 2>&1 & echo $!'", 15)
        if out.strip().isdigit():
            facts["pid"] = out.strip()
    if "uuid" in kinds:
        # The spare disk (L13), with one ext4 partition, as the answer's disk.
        _exec(root_client, "test -e /dev/sdb1 || { echo ',,L' | sfdisk -q /dev/sdb; udevadm settle; }; "
                           "blkid -s TYPE -o value /dev/sdb1 | grep -q . || mkfs.ext4 -q /dev/sdb1", 90)
        _, out, _ = _exec(root_client, "blkid -s UUID -o value /dev/sdb1", 15)
        if re.fullmatch(r"[0-9a-fA-F-]{8,}", out.strip()):
            facts["uuid"] = out.strip()
    return facts


def fill_placeholders(answer: str, placeholders: list[dict], facts: dict[str, str]) -> tuple[str, list[str], list[str]]:
    """(answer with placeholders filled, what changed, what couldn't be)."""
    changes, unfilled = [], []
    for p in sorted(placeholders, key=lambda p: -len(p["token"])):  # longest first: $service vs $service_name
        value = facts.get(p["token"]) or facts.get(p["kind"], "")
        if not value:
            unfilled.append(f"{p['token']} ({p['kind'].replace('_', ' ')})")
            continue
        answer = answer.replace(p["token"], value)
        changes.append(f"{p['token']} -> {value} ({p['kind'].replace('_', ' ')})")
    return answer, changes, unfilled


PRESUME_TIMEOUT_S = 240


def _setup_stage(result, root, presumed: dict, question: str, answer: str, other_ip: str, say):
    """L16/L17: set up what the question presumes and fill placeholders.
    Returns the answer as the lab will follow it, and its steps."""
    if not presumed or presumed.get("error"):
        result.setup = {"note": "no reading from the model" + (f": {presumed['error']}" if presumed.get("error") else "")}
        return answer, parse_steps(answer)
    setup, placeholders, dropped = plan_setup(presumed, question, answer)
    done = apply_setup(root, setup, say) if setup else []
    facts = lab_facts(root, {p["kind"] for p in placeholders}, TARGET_PAIR_IP, other_ip) if placeholders else {}
    for p in placeholders:  # /path/to/<name>: the lab's directory, keeping the answer's name for it
        if p["kind"] == "path" and p["token"].startswith("/path/to/"):
            facts[p["token"]] = LAB_DIR + "/" + p["token"].rstrip("/").rsplit("/", 1)[-1]
    filled, changes, unfilled = fill_placeholders(answer, placeholders, facts)
    result.setup = {"done": done, "placeholders": changes, "unfilled": unfilled, "refused": dropped}
    if done:
        say(f"# setup: the lab created {', '.join(done)} -- the question assumes they already exist\n", now=True)
    if changes:
        say(f"# setup: placeholders filled from this lab: {'; '.join(changes)}\n")
    if unfilled:
        say(f"# setup: placeholders the lab has no value for (left as written): {', '.join(unfilled)}\n")
    steps = parse_steps(filled)
    result.steps = [asdict(s) for s in steps]
    return filled, steps


def run_advice(answer: str, make_vm, progress=None, run_id: str = "", question: str = "",
               make_prober=None, pair_bridges=None, keep_vm=None, repair: bool = True,
               model_fix=None, presume=None) -> RunResult:
    """Run `answer` in a VM from make_vm(**kw) (an un-booted microvm.MicroVM;
    kw may carry pair_bridge and scratch_from). With make_prober(bridge)
    and pair_bridges=(create, delete), goal probes run from a second VM at
    the end (L8). `progress(result)` is called after each step, and at most
    once a second while output streams. keep_vm(vm, info) -> bool, if given,
    is offered the target VM at the end; when it returns True the VM is not
    torn down (the caller now owns it). presume(question, answer) -> dict, if
    given, is the model's reading of what the question presumes (L16/L17);
    it runs while the VM boots."""
    result = RunResult(id=run_id or uuid.uuid4().hex[:12], started_at=time.time())
    global TARGET_PAIR_IP
    TARGET_PAIR_IP = _MICROVM_TARGET_IP
    steps = parse_steps(answer)
    result.steps = [asdict(s) for s in steps]
    publish = progress or (lambda r: None)
    last_pub = [0.0]

    def say(text: str, now: bool = False) -> None:
        result.transcript = (result.transcript + text)[-_TRANSCRIPT_KEEP:]
        if now or time.monotonic() - last_pub[0] > 1.0:
            last_pub[0] = time.monotonic()
            publish(result)
    limit = question_limit(question)
    if limit:
        result.status, result.verdict = "done", "not_testable"
        result.summary = (f"Can't be tested in this lab: {limit}. The answer can only be proven on a real machine; "
                          "the lab didn't try it.")
        result.finished_at = time.time()
        publish(result)
        return result
    if not any(s.kind in ("run", "write", "append", "prepend", "edit", "prose") for s in steps):
        result.status, result.verdict = "done", "not_runnable"
        result.summary = "Nothing in this answer could be run as a step."
        result.finished_at = time.time()
        publish(result)
        return result

    pair = pair_bridges[0]() if (make_prober and pair_bridges) else ""
    vm = make_vm(pair_bridge=pair) if pair else make_vm()
    prober = None
    student = root = prober_root = None
    deadline = time.monotonic() + RUN_TIMEOUT_S

    full_vm = bool(getattr(vm, "reboots_in_place", False))
    global _full_vm_run
    _full_vm_run = full_vm

    def connect():
        nonlocal root, student
        root = vm.ssh_client("root")
        student = vm.ssh_client("student")
        if pair and not full_vm:
            _exec(root, f"ip link set eth1 up && ip addr replace {TARGET_PAIR_IP}/24 dev eth1", 15)

    kept = False
    login_prober = None
    budget = {"run": REPAIRS_PER_RUN, "model": MODEL_FIXES_PER_RUN}
    global _lab_password, _unit_scripts
    _lab_password = "Lab-" + secrets.token_hex(6)
    _unit_scripts = {p for line in answer.splitlines() if "ExecStart" in line
                     for p in _STUB_SCRIPT_RE.findall(_substitute(line)[0])}
    presumed: dict = {}
    presume_thread = None
    if presume:
        def _ask() -> None:
            try:
                presumed.update(presume(question, answer) or {})
            except Exception as e:  # noqa: BLE001 -- setup is an aid; the run goes on without it
                presumed["error"] = repr(e)[:200]
        presume_thread = threading.Thread(target=_ask, daemon=True)
        presume_thread.start()
    try:
        say("# booting a fresh Ubuntu 22.04 lab machine...\n", now=True)
        t_boot = time.monotonic()
        vm.boot()
        result.vm = {"vcpus": vm.vcpu_count, "mem_mib": vm.mem_size_mib, "scratch_mib": vm.scratch_mib}
        if full_vm and vm.target_addr:
            # F4: the prober reaches a full VM at its lab address, known only
            # now; re-apply placeholder substitutions (server_ip -> target).
            TARGET_PAIR_IP = vm.target_addr
            steps = parse_steps(answer)
            result.steps = [asdict(s) for s in steps]
            result.vm["kind"] = "full"
        connect()
        say(f"# ready in {time.monotonic() - t_boot:.0f}s: {vm.vcpu_count} vCPU, {vm.mem_size_mib}MB RAM, "
            f"{vm.scratch_mib // 1024}GB disk\n# lab setup: apt and debconf take the defaults, as a person "
            "following the answer would; modprobe succeeds for modules built into the lab's kernel, as it "
            "would on a real machine\n")
        _exec(root, _HARNESS_SETUP, 60)
        global _target_clock_offset
        t_a = time.time()
        _, guest_now, _ = _exec(root, "date +%s.%N", 15)
        try:
            _target_clock_offset = float(guest_now.strip()) - (t_a + time.time()) / 2
        except ValueError:
            _target_clock_offset = 0.0
        say(f"# lab machine clock: {_target_clock_offset:+.1f}s from the lab's (codes follow the machine's clock)\n")
        baseline = {}
        pam = _pam_services_touched(steps)
        sshd_changed = "sshd" in pam or any("sshd_config" in (s.target or "") or
                                            (s.kind == "run" and "sshd_config" in s.source) for s in steps)
        auth_related = bool(pam) or sshd_changed or bool(_MFA_RE.search(question + "\n" + answer))
        if make_prober and pair:
            say("# booting the prober: a second machine on a private link, to test the result from outside\n")
            prober = make_prober(pair)
            prober.boot()
            prober_root = prober.ssh_client("root")
            sftp = prober_root.open_sftp()
            with sftp.open("/tmp/probe_ssh.py", "w") as f:
                f.write(_PROBER_SSH)
            sftp.close()
            if auth_related:
                services = pam - {"sshd", "common-password", "common-session", "common-account"}
                if "common-auth" in services:
                    services = (services - {"common-auth"}) | {"login", "su"}
                login_prober = _LoginProber(root, prober_root, sorted(services))
                login_prober.prepare()
                baseline["ssh"] = login_prober.ssh()
                say(f"# baseline before the answer -- SSH login as student from the prober: "
                    f"{'works' if baseline['ssh'].get('ok') else 'fails'}\n")
                for svc in login_prober.services:
                    baseline[f"pam:{svc}"] = login_prober.pam(svc)
                    say(f"# baseline -- '{svc}' login (PAM): "
                        f"{'works' if baseline[f'pam:{svc}']['ok'] else 'fails'}\n")
        if presume_thread is not None:
            presume_thread.join(timeout=PRESUME_TIMEOUT_S)
            answer, steps = _setup_stage(result, root, presumed, question, answer,
                                         (prober.ip or "") if prober is not None else "", say)
            _unit_scripts = {p for line in answer.splitlines() if "ExecStart" in line
                             for p in _STUB_SCRIPT_RE.findall(_substitute(line)[0])}
        say("# now following the answer, step by step\n", now=True)

        for s in steps:
            if time.monotonic() > deadline:
                s.cls, s.detail = "timeout", "the whole run hit its time limit before this step"
                continue
            t0 = time.monotonic()
            if s.kind in ("run", "write", "append", "prepend", "edit", "prose") and (s.needs or _STUB_SCRIPT_RE.search(s.source)):
                made = _prepare_needs(root, s)
                if made:
                    s.note = (s.note + "; " if s.note else "") + "the lab created " + ", ".join(made)
                    say(f"# the lab created {', '.join(made)} (the answer assumes they exist)\n")
            if s.kind == "run":
                say("\n$ " + s.source.replace("\n", "\n> ") + "\n", now=True)
            elif s.kind in ("write", "append", "prepend", "edit"):
                how = s.note.partition("|")[0]
                say(f"\n# {s.kind} {s.target} ({how}):\n" + textwrap.indent(s.source, "  ") + "\n", now=True)
            elif s.kind == "prose":
                say(f"\n# edit {s.target} as the text describes: "
                    + "; ".join(f"{op} {a!r}" + (f" -> {b!r}" if b else "") for op, a, b in s.edit_ops) + "\n", now=True)
            else:
                say(f"\n# skipped: {s.source.splitlines()[0][:100]} -- {s.note}\n", now=True)
            if s.kind == "run" and _REBOOT_RE.search(s.source):
                # A real reboot: the guest shuts down cleanly (reboot=k ends
                # the VMM), then a new VM boots from the same disk.
                try:
                    _exec_step(student, s.source, 60)[0]
                except (OSError, EOFError):
                    pass  # the connection going away is the point
                say("# the machine is shutting down to reboot...\n", now=True)
                if full_vm:
                    # A full VM reboots in place: wait for it, then reconnect.
                    if vm.wait_exit(90):
                        connect()
                        s.exit, s.cls = 0, "ok"
                        s.note = f"rebooted: the machine came back in {time.monotonic() - t0:.0f}s"
                        say(f"# back up after the reboot ({time.monotonic() - t0:.0f}s)\n", now=True)
                    else:
                        s.exit, s.cls, s.detail = 1, "step_failed", "the machine did not come back after the reboot"
                elif vm.wait_exit(90):
                    saved = vm.jail_dir + ".scratch"
                    vm.take_scratch(saved)
                    vm.teardown()
                    kw = {"scratch_from": saved}
                    if pair:
                        kw["pair_bridge"] = pair
                    vm = make_vm(**kw)
                    vm.boot()
                    connect()
                    s.exit, s.cls = 0, "ok"
                    s.note = f"rebooted: the sandbox restarted on the same disk in {time.monotonic() - t0:.0f}s"
                    say(f"# back up after the reboot ({time.monotonic() - t0:.0f}s), same disk\n", now=True)
                else:
                    s.exit, s.cls, s.detail = 1, "step_failed", "the machine did not restart"
            elif s.kind == "run":
                step_log = _StepLog(say)
                code, out, timed_out, replies, marks, fullscreen = _exec_step(
                    student, s.source, STEP_TIMEOUT_S, on_output=step_log)
                step_log.close()
                s.exit, s.output = code, out[-_OUTPUT_KEEP:]
                s.cls, s.detail, benign_note = _judge(code, out, timed_out, marks, fullscreen)
                if benign_note:
                    s.note = (s.note + "; " if s.note else "") + benign_note
                s.attempts.append({"by": "answer", "exit": code, "cls": s.cls,
                                   "failed": [m for m in marks if not _benign(m[1], m[0])][:3]})
                limit = _lab_limit(s) if s.cls != "ok" else ""
                if limit:
                    s.cls, s.detail = "lab_limit", f"can't be tested in this lab -- {limit} ({s.detail or s.cls})"
                    say(f"# can't be tested here: {limit}\n")
                elif s.cls != "ok":
                    say(f"# {s.cls.replace('_', ' ')}: {s.detail}\n")
                    if repair:
                        _repair_step(s, student, root, say, budget, model_fix, question)
                if replies:
                    shown = ["the code from the new secret" if r.isdigit() and len(r) == 6 else f"'{r}'"
                             for r in replies[:8]]
                    s.note = "answered its questions: " + ", ".join(shown) + (" ..." if len(replies) > 8 else "")
            elif s.kind == "prose":
                ok, what = _apply_prose(root, s)
                s.exit, s.cls = (0, "ok") if ok else (1, "file_missing" if "doesn't exist" in what else "step_failed")
                s.detail = "" if ok else what
                s.note = what if ok else s.note
                say(f"# {what}\n")
            elif s.kind in ("write", "append", "prepend", "edit"):
                how, _, flag = s.note.partition("|")
                s.note = how
                if s.target == "/etc/fstab":
                    s.source = _fill_fstab_uuids(root, s, steps)
                if s.target.startswith("/etc/wireguard/"):
                    s.source = _fill_wg_keys(root, s, steps)
                existed, ok = _put_file(root, s.target, s.source, s.kind)
                say("# written\n" if ok else f"# could not write {s.target}\n")
                s.exit = 0 if ok else 1
                if not ok:
                    s.cls, s.detail = "step_failed", f"could not write {s.target}"
                elif flag == "expects-existing" and not existed and _lab_limit(s):
                    s.cls = "lab_limit"
                    s.detail = f"can't be tested in this lab -- {_lab_limit(s)} ({s.target} doesn't exist here)"
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

        say("\n# checking the result...\n", now=True)
        # Whole-run checks, as root so a step that broke sudo can't hide them.
        pkgs, services, paths = _collect_facts(steps)
        services_started = services
        checks = []
        for p in pkgs:
            code, out, _ = _exec(root, f"dpkg-query -W -f='${{Status}}' {shlex.quote(p)}", 30)
            checks.append({"kind": "package", "subject": p, "ok": "install ok installed" in out,
                           "detail": out.strip()[:200]})
        started = set()
        for s in steps:
            if s.kind == "run" and s.cls == "ok":
                started |= set(re.findall(r"\bsystemctl\s+(?:start|restart|reload)\s+([\w@.-]+)", s.source))
                started |= set(re.findall(r"\bsystemctl\s+enable\s+--now\s+([\w@.-]+)", s.source))
        for svc in services:
            # `enable` alone means "at the next boot" (found in L10: a unit
            # the answer only enabled was failed for not running yet).
            want = "active" if svc in started else "enabled"
            verb = "is-enabled" if want == "enabled" else "is-active"
            code, out, _ = _exec(root, f"systemctl {verb} {shlex.quote(svc)}", 30)
            state = out.strip()
            checks.append({"kind": "service", "subject": svc + ("" if want == "active" else " (enabled for boot)"),
                           "ok": state == want, "detail": "" if state == want else state[:200]})
        for s in steps:
            # A crontab edit: is the answer's line really in that crontab?
            if s.target.startswith("crontab:") and s.cls == "ok":
                user = s.target.split(":", 1)[1]
                _, tab, _ = _exec(root, f"crontab -u {shlex.quote(user)} -l 2>/dev/null", 15)
                want = [ln.strip() for ln in s.source.splitlines() if ln.strip() and not ln.strip().startswith("#")]
                checks.append({"kind": "cron", "subject": f"{user}'s crontab has the answer's entry",
                               "ok": bool(want) and all(w in tab for w in want),
                               "detail": "" if want and all(w in tab for w in want) else tab.strip()[-200:]})
        _explain_missing_packages(steps, checks, root)
        _add_corrections(steps, root)
        checks += _sshd_effective_checks(steps, root)
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
        if prober_root is not None:
            if not full_vm:
                _exec(root, f"ip link set eth1 up && ip addr replace {TARGET_PAIR_IP}/24 dev eth1", 15)
            # After a reboot the target's eth1 has a new MAC; the prober's ARP
            # entry for it would point at the old one (found live: SSH timed
            # out, a port check seconds later worked).
            _exec(prober_root, "ip neigh flush all", 10)
            say("# the prober is testing logins, ports and web servers from outside...\n", now=True)
            if login_prober is not None:
                login_prober.root = root  # a reboot replaced the connection
                checks += _login_probes(steps, login_prober, baseline, question, answer, sshd_changed)
            checks += _network_probes(steps, root, prober_root, answer, services_started, pkgs)
        say("# checking what the question asked for...\n", True)
        checks += _goal_checks(question, answer, steps, root, prober_root,
                               (prober.ip or "") if prober is not None else "")
        result.checks = checks
        result.steps = [asdict(x) for x in steps]
        _finish_verdict(result, steps, checks)
        # Every failed run (found in L11: WireGuard runs with a repaired step
        # got none, and the next attempt was written blind).
        if result.verdict == "failed":
            result.diagnosis = _diagnose(root, services, steps)
            if result.diagnosis:
                say("\n# what the machine says about it:\n" + result.diagnosis + "\n")
        repaired = [s for s in steps if s.cls == "repaired"]
        if repaired:
            twin = RunResult(id=result.id, started_at=result.started_at)
            as_fixed = [Step(**{**asdict(s), "cls": "ok" if s.cls == "repaired" else s.cls}) for s in steps]
            _finish_verdict(twin, as_fixed, checks)
            result.repaired = {
                "verdict": twin.verdict,
                "summary": twin.summary,
                "changes": [f"step {s.n}: {s.repair}" for s in repaired],
                "risky": any("[risky]" in s.repair for s in repaired),
                "procedure": _procedure(steps),
            }
            say(f"\n# after the lab's repairs: {twin.verdict} -- {'; '.join(result.repaired['changes'])}\n")
        for c in checks:
            mark = "i" if c.get("decisive") is False else ("ok " if c["ok"] else "FAIL")
            say(f"# [{mark}] {c['kind']}: {c['subject']}" + (f" -- {c['detail'][:160]}" if c["detail"] else "") + "\n")
        say(f"\n# verdict: {result.verdict} -- {result.summary}\n", now=True)
        if keep_vm is not None:
            info = {"user": "student", "pam_services": list(login_prober.services) if login_prober else [],
                    "sshd_changed": sshd_changed,
                    "password": login_prober.pw if login_prober else "",
                    "totp_secret": login_prober.secret if login_prober else ""}
            kept = bool(keep_vm(vm, info))
    except Exception as e:  # noqa: BLE001 -- any failure is reported as the run's error
        result.status, result.error = "error", f"{type(e).__name__}: {e}"
        result.verdict = result.verdict or "partial"
        result.summary = result.summary or "The lab run could not finish."
    finally:
        for c in (student, root, prober_root):
            try:
                if c is not None:
                    c.close()
            except Exception:  # noqa: BLE001, S110 -- closing a dead session is not news
                pass
        for machine in (prober,) if kept else (vm, prober):
            try:
                if machine is not None:
                    machine.teardown()
            except Exception as e:  # noqa: BLE001 -- the run's result stands; say so and move on
                result.error = result.error or f"teardown failed: {e}"
        if pair:
            pair_bridges[1](pair)
        result.finished_at = time.time()
        if result.status == "running":
            result.status = "done"
        publish(result)
    return result


# Commands that change the system (found in L10: `hostnamectl set-hostname`
# was graded "nothing changed").
_STATE_CHANGE_RE = re.compile(
    r"\b(?:hostnamectl\s+set-|timedatectl\s+set-|useradd|usermod|userdel|adduser|deluser|groupadd|gpasswd|passwd|"
    r"chmod|chown|chgrp|setfacl|ln\s+-s|mkdir|touch|tee|sed\s+-i|sysctl\s+-w|ufw\s+(?:allow|deny|enable|default|limit)|"
    r"iptables\s+-[AIDPt]|nft\s+add|crontab|mount|swapon|mkfs|mdadm\s+--create|pvcreate|vgcreate|lvcreate|"
    r"tar\s+-?[a-z]*x|unzip|git\s+clone|pip3?\s+install|npm\s+install|update-alternatives|locale-gen|"
    r"dpkg-reconfigure|netplan\s+apply|ip\s+(?:addr|address|route)\s+add|openssl\s+req|ssh-keygen|wg\s+genkey)\b")


# Validators that explain *why* a service misbehaves, by what the answer used.
_DIAGNOSTICS = [
    (re.compile(r"\bbind9?\b|\bnamed\b"), "named-checkconf -z 2>&1 | grep -v ': loaded serial' | tail -12"),
    (re.compile(r"\bnginx\b"), "nginx -t 2>&1 | tail -6"),
    (re.compile(r"\bapache2?\b"), "apache2ctl configtest 2>&1 | tail -6"),
    (re.compile(r"\bsshd?\b"), "sshd -t 2>&1 | tail -6"),
    (re.compile(r"\bnetplan\b"), "netplan get 2>&1 | tail -12"),
]


def _diagnose(root, services, steps: list[Step], limit: int = 2400) -> str:
    """Journals of the services the answer touched and the matching
    validators' output -- what a person would look at next."""
    text = "\n".join(s.source + "\n" + (s.target or "") for s in steps)
    parts = []
    for rx, cmd in _DIAGNOSTICS:
        if rx.search(text):
            _, out, _ = _exec(root, cmd, 30)
            if out.strip():
                parts.append(f"$ {cmd.split(' 2>&1')[0]}\n{out.strip()}")
    for svc in sorted(services)[:4]:
        _, out, _ = _exec(root, f"journalctl -u {shlex.quote(svc)} -n 12 --no-pager -o cat 2>/dev/null", 30)
        if out.strip() and "-- No entries --" not in out:
            parts.append(f"$ journalctl -u {svc} (last lines)\n{out.strip()}")
    return "\n\n".join(parts)[:limit]


def _finish_verdict(result: RunResult, steps: list[Step], checks: list[dict]) -> None:
    acted = [s for s in steps if s.kind in ("run", "write", "append", "prepend", "edit", "prose")]
    limits = [s for s in acted if s.cls == "lab_limit"]
    bad = [s for s in acted if s.cls not in ("ok", "lab_limit")]
    failed_checks = [c for c in checks if not c["ok"] and c.get("decisive", True)]
    changed = any(s.kind in ("write", "append", "prepend", "edit", "prose") or _APT_INSTALL_RE.search(s.source)
                  or _SERVICE_RE.search(s.source) or _STATE_CHANGE_RE.search(s.source) for s in acted if s.cls == "ok")
    # L12: a check on what the answer set out to achieve.
    goal = [c for c in checks if c["ok"] and c.get("decisive", True)
            and c["kind"] in ("goal", "login", "http", "cron", "effective")]
    if limits and not bad and not failed_checks:
        whys = list(dict.fromkeys(s.detail.split(" -- ", 1)[-1].rsplit(" (", 1)[0] for s in limits))
        result.verdict = "not_testable"
        result.summary = ("Can't be tested in this lab: " + "; ".join(whys) + f". The other {len(acted) - len(limits)} "
                          "steps worked, but the answer as a whole can only be proven on a real machine.")
    elif not bad and not failed_checks and changed and goal:
        result.verdict = "goal_verified"
        result.summary = (f"All {len(acted)} steps worked in a fresh Ubuntu 22.04 sandbox, and what they were meant to "
                          f"achieve was checked: " + "; ".join(c["subject"] for c in goal[:3])
                          + ("; ..." if len(goal) > 3 else "") + ".")
    elif not bad and not failed_checks and changed:
        result.verdict = "ran_clean"
        result.summary = (f"All {len(acted)} steps worked and every check passed, but nothing tested what they were "
                          "meant to achieve, so this is not reused until someone reviews it.")
    elif not bad and not failed_checks:
        result.verdict = "partial"
        result.summary = f"All {len(acted)} steps ran, but none changed the system, so there was nothing to verify."
    else:
        result.verdict = "failed"
        parts = []
        lockouts = [c for c in failed_checks if c["kind"] == "login" and "still works" in c["subject"]]
        if lockouts:
            what = ", ".join(c["subject"].split(" (")[0].replace(" still works", "") for c in lockouts)
            parts.append(f"LOCKOUT: following this answer breaks {what}, which worked before it")
        for s in bad + limits:
            parts.append(f"step {s.n}: {s.detail or s.cls.replace('_', ' ')}"
                         + (" (the lab repaired it)" if s.cls == "repaired" else ""))
        for c in failed_checks:
            parts.append(f"{c['kind']} check failed: {c['subject']}")
        result.summary = "; ".join(parts[:6]) + ("; ..." if len(parts) > 6 else "")


def plain_step_line(step: dict) -> str:
    """One human line per step for the page and the corpus."""
    icon = {"ok": "✓", "skipped": "–", "": "·", "repaired": "↻", "lab_limit": "⊘"}.get(step["cls"], "✗")
    what = step["target"] and f"{step['kind']} {step['target']}" or step["source"].splitlines()[0][:80]
    extra = step["detail"] or step["note"]
    if step.get("repair"):
        extra = f"{extra} -- repaired: {step['repair']}" if extra else f"repaired: {step['repair']}"
    return f"{icon} {step['n']}. {what}" + (f" — {extra}" if extra else "")


def text_report(result: RunResult) -> str:
    out = io.StringIO()
    out.write(f"verdict: {result.verdict} -- {result.summary}\n")
    for s in result.steps:
        out.write(plain_step_line(s) + "\n")
    for c in result.checks:
        mark = "ℹ" if c.get("decisive") is False else ("✓" if c["ok"] else "✗")
        show = c["detail"] and (not c["ok"] or c["kind"] in ("login", "http"))
        out.write(f"{mark} {c['kind']}: {c['subject']}" + (f" -- {c['detail'][:160]}" if show else "") + "\n")
    return out.getvalue()
