"""llm-chat Stage 13 — the local-capture client's server side.

A student running a model on their own machine submits what the model was
asked and the code it produced. The submission carries NO execution
evidence: a coordinator re-runs the code in its own sandbox
(verify_proxy.py's POST /sandbox/reverify) and the corpus row this module
writes holds the coordinator's result, never the client's. That keeps the
Phase 3 corpus's one guarantee intact — every result in it was actually
executed by us.

Trust domains, same split as llm_examples_routes.py:
- POST /v1/llm-chat/client-submissions and GET .../<id> are reachable on
  the examples_listener bind (port 8083), authenticated per student by a
  bearer token from llm_client_tokens. Never the shared
  CLOUDCORE_API_TOKEN.
- Token create/list/revoke and the submissions listing are dashboard-only
  (127.0.0.1:8080), behind the admin token.

Submissions are accepted immediately (202) and re-verified by a background
worker, so a client never waits on a microVM boot. With no llm-chat
coordinator running, a submission simply stays `pending` and is retried
every RETRY_INTERVAL_S until one is, or until it expires.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import secrets
import threading
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timedelta, timezone

from flask import Blueprint, jsonify, request

import db
import llm_deployments_store
import llm_examples_store
import store
from llm_examples_routes import KNOWN_LANGUAGES
from models import now_iso
import cc_token

log = logging.getLogger(__name__)

client_capture_bp = Blueprint("llm_client_capture", __name__)

API_TOKEN = cc_token.master_token()

CLIENT_REACHABLE_ENDPOINTS = {
    "llm_client_capture.submit",
    "llm_client_capture.submission_status",
}

SUBMISSIONS_PER_HOUR = int(os.environ.get("LLM_CLIENT_SUBMISSIONS_PER_HOUR", "20"))
MAX_CODE_BYTES = 64 * 1024
MAX_PROMPT_BYTES = 16 * 1024
MAX_NOTE_BYTES = 2 * 1024
RETRY_INTERVAL_S = 60
EXPIRE_AFTER = timedelta(hours=24)
# A non-Python re-run can queue behind other Runs on the coordinator
# (verify_proxy.py's RUN_QUEUE_WAIT_SECONDS, 120s default) before booting.
_REVERIFY_TIMEOUT_S = 240

# Fields that only a coordinator may produce. Rejected outright rather than
# silently dropped, so a client can never believe they were stored.
_EXECUTION_FIELDS = {
    "exec_stdout", "exec_stderr", "exec_exit_code", "passed",
    "fix_explanation", "fixed_code", "fix_exec_stdout", "fix_exec_stderr", "fix_passed",
    "stdout", "stderr", "exit_code",
}

_wake = threading.Event()
_worker_started = False
_worker_lock = threading.Lock()


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _err(status: int, title: str, detail: str = ""):
    body = {"status": status, "title": title}
    if detail:
        body["detail"] = detail
    return jsonify(body), status


def _admin_auth():
    token = request.headers.get("Authorization", "").removeprefix("Bearer ")
    if token != API_TOKEN:
        return _err(401, "Unauthorized")
    return None


def _client_token_row():
    """The llm_client_tokens row for this request's bearer token, or None
    if it's missing, unknown or revoked."""
    token = request.headers.get("Authorization", "").removeprefix("Bearer ").strip()
    if not token:
        return None
    row = db.get_db().execute(
        "SELECT * FROM llm_client_tokens WHERE token_sha256=? AND revoked_at IS NULL",
        (_hash(token),)).fetchone()
    return dict(row) if row else None


# --- admin: tokens ---------------------------------------------------------

@client_capture_bp.post("/v1/llm-chat/client-tokens")
def create_token():
    err = _admin_auth()
    if err:
        return err
    label = str((request.get_json(silent=True) or {}).get("label") or "").strip()[:100]
    if not label:
        return _err(400, "Bad Request", "'label' is required (e.g. the student's name)")
    token = "lcc_" + secrets.token_urlsafe(32)
    token_id = str(uuid.uuid4())
    c = db.get_db()
    c.execute("INSERT INTO llm_client_tokens (id, label, token_sha256, created_at) VALUES (?,?,?,?)",
              (token_id, label, _hash(token), now_iso()))
    c.commit()
    # The only time the token itself ever leaves this process.
    return jsonify({"id": token_id, "label": label, "token": token}), 201


@client_capture_bp.get("/v1/llm-chat/client-tokens")
def list_tokens():
    err = _admin_auth()
    if err:
        return err
    rows = db.get_db().execute(
        """SELECT t.id, t.label, t.created_at, t.revoked_at, t.last_used_at,
                  (SELECT count(*) FROM llm_client_submissions s WHERE s.token_id = t.id) AS submissions
           FROM llm_client_tokens t ORDER BY t.created_at DESC""").fetchall()
    return jsonify({"items": [dict(r) for r in rows]})


@client_capture_bp.delete("/v1/llm-chat/client-tokens/<token_id>")
def revoke_token(token_id):
    err = _admin_auth()
    if err:
        return err
    c = db.get_db()
    cur = c.execute("UPDATE llm_client_tokens SET revoked_at=? WHERE id=? AND revoked_at IS NULL",
                    (now_iso(), token_id))
    c.commit()
    if cur.rowcount == 0:
        return _err(404, "Not Found", "no active token with that id")
    return jsonify({"id": token_id, "revoked": True})


@client_capture_bp.get("/v1/llm-chat/client-submissions")
def list_submissions():
    err = _admin_auth()
    if err:
        return err
    rows = db.get_db().execute(
        """SELECT s.id, s.status, s.language, s.model_filename, s.attempts, s.last_error,
                  s.example_id, s.created_at, s.updated_at, t.label AS token_label
           FROM llm_client_submissions s LEFT JOIN llm_client_tokens t ON t.id = s.token_id
           ORDER BY s.created_at DESC LIMIT 500""").fetchall()
    return jsonify({"items": [dict(r) for r in rows]})


# --- client-facing ---------------------------------------------------------

@client_capture_bp.post("/v1/llm-chat/client-submissions")
def submit():
    tok = _client_token_row()
    if tok is None:
        return _err(401, "Unauthorized", "missing, unknown or revoked capture token")
    body = request.get_json(silent=True)
    if not isinstance(body, dict):
        return _err(400, "Bad Request", "JSON object body required")

    sent_exec = sorted(_EXECUTION_FIELDS & body.keys())
    if sent_exec:
        return _err(400, "Bad Request",
                    f"execution results are produced by the coordinator's own re-run, "
                    f"never accepted from a client: remove {', '.join(sent_exec)}")
    for required in ("prompt", "code", "language", "model_filename"):
        if not str(body.get(required) or "").strip():
            return _err(400, "Bad Request", f"'{required}' is required")
    language = str(body["language"]).strip().lower()
    if language not in KNOWN_LANGUAGES:
        return _err(400, "Bad Request", f"language must be one of {sorted(KNOWN_LANGUAGES)}")
    code, prompt = str(body["code"]), str(body["prompt"])
    note = str(body.get("client_note") or "")
    if len(code.encode()) > MAX_CODE_BYTES or len(prompt.encode()) > MAX_PROMPT_BYTES \
            or len(note.encode()) > MAX_NOTE_BYTES:
        return _err(413, "Payload Too Large",
                    f"limits: code {MAX_CODE_BYTES}B, prompt {MAX_PROMPT_BYTES}B, note {MAX_NOTE_BYTES}B")

    c = db.get_db()
    hour_ago = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    recent = c.execute("SELECT count(*) FROM llm_client_submissions WHERE token_id=? AND created_at >= ?",
                       (tok["id"], hour_ago)).fetchone()[0]
    if recent >= SUBMISSIONS_PER_HOUR:
        return _err(429, "Too Many Requests", f"limit is {SUBMISSIONS_PER_HOUR} submissions per hour")

    sub_id = str(uuid.uuid4())
    now = now_iso()
    c.execute(
        """INSERT INTO llm_client_submissions
           (id, token_id, model_filename, prompt, code, language, client_note, created_at, updated_at)
           VALUES (?,?,?,?,?,?,?,?,?)""",
        (sub_id, tok["id"], str(body["model_filename"])[:200], prompt, code, language, note, now, now))
    c.execute("UPDATE llm_client_tokens SET last_used_at=? WHERE id=?", (now, tok["id"]))
    c.commit()
    _wake.set()
    return jsonify({"id": sub_id, "status": "pending"}), 202


@client_capture_bp.get("/v1/llm-chat/client-submissions/<sub_id>")
def submission_status(sub_id):
    tok = _client_token_row()
    if tok is None:
        return _err(401, "Unauthorized", "missing, unknown or revoked capture token")
    row = db.get_db().execute("SELECT * FROM llm_client_submissions WHERE id=? AND token_id=?",
                              (sub_id, tok["id"])).fetchone()
    if row is None:
        return _err(404, "Not Found")
    sub = dict(row)
    out = {"id": sub["id"], "status": sub["status"], "language": sub["language"],
           "attempts": sub["attempts"], "last_error": sub["last_error"]}
    if sub["example_id"]:
        ex = db.get_db().execute(
            "SELECT exec_stdout, exec_stderr, exec_exit_code, passed FROM llm_verification_examples WHERE id=?",
            (sub["example_id"],)).fetchone()
        if ex:
            out["result"] = {"stdout": ex["exec_stdout"], "stderr": ex["exec_stderr"],
                             "exit_code": ex["exec_exit_code"], "passed": bool(ex["passed"])}
    return jsonify(out)


# --- re-verification worker --------------------------------------------------

def _coordinator_urls() -> list[str]:
    """Base URLs of every registered llm-chat deployment whose instance is
    running right now, resolved fresh each time (same trust boundary as
    llm_deployments_routes._poll_live: never a self-reported address)."""
    urls = []
    for dep in llm_deployments_store.list_deployments():
        if dep.get("example") != "llm-chat":
            continue
        inst = store.find_instance_by_name(dep["name"])
        if inst and inst.status.value == "running" and inst.private_ip:
            urls.append(f"http://{inst.private_ip}:{dep['port']}")
    return urls


def _reverify(base_url: str, language: str, code: str) -> dict:
    req = urllib.request.Request(
        base_url + "/sandbox/reverify", method="POST",
        data=json.dumps({"language": language, "code": code}).encode(),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {API_TOKEN}"})
    with urllib.request.urlopen(req, timeout=_REVERIFY_TIMEOUT_S) as resp:
        return json.loads(resp.read())


def _set(sub_id: str, **fields) -> None:
    fields["updated_at"] = now_iso()
    cols = ", ".join(f"{k}=?" for k in fields)
    c = db.get_db()
    c.execute(f"UPDATE llm_client_submissions SET {cols} WHERE id=?", (*fields.values(), sub_id))
    c.commit()


def process_pending() -> int:
    """One pass over every pending submission. Returns how many reached a
    final state. Safe to call concurrently with new submissions."""
    rows = [dict(r) for r in db.get_db().execute(
        "SELECT * FROM llm_client_submissions WHERE status='pending' ORDER BY created_at").fetchall()]
    if not rows:
        return 0
    done = 0
    urls = _coordinator_urls()
    for sub in rows:
        created = datetime.fromisoformat(sub["created_at"])
        if datetime.now(timezone.utc) - created > EXPIRE_AFTER:
            _set(sub["id"], status="expired",
                 last_error="no llm-chat coordinator re-verified this within 24h")
            done += 1
            continue
        if not urls:
            _set(sub["id"], attempts=sub["attempts"] + 1,
                 last_error="no running llm-chat coordinator to re-verify on")
            continue
        try:
            result = _reverify(urls[0], sub["language"], sub["code"])
        except urllib.error.HTTPError as e:
            detail = e.read().decode(errors="replace")[:300]
            # 4xx: the coordinator will never accept this one; retrying won't help.
            if 400 <= e.code < 500:
                _set(sub["id"], status="rejected", attempts=sub["attempts"] + 1,
                     last_error=f"coordinator refused it: HTTP {e.code} {detail}")
                done += 1
            else:
                _set(sub["id"], attempts=sub["attempts"] + 1, last_error=f"HTTP {e.code} {detail}")
            continue
        except (urllib.error.URLError, OSError, ValueError) as e:
            _set(sub["id"], attempts=sub["attempts"] + 1, last_error=str(e)[:300])
            continue
        example_id = llm_examples_store.record_example(
            source="local-client", build_id="", model_filename=sub["model_filename"],
            prompt=sub["prompt"], generated_code=sub["code"],
            exec_stdout=result.get("stdout") or "", exec_stderr=result.get("stderr") or "",
            exec_exit_code=result.get("exit_code"),
            passed=(not result.get("timed_out") and result.get("exit_code") == 0),
            language=sub["language"], client_token_id=sub["token_id"])
        _set(sub["id"], status="verified", attempts=sub["attempts"] + 1,
             last_error="", example_id=example_id)
        done += 1
    return done


def _worker() -> None:
    while True:
        try:
            process_pending()
        except Exception:
            log.exception("llm client capture: re-verification pass failed")
        _wake.wait(RETRY_INTERVAL_S)
        _wake.clear()


def start() -> None:
    global _worker_started
    with _worker_lock:
        if _worker_started:
            return
        _worker_started = True
    threading.Thread(target=_worker, daemon=True, name="llm-client-capture").start()
