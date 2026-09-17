"""Queue of failed builds' own logs, waiting for the next llm_ingest
scheduler wakeup to analyze — see api/scheduler.py's own
_run_llm_ingest_schedule. Deliberately not engine-specific: both
api/build_engine.py (Ansible) and api/tofu_engine.py (OpenTofu) queue
into the same table, since what happens to a queued row afterwards
(read it, draft a Finding, delete it) has nothing to do with which
engine produced it.
"""
from __future__ import annotations

import json
import uuid

import db
from models import now_iso


def _redact(var_overrides: dict) -> dict:
    """Strip anything token-shaped before it's stored or ever sent to
    a model/peer — same convention the dashboard's own var-form
    already uses to decide which fields render as password inputs."""
    return {
        k: ("***redacted***" if "token" in k.lower() else v)
        for k, v in var_overrides.items()
    }


def queue_failure(engine: str, build_id: str, template: str,
                   var_overrides: dict, log_lines: list[str], exit_code) -> None:
    c = db.get_db()
    c.execute(
        """INSERT INTO failed_build_logs
           (id, engine, build_id, template, var_overrides, log, exit_code, created_at)
           VALUES (?,?,?,?,?,?,?,?)""",
        (str(uuid.uuid4()), engine, build_id, template,
         json.dumps(_redact(var_overrides)), json.dumps(log_lines),
         exit_code, now_iso()))
    c.commit()


def list_pending(limit: int = 20) -> list[dict]:
    """Oldest first — a backlog of failures should get analyzed in the
    order they happened, not last-in-first-out."""
    rows = db.get_db().execute(
        "SELECT * FROM failed_build_logs ORDER BY created_at ASC LIMIT ?",
        (limit,)).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["var_overrides"] = json.loads(d.get("var_overrides") or "{}")
        d["log"] = json.loads(d.get("log") or "[]")
        out.append(d)
    return out


def delete(queue_id: str) -> None:
    """Called once a queued failure has been turned into a real
    Finding — that Finding is the permanent record from here on, not
    this row (see this module's own docstring)."""
    c = db.get_db()
    c.execute("DELETE FROM failed_build_logs WHERE id=?", (queue_id,))
    c.commit()
