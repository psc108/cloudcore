"""SQLite CRUD for NFS servers."""
from __future__ import annotations

import json
from typing import Optional

import db
from models import NfsServer, NfsServerStatus


def _from_row(row) -> NfsServer:
    return NfsServer(
        id=row["id"], name=row["name"], vpc_id=row["vpc_id"],
        flavor=row["flavor"], disk_gb=row["disk_gb"],
        status=NfsServerStatus(row["status"]),
        private_ip=row["private_ip"], ssh_host_port=row["ssh_host_port"],
        domain_name=row["domain_name"],
        shares=json.loads(row["shares"]),
        created_at=row["created_at"], tags=json.loads(row["tags"]),
    )


def _upsert(s: NfsServer) -> None:
    db.get_db().execute("""INSERT INTO nfs_servers
        (id,name,vpc_id,flavor,disk_gb,status,private_ip,ssh_host_port,
         domain_name,shares,created_at,tags)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(id) DO UPDATE SET
            name=excluded.name, status=excluded.status,
            private_ip=excluded.private_ip, ssh_host_port=excluded.ssh_host_port,
            domain_name=excluded.domain_name, shares=excluded.shares,
            tags=excluded.tags""",
        (s.id, s.name, s.vpc_id, s.flavor, s.disk_gb, s.status.value,
         s.private_ip, s.ssh_host_port, s.domain_name,
         json.dumps(s.shares), s.created_at, json.dumps(s.tags)))
    db.get_db().commit()


def put(s: NfsServer) -> None:
    _upsert(s)


def get(nfs_id: str) -> Optional[NfsServer]:
    row = db.get_db().execute(
        "SELECT * FROM nfs_servers WHERE id=? AND status != 'deleted'",
        (nfs_id,)).fetchone()
    return _from_row(row) if row else None


def find_by_name(name: str) -> Optional[NfsServer]:
    row = db.get_db().execute(
        "SELECT * FROM nfs_servers WHERE name=? AND status != 'deleted'",
        (name,)).fetchone()
    return _from_row(row) if row else None


def list_all() -> list[NfsServer]:
    rows = db.get_db().execute(
        "SELECT * FROM nfs_servers WHERE status != 'deleted' ORDER BY created_at").fetchall()
    return [_from_row(r) for r in rows]


def delete(nfs_id: str) -> bool:
    c = db.get_db()
    r = c.execute(
        "UPDATE nfs_servers SET status='deleted' WHERE id=? AND status != 'deleted'",
        (nfs_id,))
    c.commit()
    return r.rowcount > 0
