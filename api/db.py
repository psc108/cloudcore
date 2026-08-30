"""Central SQLite database for CloudCore.

All modules import `get_db()` to get a connection.  The DB is opened once at
startup (WAL mode, foreign-keys on) and shared via a module-level handle.
"""
from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path

_DB_FILE = Path(__file__).parent / "cloudcore.db"
_conn: sqlite3.Connection | None = None
_lock = threading.Lock()

_SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS security_groups (
    id              TEXT PRIMARY KEY,
    name            TEXT NOT NULL,
    description     TEXT NOT NULL DEFAULT '',
    vpc_id          TEXT NOT NULL DEFAULT '',
    ingress_rules   TEXT NOT NULL DEFAULT '[]',
    egress_rules    TEXT NOT NULL DEFAULT '[]',
    status          TEXT NOT NULL DEFAULT 'active',
    created_at      TEXT NOT NULL,
    tags            TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS vpcs (
    id          TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    cidr_block  TEXT NOT NULL,
    dns_support INTEGER NOT NULL DEFAULT 1,
    status      TEXT NOT NULL DEFAULT 'active',
    created_at  TEXT NOT NULL,
    tags        TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS instances (
    id                  TEXT PRIMARY KEY,
    name                TEXT NOT NULL,
    image_id            TEXT NOT NULL,
    flavor              TEXT NOT NULL,
    vpc_id              TEXT NOT NULL,
    subnet_id           TEXT NOT NULL,
    security_group_ids  TEXT NOT NULL DEFAULT '[]',
    user_data           TEXT,
    private_ip          TEXT NOT NULL DEFAULT '',
    public_ip           TEXT NOT NULL DEFAULT '',
    status              TEXT NOT NULL DEFAULT 'pending',
    created_at          TEXT NOT NULL,
    tags                TEXT NOT NULL DEFAULT '{}',
    domain_name         TEXT NOT NULL DEFAULT '',
    ssh_host_port       INTEGER NOT NULL DEFAULT 0,
    ssh_user            TEXT NOT NULL DEFAULT 'ubuntu',
    users               TEXT NOT NULL DEFAULT '[]'
);

CREATE TABLE IF NOT EXISTS load_balancers (
    id          TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    type        TEXT NOT NULL DEFAULT 'application',
    vpc_id      TEXT NOT NULL DEFAULT '',
    subnet_ids  TEXT NOT NULL DEFAULT '[]',
    internal    INTEGER NOT NULL DEFAULT 0,
    dns_name    TEXT NOT NULL DEFAULT '',
    listen_port INTEGER NOT NULL DEFAULT 0,
    backends    TEXT NOT NULL DEFAULT '[]',
    listeners   TEXT NOT NULL DEFAULT '[]',
    health_check TEXT NOT NULL DEFAULT '{}',
    status      TEXT NOT NULL DEFAULT 'active',
    created_at  TEXT NOT NULL,
    tags        TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS dns_zones (
    name        TEXT PRIMARY KEY,
    created_at  TEXT NOT NULL,
    builtin     INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS dns_records (
    id              TEXT PRIMARY KEY,
    zone_name       TEXT NOT NULL REFERENCES dns_zones(name) ON DELETE CASCADE,
    name            TEXT NOT NULL,
    fqdn            TEXT NOT NULL,
    type            TEXT NOT NULL,
    value           TEXT NOT NULL,
    ttl             INTEGER NOT NULL DEFAULT 300,
    resource_type   TEXT NOT NULL DEFAULT 'manual',
    resource_id     TEXT NOT NULL DEFAULT '',
    created_at      TEXT NOT NULL,
    UNIQUE(zone_name, name, type)
);

CREATE TABLE IF NOT EXISTS nfs_servers (
    id            TEXT PRIMARY KEY,
    name          TEXT NOT NULL,
    vpc_id        TEXT NOT NULL,
    flavor        TEXT NOT NULL DEFAULT 'standard.medium',
    disk_gb       INTEGER NOT NULL DEFAULT 20,
    status        TEXT NOT NULL DEFAULT 'pending',
    private_ip    TEXT NOT NULL DEFAULT '',
    ssh_host_port INTEGER NOT NULL DEFAULT 0,
    domain_name   TEXT NOT NULL DEFAULT '',
    shares        TEXT NOT NULL DEFAULT '[]',
    created_at    TEXT NOT NULL,
    tags          TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS tofu_builds (
    id              TEXT PRIMARY KEY,
    template        TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'pending',
    created_at      TEXT NOT NULL,
    started_at      TEXT,
    finished_at     TEXT,
    created_by      TEXT NOT NULL DEFAULT 'ui',
    var_overrides   TEXT NOT NULL DEFAULT '{}',
    log             TEXT NOT NULL DEFAULT '[]',
    exit_code       INTEGER,
    provisioned     TEXT NOT NULL DEFAULT '[]'
);

CREATE TABLE IF NOT EXISTS builds (
    id              TEXT PRIMARY KEY,
    template        TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'pending',
    created_at      TEXT NOT NULL,
    started_at      TEXT,
    finished_at     TEXT,
    created_by      TEXT NOT NULL DEFAULT 'ui',
    var_overrides   TEXT NOT NULL DEFAULT '{}',
    log             TEXT NOT NULL DEFAULT '[]',
    exit_code       INTEGER,
    provisioned     TEXT NOT NULL DEFAULT '[]'
);
"""


def init(db_file: Path | None = None) -> None:
    """Open the database, apply schema, migrate from JSON if needed."""
    global _conn
    path = db_file or _DB_FILE
    _conn = sqlite3.connect(str(path), check_same_thread=False)
    _conn.row_factory = sqlite3.Row
    _conn.executescript(_SCHEMA)
    _conn.commit()
    _migrate_json()


def get_db() -> sqlite3.Connection:
    if _conn is None:
        raise RuntimeError("db.init() has not been called")
    return _conn


# ---------------------------------------------------------------------------
# JSON migration (runs once — renames files after import)
# ---------------------------------------------------------------------------

def _migrate_json() -> None:
    base = _DB_FILE.parent

    state_file = base / "state.json"
    if state_file.exists():
        _import_state_json(state_file)
        state_file.rename(base / "state.json.migrated")

    dns_file = base / "dns.json"
    if dns_file.exists():
        _import_dns_json(dns_file)
        dns_file.rename(base / "dns.json.migrated")

    builds_file = base / "builds.json"
    if builds_file.exists():
        _import_builds_json(builds_file)
        builds_file.rename(base / "builds.json.migrated")


def _import_state_json(path: Path) -> None:
    data = json.loads(path.read_text())
    c = _conn
    for v in data.get("vpcs", {}).values():
        c.execute("""INSERT OR IGNORE INTO vpcs
            (id,name,cidr_block,dns_support,status,created_at,tags) VALUES
            (?,?,?,?,?,?,?)""",
            (v["id"], v["name"], v["cidr_block"], int(v.get("dns_support", True)),
             v.get("status", "active"), v["created_at"], json.dumps(v.get("tags", {}))))
    for i in data.get("instances", {}).values():
        c.execute("""INSERT OR IGNORE INTO instances
            (id,name,image_id,flavor,vpc_id,subnet_id,security_group_ids,
             user_data,private_ip,public_ip,status,created_at,tags,
             domain_name,ssh_host_port,ssh_user,users) VALUES
            (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (i["id"], i["name"], i["image_id"], i["flavor"], i["vpc_id"],
             i["subnet_id"], json.dumps(i.get("security_group_ids", [])),
             i.get("user_data"), i.get("private_ip", ""), i.get("public_ip", ""),
             i.get("status", "pending"), i["created_at"], json.dumps(i.get("tags", {})),
             i.get("domain_name", ""), i.get("ssh_port", 0),
             i.get("ssh_user", "ubuntu"), json.dumps(i.get("users", []))))
    for lb in data.get("load_balancers", {}).values():
        c.execute("""INSERT OR IGNORE INTO load_balancers
            (id,name,type,vpc_id,subnet_ids,internal,dns_name,listen_port,
             backends,status,created_at,tags) VALUES
            (?,?,?,?,?,?,?,?,?,?,?,?)""",
            (lb["id"], lb["name"], lb.get("type", "application"), lb.get("vpc_id", ""),
             json.dumps(lb.get("subnet_ids", [])), int(lb.get("internal", False)),
             lb.get("dns_name", ""), lb.get("listen_port", 0),
             json.dumps(lb.get("backends", [])), lb.get("status", "active"),
             lb["created_at"], json.dumps(lb.get("tags", {}))))
    c.commit()


def _import_dns_json(path: Path) -> None:
    from models import now_iso
    from dns import BUILTIN_ZONES
    data = json.loads(path.read_text())
    c = _conn
    for zone_name, zone in data.items():
        builtin = 1 if zone_name in BUILTIN_ZONES else 0
        c.execute("INSERT OR IGNORE INTO dns_zones (name,created_at,builtin) VALUES (?,?,?)",
                  (zone_name, zone.get("created_at", now_iso()), builtin))
        for rec in zone.get("records", {}).values():
            c.execute("""INSERT OR IGNORE INTO dns_records
                (id,zone_name,name,fqdn,type,value,ttl,resource_type,resource_id,created_at)
                VALUES (?,?,?,?,?,?,?,?,?,?)""",
                (str(__import__("uuid").uuid4()), zone_name, rec["name"], rec.get("fqdn", ""),
                 rec["type"], rec["value"], rec.get("ttl", 300),
                 rec.get("resource_type", "manual"), rec.get("resource_id", ""),
                 rec.get("created_at", now_iso())))
    c.commit()


def _import_builds_json(path: Path) -> None:
    data = json.loads(path.read_text())
    c = _conn
    for b in data.values():
        c.execute("""INSERT OR IGNORE INTO builds
            (id,template,status,created_at,started_at,finished_at,created_by,
             var_overrides,log,exit_code,provisioned) VALUES
            (?,?,?,?,?,?,?,?,?,?,?)""",
            (b["id"], b["template"], b["status"], b["created_at"],
             b.get("started_at"), b.get("finished_at"), b.get("created_by", "ui"),
             json.dumps(b.get("var_overrides", {})), json.dumps(b.get("log", [])),
             b.get("exit_code"), json.dumps(b.get("provisioned", []))))
    c.commit()
