#!/usr/bin/env python3
"""Reverse proxy + grounded code verification for llm-chat's coordinator.

Sits in front of llama-server (which binds 127.0.0.1 only once this is
in place) on the port the load balancer actually points at. As of
Phase 4, this proxy IS the deployment's only interface -- llama-server's
own general-purpose webui is no longer reachable at all (GET / serves
the sandbox page instead; passthrough is now an explicit allowlist of
just /health for the LB's own health check, see do_GET/_not_found).
Every model interaction now goes through POST /sandbox/ask, which
builds its own messages list (a tightly scoped system prompt + the
browser's own held conversation + this turn's code/question) rather
than relaying an arbitrary caller-supplied one -- deliberately: a
free-form chat box invites exactly the ungrounded, off-topic question
this whole mechanism has no way to verify.

The model's response is relayed to the browser in real time exactly as
llama-server streams it, while this process also accumulates the full
assistant text. Once the model's own stream ends, if the text contains
a fenced Python code block, the code is run in a sandbox and the real
result is appended as more streamed content in the SAME turn -- never
a separate UI element, never summarized or reworded, clearly labeled
as actually executed rather than model output.

Phase 2: if that first execution fails, up to VERIFY_MAX_FIX_ROUNDS
grounded fix attempts follow automatically, in the same turn -- each
one a fresh internal completion (direct to llama-server's own internal
port, never back through this proxy) grounded in the REAL traceback
just captured, not another unverified guess. Only the truly final
block in the whole chain ever tells the student to ask again
themselves; an intermediate failure is followed by another automatic
attempt, so inviting the student to ask there would be misleading.

Phase 4 also adds POST /sandbox/run -- the student's own code, executed
as-is via the same sandbox, with no model involved and nothing
captured (there's no model claim to ground).

See llm-chat-interactive-sandbox-Phased-Implementation.md (Phase 4)
and llm-chat-verification-Phased-Implementation.md (Phases 1-3) for
the full design rationale.

Pure stdlib -- no new dependency on the guest image, matching the only
real precedent for a CloudCore-authored guest-side service found in
this codebase (examples/ha-frontend-lb's serve-ca-certs.py).
"""
from __future__ import annotations

import html
import http.client
import http.server
import json
import os
import pwd
import re
import resource
import shutil
import signal
import socket
import tempfile
import threading
import urllib.error
import urllib.request

UPSTREAM_HOST = "127.0.0.1"
UPSTREAM_PORT = 8721

LISTEN_PORT = int(os.environ.get("VERIFY_LISTEN_PORT", "8620"))
ENABLE_VERIFICATION = os.environ.get("VERIFY_ENABLED", "true").lower() == "true"
VERIFY_TIMEOUT_S = int(os.environ.get("VERIFY_TIMEOUT_SECONDS", "15"))
VERIFY_MAX_MEMORY_MB = int(os.environ.get("VERIFY_MAX_MEMORY_MB", "256"))
VERIFY_MAX_FIX_ROUNDS = int(os.environ.get("VERIFY_MAX_FIX_ROUNDS", "3"))
SANDBOX_USER = "sandboxrunner"

# Phase 3 -- central learning corpus capture (api/examples_listener.py,
# api/llm_examples_routes.py). Best-effort only: a capture failure must
# never affect the chat response itself, so every call site wraps this
# in try/except and only ever logs. Empty EXAMPLES_API_BASE (unset)
# disables capture entirely rather than failing outward -- lets this
# same proxy source run against an older API host with no Phase 3
# routes at all.
EXAMPLES_API_BASE = os.environ.get("EXAMPLES_API_BASE", "").rstrip("/")
EXAMPLES_API_TOKEN = os.environ.get("EXAMPLES_API_TOKEN", "")
EXAMPLES_MODEL_FILENAME = os.environ.get("EXAMPLES_MODEL_FILENAME", "")

# Phase 4 -- the interactive sandbox's own system prompt. Deliberately
# separate from (and replaces the purpose of) webui_system_message,
# which only ever shaped llama-server's OWN webui -- moot now that
# GET / serves the sandbox instead (see do_GET). A mitigation, not a
# guarantee (a system prompt can still be talked around); the real
# safety net stays run_sandboxed()'s own grounding, same as Phases 1-2.
#
# Read from a plain text file, not an env var -- unlike every other
# VERIFY_*/EXAMPLES_* setting, this one is free-form prose (spaces,
# punctuation, a student's own template overrides), which a systemd
# Environment= line can't carry safely without real quoting risk. Same
# write-a-file convention coordinator-cloud-init.yaml.tftpl's own
# webui-config.json and verify_proxy.py entries already use. Falls
# back to a sensible built-in default so this file also runs correctly
# outside cloud-init (e.g. this module's own local tests).
_SANDBOX_SYSTEM_MESSAGE_DEFAULT = (
    "You are a lab coding assistant. Only discuss the Python code the "
    "student has provided in this conversation. If asked something "
    "unrelated to that code or to this lab exercise, politely decline "
    "and redirect the student back to their code. When suggesting a "
    "fix, provide the complete corrected script in a single fenced "
    "python code block."
)
_SANDBOX_SYSTEM_MESSAGE_PATH = os.environ.get(
    "SANDBOX_SYSTEM_MESSAGE_FILE", "/opt/llama.cpp/sandbox-system-message.txt")
try:
    SANDBOX_SYSTEM_MESSAGE = open(_SANDBOX_SYSTEM_MESSAGE_PATH).read().strip() \
        or _SANDBOX_SYSTEM_MESSAGE_DEFAULT
except OSError:
    SANDBOX_SYSTEM_MESSAGE = _SANDBOX_SYSTEM_MESSAGE_DEFAULT

# Stage 2 -- CodeMirror assets embedded into this guest's own cloud-init
# (coordinator-cloud-init.yaml.tftpl's own write_files, same mechanism
# verify_proxy_source itself already proves) and served from here, not
# a CDN -- matches this whole project's offline-capable convention (see
# api/server.py's own GET /vendor/<path>, the dashboard's equivalent).
# An explicit filename allowlist, not a general static-file server --
# same least-exposure discipline this file already applies elsewhere
# (do_GET's own route allowlist, examples_listener.py's endpoint gate).
VENDOR_DIR = os.environ.get("SANDBOX_VENDOR_DIR", "/opt/llama.cpp/vendor")
VENDOR_CONTENT_TYPES = {
    "codemirror.min.js": "text/javascript",
    "codemirror.min.css": "text/css",
    "codemirror-theme-dracula.min.css": "text/css",
    "codemirror-addon-matchbrackets.min.js": "text/javascript",
    "codemirror-mode-python.min.js": "text/javascript",
}

# How often (seconds) to send an SSE keep-alive comment to the browser
# while waiting on an internal fix-round completion -- HAProxy's own
# timeout client/server (api/lb.py, 300s) is an inactivity timer, so a
# slow internal call needs *something* flowing periodically or the LB
# would sever the connection before the fix round ever finishes.
HEARTBEAT_INTERVAL_S = 20

# Real ceiling on what's shown back -- a runaway print loop shouldn't
# blow up the response; still a wide enough window to see real output.
MAX_OUTPUT_CHARS = 8000

CODE_BLOCK_RE = re.compile(r"```(python|py)?[ \t]*\n(.*?)```", re.DOTALL)
_PY_HINTS = ("def ", "import ", "print(", "class ", "for ", "if __name__")


def extract_python_code(text: str) -> str | None:
    """First fenced code block that's explicitly tagged python/py, or
    (untagged fences only) looks like Python by a cheap keyword check.
    Returns None if nothing worth running was found."""
    for lang, code in CODE_BLOCK_RE.findall(text):
        if lang in ("python", "py"):
            return code
        if not lang and any(h in code for h in _PY_HINTS):
            return code
    return None


def _sandbox_uid_gid() -> tuple[int, int]:
    pw = pwd.getpwnam(SANDBOX_USER)
    return pw.pw_uid, pw.pw_gid


def run_sandboxed(code: str) -> dict:
    """Executes `code` in an isolated net+pid namespace as an
    unprivileged, resource-limited user, and returns the REAL result
    verbatim -- never summarized, never reworded. {"stdout", "stderr",
    "exit_code", "timed_out"}."""
    tmpdir = tempfile.mkdtemp(prefix="verify-")
    script_path = os.path.join(tmpdir, "script.py")
    uid, gid = _sandbox_uid_gid()
    try:
        with open(script_path, "w") as f:
            f.write(code)
        os.chown(tmpdir, uid, gid)
        os.chown(script_path, uid, gid)

        mem_bytes = VERIFY_MAX_MEMORY_MB * 1024 * 1024
        # This bootstrap runs as root (unshare needs CAP_SYS_ADMIN, which
        # only the still-privileged verify-proxy process has) INSIDE the
        # freshly created net/pid namespace, then immediately drops to
        # the unprivileged sandbox user, applies resource limits, and
        # execs the real interpreter -- so the process that actually
        # runs the model's code is never root and is confined to this
        # one isolated namespace throughout.
        bootstrap = (
            "import os,resource;"
            f"os.setgid({gid});os.setuid({uid});"
            f"resource.setrlimit(resource.RLIMIT_CPU,({VERIFY_TIMEOUT_S},{VERIFY_TIMEOUT_S}));"
            f"resource.setrlimit(resource.RLIMIT_AS,({mem_bytes},{mem_bytes}));"
            "resource.setrlimit(resource.RLIMIT_NPROC,(32,32));"
            f"resource.setrlimit(resource.RLIMIT_FSIZE,({10*1024*1024},{10*1024*1024}));"
            f"os.execvp('python3',['python3',{script_path!r}])"
        )
        cmd = ["unshare", "--net", "--pid", "--fork", "--mount-proc", "--",
               "python3", "-c", bootstrap]

        import subprocess
        proc = subprocess.Popen(
            cmd, cwd=tmpdir, stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            start_new_session=True,
        )
        timed_out = False
        try:
            stdout, stderr = proc.communicate(timeout=VERIFY_TIMEOUT_S + 5)
        except subprocess.TimeoutExpired:
            timed_out = True
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            except ProcessLookupError:
                pass
            stdout, stderr = proc.communicate()

        stderr_text = stderr.decode(errors="replace")
        if timed_out:
            stderr_text += f"\n[Execution killed: exceeded {VERIFY_TIMEOUT_S}s]"
        return {
            "stdout": stdout.decode(errors="replace")[:MAX_OUTPUT_CHARS],
            "stderr": stderr_text[:MAX_OUTPUT_CHARS],
            "exit_code": proc.returncode,
            "timed_out": timed_out,
        }
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def format_verification_block(result: dict, final: bool = True) -> str:
    """`final` controls only the trailing "ask again" invite -- an
    intermediate round in the Phase 2 fix loop is followed automatically
    by another attempt, so inviting the student to ask again there
    would be misleading; only the actual last block in a chain passes
    final=True."""
    passed = (not result["timed_out"]) and result["exit_code"] == 0
    lines = ["\n\n---\n### ACTUALLY EXECUTED (not model output)\n"]
    if result["stdout"].strip():
        lines.append("**stdout:**\n```\n" + result["stdout"].rstrip() + "\n```\n")
    if result["stderr"].strip():
        lines.append("**stderr:**\n```\n" + result["stderr"].rstrip() + "\n```\n")
    lines.append(f"**exit code:** {result['exit_code']}\n")
    if not passed and final:
        lines.append(
            "\n_This code did not run successfully. You can ask for "
            "another attempt in your next message._\n"
        )
    return "".join(lines)


def _call_llama_direct(messages: list, heartbeat=None, timeout: int = 3600,
                        max_tokens: int | None = None) -> str:
    """A fresh, non-streaming completion direct to llama-server's own
    internal port -- deliberately never back through this proxy itself
    (would re-enter this same interception logic pointlessly and risks
    recursion). Used only by the Phase 2 fix loop's own follow-up
    requests. `heartbeat`, if given, is called roughly every
    HEARTBEAT_INTERVAL_S while this blocks, to keep the browser's own
    SSE connection alive during a slow internal generation.

    `timeout` defaults to a full hour, not a few minutes: this is a
    non-streaming request, so the socket sits waiting for the ENTIRE
    generation to finish before any bytes arrive at all (unlike the
    browser-facing SSE path, which stays alive on its own via
    continuously streamed chunks) -- confirmed live that the previous
    900s default was too short for a real generation on this hardware
    and aborted a genuinely-still-working fix round outright. Also
    caps `max_tokens` to match the original request when known --
    found live that leaving it unset let llama-server fall back to its
    own effectively-unbounded default (n_predict=-1), making a fix
    round's own real duration unpredictable."""
    stop = threading.Event()

    def _beat():
        while not stop.wait(HEARTBEAT_INTERVAL_S):
            try:
                heartbeat()
            except Exception:
                return

    beat_thread = None
    if heartbeat is not None:
        beat_thread = threading.Thread(target=_beat, daemon=True)
        beat_thread.start()
    try:
        conn = http.client.HTTPConnection(UPSTREAM_HOST, UPSTREAM_PORT, timeout=timeout)
        payload = {"messages": messages, "temperature": 0.2, "stream": False}
        if max_tokens:
            payload["max_tokens"] = max_tokens
        body = json.dumps(payload).encode()
        conn.request("POST", "/v1/chat/completions", body=body,
                      headers={"Content-Type": "application/json",
                               "Content-Length": str(len(body))})
        resp = conn.getresponse()
        data = json.loads(resp.read())
        conn.close()
        return data["choices"][0]["message"]["content"]
    finally:
        stop.set()
        if beat_thread is not None:
            beat_thread.join(timeout=2)


def verify_and_maybe_fix(original_messages: list, code: str, heartbeat=None,
                          max_tokens: int | None = None) -> tuple[str, dict]:
    """Runs the initial sandboxed execution and, if it fails, up to
    VERIFY_MAX_FIX_ROUNDS grounded fix attempts (Phase 2) -- each one
    grounded in the REAL traceback from the attempt before it, not
    another unverified guess. `max_tokens`, when known, is passed
    through to each fix round's own completion so it isn't left
    effectively unbounded.

    Returns (markdown, capture) -- markdown is the complete text to
    append to the model's own response, unchanged from before this
    return type grew a second element; capture is the same real data
    shaped for Phase 3's learning-corpus record (llm_examples_store's
    own field names): the initial code/result always, plus the LAST
    fix round actually attempted (if any) -- the DB schema holds one
    fix slot, representing where the chain ended up, not every
    intermediate round."""
    result = run_sandboxed(code)
    capture = {
        "generated_code": code,
        "exec_stdout": result["stdout"], "exec_stderr": result["stderr"],
        "exec_exit_code": result["exit_code"],
        "passed": (not result["timed_out"] and result["exit_code"] == 0),
    }
    if capture["passed"] or VERIFY_MAX_FIX_ROUNDS <= 0:
        return format_verification_block(result, final=True), capture

    blocks = [format_verification_block(result, final=False)]
    messages = list(original_messages)
    messages.append({"role": "assistant", "content": f"```python\n{code}\n```"})

    for round_num in range(1, VERIFY_MAX_FIX_ROUNDS + 1):
        is_last_round = round_num == VERIFY_MAX_FIX_ROUNDS
        fix_prompt = (
            "This code was executed and failed with the following real "
            f"output:\n\n```\n{(result['stderr'] or result['stdout']).strip()}\n```\n\n"
            "Explain exactly what is wrong, quoting the failing line, then "
            "provide a corrected version of the complete script."
        )
        messages.append({"role": "user", "content": fix_prompt})

        try:
            fix_text = _call_llama_direct(messages, heartbeat=heartbeat, max_tokens=max_tokens)
        except Exception as e:
            blocks.append(
                f"\n\n---\n### Fix attempt {round_num} of {VERIFY_MAX_FIX_ROUNDS} "
                f"failed to generate ({e})\n"
            )
            break

        messages.append({"role": "assistant", "content": fix_text})
        blocks.append(f"\n\n---\n### Fix attempt {round_num} of {VERIFY_MAX_FIX_ROUNDS}\n\n{fix_text}")
        capture["fix_explanation"] = fix_text

        new_code = extract_python_code(fix_text)
        if not new_code:
            blocks.append(
                "\n\n_No runnable code block found in this fix attempt._\n"
                + ("\n_This code did not run successfully. You can ask for "
                   "another attempt in your next message._\n" if is_last_round else "")
            )
            break

        result = run_sandboxed(new_code)
        passed = not result["timed_out"] and result["exit_code"] == 0
        blocks.append(format_verification_block(result, final=(passed or is_last_round)))
        code = new_code
        capture.update({
            "fixed_code": new_code,
            "fix_exec_stdout": result["stdout"], "fix_exec_stderr": result["stderr"],
            "fix_passed": passed,
        })
        if passed:
            break

    return "".join(blocks), capture


def _extract_prompt(original_messages: list) -> str:
    """The most recent user-role message -- what the student actually
    asked in this turn, not the whole running conversation."""
    for msg in reversed(original_messages or []):
        if msg.get("role") == "user":
            return msg.get("content") or ""
    return ""


def capture_example(original_messages: list, capture: dict,
                     source: str = "llm-chat-coordinator") -> None:
    """POSTs one grounded-verification transaction back to the CloudCore
    API's examples-capture endpoint (api/llm_examples_routes.py). Fully
    best-effort -- any failure (capture disabled, host unreachable,
    non-2xx) is swallowed after one stderr line for journald, since a
    capture problem must never affect the chat response itself.
    `source` distinguishes the sandbox's own on-demand Ask transactions
    ("llm-chat-sandbox") from the default chat-originated ones -- see
    do_POST's /sandbox/ask branch."""
    if not EXAMPLES_API_BASE or not EXAMPLES_MODEL_FILENAME:
        return
    payload = {
        "source": source,
        "model_filename": EXAMPLES_MODEL_FILENAME,
        "prompt": _extract_prompt(original_messages),
        **capture,
    }
    try:
        body = json.dumps(payload).encode()
        req = urllib.request.Request(
            EXAMPLES_API_BASE + "/v1/llm-chat/examples", data=body, method="POST",
            headers={"Content-Type": "application/json",
                     "Authorization": f"Bearer {EXAMPLES_API_TOKEN}"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            resp.read()
    except (urllib.error.URLError, OSError, ValueError) as e:
        print(f"verify-proxy: example capture failed (non-fatal): {e}", flush=True)


# Phase 4 -- the interactive sandbox itself. Stdlib-rendered, no new
# frontend framework, matching /examples' own convention. Plain
# <textarea> for Stage 1 (see the Interactive Sandbox phased-
# implementation doc's own Stage 1/2 split). Stage 2: CodeMirror 5,
# same version already vendored for the Dashboard's own Editor page
# (ui/vendor/), shipped into THIS guest's own cloud-init instead (the
# dashboard's /vendor/ route serves the admin host, not this
# student-facing coordinator -- see GET /vendor/<name> below) and
# served from local files, never a CDN, same offline-capable
# convention this whole project already holds to.
# All state (code buffer, ask conversation) lives client-side in
# localStorage -- no server-side student identity, matching Phase 3's
# own privacy stance. Every fetch to /sandbox/run or /sandbox/ask
# disables its own button while in flight -- the simplest real abuse
# mitigation for Stage 1 (see the doc's own "Abuse/rate consideration").
SANDBOX_PAGE_HTML = """<!doctype html>
<html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>llm-chat -- Sandbox</title>
<link rel="stylesheet" href="/vendor/codemirror.min.css">
<link rel="stylesheet" href="/vendor/codemirror-theme-dracula.min.css">
<script src="/vendor/codemirror.min.js"></script>
<script src="/vendor/codemirror-mode-python.min.js"></script>
<script src="/vendor/codemirror-addon-matchbrackets.min.js"></script>
<style>
:root { color-scheme: light dark; }
body { font-family: system-ui, sans-serif; max-width: 1000px; margin: 1.5rem auto; padding: 0 1rem; color: #1a1a1a; }
h1 { font-size: 1.4rem; margin-bottom: 0.25rem; }
.sub { color: #666; font-size: 0.85rem; margin: 0 0 1.25rem; }
.panel { border: 1px solid #ddd; border-radius: 8px; padding: 1rem 1.25rem; margin-bottom: 1.25rem; }
.panel h2 { font-size: 1rem; margin: 0 0 0.75rem; }
#codeHost { border: 1px solid #ccc; border-radius: 6px; overflow: hidden; }
#codeHost .CodeMirror { height: 320px; font-family: ui-monospace, "SF Mono", Menlo, monospace; font-size: 0.9rem; }
.row { display: flex; gap: 0.6rem; align-items: center; margin-top: 0.75rem; flex-wrap: wrap; }
button { font: inherit; padding: 0.45rem 1rem; border-radius: 6px; border: 1px solid #999; background: #f2f2f2; cursor: pointer; }
button:hover:not(:disabled) { background: #e8e8e8; }
button:disabled { opacity: 0.5; cursor: default; }
button.primary { background: #2a5db0; border-color: #2a5db0; color: #fff; }
button.primary:hover:not(:disabled) { background: #234f96; }
.status { font-size: 0.85rem; color: #666; }
pre { background: #f6f6f6; border-radius: 4px; padding: 0.6rem; overflow-x: auto; white-space: pre-wrap; word-break: break-word; margin: 0.5rem 0 0; }
.result h4 { margin: 0.75rem 0 0.25rem; font-size: 0.85rem; }
.result.pass .exitline { color: #0a7a2f; font-weight: 600; }
.result.fail .exitline { color: #b02a2a; font-weight: 600; }
#transcript { display: flex; flex-direction: column; gap: 0.75rem; max-height: 420px; overflow-y: auto; padding: 0.25rem 0; }
.msg { border-radius: 6px; padding: 0.5rem 0.75rem; }
.msg.student { background: #eef3fb; }
.msg.model { background: #f6f6f6; }
.msg .who { font-size: 0.75rem; color: #888; margin-bottom: 0.25rem; text-transform: uppercase; letter-spacing: 0.03em; }
.msg .content { white-space: pre-wrap; word-break: break-word; font-size: 0.9rem; }
#question { flex: 1; min-width: 200px; font: inherit; padding: 0.45rem 0.6rem; border: 1px solid #ccc; border-radius: 6px; }
footer { margin-top: 1.5rem; font-size: 0.8rem; color: #888; }
footer a { color: inherit; }
</style></head>
<body>
<h1>Sandbox</h1>
<p class="sub">Write real Python, run it for real, and ask the model about it -- every response you get back is grounded in an actual execution, not just the model's own word for it.</p>

<div class="panel">
  <h2>Your code</h2>
  <div id="codeHost"></div>
  <div class="row">
    <button id="runBtn" class="primary" onclick="runCode()">Run</button>
    <button onclick="clearAll()">Clear session</button>
    <span id="runStatus" class="status"></span>
  </div>
  <div id="runResult"></div>
</div>

<div class="panel">
  <h2>Ask about this code</h2>
  <div id="transcript"></div>
  <div class="row">
    <input id="question" type="text" placeholder="e.g. why does this fail on an empty list?" onkeydown="if(event.key==='Enter')askModel()">
    <button id="askBtn" class="primary" onclick="askModel()">Ask</button>
  </div>
</div>

<footer>Published examples from sessions like this one: <a href="/examples">/examples</a></footer>

<script>
const CODE_KEY = 'sandboxCode', HISTORY_KEY = 'sandboxHistory';
const transcriptEl = document.getElementById('transcript');

// CodeMirror(host, {...}), not .fromTextArea() -- same init pattern
// the Dashboard's own Editor page already uses (ui/src/js/18-editor.js).
const cm = CodeMirror(document.getElementById('codeHost'), {
  mode: 'python', theme: 'dracula', lineNumbers: true, matchBrackets: true,
  indentUnit: 4, tabSize: 4, viewportMargin: Infinity,
});
cm.on('change', saveCode);

function loadState() {
  cm.setValue(localStorage.getItem(CODE_KEY) || '');
  renderTranscript();
}
function saveCode() { localStorage.setItem(CODE_KEY, cm.getValue()); }

function getHistory() {
  try { return JSON.parse(localStorage.getItem(HISTORY_KEY) || '[]'); }
  catch (e) { return []; }
}
function saveHistory(h) { localStorage.setItem(HISTORY_KEY, JSON.stringify(h)); }

function renderTranscript() {
  const h = getHistory();
  transcriptEl.innerHTML = h.map(m => `
    <div class="msg ${m.role === 'user' ? 'student' : 'model'}">
      <div class="who">${m.role === 'user' ? 'You' : 'Model'}</div>
      <div class="content"></div>
    </div>`).join('');
  // textContent, not innerHTML, for the actual message body -- never
  // trust/render model or student text as markup.
  [...transcriptEl.children].forEach((el, i) => { el.querySelector('.content').textContent = h[i].content; });
  transcriptEl.scrollTop = transcriptEl.scrollHeight;
}

function clearAll() {
  if (!confirm('Clear your code and conversation? This only affects this browser.')) return;
  localStorage.removeItem(CODE_KEY);
  localStorage.removeItem(HISTORY_KEY);
  cm.setValue('');
  renderTranscript();
  document.getElementById('runResult').innerHTML = '';
}

async function runCode() {
  const btn = document.getElementById('runBtn');
  const status = document.getElementById('runStatus');
  const out = document.getElementById('runResult');
  if (!cm.getValue().trim()) return;
  btn.disabled = true;
  status.textContent = 'Running...';
  out.innerHTML = '';
  try {
    const resp = await fetch('/sandbox/run', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({code: cm.getValue()}),
    });
    const data = await resp.json();
    if (!resp.ok) { status.textContent = 'Error: ' + (data.error || resp.status); return; }
    const passed = !data.timed_out && data.exit_code === 0;
    status.textContent = '';
    const esc = s => { const d = document.createElement('div'); d.textContent = s; return d.innerHTML; };
    out.innerHTML = `<div class="result ${passed ? 'pass' : 'fail'}">
      ${data.stdout.trim() ? `<h4>stdout</h4><pre>${esc(data.stdout)}</pre>` : ''}
      ${data.stderr.trim() ? `<h4>stderr</h4><pre>${esc(data.stderr)}</pre>` : ''}
      <p class="exitline">exit code: ${data.exit_code}${data.timed_out ? ' (timed out)' : ''}</p>
    </div>`;
  } catch (e) {
    status.textContent = 'Request failed: ' + e.message;
  } finally {
    btn.disabled = false;
  }
}

async function askModel() {
  const btn = document.getElementById('askBtn');
  const qEl = document.getElementById('question');
  const question = qEl.value.trim();
  if (!question) return;

  const history = getHistory();
  history.push({role: 'user', content: question});
  saveHistory(history);
  renderTranscript();
  qEl.value = '';
  btn.disabled = true;

  // Placeholder model bubble, filled in as tokens stream -- textContent
  // only, same no-markup-from-untrusted-text rule as renderTranscript().
  const bubble = document.createElement('div');
  bubble.className = 'msg model';
  bubble.innerHTML = '<div class="who">Model</div><div class="content"></div>';
  transcriptEl.appendChild(bubble);
  const bubbleContent = bubble.querySelector('.content');
  transcriptEl.scrollTop = transcriptEl.scrollHeight;

  let assistantText = '';
  try {
    const resp = await fetch('/sandbox/ask', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({code: cm.getValue(), question, history: history.slice(0, -1)}),
    });
    if (!resp.ok || !resp.body) {
      bubbleContent.textContent = 'Request failed (' + resp.status + ')';
    } else {
      const reader = resp.body.getReader();
      const decoder = new TextDecoder();
      let buf = '';
      while (true) {
        const {done, value} = await reader.read();
        if (done) break;
        buf += decoder.decode(value, {stream: true});
        const events = buf.split('\\n\\n');
        buf = events.pop();
        for (const evt of events) {
          const line = evt.split('\\n').find(l => l.startsWith('data: '));
          if (!line) continue;
          const payload = line.slice(6);
          if (payload === '[DONE]') continue;
          try {
            const obj = JSON.parse(payload);
            const delta = (obj.choices[0].delta || {}).content || '';
            if (delta) { assistantText += delta; bubbleContent.textContent = assistantText; transcriptEl.scrollTop = transcriptEl.scrollHeight; }
          } catch (e) { /* skip malformed/comment lines (SSE heartbeats etc.) */ }
        }
      }
    }
  } catch (e) {
    bubbleContent.textContent = 'Request failed: ' + e.message;
  } finally {
    btn.disabled = false;
  }

  const h = getHistory();
  h.push({role: 'assistant', content: assistantText || bubbleContent.textContent});
  saveHistory(h);
}

loadState();
</script>
</body></html>
"""


class ProxyHandler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "verify-proxy/1"

    def log_message(self, fmt, *args):
        pass  # journald already captures stdout/stderr for this unit; avoid double-logging

    # --- dispatch -----------------------------------------------------

    def _clean_path(self) -> str:
        return self.path.split("?", 1)[0].rstrip("/") or "/"

    def do_GET(self):
        path = self._clean_path()
        if path == "/":
            self._serve_sandbox_page()
        elif path == "/examples":
            self._serve_examples_page()
        elif path == "/health":
            self._proxy_passthrough()
        elif path.startswith("/vendor/"):
            self._serve_vendor_file(path[len("/vendor/"):])
        else:
            self._not_found()

    def do_HEAD(self):
        if self._clean_path() == "/health":
            self._proxy_passthrough()
        else:
            self._not_found()

    def do_PUT(self):
        self._not_found()

    def do_DELETE(self):
        self._not_found()

    def do_PATCH(self):
        self._not_found()

    def do_POST(self):
        path = self._clean_path()
        if path == "/sandbox/run":
            self._handle_sandbox_run()
        elif path == "/sandbox/ask":
            self._handle_sandbox_ask()
        else:
            self._not_found()

    def _not_found(self):
        # Phase 4: the sandbox is the only interface this deployment
        # exposes now -- llama-server's own webui and its raw
        # /v1/chat/completions are deliberately no longer reachable
        # from outside (only /sandbox/ask calls that internally). Same
        # least-exposure discipline api/examples_listener.py's own
        # endpoint allowlist already uses.
        self.send_response(404)
        self.send_header("Content-Type", "text/plain")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(b"not found")

    # --- plain reverse proxy (/health only -- see do_GET/do_HEAD) -----

    def _upstream_request(self, body: bytes | None):
        conn = http.client.HTTPConnection(UPSTREAM_HOST, UPSTREAM_PORT, timeout=600)
        headers = {k: v for k, v in self.headers.items()
                   if k.lower() not in ("host", "content-length", "connection")}
        if body is not None:
            headers["Content-Length"] = str(len(body))
        conn.request(self.command, self.path, body=body, headers=headers)
        return conn, conn.getresponse()

    def _proxy_passthrough(self):
        length = int(self.headers.get("Content-Length", 0) or 0)
        body = self.rfile.read(length) if length else None
        try:
            conn, resp = self._upstream_request(body)
        except (ConnectionRefusedError, socket.timeout, OSError) as e:
            self.send_response(502)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            self.wfile.write(f"verify-proxy: upstream unreachable: {e}".encode())
            return
        resp_body = resp.read()
        self.send_response(resp.status)
        for k, v in resp.getheaders():
            if k.lower() in ("transfer-encoding", "connection", "content-length"):
                continue
            self.send_header(k, v)
        self.send_header("Content-Length", str(len(resp_body)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(resp_body)
        conn.close()

    # --- Phase 4: the sandbox's own two actions -------------------------

    def _read_json_body(self) -> dict:
        length = int(self.headers.get("Content-Length", 0) or 0)
        body = self.rfile.read(length) if length else b""
        try:
            return json.loads(body) if body else {}
        except json.JSONDecodeError:
            return {}

    def _handle_sandbox_run(self):
        """Plain Run -- executes the student's own current buffer as-is,
        synchronously, through the exact same run_sandboxed() Phases 1-2
        already proved live. Bounded to a few seconds by
        VERIFY_TIMEOUT_SECONDS, so a plain JSON response is enough; no
        SSE/streaming needed for this action. Not captured -- this is
        the student's own code, not a model claim, so there's nothing
        to ground against."""
        req_json = self._read_json_body()
        code = req_json.get("code") or ""
        if not code.strip():
            out = json.dumps({"error": "code is required"}).encode()
            self.send_response(400)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(out)))
            self.end_headers()
            self.wfile.write(out)
            return

        result = run_sandboxed(code)
        out = json.dumps(result).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)

    def _handle_sandbox_ask(self):
        """Ask the model about the current code -- builds a fresh
        messages list from SANDBOX_SYSTEM_MESSAGE + the browser's own
        held conversation history + this turn's code/question, streams
        the real generation back via the same SSE mechanics the old
        chat endpoint used, then grounds and captures it exactly the
        same way (source="llm-chat-sandbox", distinguishing these rows
        from chat-originated ones in the shared Phase 3 corpus)."""
        req_json = self._read_json_body()
        code = (req_json.get("code") or "").strip()
        question = (req_json.get("question") or "").strip()
        history = req_json.get("history") or []
        max_tokens = req_json.get("max_tokens")

        if not question:
            self.send_response(400)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            self.wfile.write(b"question is required")
            return

        user_turn = (f"Here is my current code:\n```python\n{code}\n```\n\n{question}"
                     if code else question)
        messages = ([{"role": "system", "content": SANDBOX_SYSTEM_MESSAGE}]
                    + list(history) + [{"role": "user", "content": user_turn}])

        upstream_payload = {"messages": messages, "stream": True}
        if max_tokens:
            upstream_payload["max_tokens"] = max_tokens
        upstream_body = json.dumps(upstream_payload).encode()

        try:
            conn = http.client.HTTPConnection(UPSTREAM_HOST, UPSTREAM_PORT, timeout=600)
            conn.request("POST", "/v1/chat/completions", body=upstream_body,
                          headers={"Content-Type": "application/json",
                                   "Content-Length": str(len(upstream_body))})
            resp = conn.getresponse()
        except (ConnectionRefusedError, socket.timeout, OSError) as e:
            self.send_response(502)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            self.wfile.write(f"verify-proxy: upstream unreachable: {e}".encode())
            return

        self._relay_and_verify_stream(resp, messages, max_tokens, capture_source="llm-chat-sandbox")
        conn.close()

    def _relay_and_verify_stream(self, resp, request_messages: list, request_max_tokens=None,
                                  capture_source: str = "llm-chat-coordinator"):
        self.send_response(resp.status)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.end_headers()

        accumulated = []
        last_chunk_meta: dict = {}
        buf = b""
        while True:
            chunk = resp.read(1)
            if not chunk:
                break
            buf += chunk
            if not buf.endswith(b"\n"):
                continue
            line = buf
            buf = b""

            text = line.decode(errors="replace").strip()
            is_done = text.startswith("data: ") and text[len("data: "):] == "[DONE]"
            if is_done:
                # Swallow the upstream's own [DONE] -- our own single
                # [DONE] goes out after any verification block below,
                # never before it (most SSE clients stop reading at the
                # first one, so anything sent after an earlier [DONE]
                # would be silently dropped).
                break

            try:
                self.wfile.write(line)
                self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                return  # student navigated away mid-stream -- nothing more to do

            if not text.startswith("data: "):
                continue
            payload = text[len("data: "):]
            try:
                obj = json.loads(payload)
                delta = obj["choices"][0].get("delta", {})
                if "content" in delta and delta["content"]:
                    accumulated.append(delta["content"])
                last_chunk_meta = {k: obj.get(k) for k in ("id", "model", "system_fingerprint")}
            except (json.JSONDecodeError, KeyError, IndexError, TypeError):
                continue

        full_text = "".join(accumulated)
        code = extract_python_code(full_text) if ENABLE_VERIFICATION else None
        if code:
            extra, capture = verify_and_maybe_fix(request_messages, code, heartbeat=self._sse_heartbeat,
                                                    max_tokens=request_max_tokens)
            self._write_sse_delta(extra, last_chunk_meta)
            capture_example(request_messages, capture, source=capture_source)

        try:
            self.wfile.write(b"data: [DONE]\n\n")
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _write_sse_delta(self, text: str, meta: dict):
        obj = {
            "choices": [{"index": 0, "delta": {"content": text}, "finish_reason": None}],
            "object": "chat.completion.chunk",
            **meta,
        }
        try:
            self.wfile.write(f"data: {json.dumps(obj)}\n\n".encode())
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _sse_heartbeat(self):
        # SSE comment line (leading ":") -- valid per the SSE spec,
        # silently ignored by any real client, exists purely to keep
        # HAProxy's own inactivity timer from firing while a slow
        # Phase 2 fix-round completion is still in flight. Called from
        # _call_llama_direct's own background timer thread, not the
        # main request thread -- raises on a dead connection so that
        # thread's own loop stops cleanly instead of retrying forever.
        self.wfile.write(b": verifying...\n\n")
        self.wfile.flush()

    # --- Phase 4: the sandbox itself ------------------------------------

    def _serve_sandbox_page(self):
        out = SANDBOX_PAGE_HTML.encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)

    def _serve_vendor_file(self, name: str):
        """Stage 2's own CodeMirror assets, embedded into this guest's
        cloud-init at build time -- see VENDOR_DIR/VENDOR_CONTENT_TYPES'
        own comment. Pinned, versioned files, so a long-lived cache is
        fine, same reasoning api/server.py's own GET /vendor/<path>
        already documents for the dashboard's equivalent."""
        content_type = VENDOR_CONTENT_TYPES.get(name)
        if not content_type:
            self._not_found()
            return
        try:
            with open(os.path.join(VENDOR_DIR, name), "rb") as f:
                body = f.read()
        except OSError:
            self._not_found()
            return
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", "public, max-age=31536000, immutable")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    # --- Phase 3: student review page ----------------------------------

    def _serve_examples_page(self):
        """Small, self-rendered HTML page (stdlib only, no new frontend
        framework -- matches this whole proxy's own convention) showing
        every published grounded-verification example's full journey:
        prompt -> wrong code -> real failure -> grounded explanation ->
        fix -> real re-verification result. Served on the SAME URL/port
        students already use for chat -- no new credentials, no new
        address to distribute, matches Phase 3's own design intent
        ('for the benefit of all students', not gated per-person)."""
        items = []
        error = None
        if EXAMPLES_API_BASE:
            try:
                req = urllib.request.Request(
                    EXAMPLES_API_BASE + "/v1/llm-chat/examples/published?limit=100")
                with urllib.request.urlopen(req, timeout=10) as resp:
                    items = json.loads(resp.read()).get("items", [])
            except (urllib.error.URLError, OSError, ValueError) as e:
                error = str(e)
        else:
            error = "example capture is not configured for this deployment"

        body = self._render_examples_html(items, error)
        out = body.encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)

    @staticmethod
    def _render_examples_html(items: list, error: str | None) -> str:
        esc = html.escape

        def _block(label: str, text: str) -> str:
            if not (text or "").strip():
                return ""
            return f"<h4>{esc(label)}</h4><pre>{esc(text)}</pre>"

        cards = []
        for it in items:
            passed_badge = '<span class="pass">PASSED</span>' if it.get("passed") else '<span class="fail">FAILED</span>'
            parts = [
                f'<div class="card">',
                f'<div class="meta">{esc(it.get("model_filename",""))} &middot; {esc(it.get("created_at",""))} &middot; {passed_badge}</div>',
                _block("Prompt", it.get("prompt", "")),
                _block("Generated code", it.get("generated_code", "")),
                _block("Actually executed -- stdout", it.get("exec_stdout", "")),
                _block("Actually executed -- stderr", it.get("exec_stderr", "")),
            ]
            if it.get("fix_explanation"):
                fix_badge = '<span class="pass">FIX PASSED</span>' if it.get("fix_passed") else '<span class="fail">FIX FAILED</span>'
                parts += [
                    f'<div class="meta">{fix_badge}</div>',
                    _block("Grounded explanation + fix", it.get("fix_explanation", "")),
                    _block("Fixed code", it.get("fixed_code", "")),
                    _block("Re-execution -- stdout", it.get("fix_exec_stdout", "")),
                    _block("Re-execution -- stderr", it.get("fix_exec_stderr", "")),
                ]
            parts.append("</div>")
            cards.append("".join(parts))

        body_html = (
            f'<p class="error">Examples aren\'t available right now: {esc(error)}</p>' if error
            else ('<p class="empty">No examples have been published yet.</p>' if not items
                  else "\n".join(cards))
        )

        return f"""<!doctype html>
<html><head><meta charset="utf-8">
<title>llm-chat -- Learning Examples</title>
<style>
body {{ font-family: system-ui, sans-serif; max-width: 900px; margin: 2rem auto; padding: 0 1rem; color: #1a1a1a; }}
h1 {{ font-size: 1.4rem; }}
.card {{ border: 1px solid #ddd; border-radius: 8px; padding: 1rem 1.25rem; margin: 1.25rem 0; }}
.meta {{ color: #666; font-size: 0.85rem; margin-bottom: 0.5rem; }}
h4 {{ margin: 0.75rem 0 0.25rem; font-size: 0.9rem; }}
pre {{ background: #f6f6f6; border-radius: 4px; padding: 0.6rem; overflow-x: auto; white-space: pre-wrap; word-break: break-word; }}
.pass {{ color: #0a7a2f; font-weight: 600; }}
.fail {{ color: #b02a2a; font-weight: 600; }}
.error, .empty {{ color: #666; }}
</style></head>
<body>
<h1>Learning Examples</h1>
<p class="meta">Real prompts, real code, real execution results -- curated from this deployment's own chat sessions. Nothing here is summarized or reworded.</p>
{body_html}
</body></html>
"""


def main():
    server = http.server.ThreadingHTTPServer(("0.0.0.0", LISTEN_PORT), ProxyHandler)
    server.serve_forever()


if __name__ == "__main__":
    main()
