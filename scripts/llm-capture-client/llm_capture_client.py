#!/usr/bin/env python3
"""llm-capture-client -- send code a local model wrote to the CloudCore
llm-chat learning corpus for real, server-side re-verification.

The client never sends execution results. The CloudCore host re-runs the
code on an llm-chat coordinator's own sandbox and records that result, so
anything that reaches the corpus was actually executed there.

Subcommands:
  submit   send one prompt + code file
  status   show a submission's state and, once verified, the real result
  watch    run a local proxy in front of an OpenAI-compatible model server
           (Ollama, llama-server, ...) and submit the first fenced code
           block of every answer that has one

See README.md for setup.
"""
from __future__ import annotations

import argparse
import ipaddress
import json
import os
import re
import socket
import stat
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Optional
from urllib.parse import urlsplit

import httpx

TOKEN_FILE = Path.home() / ".config" / "llm-capture" / "token"

# Must match the server's accepted set (api/llm_examples_routes.py
# KNOWN_LANGUAGES); aliases mirror llm-chat's own fence handling.
LANG_ALIASES = {
    "python": "python", "py": "python", "python3": "python",
    "bash": "bash", "sh": "bash", "shell": "bash",
    "javascript": "javascript", "js": "javascript", "node": "javascript", "nodejs": "javascript",
    "c": "c",
    "cpp": "cpp", "c++": "cpp", "cxx": "cpp", "cc": "cpp",
    "go": "go", "golang": "go",
}
EXT_LANG = {".py": "python", ".sh": "bash", ".js": "javascript", ".mjs": "javascript",
            ".c": "c", ".cpp": "cpp", ".cc": "cpp", ".cxx": "cpp", ".go": "go"}
FENCE_RE = re.compile(r"```([A-Za-z0-9_+#.-]*)[ \t]*\n(.*?)```", re.DOTALL)

EXIT_OK, EXIT_USAGE, EXIT_AUTH, EXIT_SERVER, EXIT_NETWORK = 0, 2, 3, 4, 5


class ClientError(Exception):
    def __init__(self, message: str, code: int):
        super().__init__(message)
        self.code = code


def log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


# --- configuration -----------------------------------------------------------

def load_token() -> str:
    """LLM_CAPTURE_TOKEN, else ~/.config/llm-capture/token. Never a CLI
    argument, so it stays out of shell history and `ps` output."""
    token = os.environ.get("LLM_CAPTURE_TOKEN", "").strip()
    if token:
        return token
    if not TOKEN_FILE.exists():
        raise ClientError(f"no token: set LLM_CAPTURE_TOKEN or write it to {TOKEN_FILE} (mode 0600)",
                          EXIT_AUTH)
    mode = TOKEN_FILE.stat().st_mode
    if mode & (stat.S_IRWXG | stat.S_IRWXO):
        raise ClientError(f"{TOKEN_FILE} is readable by other users; run: chmod 600 {TOKEN_FILE}",
                          EXIT_AUTH)
    return TOKEN_FILE.read_text().strip()


def check_server_url(url: str) -> str:
    """The capture listener is plain HTTP by design, so only private or
    loopback addresses are allowed: the token must never cross the
    internet in clear text."""
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise ClientError(f"--server must be an http(s) URL, got {url!r}", EXIT_USAGE)
    if parts.scheme == "http":
        try:
            addrs = {info[4][0] for info in socket.getaddrinfo(parts.hostname, parts.port or 80)}
        except socket.gaierror as e:
            raise ClientError(f"cannot resolve {parts.hostname}: {e}", EXIT_NETWORK)
        public = [a for a in addrs
                  if not (ipaddress.ip_address(a).is_private or ipaddress.ip_address(a).is_loopback)]
        if public:
            raise ClientError(f"refusing plain HTTP to a public address ({', '.join(public)}); "
                              f"the capture token would travel unencrypted", EXIT_USAGE)
    return url.rstrip("/")


def server_url(args: argparse.Namespace) -> str:
    url = args.server or os.environ.get("LLM_CAPTURE_SERVER", "")
    if not url:
        raise ClientError("no server: pass --server or set LLM_CAPTURE_SERVER "
                          "(e.g. http://192.168.1.106:8083)", EXIT_USAGE)
    return check_server_url(url)


# --- API calls -----------------------------------------------------------------

def _request(method: str, url: str, token: str, body: Optional[dict] = None) -> dict:
    try:
        resp = httpx.request(method, url, json=body, timeout=30,
                             headers={"Authorization": f"Bearer {token}"})
    except httpx.HTTPError as e:
        raise ClientError(f"cannot reach {url}: {e}", EXIT_NETWORK)
    try:
        data = resp.json()
    except ValueError:
        data = {"detail": resp.text[:300]}
    if resp.status_code == 401:
        raise ClientError(f"rejected: {data.get('detail', 'unauthorized')}", EXIT_AUTH)
    if resp.status_code >= 400:
        raise ClientError(f"HTTP {resp.status_code}: {data.get('detail') or data.get('title')}",
                          EXIT_SERVER)
    return data


def submit_one(base: str, token: str, prompt: str, code: str, language: str, model: str,
               note: str = "") -> dict:
    return _request("POST", f"{base}/v1/llm-chat/client-submissions", token,
                    {"prompt": prompt, "code": code, "language": language,
                     "model_filename": model, "client_note": note})


def get_status(base: str, token: str, sub_id: str) -> dict:
    return _request("GET", f"{base}/v1/llm-chat/client-submissions/{sub_id}", token)


def print_status(s: dict) -> None:
    print(f"{s['id']}  {s['status']}  ({s['language']}, attempts: {s['attempts']})")
    if s.get("last_error"):
        print(f"  last error: {s['last_error']}")
    result = s.get("result")
    if result:
        print(f"  re-run by the coordinator: exit {result['exit_code']}, "
              f"{'PASSED' if result['passed'] else 'FAILED'}")
        for name in ("stdout", "stderr"):
            if (result.get(name) or "").strip():
                print(f"  --- {name} ---")
                print("  " + result[name].rstrip().replace("\n", "\n  "))


# --- subcommands ---------------------------------------------------------------

def cmd_submit(args: argparse.Namespace) -> int:
    code_path = Path(args.file)
    if not code_path.is_file():
        raise ClientError(f"no such file: {code_path}", EXIT_USAGE)
    language = LANG_ALIASES.get((args.language or "").lower()) or EXT_LANG.get(code_path.suffix.lower())
    if not language:
        raise ClientError("cannot tell the language from the file extension; pass --language", EXIT_USAGE)
    prompt = Path(args.prompt_file).read_text() if args.prompt_file else args.prompt
    if not prompt:
        raise ClientError("pass --prompt or --prompt-file (what the model was asked)", EXIT_USAGE)
    code = code_path.read_text()
    base = server_url(args)
    if args.dry_run:
        print(json.dumps({"server": base, "language": language, "model_filename": args.model,
                          "prompt": prompt[:200], "code_bytes": len(code.encode())}, indent=2))
        return EXIT_OK
    token = load_token()
    res = submit_one(base, token, prompt, code, language, args.model, args.note or "")
    log(f"submitted {res['id']} ({language}); the CloudCore host will re-run it on a coordinator")
    if not args.wait:
        print(res["id"])
        return EXIT_OK
    deadline = time.monotonic() + args.wait
    while True:
        s = get_status(base, token, res["id"])
        if s["status"] != "pending" or time.monotonic() > deadline:
            print_status(s)
            return EXIT_OK
        time.sleep(3)


def cmd_status(args: argparse.Namespace) -> int:
    print_status(get_status(server_url(args), load_token(), args.id))
    return EXIT_OK


def first_code_block(text: str) -> Optional[tuple[str, str]]:
    """(language, code) for the first fenced block whose tag names a
    supported language. Untagged blocks are skipped: guessing a language
    would put mislabelled rows in the corpus."""
    for tag, code in FENCE_RE.findall(text):
        lang = LANG_ALIASES.get(tag.lower())
        if lang and code.strip():
            return lang, code
    return None


def cmd_watch(args: argparse.Namespace) -> int:
    base = server_url(args)
    token = load_token()
    upstream = args.upstream.rstrip("/")
    host, _, port = args.listen.rpartition(":")
    if not host or not port.isdigit():
        raise ClientError("--listen must be HOST:PORT", EXIT_USAGE)

    def capture(prompt: str, answer: str, model: str) -> None:
        found = first_code_block(answer)
        if not found:
            return
        language, code = found
        if args.dry_run:
            log(f"[dry-run] would submit {language} code ({len(code)} chars) for: {prompt[:60]!r}")
            return
        try:
            res = submit_one(base, token, prompt, code, language, model)
            log(f"captured {language} code -> submission {res['id']}")
        except ClientError as e:
            log(f"capture failed (the chat itself is unaffected): {e}")

    class Proxy(BaseHTTPRequestHandler):
        def log_message(self, fmt, *a):  # quiet; the capture lines are what matter
            pass

        def _forward(self, method: str) -> None:
            length = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(length) if length else b""
            headers = {k: v for k, v in self.headers.items()
                       if k.lower() not in ("host", "content-length", "connection")}
            payload = {}
            if self.path.rstrip("/").endswith("/chat/completions") and body:
                try:
                    payload = json.loads(body)
                except ValueError:
                    payload = {}
            try:
                with httpx.stream(method, upstream + self.path, content=body, headers=headers,
                                  timeout=httpx.Timeout(10, read=None)) as resp:
                    self.send_response(resp.status_code)
                    for k, v in resp.headers.items():
                        if k.lower() not in ("content-length", "transfer-encoding", "connection",
                                             "content-encoding"):
                            self.send_header(k, v)
                    self.send_header("Connection", "close")
                    self.end_headers()
                    chunks = []
                    for chunk in resp.iter_bytes():
                        self.wfile.write(chunk)
                        self.wfile.flush()
                        if payload:
                            chunks.append(chunk)
            except httpx.HTTPError as e:
                self.send_error(502, f"upstream error: {e}")
                return
            if payload and resp.status_code == 200:
                answer = _answer_text(b"".join(chunks), bool(payload.get("stream")))
                prompt = next((m.get("content") or "" for m in reversed(payload.get("messages") or [])
                               if m.get("role") == "user"), "")
                model = args.model or payload.get("model") or "unknown-local-model"
                if isinstance(prompt, str) and prompt and answer:
                    threading.Thread(target=capture, args=(prompt, answer, model), daemon=True).start()

        def do_GET(self):
            self._forward("GET")

        def do_POST(self):
            self._forward("POST")

    server = ThreadingHTTPServer((host, int(port)), Proxy)
    log(f"proxying http://{args.listen} -> {upstream}; point your chat tool at the former. Ctrl-C to stop.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log("stopped")
    return EXIT_OK


def _answer_text(raw: bytes, streamed: bool) -> str:
    """The assistant's full text from an OpenAI-compatible response, either
    one JSON body or an SSE stream of delta chunks."""
    text = raw.decode(errors="replace")
    if not streamed:
        try:
            return json.loads(text)["choices"][0]["message"]["content"] or ""
        except (ValueError, KeyError, IndexError, TypeError):
            return ""
    out = []
    for line in text.splitlines():
        if not line.startswith("data:"):
            continue
        data = line[5:].strip()
        if data == "[DONE]":
            break
        try:
            delta = json.loads(data)["choices"][0].get("delta", {})
        except (ValueError, KeyError, IndexError, TypeError):
            continue
        out.append(delta.get("content") or "")
    return "".join(out)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="llm-capture-client", description=__doc__.split("\n\n")[0])
    p.add_argument("--server", help="capture listener URL, e.g. http://192.168.1.106:8083 "
                                    "(or env LLM_CAPTURE_SERVER)")
    sub = p.add_subparsers(dest="command", required=True)

    s = sub.add_parser("submit", help="send one prompt + code file")
    s.add_argument("--file", required=True, help="the code the model produced")
    s.add_argument("--prompt", help="what the model was asked")
    s.add_argument("--prompt-file", help="read the prompt from a file instead")
    s.add_argument("--language", help="override; otherwise inferred from the file extension")
    s.add_argument("--model", required=True, help="which local model produced the code")
    s.add_argument("--note", help="optional free-text note for the reviewer")
    s.add_argument("--wait", type=int, default=0, metavar="SECONDS",
                   help="poll until re-verified (or this many seconds pass)")
    s.add_argument("--dry-run", action="store_true", help="show what would be sent, send nothing")
    s.set_defaults(func=cmd_submit)

    st = sub.add_parser("status", help="show a submission's state and result")
    st.add_argument("id")
    st.set_defaults(func=cmd_status)

    w = sub.add_parser("watch", help="proxy a local model server and capture its code")
    w.add_argument("--upstream", required=True,
                   help="your model server, e.g. http://127.0.0.1:11434 (Ollama) or :8080 (llama-server)")
    w.add_argument("--listen", default="127.0.0.1:8085", help="where this proxy listens (default %(default)s)")
    w.add_argument("--model", help="model name to record (default: the request's own 'model' field)")
    w.add_argument("--dry-run", action="store_true", help="log what would be captured, send nothing")
    w.set_defaults(func=cmd_watch)
    return p


def main(argv: Optional[list[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except ClientError as e:
        log(f"error: {e}")
        return e.code


if __name__ == "__main__":
    sys.exit(main())
