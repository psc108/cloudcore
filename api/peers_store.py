"""Cross-host peering store — SQLite-backed CRUD for the `peers` and
`pairing_requests` tables (api/db.py). Plain-dict rows (sqlite3.Row ->
dict), not a models.py dataclass — these aren't Terraform-style
resources with their own lifecycle validation, just handshake/session
state peers_routes.py reads and writes directly.
"""
from __future__ import annotations

from typing import Optional

import db
from models import new_id, now_iso

# Columns never returned by a to_public_dict() call — the whole point
# of a per-peer token is that only the two hosts on either end of one
# specific pairing ever see it; leaking it through GET /v1/peers (the
# dashboard's own list view) would defeat that.
_SECRET_COLUMNS = {"local_token", "remote_token"}


def _row_to_dict(row) -> dict:
    return dict(row)


def to_public_dict(row: dict) -> dict:
    return {k: v for k, v in row.items() if k not in _SECRET_COLUMNS}


# --- peers ---

def list_peers(status: Optional[str] = None) -> list[dict]:
    if status:
        rows = db.get_db().execute(
            "SELECT * FROM peers WHERE status=? ORDER BY created_at", (status,)).fetchall()
    else:
        rows = db.get_db().execute("SELECT * FROM peers ORDER BY created_at").fetchall()
    return [_row_to_dict(r) for r in rows]


def get_peer(peer_id: str) -> Optional[dict]:
    row = db.get_db().execute("SELECT * FROM peers WHERE id=?", (peer_id,)).fetchone()
    return _row_to_dict(row) if row else None


def find_peer_by_local_token(token: str) -> Optional[dict]:
    """Resolve an inbound request's bearer token to the peer it belongs
    to — used by peers_routes.py's peer-auth check. Only ever matches
    an approved peer; a revoked peer's local_token was cleared."""
    row = db.get_db().execute(
        "SELECT * FROM peers WHERE local_token=? AND status='approved'", (token,)).fetchone()
    return _row_to_dict(row) if row else None


def find_pending_outbound_by_local_token(token: str) -> Optional[dict]:
    """The other half of the handshake's token check: does `token`
    match a peers row WE created as an outbound pairing attempt,
    still awaiting the target's POST /v1/peers/complete callback?"""
    row = db.get_db().execute(
        "SELECT * FROM peers WHERE local_token=? AND status='pending_outbound'", (token,)).fetchone()
    return _row_to_dict(row) if row else None


def insert_peer(**fields) -> dict:
    fields.setdefault("id", new_id())
    fields.setdefault("created_at", now_iso())
    fields.setdefault("status", "pending_outbound")
    fields.setdefault("wg_tunnel_status", "unknown")
    cols = list(fields.keys())
    placeholders = ",".join("?" for _ in cols)
    c = db.get_db()
    c.execute(f"INSERT INTO peers ({','.join(cols)}) VALUES ({placeholders})",
              [fields[k] for k in cols])
    c.commit()
    return get_peer(fields["id"])


def update_peer(peer_id: str, **fields) -> Optional[dict]:
    if not fields:
        return get_peer(peer_id)
    set_clause = ",".join(f"{k}=?" for k in fields)
    c = db.get_db()
    c.execute(f"UPDATE peers SET {set_clause} WHERE id=?", [*fields.values(), peer_id])
    c.commit()
    return get_peer(peer_id)


def revoke_peer(peer_id: str) -> Optional[dict]:
    return update_peer(peer_id, status="revoked", local_token=None, remote_token=None)


# --- pairing_requests ---

def list_pairing_requests(status: Optional[str] = None) -> list[dict]:
    if status:
        rows = db.get_db().execute(
            "SELECT * FROM pairing_requests WHERE status=? ORDER BY created_at",
            (status,)).fetchall()
    else:
        rows = db.get_db().execute(
            "SELECT * FROM pairing_requests ORDER BY created_at").fetchall()
    return [_row_to_dict(r) for r in rows]


def get_pairing_request(request_id: str) -> Optional[dict]:
    row = db.get_db().execute(
        "SELECT * FROM pairing_requests WHERE id=?", (request_id,)).fetchone()
    return _row_to_dict(row) if row else None


def count_pending_pairing_requests() -> int:
    row = db.get_db().execute(
        "SELECT COUNT(*) AS n FROM pairing_requests WHERE status='pending'").fetchone()
    return row["n"]


def expire_stale_pairing_requests() -> None:
    """Opportunistic sweep — called before accepting a new bootstrap
    request, not on a timer. Cheap (one UPDATE) and keeps
    count_pending_pairing_requests()'s abuse-prevention cap from being
    permanently eaten by requests nobody ever acted on."""
    c = db.get_db()
    c.execute(
        "UPDATE pairing_requests SET status='expired' "
        "WHERE status='pending' AND expires_at < ?", (now_iso(),))
    c.commit()


def insert_pairing_request(**fields) -> dict:
    fields.setdefault("id", new_id())
    fields.setdefault("created_at", now_iso())
    fields.setdefault("status", "pending")
    cols = list(fields.keys())
    placeholders = ",".join("?" for _ in cols)
    c = db.get_db()
    c.execute(f"INSERT INTO pairing_requests ({','.join(cols)}) VALUES ({placeholders})",
              [fields[k] for k in cols])
    c.commit()
    return get_pairing_request(fields["id"])


def update_pairing_request(request_id: str, **fields) -> Optional[dict]:
    if not fields:
        return get_pairing_request(request_id)
    set_clause = ",".join(f"{k}=?" for k in fields)
    c = db.get_db()
    c.execute(f"UPDATE pairing_requests SET {set_clause} WHERE id=?",
              [*fields.values(), request_id])
    c.commit()
    return get_pairing_request(request_id)
