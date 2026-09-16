"""Outbound HTTP calls to another host's own peer listener
(api/peer_listener.py) — the pairing bootstrap request, the approval
callback, and (Stage 5) proxied instance CRUD. Built on stdlib
urllib.request rather than adding `requests` as an explicit dependency
(it's only present today as an undeclared transitive dep of
ansible/paramiko — not something to rely on) — same reasoning this
feature already applied to avoid a new crypto library (api/identity.py,
api/peer_crypto.py shell out to ssh-keygen instead).

Short, fixed timeouts throughout: every caller of this module is on a
request path a human is actively waiting on (a pairing click, a
build), and an unreachable peer should fail fast and visibly rather
than hang a request thread.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any, Optional

TIMEOUT = 8


@dataclass
class PeerResponse:
    status: int
    body: Any  # parsed JSON, or {} if the body wasn't valid JSON


class PeerUnreachable(Exception):
    """The peer's listener couldn't be reached at all (connection
    refused/timed out/DNS failure) — distinct from a reachable peer
    returning a non-2xx status, which callers see as a normal
    PeerResponse with that status instead of an exception."""


def _request(method: str, url: str, json_body: Optional[dict], token: Optional[str]) -> PeerResponse:
    data = json.dumps(json_body).encode() if json_body is not None else None
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            raw = resp.read()
            body = json.loads(raw) if raw else {}
            return PeerResponse(status=resp.status, body=body)
    except urllib.error.HTTPError as e:
        raw = e.read()
        try:
            body = json.loads(raw) if raw else {}
        except json.JSONDecodeError:
            body = {}
        return PeerResponse(status=e.code, body=body)
    except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as e:
        raise PeerUnreachable(str(e)) from e


def post(url: str, json_body: dict, token: Optional[str] = None) -> PeerResponse:
    return _request("POST", url, json_body, token)


def get(url: str, token: Optional[str] = None) -> PeerResponse:
    return _request("GET", url, None, token)


def delete(url: str, token: Optional[str] = None) -> PeerResponse:
    return _request("DELETE", url, None, token)
