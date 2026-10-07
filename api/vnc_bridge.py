"""A minimal WebSocket <-> TCP bridge for the dashboard's VNC viewer
(lfs-os-Phased-Implementation.md, B3).

noVNC in the browser speaks RFB over a WebSocket; QEMU's VNC display listens
on the host's loopback. The API runs on Werkzeug's own server, which has no
WebSocket support, and the hosts' system Python has no WebSocket package, so
this does the RFC 6455 part itself: the handshake, then binary frames in both
directions (client frames are masked, ours are not), answering pings and
honouring close. Standard library only.

serve() takes over the request's raw socket and returns when either side
closes; the caller ends the HTTP request without writing anything more.
"""

from __future__ import annotations

import base64
import hashlib
import select
import socket
import struct

_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
_OP_TEXT, _OP_BINARY, _OP_CLOSE, _OP_PING, _OP_PONG = 0x1, 0x2, 0x8, 0x9, 0xA
_MAX_FRAME = 16 << 20


def _accept_key(key: str) -> str:
    return base64.b64encode(hashlib.sha1((key + _GUID).encode()).digest()).decode()


def _frame(op: int, payload: bytes) -> bytes:
    n = len(payload)
    head = bytes([0x80 | op])
    if n < 126:
        head += bytes([n])
    elif n < 1 << 16:
        head += bytes([126]) + struct.pack(">H", n)
    else:
        head += bytes([127]) + struct.pack(">Q", n)
    return head + payload


class _Reader:
    """Reassembles masked client frames from whatever recv() hands over."""

    def __init__(self) -> None:
        self.buf = b""
        self.fragments: list[bytes] = []

    def feed(self, data: bytes) -> list[tuple[int, bytes]]:
        self.buf += data
        out = []
        while True:
            if len(self.buf) < 2:
                return out
            b0, b1 = self.buf[0], self.buf[1]
            fin, op, masked, n = b0 & 0x80, b0 & 0x0F, b1 & 0x80, b1 & 0x7F
            pos = 2
            if n == 126:
                if len(self.buf) < 4:
                    return out
                n, pos = struct.unpack(">H", self.buf[2:4])[0], 4
            elif n == 127:
                if len(self.buf) < 10:
                    return out
                n, pos = struct.unpack(">Q", self.buf[2:10])[0], 10
            if n > _MAX_FRAME:
                raise ValueError("frame too large")
            mask = b""
            if masked:
                if len(self.buf) < pos + 4:
                    return out
                mask, pos = self.buf[pos:pos + 4], pos + 4
            if len(self.buf) < pos + n:
                return out
            payload = self.buf[pos:pos + n]
            self.buf = self.buf[pos + n:]
            if masked:
                payload = bytes(c ^ mask[i % 4] for i, c in enumerate(payload))
            if op in (_OP_CLOSE, _OP_PING, _OP_PONG):
                out.append((op, payload))
            elif op == 0x0 or not fin:  # fragmented message
                self.fragments.append(payload)
                if fin:
                    out.append((_OP_BINARY, b"".join(self.fragments)))
                    self.fragments = []
            else:
                out.append((op, payload))


def serve(client: socket.socket, key: str, protocols: str, vnc_port: int) -> None:
    """Handshake on the client's raw socket, then relay until either side closes."""
    proto = "binary" if "binary" in [p.strip() for p in protocols.split(",")] else ""
    client.sendall(("HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                    f"Sec-WebSocket-Accept: {_accept_key(key)}\r\n"
                    + (f"Sec-WebSocket-Protocol: {proto}\r\n" if proto else "") + "\r\n").encode())
    try:
        vnc = socket.create_connection(("127.0.0.1", vnc_port), timeout=10)
    except OSError:
        client.sendall(_frame(_OP_CLOSE, struct.pack(">H", 1011) + b"display unavailable"))
        return
    vnc.settimeout(None)
    client.settimeout(None)
    reader = _Reader()
    try:
        while True:
            ready, _, _ = select.select([client, vnc], [], [], 300)
            if not ready:
                continue
            if vnc in ready:
                data = vnc.recv(1 << 16)
                if not data:
                    client.sendall(_frame(_OP_CLOSE, struct.pack(">H", 1000)))
                    return
                client.sendall(_frame(_OP_BINARY, data))
            if client in ready:
                data = client.recv(1 << 16)
                if not data:
                    return
                for op, payload in reader.feed(data):
                    if op == _OP_CLOSE:
                        client.sendall(_frame(_OP_CLOSE, payload[:2]))
                        return
                    if op == _OP_PING:
                        client.sendall(_frame(_OP_PONG, payload))
                    elif op in (_OP_BINARY, _OP_TEXT):
                        vnc.sendall(payload)
    except (OSError, ValueError):
        return
    finally:
        vnc.close()
