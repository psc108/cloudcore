"""Store for the LLM Performance page's "live deployments" registry —
see db.py's own llm_deployments table comment for why this is separate
from llm_ingestions (the ephemeral ingest-schedule history). Pure CRUD
only, same as store.py's own split from lb.py — resolving a registered
name to a live instance/address and actually polling it for stats is a
network-I/O concern that belongs in the routes layer
(llm_deployments_routes.py), not here.
"""
from __future__ import annotations

import uuid
from typing import Optional

import db
from models import now_iso


def register_deployment(name: str, example: str, port: int, stats_path: str) -> dict:
    """Upsert by name — a redeployed example (same Terraform naming
    convention, different CloudCore instance underneath) re-registering
    just refreshes this row rather than accumulating stale duplicates
    every rebuild. See db.py's own table comment for why no
    instance_id/address is stored here at all."""
    c = db.get_db()
    existing = c.execute("SELECT id FROM llm_deployments WHERE name=?", (name,)).fetchone()
    deployment_id = existing["id"] if existing else str(uuid.uuid4())
    c.execute(
        """INSERT INTO llm_deployments (id, name, example, port, stats_path, registered_at)
           VALUES (?,?,?,?,?,?)
           ON CONFLICT(name) DO UPDATE SET
               example=excluded.example, port=excluded.port,
               stats_path=excluded.stats_path, registered_at=excluded.registered_at""",
        (deployment_id, name, example, port, stats_path, now_iso()))
    c.commit()
    return dict(c.execute("SELECT * FROM llm_deployments WHERE id=?", (deployment_id,)).fetchone())


def list_deployments() -> list[dict]:
    rows = db.get_db().execute(
        "SELECT * FROM llm_deployments ORDER BY registered_at DESC").fetchall()
    return [dict(r) for r in rows]


def get_deployment(deployment_id: str) -> Optional[dict]:
    row = db.get_db().execute(
        "SELECT * FROM llm_deployments WHERE id=?", (deployment_id,)).fetchone()
    return dict(row) if row else None


def delete_deployment(deployment_id: str) -> bool:
    c = db.get_db()
    cur = c.execute("DELETE FROM llm_deployments WHERE id=?", (deployment_id,))
    c.commit()
    return cur.rowcount > 0
