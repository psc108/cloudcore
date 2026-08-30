"""Security group store — SQLite-backed CRUD."""
from __future__ import annotations

import json
from typing import Optional

import db
from models import SecurityGroup, SecurityGroupStatus


def _from_row(row) -> SecurityGroup:
    sg = SecurityGroup(
        id=row["id"], name=row["name"], description=row["description"],
        vpc_id=row["vpc_id"],
        ingress_rules=json.loads(row["ingress_rules"]),
        egress_rules=json.loads(row["egress_rules"]),
        created_at=row["created_at"],
        tags=json.loads(row["tags"]),
    )
    sg.status = SecurityGroupStatus(row["status"])
    return sg


def list_all() -> list[SecurityGroup]:
    rows = db.get_db().execute(
        "SELECT * FROM security_groups WHERE status != 'deleted'").fetchall()
    return [_from_row(r) for r in rows]


def get(sg_id: str) -> Optional[SecurityGroup]:
    row = db.get_db().execute(
        "SELECT * FROM security_groups WHERE id=? AND status != 'deleted'",
        (sg_id,)).fetchone()
    return _from_row(row) if row else None


def find_by_name(name: str) -> Optional[SecurityGroup]:
    row = db.get_db().execute(
        "SELECT * FROM security_groups WHERE name=? AND status != 'deleted'",
        (name,)).fetchone()
    return _from_row(row) if row else None


def put(sg: SecurityGroup) -> None:
    db.get_db().execute("""INSERT INTO security_groups
        (id,name,description,vpc_id,ingress_rules,egress_rules,status,created_at,tags)
        VALUES (?,?,?,?,?,?,?,?,?)
        ON CONFLICT(id) DO UPDATE SET
            name=excluded.name, description=excluded.description,
            vpc_id=excluded.vpc_id,
            ingress_rules=excluded.ingress_rules,
            egress_rules=excluded.egress_rules,
            status=excluded.status, tags=excluded.tags""",
        (sg.id, sg.name, sg.description, sg.vpc_id,
         json.dumps(sg.ingress_rules), json.dumps(sg.egress_rules),
         sg.status.value, sg.created_at, json.dumps(sg.tags)))
    db.get_db().commit()


def delete(sg_id: str) -> bool:
    c = db.get_db().execute(
        "UPDATE security_groups SET status='deleted' WHERE id=? AND status != 'deleted'",
        (sg_id,))
    db.get_db().commit()
    return c.rowcount > 0
