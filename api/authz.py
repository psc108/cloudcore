"""Central, default-deny authorization for CloudCore's API
(cloudcore-auth-Phased-Implementation.md, layer 1: A1-A4).

Before this, each blueprint checked tokens its own way and a route with no
check was simply open: F-201's security-group routes, and (found building
this) the file editor, NFS-server and help-article routes -- reachable from
any web page open in the operator's browser, which can send requests to
127.0.0.1:8080 without reading the replies.

Now every request is identified once, from whichever token it carries:

  admin    the master token (~/.config/cloudcore/api.env), or a named
           admin token from the api_tokens table
  peer     an approved peer's own token
  capture  the llm-chat capture token (env, or a named capture token)
  labvm    the lab-VM broker token (env, or a named labvm token)
  student  a per-student llm-chat client token (llm_client_tokens)

and every route has an allowed set of identities. A route that isn't listed
is **admin-only**: a new route can't ship open by accident. The per-bind
gates in server.py (what the peer and examples listeners may reach) and the
blueprints' own checks stay as a second layer.

A3: every state-changing request (POST/PUT/PATCH/DELETE) is written to the
audit_log table and logged, with the identity, route, status and source.
A2/A4: named tokens -- created (shown once), listed, revoked -- via
/v1/auth/tokens (admin), stored as SHA-256 hashes with optional expiry.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import secrets
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from flask import Blueprint, abort, g, jsonify, request

import cc_token
import db
import peers_store

log = logging.getLogger("cloudcore.audit")
auth_bp = Blueprint("auth", __name__)

SCOPES = ("admin", "peer", "capture", "labvm", "student")

# Routes anyone may call: the dashboard's own files, curated published
# examples, the pairing handshake (which checks its own signed tokens), and
# the VNC WebSocket bridge, which checks its own one-time ticket: browsers
# can't send the API token on a WebSocket, so an authenticated call to
# vnc-ticket issues a 60-second, single-use ticket for one instance (B3).
PUBLIC = {"ui", "ui_vendor", "static", "llm_examples.list_published",
          "peers.pairing_request_bootstrap", "peers.self_info", "peers.complete_pairing",
          "instance_vnc_websocket"}

# Browsers' EventSource can't send headers, so these two log streams take
# ?token= (the dashboard's build logs). Nowhere else.
QUERY_TOKEN_RULES = {"/v1/builds/<build_id>/log", "/v1/tofu/builds/<build_id>/log"}

_SCHEMA = """
CREATE TABLE IF NOT EXISTS api_tokens (
    id           TEXT PRIMARY KEY,
    name         TEXT NOT NULL,
    scope        TEXT NOT NULL,
    token_sha256 TEXT NOT NULL UNIQUE,
    created_at   TEXT NOT NULL,
    expires_at   TEXT,
    revoked_at   TEXT,
    last_used_at TEXT
);
CREATE TABLE IF NOT EXISTS audit_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          TEXT NOT NULL,
    identity    TEXT NOT NULL,
    identity_id TEXT NOT NULL,
    method      TEXT NOT NULL,
    path        TEXT NOT NULL,
    endpoint    TEXT NOT NULL,
    status      INTEGER NOT NULL,
    source      TEXT NOT NULL
);
"""

_access: dict[str, frozenset] = {}


@dataclass(frozen=True)
class Identity:
    scope: str
    id: str
    name: str


def configure(peer_endpoints: set, capture_endpoints: set, student_endpoints: set, labvm_endpoints: set,
              lab_or_admin_endpoints: set = frozenset()) -> None:
    """Each route's allowed identities; anything unlisted is admin-only.
    lab_or_admin: the LFS build's journal and queue (lfs_build.py), written by
    the llm-chat coordinator with its lab token and read and annotated from
    the dashboard."""
    for ep in peer_endpoints - PUBLIC:
        _access[ep] = frozenset({"admin", "peer"})
    for ep in capture_endpoints - PUBLIC:
        _access[ep] = frozenset({"admin", "capture"})
    for ep in student_endpoints - PUBLIC:
        _access[ep] = frozenset({"student"})
    for ep in labvm_endpoints - PUBLIC:
        _access[ep] = frozenset({"labvm"})
    for ep in lab_or_admin_endpoints - PUBLIC:
        _access[ep] = frozenset({"admin", "labvm"})


def allowed(endpoint: str) -> frozenset | None:
    """None means public."""
    if endpoint in PUBLIC:
        return None
    return _access.get(endpoint, frozenset({"admin"}))


def _sha(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _conn():
    conn = db.get_db()
    conn.executescript(_SCHEMA)
    return conn


def _now() -> datetime:
    return datetime.now(timezone.utc)


def identify(token: str) -> Identity | None:
    if not token:
        return None
    if hmac.compare_digest(token, cc_token.master_token()):
        return Identity("admin", "master", "master token (api.env)")
    row = _conn().execute("SELECT * FROM api_tokens WHERE token_sha256 = ? AND revoked_at IS NULL",
                          (_sha(token),)).fetchone()
    if row and (not row["expires_at"] or datetime.fromisoformat(row["expires_at"]) > _now()):
        conn = _conn()
        conn.execute("UPDATE api_tokens SET last_used_at = ? WHERE id = ?", (_now().isoformat(), row["id"]))
        conn.commit()
        return Identity(row["scope"], row["id"], row["name"])
    peer = peers_store.find_peer_by_local_token(token)
    if peer:
        return Identity("peer", peer["id"], peer.get("hostname") or peer["id"])
    for scope, value in (("capture", cc_token.examples_token()), ("labvm", cc_token.labvm_token())):
        if value and hmac.compare_digest(token, value):
            return Identity(scope, f"{scope}-env", f"{scope} token (api.env)")
    student = _conn().execute(
        "SELECT id, label FROM llm_client_tokens WHERE token_sha256 = ? AND revoked_at IS NULL",
        (_sha(token),)).fetchone()
    if student:
        return Identity("student", student["id"], student["label"])
    return None


def _request_token() -> str:
    auth = request.headers.get("Authorization", "")
    token = auth.removeprefix("Bearer ").strip() if auth.startswith("Bearer ") else ""
    if not token and request.method == "GET" and request.url_rule is not None \
            and request.url_rule.rule in QUERY_TOKEN_RULES:
        token = request.args.get("token", "")
    return token


def gate():
    """app.before_request: identify the caller and enforce the route's set."""
    if request.endpoint is None:
        return None  # 404/405: Flask answers those itself
    if request.method == "OPTIONS":
        # CORS preflight: browsers send it without credentials and abandon
        # the real request on anything but 2xx (F-209). Flask answers it
        # itself without running the route -- no route handles OPTIONS --
        # so letting it through grants nothing.
        return None
    need = allowed(request.endpoint)
    ident = identify(_request_token())
    g.identity = ident
    if need is None:
        return None
    if ident is None:
        return jsonify({"status": 401, "title": "Unauthorized",
                        "detail": "a valid API token is required for this route"}), 401
    if ident.scope not in need:
        return jsonify({"status": 403, "title": "Forbidden",
                        "detail": f"a {ident.scope} token can't call this route"}), 403
    return None


def audit(response):
    """app.after_request: record every state-changing request."""
    if request.method in ("POST", "PUT", "PATCH", "DELETE") and request.endpoint not in (None, "static"):
        ident = getattr(g, "identity", None)
        who, wid = (ident.scope, ident.id) if ident else ("anonymous", "")
        try:
            conn = _conn()
            conn.execute("INSERT INTO audit_log (ts, identity, identity_id, method, path, endpoint, status, source) "
                         "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                         (_now().isoformat(), who, wid, request.method, request.path[:300], request.endpoint,
                          response.status_code, request.remote_addr or ""))
            conn.commit()
        except Exception:  # noqa: BLE001 -- auditing must never break a request
            log.exception("audit write failed")
        log.info("AUDIT %s %s %s by %s:%s -> %s from %s", request.method, request.path, request.endpoint,
                 who, wid, response.status_code, request.remote_addr)
    return response


# --- A2/A4: named tokens (admin only, via the default) ----------------------

@auth_bp.get("/v1/auth/tokens")
def list_tokens():
    rows = _conn().execute("SELECT id, name, scope, created_at, expires_at, revoked_at, last_used_at "
                           "FROM api_tokens ORDER BY created_at DESC").fetchall()
    return jsonify({"items": [dict(r) for r in rows]})


@auth_bp.post("/v1/auth/tokens")
def create_token():
    body = request.get_json(force=True, silent=True) or {}
    name, scope = str(body.get("name") or "").strip(), body.get("scope")
    if not name or len(name) > 80:
        abort(400, "name is required (at most 80 characters)")
    if scope not in ("admin", "capture", "labvm"):
        return jsonify({"status": 400, "title": "Bad Request",
                        "detail": "scope must be admin, capture or labvm (peer and student tokens have their own flows)"}), 400
    days = body.get("expires_days")
    expires = (_now() + timedelta(days=int(days))).isoformat() if days else None
    token = f"cc{scope[0]}_" + secrets.token_urlsafe(32)
    tid = uuid.uuid4().hex[:12]
    conn = _conn()
    conn.execute("INSERT INTO api_tokens (id, name, scope, token_sha256, created_at, expires_at) VALUES (?, ?, ?, ?, ?, ?)",
                 (tid, name, scope, _sha(token), _now().isoformat(), expires))
    conn.commit()
    # Shown once; only the hash is kept.
    return jsonify({"id": tid, "name": name, "scope": scope, "expires_at": expires, "token": token}), 201


@auth_bp.delete("/v1/auth/tokens/<tid>")
def revoke_token(tid):
    conn = _conn()
    cur = conn.execute("UPDATE api_tokens SET revoked_at = ? WHERE id = ? AND revoked_at IS NULL",
                       (_now().isoformat(), tid))
    conn.commit()
    return ("", 204) if cur.rowcount else (jsonify({"status": 404, "title": "Not Found"}), 404)


@auth_bp.get("/v1/auth/audit")
def list_audit():
    limit = min(int(request.args.get("limit", 100)), 1000)
    rows = _conn().execute("SELECT * FROM audit_log ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    return jsonify({"items": [dict(r) for r in rows]})
