"""Central store for llm-chat's grounded-verification examples — the
Phase 3 learning corpus. See db.py's own llm_verification_examples
table comment for the capture-everything / status-only-gates-display
design. Mirrors failure_queue.py's shape (record/list/set-status),
same convention.
"""
from __future__ import annotations

import json
import uuid
from typing import Iterator

import db
from models import now_iso

VALID_STATUSES = ("pending", "published", "hidden")


def record_example(source: str, build_id: str, model_filename: str,
                    prompt: str, generated_code: str,
                    exec_stdout: str, exec_stderr: str, exec_exit_code,
                    passed: bool, fix_explanation: str = "", fixed_code: str = "",
                    fix_exec_stdout: str = "", fix_exec_stderr: str = "",
                    fix_passed: bool | None = None) -> str:
    example_id = str(uuid.uuid4())
    c = db.get_db()
    c.execute(
        """INSERT INTO llm_verification_examples
           (id, source, build_id, model_filename, prompt, generated_code,
            exec_stdout, exec_stderr, exec_exit_code, passed,
            fix_explanation, fixed_code, fix_exec_stdout, fix_exec_stderr,
            fix_passed, status, created_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (example_id, source, build_id, model_filename, prompt, generated_code,
         exec_stdout, exec_stderr, exec_exit_code, int(passed),
         fix_explanation, fixed_code, fix_exec_stdout, fix_exec_stderr,
         None if fix_passed is None else int(fix_passed),
         "pending", now_iso()))
    c.commit()
    return example_id


def _row_to_dict(r) -> dict:
    d = dict(r)
    d["passed"] = bool(d["passed"])
    if d["fix_passed"] is not None:
        d["fix_passed"] = bool(d["fix_passed"])
    return d


def list_examples(status: str | None = None, limit: int = 200) -> list[dict]:
    """Newest first — both the Dashboard moderation list and the public
    published feed read most-recent-first."""
    c = db.get_db()
    if status:
        rows = c.execute(
            "SELECT * FROM llm_verification_examples WHERE status=? "
            "ORDER BY created_at DESC LIMIT ?", (status, limit)).fetchall()
    else:
        rows = c.execute(
            "SELECT * FROM llm_verification_examples "
            "ORDER BY created_at DESC LIMIT ?", (limit,)).fetchall()
    return [_row_to_dict(r) for r in rows]


def set_status(example_id: str, status: str) -> bool:
    if status not in VALID_STATUSES:
        return False
    c = db.get_db()
    cur = c.execute("UPDATE llm_verification_examples SET status=? WHERE id=?",
                     (status, example_id))
    c.commit()
    return cur.rowcount > 0


def export_jsonl() -> Iterator[str]:
    """Full corpus, every status — the actual training-data path. A
    generator so a large corpus doesn't need to fully materialize in
    memory before the first byte is sent."""
    c = db.get_db()
    cursor = c.execute(
        "SELECT * FROM llm_verification_examples ORDER BY created_at ASC")
    for row in cursor:
        yield json.dumps(_row_to_dict(row)) + "\n"
