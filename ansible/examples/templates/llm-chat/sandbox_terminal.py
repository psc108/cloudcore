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

Deliberately NOT run through jailer (chroot + uid/gid drop + cgroups)
in this first pass -- firecracker runs directly as root here, same as
Stage A's own live-verified smoke test. jailer is a HOST-side hardening
layer on top of the VMM process itself; it does not change the actual
guest-to-host isolation boundary this feature's whole security model
rests on (that's the KVM guest-kernel boundary plus the iptables
egress policy set up in cloud-init, both already live-verified in
Stage A). Flagged explicitly as a named follow-up, not a silent gap.
"""
from __future__ import annotations

import asyncio
import functools
import ipaddress
import json
import os
import select
import shutil
import signal
import socket
import subprocess
import threading
import time
import uuid
from http.client import HTTPConnection

import paramiko
import websockets
import websockets.legacy.server

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

FC_BIN = "/opt/firecracker/firecracker"
KERNEL_PATH = "/opt/firecracker/vmlinux"
GOLDEN_ROOTFS = "/opt/firecracker/golden-rootfs.ext4"
SESSION_DIR = "/srv/jailer/sessions"
FCBR = "fcbr0"

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
VCPU_COUNT = 1
MEM_SIZE_MIB = 256

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

_ip_lock = threading.Lock()
_ip_pool = [str(ip) for ip in ipaddress.ip_network(SANDBOX_SUBNET).hosts()][8:-4]
_ip_in_use: set[str] = set()


def _alloc_ip() -> str | None:
    with _ip_lock:
        for ip in _ip_pool:
            if ip not in _ip_in_use:
                _ip_in_use.add(ip)
                return ip
    return None


def _release_ip(ip: str | None):
    if ip is None:
        return
    with _ip_lock:
        _ip_in_use.discard(ip)


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


class BootError(Exception):
    pass


class MicroVM:
    """One Firecracker-backed session: a fresh TAP device, a fresh copy
    of the golden rootfs, a fresh ephemeral SSH keypair delivered via
    MMDS, and the firecracker process itself. Nothing here is shared
    across sessions or reused between them."""

    def __init__(self):
        self.session_id = uuid.uuid4().hex[:12]
        self.ip: str | None = None
        self.tap_name = f"fc-tap-{self.session_id[:8]}"
        self.session_dir = os.path.join(SESSION_DIR, self.session_id)
        self.rootfs_path = os.path.join(self.session_dir, "rootfs.ext4")
        self.api_sock = os.path.join(self.session_dir, "api.sock")
        self.private_key_path = os.path.join(self.session_dir, "id_ed25519")
        self.public_key_text = ""
        self.proc: subprocess.Popen | None = None

    def _api(self, method: str, path: str, body: dict | None = None):
        conn = HTTPConnection("localhost", timeout=5)
        conn.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        conn.sock.connect(self.api_sock)
        data = json.dumps(body).encode() if body is not None else None
        conn.request(method, path, body=data, headers={"Content-Type": "application/json"})
        resp = conn.getresponse()
        raw = resp.read()
        conn.close()
        if resp.status >= 300:
            raise BootError(f"Firecracker API {method} {path} -> {resp.status}: {raw!r}")
        return raw

    def boot(self):
        os.makedirs(self.session_dir, mode=0o700, exist_ok=True)

        self.ip = _alloc_ip()
        if self.ip is None:
            raise BootError("No sandbox IP available (concurrent session limit reached)")

        # A fresh copy per session -- the golden image is never itself
        # written to, so two sessions can never see or corrupt each
        # other's state. Reflink/CoW isn't available on ext4, so this
        # is a real, unavoidable 768MB copy -- bounded by
        # TERMINAL_BOOT_TIMEOUT_S same as everything else in boot().
        shutil.copyfile(GOLDEN_ROOTFS, self.rootfs_path)

        subprocess.run(["ssh-keygen", "-t", "ed25519", "-N", "", "-q",
                         "-f", self.private_key_path], check=True)
        with open(self.private_key_path + ".pub") as f:
            self.public_key_text = f.read().strip()
        os.chmod(self.private_key_path, 0o600)

        subprocess.run(["ip", "tuntap", "add", self.tap_name, "mode", "tap"], check=True)
        subprocess.run(["ip", "link", "set", self.tap_name, "master", FCBR], check=True)
        subprocess.run(["ip", "link", "set", self.tap_name, "up"], check=True)

        if os.path.exists(self.api_sock):
            os.remove(self.api_sock)
        self.proc = subprocess.Popen(
            [FC_BIN, "--api-sock", self.api_sock],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            start_new_session=True,
        )

        # The API socket takes a moment to appear after the process
        # starts -- poll for it rather than a blind sleep.
        deadline = time.monotonic() + 5
        while not os.path.exists(self.api_sock):
            if time.monotonic() > deadline:
                raise BootError("Firecracker API socket never appeared")
            time.sleep(0.05)

        gw = str(list(ipaddress.ip_network(SANDBOX_SUBNET).hosts())[0])
        prefixlen = ipaddress.ip_network(SANDBOX_SUBNET).prefixlen
        mask = str(ipaddress.ip_network(f"0.0.0.0/{prefixlen}").netmask)
        # dns0-ip=$gw -- the coordinator's own dnsmasq -- lets the
        # kernel's own IP-Config write a working /etc/resolv.conf at
        # boot, same as confirmed live in the Stage A smoke test.
        boot_args = (f"console=ttyS0 reboot=k panic=1 pci=off "
                     f"ip={self.ip}::{gw}:{mask}::eth0:off:{gw}")
        self._api("PUT", "/boot-source", {
            "kernel_image_path": KERNEL_PATH,
            "boot_args": boot_args,
        })
        self._api("PUT", "/drives/rootfs", {
            "drive_id": "rootfs",
            "path_on_host": self.rootfs_path,
            "is_root_device": True,
            "is_read_only": False,
        })
        self._api("PUT", "/network-interfaces/eth0", {
            "iface_id": "eth0",
            "guest_mac": "AA:FC:00:00:00:01",
            "host_dev_name": self.tap_name,
        })
        self._api("PUT", "/machine-config", {
            "vcpu_count": VCPU_COUNT,
            "mem_size_mib": MEM_SIZE_MIB,
        })
        # MMDS -- the actual per-session credential delivery. V1 (no
        # session-token dance), matching fetch-mmds-key.sh's own guest-
        # side V1 GET. The private key is generated above and never
        # written anywhere but this session's own directory -- only
        # the PUBLIC half goes into MMDS.
        self._api("PUT", "/mmds/config", {
            "version": "V1",
            "network_interfaces": ["eth0"],
        })
        self._api("PUT", "/mmds", {
            "latest": {"meta-data": {"public-key": self.public_key_text}}
        })
        self._api("PUT", "/actions", {"action_type": "InstanceStart"})

        self._wait_for_ssh()

    def _wait_for_ssh(self):
        deadline = time.monotonic() + TERMINAL_BOOT_TIMEOUT_S
        while time.monotonic() < deadline:
            if self.proc.poll() is not None:
                raise BootError("Firecracker process exited during boot")
            try:
                with socket.create_connection((self.ip, 22), timeout=0.5):
                    return
            except OSError:
                time.sleep(0.2)
        raise BootError(f"Guest did not become SSH-reachable within {TERMINAL_BOOT_TIMEOUT_S}s")

    def ssh_connect(self) -> tuple[paramiko.SSHClient, paramiko.Channel]:
        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        client.connect(
            hostname=self.ip, port=22, username="student",
            key_filename=self.private_key_path,
            timeout=10, banner_timeout=10,
        )
        transport = client.get_transport()
        channel = transport.open_session()
        channel.get_pty(term="xterm-256color", width=_COLS_DEFAULT, height=_ROWS_DEFAULT)
        channel.invoke_shell()
        return client, channel

    def teardown(self):
        if self.proc is not None and self.proc.poll() is None:
            try:
                self.proc.send_signal(signal.SIGTERM)
                self.proc.wait(timeout=3)
            except Exception:
                try:
                    self.proc.kill()
                except Exception:
                    pass
        try:
            subprocess.run(["ip", "link", "del", self.tap_name],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception:
            pass
        try:
            shutil.rmtree(self.session_dir, ignore_errors=True)
        except Exception:
            pass
        _release_ip(self.ip)


async def _terminal_handler(websocket):
    ip = _client_ip(websocket)

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

    vm = MicroVM()
    loop = asyncio.get_event_loop()
    client = channel = None
    try:
        await send({"type": "output", "data": "Booting a fresh sandboxed shell...\r\n"})
        try:
            await loop.run_in_executor(None, vm.boot)
        except BootError as e:
            await send({"type": "error", "data": f"Sandbox failed to start: {e}"})
            return

        try:
            client, channel = await loop.run_in_executor(None, vm.ssh_connect)
        except Exception as e:
            await send({"type": "error", "data": f"Could not connect to sandbox: {e}"})
            return

        with _client_lock:
            _client_vm[ip] = vm

        preview_note = (
            f"Ports {', '.join(str(p) for p in PREVIEW_PORTS)} are reachable from your browser -- "
            f"start a web server on any of them and open this same host at that port in a new tab.\r\n"
        ) if PREVIEW_PORTS else ""
        await send({"type": "connected",
                     "data": "Connected. This shell has real internet access and is fully isolated -- "
                             "it cannot reach anything else.\r\n" + preview_note})

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

        async def ssh_reader():
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
                except Exception:
                    break
            stop_event.set()

        reader_task = asyncio.ensure_future(ssh_reader())

        try:
            while not stop_event.is_set():
                now = time.monotonic()
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
    os.makedirs(SESSION_DIR, mode=0o700, exist_ok=True)
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
