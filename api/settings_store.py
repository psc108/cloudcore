"""SQLite-backed key/value settings store — generic, not tied to any one feature."""
from __future__ import annotations

import json
from typing import Any

import db


def get(key: str, default: Any = None) -> Any:
    row = db.get_db().execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return json.loads(row["value"]) if row else default


def set(key: str, value: Any) -> None:
    c = db.get_db()
    c.execute(
        "INSERT INTO settings (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, json.dumps(value)),
    )
    c.commit()


def get_prefixed(prefix: str) -> dict[str, Any]:
    """All settings whose key starts with '<prefix>.', keyed by the remainder."""
    rows = db.get_db().execute(
        "SELECT key, value FROM settings WHERE key LIKE ? ESCAPE '\\'",
        (prefix.replace("_", r"\_").replace("%", r"\%") + ".%",),
    ).fetchall()
    return {r["key"][len(prefix) + 1:]: json.loads(r["value"]) for r in rows}
