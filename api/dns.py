from __future__ import annotations

import uuid
from typing import Optional

import db
from models import now_iso

BUILTIN_ZONES = ("instances.cloudcore.local", "lb.cloudcore.local")


def load() -> None:
    """Ensure built-in zones exist. Called at startup."""
    c = db.get_db()
    for z in BUILTIN_ZONES:
        c.execute("INSERT OR IGNORE INTO dns_zones (name,created_at,builtin) VALUES (?,?,1)",
                  (z, now_iso()))
    c.commit()


# ---------------------------------------------------------------------------
# Zones
# ---------------------------------------------------------------------------

def list_zones() -> list[dict]:
    rows = db.get_db().execute("SELECT * FROM dns_zones ORDER BY name").fetchall()
    result = []
    for r in rows:
        count = db.get_db().execute(
            "SELECT COUNT(*) FROM dns_records WHERE zone_name=?", (r["name"],)).fetchone()[0]
        result.append({
            "name": r["name"], "created_at": r["created_at"],
            "record_count": count, "builtin": bool(r["builtin"]),
        })
    return result


def get_zone(name: str) -> Optional[dict]:
    row = db.get_db().execute("SELECT * FROM dns_zones WHERE name=?", (name,)).fetchone()
    return dict(row) if row else None


def create_zone(name: str) -> dict:
    if get_zone(name):
        raise ValueError(f"Zone '{name}' already exists")
    ts = now_iso()
    db.get_db().execute("INSERT INTO dns_zones (name,created_at,builtin) VALUES (?,?,0)", (name, ts))
    db.get_db().commit()
    return {"name": name, "created_at": ts, "record_count": 0, "builtin": False}


def delete_zone(name: str) -> bool:
    c = db.get_db().execute("DELETE FROM dns_zones WHERE name=?", (name,))
    db.get_db().commit()
    return c.rowcount > 0


# ---------------------------------------------------------------------------
# Records
# ---------------------------------------------------------------------------

def list_records(zone: str) -> list[dict]:
    rows = db.get_db().execute(
        "SELECT * FROM dns_records WHERE zone_name=? ORDER BY name", (zone,)).fetchall()
    return [dict(r) for r in rows]


def upsert_record(zone: str, name: str, rtype: str, value: str,
                  ttl: int = 300, resource_type: str = "manual",
                  resource_id: str = "") -> dict:
    if not get_zone(zone):
        raise ValueError(f"Zone '{zone}' not found")
    rtype = rtype.upper()
    fqdn = f"{name}.{zone}"
    existing = db.get_db().execute(
        "SELECT created_at FROM dns_records WHERE zone_name=? AND name=? AND type=?",
        (zone, name, rtype)).fetchone()
    created_at = existing["created_at"] if existing else now_iso()
    rec_id = str(uuid.uuid4()) if not existing else db.get_db().execute(
        "SELECT id FROM dns_records WHERE zone_name=? AND name=? AND type=?",
        (zone, name, rtype)).fetchone()["id"]
    db.get_db().execute("""INSERT INTO dns_records
        (id,zone_name,name,fqdn,type,value,ttl,resource_type,resource_id,created_at)
        VALUES (?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(zone_name,name,type) DO UPDATE SET
            value=excluded.value, ttl=excluded.ttl,
            resource_type=excluded.resource_type, resource_id=excluded.resource_id,
            fqdn=excluded.fqdn""",
        (rec_id, zone, name, fqdn, rtype, value, ttl, resource_type, resource_id, created_at))
    db.get_db().commit()
    return {"id": rec_id, "zone_name": zone, "name": name, "fqdn": fqdn,
            "type": rtype, "value": value, "ttl": ttl,
            "resource_type": resource_type, "resource_id": resource_id,
            "created_at": created_at}


def delete_record(zone: str, name: str, rtype: str) -> bool:
    c = db.get_db().execute(
        "DELETE FROM dns_records WHERE zone_name=? AND name=? AND type=?",
        (zone, name, rtype.upper()))
    db.get_db().commit()
    return c.rowcount > 0


def delete_records_for_resource(resource_id: str) -> None:
    db.get_db().execute("DELETE FROM dns_records WHERE resource_id=?", (resource_id,))
    db.get_db().commit()
