#!/usr/bin/env python3
"""Reverse proxy + grounded code verification for llm-chat's coordinator.

Sits in front of llama-server (which binds 127.0.0.1 only once this is
in place) on the port the load balancer actually points at. Every
request is passed through unchanged EXCEPT POST /v1/chat/completions:
the response is relayed to the browser in real time exactly as
llama-server streams it (so the existing webui renders normally, with
no added latency until generation finishes), while this process also
accumulates the full assistant text. Once the model's own stream ends,
if the text contains a fenced Python code block, the code is run in a
sandbox and the real result is appended as more streamed content in
the SAME turn -- never a separate UI element, never summarized or
reworded, clearly labeled as actually executed rather than model
output.

Phase 2: if that first execution fails, up to VERIFY_MAX_FIX_ROUNDS
grounded fix attempts follow automatically, in the same turn -- each
one a fresh internal completion (direct to llama-server's own internal
port, never back through this proxy) grounded in the REAL traceback
just captured, not another unverified guess. Only the truly final
block in the whole chain ever tells the student to ask again
themselves; an intermediate failure is followed by another automatic
attempt, so inviting the student to ask there would be misleading.

See llm-chat-verification-Phased-Implementation.md (Phases 1-2) for
the full design rationale.

Pure stdlib -- no new dependency on the guest image, matching the only
real precedent for a CloudCore-authored guest-side service found in
this codebase (examples/ha-frontend-lb's serve-ca-certs.py).
"""
from __future__ import annotations

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

UPSTREAM_HOST = "127.0.0.1"
UPSTREAM_PORT = 8721

LISTEN_PORT = int(os.environ.get("VERIFY_LISTEN_PORT", "8620"))
ENABLE_VERIFICATION = os.environ.get("VERIFY_ENABLED", "true").lower() == "true"
VERIFY_TIMEOUT_S = int(os.environ.get("VERIFY_TIMEOUT_SECONDS", "15"))
VERIFY_MAX_MEMORY_MB = int(os.environ.get("VERIFY_MAX_MEMORY_MB", "256"))
VERIFY_MAX_FIX_ROUNDS = int(os.environ.get("VERIFY_MAX_FIX_ROUNDS", "3"))
SANDBOX_USER = "sandboxrunner"

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
                          max_tokens: int | None = None) -> str:
    """Runs the initial sandboxed execution and, if it fails, up to
    VERIFY_MAX_FIX_ROUNDS grounded fix attempts (Phase 2) -- each one
    grounded in the REAL traceback from the attempt before it, not
    another unverified guess. Returns the complete markdown to append
    to the model's own response. `max_tokens`, when known, is passed
    through to each fix round's own completion so it isn't left
    effectively unbounded."""
    result = run_sandboxed(code)
    if (not result["timed_out"] and result["exit_code"] == 0) or VERIFY_MAX_FIX_ROUNDS <= 0:
        return format_verification_block(result, final=True)

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
        if passed:
            break

    return "".join(blocks)


class ProxyHandler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "verify-proxy/1"

    def log_message(self, fmt, *args):
        pass  # journald already captures stdout/stderr for this unit; avoid double-logging

    # --- dispatch -----------------------------------------------------

    def do_GET(self):
        self._proxy_passthrough()

    def do_HEAD(self):
        self._proxy_passthrough()

    def do_PUT(self):
        self._proxy_passthrough()

    def do_DELETE(self):
        self._proxy_passthrough()

    def do_PATCH(self):
        self._proxy_passthrough()

    def do_POST(self):
        if self.path.split("?", 1)[0].rstrip("/") == "/v1/chat/completions":
            self._handle_chat_completions()
        else:
            self._proxy_passthrough()

    # --- plain reverse proxy (everything except chat completions) -----

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

    # --- chat completions: relay + verify ------------------------------

    def _handle_chat_completions(self):
        length = int(self.headers.get("Content-Length", 0) or 0)
        body = self.rfile.read(length) if length else b""
        try:
            req_json = json.loads(body) if body else {}
        except json.JSONDecodeError:
            req_json = {}
        wants_stream = bool(req_json.get("stream"))

        try:
            conn, resp = self._upstream_request(body)
        except (ConnectionRefusedError, socket.timeout, OSError) as e:
            self.send_response(502)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            self.wfile.write(f"verify-proxy: upstream unreachable: {e}".encode())
            return

        if not ENABLE_VERIFICATION:
            self._relay_raw(resp, wants_stream)
            conn.close()
            return

        request_messages = req_json.get("messages") or []
        request_max_tokens = req_json.get("max_tokens")
        if wants_stream:
            self._relay_and_verify_stream(resp, request_messages, request_max_tokens)
        else:
            self._relay_and_verify_json(resp, request_messages, request_max_tokens)
        conn.close()

    def _relay_raw(self, resp, wants_stream: bool):
        body = resp.read()
        self.send_response(resp.status)
        for k, v in resp.getheaders():
            if k.lower() in ("transfer-encoding", "connection", "content-length"):
                continue
            self.send_header(k, v)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _relay_and_verify_json(self, resp, request_messages: list, request_max_tokens=None):
        body = resp.read()
        try:
            data = json.loads(body)
            content = data["choices"][0]["message"]["content"]
        except (json.JSONDecodeError, KeyError, IndexError, TypeError):
            self.send_response(resp.status)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        code = extract_python_code(content)
        if code:
            extra = verify_and_maybe_fix(request_messages, code, max_tokens=request_max_tokens)
            data["choices"][0]["message"]["content"] = content + extra
        out = json.dumps(data).encode()
        self.send_response(resp.status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)

    def _relay_and_verify_stream(self, resp, request_messages: list, request_max_tokens=None):
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
        code = extract_python_code(full_text)
        if code:
            extra = verify_and_maybe_fix(request_messages, code, heartbeat=self._sse_heartbeat,
                                          max_tokens=request_max_tokens)
            self._write_sse_delta(extra, last_chunk_meta)

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


def main():
    server = http.server.ThreadingHTTPServer(("0.0.0.0", LISTEN_PORT), ProxyHandler)
    server.serve_forever()


if __name__ == "__main__":
    main()
