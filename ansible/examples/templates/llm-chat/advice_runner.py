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
import json
import re
import secrets
import struct
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
# Example commands the student is meant to fill in ("ssh username@your_server_ip").
_PLACEHOLDER_RE = re.compile(r"\byour[_-][a-z_]+|\b(?:username|user|youruser)@|<[a-z][\w -]*>|\bexample\.com\b|"
                             r"\bYOUR_[A-Z_]+\b|\bserver_ip\b", re.IGNORECASE)


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
            placeholder = _PLACEHOLDER_RE.search(body)
            if body and placeholder:
                steps.append(Step(len(steps) + 1, "skip", body,
                                  note=f"example with a placeholder ({placeholder.group(0)}) for you to fill in; "
                                       "logins are tested from the lab's prober machine instead"))
            elif body:
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

TARGET_PAIR_IP = "172.30.0.2"
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
    sock = socket.create_connection((host, 22), timeout=8)
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


def totp(secret_b32: str, at: float | None = None, step: int = 30, digits: int = 6) -> str:
    """RFC 6238 code for a base32 secret (google-authenticator's first line)."""
    key = base64.b32decode(secret_b32.strip().upper() + "=" * (-len(secret_b32.strip()) % 8))
    counter = int((time.time() if at is None else at) // step)
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
        self.pw = "Lab-" + secrets.token_hex(6)
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
        return totp(self.secret, time.time() + 30 * ((self.attempts % 3) - 1))

    def _pace(self) -> None:
        # pam_google_authenticator's default rate limit is 3 logins per 30s.
        self.attempts += 1
        if self.secret and self.attempts % 3 == 0:
            time.sleep(31)

    def ssh(self, wrong: bool = False, key: bool = True) -> dict:
        _, out, _ = _exec(self.prober_root, f"python3 /tmp/probe_ssh.py {TARGET_PAIR_IP} student "
                                            f"{shlex.quote(self.pw)} {self._code(wrong)} "
                                            f"{'/tmp/probe_key' if key else '-'}", 60)
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


def _suggest_package(root, name: str) -> str:
    """The real package for a name the answer got wrong, from the sandbox's
    own data: Ubuntu's command-not-found database for a command, else a
    package whose name contains it (google-authenticator ->
    libpam-google-authenticator). "" when there's no clear answer."""
    _, out, _ = _exec(root, f"/usr/lib/command-not-found --ignore-installed {shlex.quote(name)} 2>&1", 20)
    m = re.search(r"sudo apt install ([a-z0-9][a-z0-9+.-]+)", out)
    if m:
        return m.group(1)
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
        right = _suggest_package(root, name)
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
    # Everything that happened on the lab machine, as a read-only terminal
    # log for the page (tail kept); and, once the run ends, how to reach the
    # machine if the caller kept it.
    transcript: str = ""
    kept: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)


STEP_TIMEOUT_S = 300
RUN_TIMEOUT_S = 1200
_OUTPUT_KEEP = 4000
_TRANSCRIPT_KEEP = 150_000

_HARNESS_SETUP = r"""set -e
# The runner answers apt's and debconf's questions the way a person
# following the answer would (yes / defaults); nothing else is changed.
printf 'APT::Get::Assume-Yes "true";\n' > /etc/apt/apt.conf.d/99lab-assume-yes
echo 'debconf debconf/frontend select Noninteractive' | debconf-set-selections
"""


_REBOOT_RE = re.compile(r"\b(?:reboot|shutdown\s+(?:-\S+\s+)*-r|systemctl\s+reboot|init\s+6)\b")


_PROMPT_TAIL_RE = re.compile(r"(?:[?:>]|\(y/n\)|\[y/n\]|\[Y/n\]|\[y/N\])\s*$", re.IGNORECASE)
_YES_NO_RE = re.compile(r"\(y/n\)|\[y/n\]|\[Y/n\]|\[y/N\]|\byes/no\b", re.IGNORECASE)
_NEW_SECRET_RE = re.compile(r"secret key is:?\s*([A-Z2-7]{16,})")


def _reply_for(prompt: str, output: str) -> str | None:
    """What a person following the answer types at `prompt`: yes to yes/no
    questions, the current code when a tool has just shown a new TOTP
    secret (as they would from their app), Enter for defaults; None when
    there is no sensible reply (a password nobody gave them)."""
    low = prompt.lower()
    secrets_seen = _NEW_SECRET_RE.findall(output)
    if "code" in low and secrets_seen:
        return totp(secrets_seen[-1])
    if "-1 to skip" in low:
        return "-1"
    if _YES_NO_RE.search(prompt):
        return "y"
    if any(w in low for w in ("password", "passphrase", "pin")):
        return None
    return ""


def _exec_step(client, command: str, timeout_s: int,
               on_output=None) -> tuple[int | None, str, bool, list[str]]:
    """Run one of the answer's steps the way a person following it would,
    answering its questions (see _reply_for). Returns (exit, output, timed
    out, replies given)."""
    chan = client.get_transport().open_session()
    chan.set_combine_stderr(True)
    chan.exec_command(f"timeout --kill-after=10 {timeout_s} bash -c {shlex.quote(command)}")
    out, replies = "", []
    last_data = time.monotonic()
    deadline = last_data + timeout_s + 30
    stdin_open = True
    try:
        while time.monotonic() < deadline:
            if chan.recv_ready():
                chunk = chan.recv(65536).decode("utf-8", "replace")
                out += chunk
                if on_output:
                    on_output(chunk)
                last_data = time.monotonic()
                continue
            if chan.exit_status_ready():
                break
            idle = time.monotonic() - last_data
            tail = out.rsplit("\n", 1)[-1]
            waiting = bool(tail.strip()) and _PROMPT_TAIL_RE.search(tail)
            if stdin_open and waiting and idle > 0.7:
                reply = _reply_for(tail, out) if len(replies) < 40 else None
                if reply is None:
                    if idle > 5:
                        chan.shutdown_write()  # nothing sensible to type: end of input
                        stdin_open = False
                else:
                    chan.sendall((reply + "\n").encode())
                    replies.append(reply or "Enter")
                    if on_output:
                        on_output(f"{reply or ''}\n" if reply else "\n")
                    last_data = time.monotonic()
            elif stdin_open and idle > 30:
                chan.shutdown_write()  # silent and not asking: whatever reads stdin gets EOF
                stdin_open = False
            time.sleep(0.05)
        while chan.recv_ready():
            chunk = chan.recv(65536).decode("utf-8", "replace")
            out += chunk
            if on_output:
                on_output(chunk)
        code = chan.recv_exit_status() if chan.exit_status_ready() else None
    except OSError as e:
        return None, out + f"\n(connection lost: {e})", False, replies
    finally:
        chan.close()
    return code, out, code in (124, 137) or code is None, replies


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


def run_advice(answer: str, make_vm, progress=None, run_id: str = "", question: str = "",
               make_prober=None, pair_bridges=None, keep_vm=None) -> RunResult:
    """Run `answer` in a VM from make_vm(**kw) (an un-booted microvm.MicroVM;
    kw may carry pair_bridge and scratch_from). With make_prober(bridge)
    and pair_bridges=(create, delete), goal probes run from a second VM at
    the end (L8). `progress(result)` is called after each step, and at most
    once a second while output streams. keep_vm(vm, info) -> bool, if given,
    is offered the target VM at the end; when it returns True the VM is not
    torn down (the caller now owns it)."""
    result = RunResult(id=run_id or uuid.uuid4().hex[:12], started_at=time.time())
    steps = parse_steps(answer)
    result.steps = [asdict(s) for s in steps]
    publish = progress or (lambda r: None)
    last_pub = [0.0]

    def say(text: str, now: bool = False) -> None:
        result.transcript = (result.transcript + text)[-_TRANSCRIPT_KEEP:]
        if now or time.monotonic() - last_pub[0] > 1.0:
            last_pub[0] = time.monotonic()
            publish(result)
    if not any(s.kind in ("run", "write", "append", "prepend", "edit") for s in steps):
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

    def connect():
        nonlocal root, student
        root = vm.ssh_client("root")
        student = vm.ssh_client("student")
        if pair:
            _exec(root, f"ip link set eth1 up && ip addr replace {TARGET_PAIR_IP}/24 dev eth1", 15)

    kept = False
    login_prober = None
    try:
        say("# booting a fresh Ubuntu 22.04 lab machine...\n", now=True)
        t_boot = time.monotonic()
        vm.boot()
        result.vm = {"vcpus": vm.vcpu_count, "mem_mib": vm.mem_size_mib, "scratch_mib": vm.scratch_mib}
        connect()
        say(f"# ready in {time.monotonic() - t_boot:.0f}s: {vm.vcpu_count} vCPU, {vm.mem_size_mib}MB RAM, "
            f"{vm.scratch_mib // 1024}GB disk\n# lab setup: apt and debconf take the defaults, as a person "
            "following the answer would\n")
        _exec(root, _HARNESS_SETUP, 60)
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
        say("# now following the answer, step by step\n", now=True)

        for s in steps:
            if time.monotonic() > deadline:
                s.cls, s.detail = "timeout", "the whole run hit its time limit before this step"
                continue
            t0 = time.monotonic()
            if s.kind == "run":
                say("\n$ " + s.source.replace("\n", "\n> ") + "\n", now=True)
            elif s.kind in ("write", "append", "prepend", "edit"):
                how = s.note.partition("|")[0]
                say(f"\n# {s.kind} {s.target} ({how}):\n" + textwrap.indent(s.source, "  ") + "\n", now=True)
            else:
                say(f"\n# skipped: {s.source.splitlines()[0][:100]} -- {s.note}\n", now=True)
            if s.kind == "run" and _REBOOT_RE.search(s.source):
                # A real reboot: the guest shuts down cleanly (reboot=k ends
                # the VMM), then a new VM boots from the same disk.
                try:
                    _exec_step(student, s.source, 60)
                except (OSError, EOFError):
                    pass  # the connection going away is the point
                say("# the machine is shutting down to reboot...\n", now=True)
                if vm.wait_exit(90):
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
                code, out, timed_out, replies = _exec_step(student, s.source, STEP_TIMEOUT_S, on_output=say)
                if code:
                    say(f"# exit {code}\n")
                s.exit, s.output = code, out[-_OUTPUT_KEEP:]
                s.cls, s.detail = classify(code, out, timed_out)
                if replies:
                    shown = ["the code from the new secret" if r.isdigit() and len(r) == 6 else f"'{r}'"
                             for r in replies[:8]]
                    s.note = "answered its questions: " + ", ".join(shown) + (" ..." if len(replies) > 8 else "")
            elif s.kind in ("write", "append", "prepend", "edit"):
                how, _, flag = s.note.partition("|")
                s.note = how
                existed, ok = _put_file(root, s.target, s.source, s.kind)
                say("# written\n" if ok else f"# could not write {s.target}\n")
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

        say("\n# checking the result...\n", now=True)
        # Whole-run checks, as root so a step that broke sudo can't hide them.
        pkgs, services, paths = _collect_facts(steps)
        services_started = services
        checks = []
        for p in pkgs:
            code, out, _ = _exec(root, f"dpkg-query -W -f='${{Status}}' {shlex.quote(p)}", 30)
            checks.append({"kind": "package", "subject": p, "ok": "install ok installed" in out,
                           "detail": out.strip()[:200]})
        for svc in services:
            code, out, _ = _exec(root, f"systemctl is-active {shlex.quote(svc)}", 30)
            checks.append({"kind": "service", "subject": svc, "ok": out.strip() == "active",
                           "detail": out.strip()[:200] if out.strip() != "active" else ""})
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
        result.checks = checks
        result.steps = [asdict(x) for x in steps]
        _finish_verdict(result, steps, checks)
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


def _finish_verdict(result: RunResult, steps: list[Step], checks: list[dict]) -> None:
    acted = [s for s in steps if s.kind in ("run", "write", "append", "prepend", "edit")]
    bad = [s for s in acted if s.cls not in ("ok",)]
    failed_checks = [c for c in checks if not c["ok"] and c.get("decisive", True)]
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
        lockouts = [c for c in failed_checks if c["kind"] == "login" and "still works" in c["subject"]]
        if lockouts:
            what = ", ".join(c["subject"].split(" (")[0].replace(" still works", "") for c in lockouts)
            parts.append(f"LOCKOUT: following this answer breaks {what}, which worked before it")
        for s in bad:
            parts.append(f"step {s.n}: {s.detail or s.cls.replace('_', ' ')}")
        for c in failed_checks:
            parts.append(f"{c['kind']} check failed: {c['subject']}")
        result.summary = "; ".join(parts[:6]) + ("; ..." if len(parts) > 6 else "")


def plain_step_line(step: dict) -> str:
    """One human line per step for the page and the corpus."""
    icon = {"ok": "✓", "skipped": "–", "": "·"}.get(step["cls"], "✗")
    what = step["target"] and f"{step['kind']} {step['target']}" or step["source"].splitlines()[0][:80]
    extra = step["detail"] or step["note"]
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
