"""
terminal.py — WebSocket SSH proxy for CloudCore instances.

Listens on ws://127.0.0.1:8081/terminal?instance_id=<id>

Protocol (text frames):
  browser → server:  {"type":"input","data":"<chars>"}
                      {"type":"resize","cols":<n>,"rows":<n>}
  server → browser:  {"type":"output","data":"<chars>"}
                      {"type":"error","data":"<message>"}
                      {"type":"connected","data":"<message>"}
"""
from __future__ import annotations

import asyncio
import json
import os
import select
import sys
import threading
from pathlib import Path

import paramiko
import websockets
import websockets.legacy.server

# Import store/compute from the same directory
sys.path.insert(0, str(Path(__file__).parent))
import db
import store
import compute

WS_HOST = "127.0.0.1"
WS_PORT = 8081
_COLS_DEFAULT = 220
_ROWS_DEFAULT = 50


def _pick_user(instance) -> tuple[str, bool]:
    """
    Return (username, is_sudo) for the terminal session.
    Prefer the first non-sudo user. Fall back to the default ssh_user
    (which is the distro default — ubuntu/debian/rocky — and is NOT
    in instance.users, so it has no sudo entry we created).
    Returns (None, False) if only sudo users exist and no default is safe.
    """
    users = instance.users or []
    # First: any non-sudo user we explicitly created
    for u in users:
        if not u.get("sudo", False):
            return u["username"], False
    # Second: the distro default user (ubuntu/debian/rocky) — never has sudo
    # from our cloud-init (we don't grant it sudo unless asked)
    if instance.ssh_user:
        return instance.ssh_user, False
    return None, False


def _ssh_connect(instance) -> tuple[paramiko.SSHClient, paramiko.Channel, str]:
    """Open an SSH connection and return (client, channel, username)."""
    username, _ = _pick_user(instance)
    if username is None:
        raise RuntimeError("NO_NONSUDO_USER")

    key_path = compute.get_cc_privkey_path()
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    client.connect(
        hostname="127.0.0.1",
        port=instance.ssh_host_port,
        username=username,
        key_filename=key_path,
        timeout=10,
        banner_timeout=10,
    )
    transport = client.get_transport()
    channel = transport.open_session()
    channel.get_pty(
        term="xterm-256color",
        width=_COLS_DEFAULT,
        height=_ROWS_DEFAULT,
    )
    channel.invoke_shell()
    return client, channel, username


async def _terminal_handler(websocket):
    """Handle one WebSocket connection → one SSH shell session."""
    # Parse instance_id from query string
    path = websocket.path  # e.g. /terminal?instance_id=abc
    instance_id = ""
    if "?" in path:
        qs = path.split("?", 1)[1]
        for part in qs.split("&"):
            if part.startswith("instance_id="):
                instance_id = part.split("=", 1)[1]

    instance = store.get_instance(instance_id)

    async def send(msg: dict):
        try:
            await websocket.send(json.dumps(msg))
        except Exception:
            pass

    if not instance:
        await send({"type": "error", "data": f"Instance '{instance_id}' not found."})
        return

    if instance.status.value != "running":
        await send({"type": "error", "data": f"Instance is {instance.status.value}, not running."})
        return

    if not instance.ssh_host_port:
        await send({"type": "error", "data": "No SSH port available for this instance."})
        return

    # Check for non-sudo user
    username, _ = _pick_user(instance)
    users = instance.users or []
    all_sudo = users and all(u.get("sudo", False) for u in users)
    if all_sudo and not instance.ssh_user:
        await send({
            "type": "error",
            "data": (
                "⚠ All users on this instance have sudo privileges.\n"
                "Please create a non-sudo user first via the Users panel\n"
                "before opening a terminal session."
            ),
        })
        return

    # Connect SSH in a thread (paramiko is blocking)
    loop = asyncio.get_event_loop()
    try:
        client, channel, username = await loop.run_in_executor(
            None, lambda: _ssh_connect(instance)
        )
    except RuntimeError as e:
        if "NO_NONSUDO_USER" in str(e):
            await send({
                "type": "error",
                "data": (
                    "⚠ No non-sudo user found on this instance.\n"
                    "Please add a non-sudo user via the Users panel\n"
                    "before opening a terminal session."
                ),
            })
        else:
            await send({"type": "error", "data": f"SSH connection failed: {e}"})
        return
    except Exception as e:
        await send({"type": "error", "data": f"SSH connection failed: {e}"})
        return

    await send({"type": "connected", "data": f"Connected as {username}@{instance.name}\r\n"})

    # Reader thread: SSH → WebSocket
    stop_event = threading.Event()

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

    # Main loop: WebSocket → SSH
    try:
        async for message in websocket:
            if stop_event.is_set():
                break
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
        try:
            channel.close()
            client.close()
        except Exception:
            pass


async def _main():
    db.init()
    async with websockets.legacy.server.serve(_terminal_handler, WS_HOST, WS_PORT):
        print(f"Terminal WS server on ws://{WS_HOST}:{WS_PORT}", flush=True)
        await asyncio.Future()  # run forever


def run():
    asyncio.run(_main())


if __name__ == "__main__":
    run()
