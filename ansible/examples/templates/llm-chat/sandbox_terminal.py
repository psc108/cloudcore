"""
sandbox_terminal.py -- WebSocket terminal for llm-chat's Stage 5 sandbox
shell. Listens on ws://127.0.0.1:{TERMINAL_PORT}/terminal.

Modeled directly on api/terminal.py's own proven WS<->SSH bridge shape
(same protocol, same reader-thread-plus-executor pattern) -- the real
difference is where the SSH target comes from: api/terminal.py connects
to an existing, already-running CloudCore instance; this service boots
a brand new, single-session Firecracker microVM per WebSocket
connection, connects to THAT, and tears it down completely when the
connection ends.

Protocol (text frames), unchanged from api/terminal.py:
  browser -> server:  {"type":"input","data":"<chars>"}
                       {"type":"resize","cols":<n>,"rows":<n>}
  server -> browser:  {"type":"output","data":"<chars>"}
                       {"type":"error","data":"<message>"}
                       {"type":"connected","data":"<message>"}
                       {"type":"warning","data":"<message>"}
                       {"type":"unresponsive","data":"<message>"}

The microVM itself (jailer, chroot, cgroup limits, read-only shared
rootfs + per-session scratch drive) lives in microvm.py, shared with
verify_proxy.py's per-run execution -- see that module's docstring.
"""
from __future__ import annotations

import asyncio
import functools
import json
import os
import select
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

import websockets
import websockets.legacy.server

from microvm import BootError, CapacityError, IpPool, MicroVM, VmSizing, sweep_orphans

# verify_proxy.py's own LB-facing convention, not loopback: HAProxy runs
# on the CloudCore host itself and reaches this service over the bridge
# at the coordinator's real private_ip:TERMINAL_PORT (api/lb.py resolves
# a bridge-mode target group's server address from inst.private_ip, never
# 127.0.0.1) -- bound to loopback only, every LB health check and real
# /terminal request got a real "connection refused" from off-box, which
# is exactly what was found live once the target group's health-check
# path itself was fixed to stop masking this as a 426-vs-200 issue.
WS_HOST = "0.0.0.0"
WS_PORT = int(os.environ.get("TERMINAL_PORT", "8622"))

FCBR = "fcbr0"
# Tags this service's jails/TAPs so its startup sweep never touches
# verify_proxy.py's own per-run microVMs.
VM_OWNER = "term"

SANDBOX_SUBNET = os.environ.get("SANDBOX_SUBNET_CIDR", "10.200.0.0/24")
_COLS_DEFAULT = 220
_ROWS_DEFAULT = 50

# Sized against this project's own existing conventions
# (RATE_LIMIT_RUN_PER_MINUTE=10, verify_timeout_seconds=15,
# idle_watcher.py's 60-120min *deployment*-level idle default) -- see
# that comment's own fuller reasoning in verify_proxy.py's Stage 4
# rate-limit constants.
TERMINAL_IDLE_TIMEOUT_S = int(os.environ.get("TERMINAL_IDLE_TIMEOUT_MINUTES", "15")) * 60
TERMINAL_MAX_SESSION_S = int(os.environ.get("TERMINAL_MAX_SESSION_MINUTES", "60")) * 60
TERMINAL_MAX_CONCURRENT = int(os.environ.get("TERMINAL_MAX_CONCURRENT_SESSIONS", "4"))
TERMINAL_BOOT_TIMEOUT_S = int(os.environ.get("TERMINAL_BOOT_TIMEOUT_SECONDS", "20"))
# A student can run something genuinely destructive from the Terminal
# (e.g. partitioning/formatting the running root fs via a Linux Help
# suggestion) that leaves the guest kernel/sshd resident but the shell
# itself unusable -- the SSH channel doesn't error in that case, so the
# existing idle/max-session detection never fires (typing into a dead
# shell still counts as real activity). This is a genuinely separate
# signal: real input was sent and no real output followed it for this
# long. 30s is deliberately generous -- a normal command's own output
# usually starts well under that, but a legitimately slow one (a big
# apt install, a large download) shouldn't false-positive.
TERMINAL_UNRESPONSIVE_S = int(os.environ.get("TERMINAL_UNRESPONSIVE_SECONDS", "30"))
# llm-chat-lab-sandbox L3: sized per session from what the coordinator
# actually has free (microvm.plan_vm_size), aiming for enough to install
# and run what lab advice suggests (databases, docker, a JVM) and accepting
# less down to the floor. Scratch is sparse: a ceiling on what a session can
# write, not disk reserved up front.
TERMINAL_SIZING = VmSizing(
    mem_target_mib=int(os.environ.get("TERMINAL_MEM_TARGET_MIB", "2048")),
    mem_floor_mib=int(os.environ.get("TERMINAL_MEM_FLOOR_MIB", "512")),
    vcpu_target=int(os.environ.get("TERMINAL_VCPU_TARGET", "2")),
    scratch_target_mib=int(os.environ.get("TERMINAL_SCRATCH_MIB", "16384")),
    scratch_floor_mib=int(os.environ.get("TERMINAL_SCRATCH_FLOOR_MIB", "2048")),
)

# Per direct request: a browser-reachable way to see the output of a web
# app a student wrote and ran in their own sandbox terminal. Fixed pool
# decided once at this example's own deploy time (examples/llm-chat/
# variables.tf's own preview_ports) -- a student's own program very
# often needs more than one port at once (a frontend + an API, etc.), so
# these four are simply always available inside every session, not
# something requested per-port. See _preview_conn_handler's own
# docstring for how a connection on one of these gets routed to the
# right student's own microVM.
PREVIEW_PORTS = [int(p) for p in os.environ.get("PREVIEW_PORTS", "").split(",") if p.strip()]

# Per-client concurrency tracking -- same shape as verify_proxy.py's own
# Stage 4 _client_state, but this is a genuinely separate process (a
# dedicated systemd service, not bolted onto verify-proxy's own
# ThreadingHTTPServer -- see the design doc's own reasoning: mixing
# asyncio's event loop with a thread-per-request HTTP server in one
# process is real, avoidable complexity), so it keeps its own
# independent state rather than sharing verify_proxy.py's in-process
# dict, which a separate process cannot see at all.
_client_lock = threading.Lock()
_client_active: dict[str, int] = {}

# Which microVM a given client IP's own terminal session is currently
# using -- read by the preview proxy below to route a browser connection
# on one of PREVIEW_PORTS to the right sandbox. Set once a session is
# genuinely connected (real boot + real SSH, not just requested) and
# cleared in the same finally block that tears the VM down. If a single
# IP somehow has more than one concurrent session (TERMINAL_MAX_CONCURRENT
# allows it; the browser UI itself never opens more than one), the most
# recently connected one simply wins here -- a deliberate simplification,
# not a correctness bug, since the frontend's own single Terminal panel
# never creates that situation in practice.
_client_vm: dict[str, "MicroVM"] = {}

_ip_pool = IpPool(SANDBOX_SUBNET, part="terminal")


def _client_ip(websocket) -> str:
    # Same reasoning as verify_proxy.py's own _client_ip(): the LB runs
    # in HTTP mode with option forwardfor, so the real browser IP lives
    # in X-Forwarded-For, not the raw TCP peer (which would be the LB
    # itself). Falls back to the raw peer for direct testing, bypassing
    # the LB entirely, same as verify_proxy.py's own testing has always
    # done.
    xff = websocket.request_headers.get("X-Forwarded-For", "")
    if xff:
        return xff.split(",")[0].strip()
    return websocket.remote_address[0]


def _ssh_shell(vm: MicroVM):
    client = vm.ssh_client()
    channel = client.get_transport().open_session()
    channel.get_pty(term="xterm-256color", width=_COLS_DEFAULT, height=_ROWS_DEFAULT)
    channel.invoke_shell()
    return client, channel


# A kept lab machine (llm-chat-lab-sandbox): verify-proxy on this same host
# owns it; this service only opens a shell on it, never boots or destroys it.
VERIFY_LOCAL_URL = os.environ.get("VERIFY_LOCAL_URL", "http://127.0.0.1:8620")


def _lab_attach_info(run_id: str, token: str) -> dict:
    qs = urllib.parse.urlencode({"id": run_id, "token": token})
    try:
        with urllib.request.urlopen(f"{VERIFY_LOCAL_URL}/sandbox/lab-run/attach?{qs}", timeout=5) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        try:
            return {"error": json.loads(e.read()).get("error", f"HTTP {e.code}")}
        except ValueError:
            return {"error": f"HTTP {e.code}"}
    except (OSError, ValueError) as e:
        return {"error": f"could not reach the lab service: {e}"}


async def _terminal_handler(websocket):
    ip = _client_ip(websocket)
    query = urllib.parse.parse_qs(urllib.parse.urlsplit(getattr(websocket, "path", "") or "").query)
    lab_id = "".join(c for c in (query.get("lab") or [""])[0] if c in "0123456789abcdef")[:12]
    lab_token = (query.get("token") or [""])[0][:64]

    async def send(msg: dict):
        try:
            await websocket.send(json.dumps(msg))
        except Exception:
            pass

    with _client_lock:
        active = _client_active.get(ip, 0)
        if active >= TERMINAL_MAX_CONCURRENT:
            await send({"type": "error", "data": "Sandbox terminal capacity full -- try again shortly."})
            return
        _client_active[ip] = active + 1

    loop = asyncio.get_event_loop()
    owned = not lab_id
    lab_expires = 0.0
    if owned:
        vm = MicroVM(VM_OWNER, _ip_pool, FCBR, sizing=TERMINAL_SIZING,
                     boot_timeout_s=TERMINAL_BOOT_TIMEOUT_S)
    else:
        info = await loop.run_in_executor(None, _lab_attach_info, lab_id, lab_token)
        if "session_id" not in info:
            await send({"type": "error", "data": f"Can't open that lab machine: {info.get('error', 'unknown')}."})
            with _client_lock:
                _client_active[ip] = max(0, _client_active.get(ip, 1) - 1)
            return
        vm = MicroVM.attached(info["session_id"], info.get("ip", ""))
        lab_expires = float(info.get("expires_at", 0))
    client = channel = None
    try:
        if owned:
            await send({"type": "output", "data": "Booting a fresh sandboxed shell...\r\n"})
            try:
                await loop.run_in_executor(None, vm.boot)
            except CapacityError as e:
                await send({"type": "error", "data": f"The lab is full: {e}."})
                return
            except BootError as e:
                await send({"type": "error", "data": f"Sandbox failed to start: {e}"})
                return
        else:
            await send({"type": "output", "data": "Opening the lab machine that ran the answer...\r\n"})

        try:
            client, channel = await loop.run_in_executor(None, _ssh_shell, vm)
        except Exception as e:
            await send({"type": "error", "data": f"Could not connect to sandbox: {e}"})
            return

        with _client_lock:
            _client_vm[ip] = vm

        preview_note = (
            f"Ports {', '.join(str(p) for p in PREVIEW_PORTS)} are reachable from your browser -- "
            f"start a web server on any of them and open this same host at that port in a new tab.\r\n"
        ) if PREVIEW_PORTS else ""
        if owned:
            size_note = (f"This sandbox: {vm.vcpu_count} vCPU, {vm.mem_size_mib}MB RAM, "
                         f"{vm.scratch_mib // 1024}GB disk. Anything can be installed with apt; "
                         f"nothing survives the session.\r\n")
            await send({"type": "connected",
                         "data": "Connected. This shell has real internet access and is fully isolated -- "
                                 "it cannot reach anything else.\r\n" + size_note + preview_note})
        else:
            until = time.strftime("%H:%M", time.localtime(lab_expires)) if lab_expires else "its deadline"
            await send({"type": "connected",
                         "data": "Connected to the lab machine that ran the answer, exactly as the run left it. "
                                 f"It is destroyed at {until} (or when you press Destroy); closing this "
                                 "terminal does not destroy it.\r\n"})

        stop_event = threading.Event()
        last_activity = time.monotonic()
        session_start = time.monotonic()
        # A real heads-up before the session just vanishes -- found live
        # (student feedback) that a hard close with no warning reads as
        # the sandbox breaking, not an expected timeout. Sent once per
        # approach to each limit; idle_warned resets on real activity so
        # a student who comes back before actually idling out can still
        # be warned again if they later drift away a second time.
        max_session_warned = False
        idle_warned = False
        # See TERMINAL_UNRESPONSIVE_S's own module-level comment --
        # last_output_at starts at "now" (not None) so a guest that
        # never produces any output at all after connecting is still
        # correctly measured against the real session start, not
        # treated as "no signal yet". last_input_sent stays None until
        # the student actually sends something -- the unresponsive
        # check only makes sense once there's real input to have gone
        # unanswered.
        last_output_at = time.monotonic()
        last_input_sent = None
        unresponsive_warned = False

        async def ssh_reader():
            nonlocal last_output_at, unresponsive_warned
            while not stop_event.is_set():
                try:
                    ready = await loop.run_in_executor(
                        None, lambda: select.select([channel], [], [], 0.1)[0]
                    )
                    if ready:
                        data = channel.recv(4096)
                        if not data:
                            break
                        await send({"type": "output", "data": data.decode("utf-8", errors="replace")})
                        last_output_at = time.monotonic()
                        unresponsive_warned = False
                except Exception:
                    break
            stop_event.set()

        reader_task = asyncio.ensure_future(ssh_reader())

        try:
            while not stop_event.is_set():
                now = time.monotonic()
                # SSH over TCP to a guest whose VMM has died never errors --
                # there's nothing left to send a RST -- so without this the
                # session would hang until the idle timeout (found live by
                # Stage 10's kill -9 failure test).
                if not vm.alive():
                    await send({"type": "error", "data": "\r\n\r\nThe sandbox VM stopped unexpectedly -- reconnect to start a fresh one.\r\n"})
                    break
                if now - session_start > TERMINAL_MAX_SESSION_S:
                    await send({"type": "error", "data": "\r\n\r\nSession time limit reached -- reconnect to start a fresh one.\r\n"})
                    break
                if now - last_activity > TERMINAL_IDLE_TIMEOUT_S:
                    await send({"type": "error", "data": "\r\n\r\nSession closed after being idle too long.\r\n"})
                    break
                if not max_session_warned and TERMINAL_MAX_SESSION_S - (now - session_start) <= 300:
                    max_session_warned = True
                    limit_min = TERMINAL_MAX_SESSION_S // 60
                    await send({"type": "warning",
                                 "data": f"This session will close soon -- the {limit_min}-minute time limit is almost up."})
                if not idle_warned and TERMINAL_IDLE_TIMEOUT_S - (now - last_activity) <= 120:
                    idle_warned = True
                    await send({"type": "warning",
                                 "data": "This session will close soon due to inactivity."})
                # Real input was sent and nothing has come back since --
                # not the same signal as idle_warned above (which only
                # tracks whether the BROWSER sent anything at all, and a
                # student retyping into a dead shell keeps that timer
                # refreshed forever). A suggestion, not an assertion --
                # a legitimately slow command with no output yet looks
                # identical from this signal alone, so the message below
                # phrases it as a question the student can dismiss by
                # just continuing to wait.
                if (not unresponsive_warned and last_input_sent is not None
                        and last_input_sent > last_output_at
                        and now - last_input_sent > TERMINAL_UNRESPONSIVE_S):
                    unresponsive_warned = True
                    await send({"type": "unresponsive",
                                 "data": "The shell hasn't responded to your last input in a "
                                         "while -- it may be stuck. You can keep waiting, or "
                                         "start a fresh session."})
                try:
                    message = await asyncio.wait_for(websocket.recv(), timeout=5.0)
                except asyncio.TimeoutError:
                    continue
                last_activity = time.monotonic()
                idle_warned = False
                try:
                    msg = json.loads(message)
                except Exception:
                    continue
                if msg.get("type") == "input":
                    channel.send(msg.get("data", ""))
                    last_input_sent = time.monotonic()
                elif msg.get("type") == "resize":
                    cols = int(msg.get("cols", _COLS_DEFAULT))
                    rows = int(msg.get("rows", _ROWS_DEFAULT))
                    channel.resize_pty(width=cols, height=rows)
        except websockets.exceptions.ConnectionClosed:
            pass
        finally:
            stop_event.set()
            reader_task.cancel()
    finally:
        if channel is not None:
            try:
                channel.close()
            except Exception:
                pass
        if client is not None:
            try:
                client.close()
            except Exception:
                pass
        if owned:
            await loop.run_in_executor(None, vm.teardown)
        with _client_lock:
            _client_active[ip] = max(0, _client_active.get(ip, 1) - 1)
            if _client_vm.get(ip) is vm:
                del _client_vm[ip]


async def _health_check(path, request_headers):
    # HAProxy's target-group health check (api/lb.py's own httpchk, plain
    # "GET /health" with no Upgrade header) hits this service directly --
    # left unhandled, every plain HTTP request falls through to the
    # websockets library's own handshake rejection, a 426 Upgrade
    # Required, which HAProxy correctly reads as "unhealthy" and marks
    # the whole backend down. Found live: exactly that, blocking every
    # request through the LB despite the WS service itself being fine.
    # Short-circuit only non-upgrade requests to /health; anything else
    # (including a real WS handshake on /terminal) falls through
    # unchanged by returning None.
    if path == "/health" and request_headers.get("Upgrade", "").lower() != "websocket":
        return (200, [("Content-Type", "text/plain")], b"ok\n")
    return None


def _extract_xff(head: bytes) -> str | None:
    for line in head.split(b"\r\n"):
        if line.lower().startswith(b"x-forwarded-for:"):
            value = line.split(b":", 1)[1].decode(errors="replace").strip()
            return value.split(",")[0].strip() or None
    return None


async def _pump(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    try:
        while True:
            chunk = await reader.read(65536)
            if not chunk:
                break
            writer.write(chunk)
            await writer.drain()
    except (ConnectionError, OSError):
        pass
    finally:
        try:
            writer.close()
        except Exception:
            pass


async def _write_and_close(writer: asyncio.StreamWriter, status_line: bytes, body: bytes) -> None:
    try:
        writer.write(
            status_line + b"\r\nContent-Type: text/plain\r\nConnection: close\r\n"
            b"Content-Length: " + str(len(body)).encode() + b"\r\n\r\n" + body)
        await writer.drain()
    except (ConnectionError, OSError):
        pass
    finally:
        writer.close()


async def _preview_conn_handler(port: int, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    """One coordinator-side listener per PREVIEW_PORTS entry (wired into
    _main() below). HAProxy terminates the browser's own HTTP connection
    and opens a fresh one to us with `option forwardfor` set (api/lb.py),
    so the only reason to look at this connection's first request at all
    is to read X-Forwarded-For off it and learn which student it's
    actually from -- that resolves to their own currently-connected
    microVM via _client_vm above, the same IP-keyed concurrency model
    _terminal_handler itself already uses. After that this is a dumb
    byte splice for the rest of the TCP connection's lifetime (including
    the request whose headers were already read, replayed verbatim), so
    whatever the student's own program actually returns -- HTML, JSON,
    images, even a WebSocket upgrade of ITS OWN -- passes through
    completely unmodified. A plain GET /health short-circuits before any
    of that, same reasoning as _health_check above: HAProxy's own health
    probe has no real session behind it, and without this it would read
    as unhealthy and mark the whole backend down."""
    try:
        head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout=10)
    except (asyncio.IncompleteReadError, asyncio.TimeoutError, ConnectionError):
        writer.close()
        return

    request_line = head.split(b"\r\n", 1)[0]
    if request_line.startswith(b"GET /health "):
        writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: text/plain\r\nConnection: close\r\n"
                     b"Content-Length: 3\r\n\r\nok\n")
        try:
            await writer.drain()
        except (ConnectionError, OSError):
            pass
        writer.close()
        return

    peer = writer.get_extra_info("peername")
    ip = _extract_xff(head) or (peer[0] if peer else "")
    with _client_lock:
        vm = _client_vm.get(ip)

    if vm is None or not vm.ip:
        await _write_and_close(
            writer, b"HTTP/1.1 502 Bad Gateway",
            b"No active sandbox terminal for this browser -- open the Terminal panel, "
            b"start a session, then run your program on this port.\n")
        return

    try:
        vm_reader, vm_writer = await asyncio.wait_for(asyncio.open_connection(vm.ip, port), timeout=5)
    except (OSError, asyncio.TimeoutError):
        await _write_and_close(
            writer, b"HTTP/1.1 502 Bad Gateway",
            f"Nothing is listening on port {port} inside your sandbox yet.\n".encode())
        return

    vm_writer.write(head)
    try:
        await vm_writer.drain()
    except (ConnectionError, OSError):
        writer.close()
        vm_writer.close()
        return

    await asyncio.gather(_pump(reader, vm_writer), _pump(vm_reader, writer), return_exceptions=True)


async def _main():
    swept = sweep_orphans(VM_OWNER)
    if swept:
        print(f"Removed {swept} microVM(s) orphaned by a previous run", flush=True)
    preview_servers = [
        await asyncio.start_server(functools.partial(_preview_conn_handler, port), WS_HOST, port)
        for port in PREVIEW_PORTS
    ]
    async with websockets.legacy.server.serve(
        _terminal_handler, WS_HOST, WS_PORT, process_request=_health_check
    ):
        print(f"Sandbox terminal WS server on ws://{WS_HOST}:{WS_PORT}", flush=True)
        if PREVIEW_PORTS:
            print(f"Preview proxy listening on {PREVIEW_PORTS}", flush=True)
        await asyncio.Future()


def run():
    asyncio.run(_main())


if __name__ == "__main__":
    run()
