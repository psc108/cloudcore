"""Scheduler — recurring/one-off resource-creation builds, plus the
7B distributed-LLM Sentinel-ingestion job kind.

Background-loop shape mirrors Sentinel's own watcher.py poll loop (the
closest existing precedent in either repo for "wake up periodically
and do work") — this codebase itself had no prior "forever" background
thread (discovery.py is on-demand, wireguard.py is event-triggered).
"""
from __future__ import annotations

import json
import os
import socket
import threading
import time
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timezone

import build_engine
import croncalc
import db
import host_stats
import peer_client
import peers_routes
import peers_store
import tofu_engine
from models import now_iso

TICK_INTERVAL_S = 30

# Sentinel is a separate, sibling project (~/IdeaProjects/sentinel) —
# a fixed, well-known local address, same convention Sentinel itself
# uses for Loki (config.py's own comment: "a fixed, well-known
# address, not something to discover"). Overridable for anyone running
# it on a non-default port.
SENTINEL_LOCAL_URL = os.environ.get("SENTINEL_LOCAL_URL", "http://127.0.0.1:8900")

_HOSTNAME = socket.gethostname()

_lock = threading.Lock()


# ---------------------------------------------------------------------------
# CRUD
# ---------------------------------------------------------------------------

def _row_to_dict(row) -> dict:
    d = dict(row)
    d["var_overrides"] = json.loads(d.get("var_overrides") or "{}")
    d["enabled"] = bool(d["enabled"])
    return d


def list_schedules() -> list[dict]:
    rows = db.get_db().execute(
        "SELECT * FROM schedules ORDER BY created_at DESC").fetchall()
    return [_row_to_dict(r) for r in rows]


def get_schedule(schedule_id: str) -> dict | None:
    row = db.get_db().execute(
        "SELECT * FROM schedules WHERE id=?", (schedule_id,)).fetchone()
    return _row_to_dict(row) if row else None


def create_schedule(*, name: str, kind: str, engine: str, template: str,
                     var_overrides: dict, recurrence: dict,
                     created_by: str = "ui") -> dict:
    if kind not in ("build", "llm_ingest"):
        raise ValueError(f"unknown schedule kind: {kind!r}")

    recurrence_type, run_at, cron_expr = _resolve_recurrence(recurrence)
    schedule_id = str(uuid.uuid4())
    now = now_iso()
    next_run_at = (run_at if recurrence_type == "once"
                   else croncalc.next_fire(cron_expr, datetime.now(timezone.utc)).isoformat())

    c = db.get_db()
    c.execute(
        """INSERT INTO schedules
           (id, name, kind, engine, template, var_overrides, recurrence_type,
            run_at, cron_expr, enabled, next_run_at, last_run_at, last_status,
            sentinel_checkpoint_event_id, created_at, created_by)
           VALUES (?,?,?,?,?,?,?,?,?,1,?,NULL,'',0,?,?)""",
        (schedule_id, name, kind, engine, template, json.dumps(var_overrides),
         recurrence_type, run_at, cron_expr, next_run_at, now, created_by))
    c.commit()
    return get_schedule(schedule_id)


def update_schedule(schedule_id: str, **fields) -> dict | None:
    existing = get_schedule(schedule_id)
    if not existing:
        return None
    sets, params = [], []
    if "name" in fields:
        sets.append("name=?"); params.append(fields["name"])
    if "enabled" in fields:
        sets.append("enabled=?"); params.append(1 if fields["enabled"] else 0)
    if "var_overrides" in fields:
        sets.append("var_overrides=?"); params.append(json.dumps(fields["var_overrides"]))
    if "recurrence" in fields:
        recurrence_type, run_at, cron_expr = _resolve_recurrence(fields["recurrence"])
        next_run_at = (run_at if recurrence_type == "once"
                       else croncalc.next_fire(cron_expr, datetime.now(timezone.utc)).isoformat())
        sets += ["recurrence_type=?", "run_at=?", "cron_expr=?", "next_run_at=?"]
        params += [recurrence_type, run_at, cron_expr, next_run_at]
    if not sets:
        return existing
    params.append(schedule_id)
    c = db.get_db()
    c.execute(f"UPDATE schedules SET {', '.join(sets)} WHERE id=?", params)
    c.commit()
    return get_schedule(schedule_id)


def delete_schedule(schedule_id: str) -> bool:
    # Child rows first — schedule_runs.schedule_id REFERENCES
    # schedules(id) and this DB runs with foreign keys ON (db.py's own
    # docstring), so deleting the parent first raises IntegrityError.
    c = db.get_db()
    c.execute("DELETE FROM llm_ingestions WHERE schedule_id=?", (schedule_id,))
    c.execute("DELETE FROM schedule_runs WHERE schedule_id=?", (schedule_id,))
    cur = c.execute("DELETE FROM schedules WHERE id=?", (schedule_id,))
    c.commit()
    return cur.rowcount > 0


def _ing_row_to_dict(row) -> dict:
    d = dict(row)
    d["peers_synced"] = json.loads(d.get("peers_synced") or "[]")
    d["coordinator_stats"] = json.loads(d.get("coordinator_stats") or "{}")
    d["worker_stats"] = json.loads(d.get("worker_stats") or "[]")
    return d


def list_runs(schedule_id: str, limit: int = 50) -> list[dict]:
    rows = db.get_db().execute(
        "SELECT * FROM schedule_runs WHERE schedule_id=? ORDER BY started_at DESC LIMIT ?",
        (schedule_id, limit)).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["log"] = json.loads(d.get("log") or "[]")
        ing = db.get_db().execute(
            "SELECT * FROM llm_ingestions WHERE run_id=?", (d["id"],)).fetchone()
        if ing:
            d["llm_ingestion"] = _ing_row_to_dict(ing)
        out.append(d)
    return out


def list_llm_ingestions(limit: int = 50) -> list[dict]:
    """Every llm_ingest run across every schedule, newest first — backs
    the LLM Performance page (per direct request: "we now need a
    performance page to show how the 7b (or any other llm we
    introduce) is performing"). Joined with the owning schedule's own
    name/template so the page can label each row without a second
    round trip, and deliberately not scoped to one schedule — a lab
    with several llm_ingest schedules (different models/templates
    later) should see them side by side on one page."""
    rows = db.get_db().execute(
        """SELECT i.*, s.name AS schedule_name, s.template AS schedule_template
           FROM llm_ingestions i JOIN schedules s ON s.id = i.schedule_id
           ORDER BY i.started_at DESC LIMIT ?""",
        (limit,)).fetchall()
    return [_ing_row_to_dict(r) for r in rows]


def _resolve_recurrence(recurrence: dict) -> tuple[str, str | None, str]:
    mode = recurrence.get("mode")
    if mode == "once":
        run_at = recurrence.get("run_at")
        if not run_at:
            raise ValueError("mode 'once' requires run_at (ISO datetime)")
        # Validate it actually parses — a malformed timestamp should be
        # rejected at create time, not silently never fire.
        datetime.fromisoformat(run_at)
        return "once", run_at, ""
    cron_expr = croncalc.build_cron(mode, **{k: v for k, v in recurrence.items() if k != "mode"})
    return "cron", None, cron_expr


# ---------------------------------------------------------------------------
# Background loop
# ---------------------------------------------------------------------------

def start() -> None:
    threading.Thread(target=_loop, daemon=True).start()


def _loop() -> None:
    while True:
        try:
            _tick()
        except Exception as e:
            print(f"[scheduler] tick failed: {e}")
        time.sleep(TICK_INTERVAL_S)


def _tick() -> None:
    with _lock:
        now = datetime.now(timezone.utc).isoformat()
        due = db.get_db().execute(
            "SELECT id FROM schedules WHERE enabled=1 AND next_run_at IS NOT NULL "
            "AND next_run_at <= ?", (now,)).fetchall()
        due_ids = [r["id"] for r in due]
        for schedule_id in due_ids:
            _advance_before_run(schedule_id)
    for schedule_id in due_ids:
        threading.Thread(target=_run_schedule, args=(schedule_id,), daemon=True).start()


def _advance_before_run(schedule_id: str) -> None:
    """Move next_run_at forward (or disable a one-off) BEFORE the run
    starts, inside the tick's own lock — a slow run can never be
    double-fired by the next tick this way, since _tick() only ever
    reads rows whose next_run_at is still due."""
    schedule = get_schedule(schedule_id)
    if not schedule:
        return
    c = db.get_db()
    if schedule["recurrence_type"] == "once":
        c.execute("UPDATE schedules SET enabled=0 WHERE id=?", (schedule_id,))
    else:
        next_run_at = croncalc.next_fire(schedule["cron_expr"], datetime.now(timezone.utc))
        c.execute("UPDATE schedules SET next_run_at=? WHERE id=?",
                   (next_run_at.isoformat(), schedule_id))
    c.commit()


def run_now(schedule_id: str) -> bool:
    """Manual trigger (dashboard 'Run Now') — reuses _run_schedule
    directly, doesn't touch next_run_at."""
    if not get_schedule(schedule_id):
        return False
    threading.Thread(target=_run_schedule, args=(schedule_id,), daemon=True).start()
    return True


# ---------------------------------------------------------------------------
# Execution
# ---------------------------------------------------------------------------

def _new_run(schedule_id: str) -> str:
    run_id = str(uuid.uuid4())
    c = db.get_db()
    c.execute(
        "INSERT INTO schedule_runs (id, schedule_id, started_at, status, summary, log) "
        "VALUES (?,?,?, 'running', '', '[]')",
        (run_id, schedule_id, now_iso()))
    c.commit()
    return run_id


def _finish_run(run_id: str, schedule_id: str, status: str, summary: str, log: list[str]) -> None:
    c = db.get_db()
    c.execute(
        "UPDATE schedule_runs SET finished_at=?, status=?, summary=?, log=? WHERE id=?",
        (now_iso(), status, summary, json.dumps(log), run_id))
    c.execute("UPDATE schedules SET last_run_at=?, last_status=? WHERE id=?",
              (now_iso(), status, schedule_id))
    c.commit()


def _run_schedule(schedule_id: str) -> None:
    schedule = get_schedule(schedule_id)
    if not schedule:
        return
    run_id = _new_run(schedule_id)
    try:
        if schedule["kind"] == "build":
            status, summary, log = _run_build_schedule(schedule)
        elif schedule["kind"] == "llm_ingest":
            status, summary, log = _run_llm_ingest_schedule(schedule, run_id)
        else:
            status, summary, log = "failed", f"unknown kind {schedule['kind']!r}", []
    except Exception as e:
        status, summary, log = "failed", f"unhandled error: {e}", [str(e)]
    _finish_run(run_id, schedule_id, status, summary, log)


def _run_build_schedule(schedule: dict) -> tuple[str, str, list[str]]:
    engine = schedule["engine"]
    template = schedule["template"]
    var_overrides = schedule["var_overrides"]
    engine_mod = tofu_engine if engine == "tofu" else build_engine
    build = engine_mod.submit_build(template, var_overrides, created_by="scheduler")
    build_id = build["id"]
    for _ in range(240):  # up to 20 minutes at 5s intervals
        time.sleep(5)
        build = engine_mod.get_build(build_id)
        if build and build["status"] not in ("pending", "running"):
            break
    status = build["status"] if build else "failed"
    outcome = "success" if status == "success" else "failed"
    return outcome, f"build {build_id} ({template}): {status}", build.get("log", []) if build else []


def _run_llm_ingest_schedule(schedule: dict, run_id: str) -> tuple[str, str, list[str]]:
    log: list[str] = []
    schedule_id = schedule["id"]
    checkpoint = schedule["sentinel_checkpoint_event_id"]
    ing_id = str(uuid.uuid4())
    ing_started = now_iso()

    def _log(line: str) -> None:
        log.append(line)

    def _save_ingestion(**fields) -> None:
        c = db.get_db()
        c.execute(
            """INSERT INTO llm_ingestions
               (id, schedule_id, run_id, started_at, finished_at, events_seen,
                findings_created, suggestions_created, peers_synced, summary_text, status,
                cluster_build_seconds, model_load_seconds, inference_seconds,
                prompt_tokens, completion_tokens, total_tokens, tokens_per_second,
                coordinator_stats, worker_stats)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(id) DO UPDATE SET
                 finished_at=excluded.finished_at, events_seen=excluded.events_seen,
                 findings_created=excluded.findings_created,
                 suggestions_created=excluded.suggestions_created,
                 peers_synced=excluded.peers_synced, summary_text=excluded.summary_text,
                 status=excluded.status,
                 cluster_build_seconds=excluded.cluster_build_seconds,
                 model_load_seconds=excluded.model_load_seconds,
                 inference_seconds=excluded.inference_seconds,
                 prompt_tokens=excluded.prompt_tokens,
                 completion_tokens=excluded.completion_tokens,
                 total_tokens=excluded.total_tokens,
                 tokens_per_second=excluded.tokens_per_second,
                 coordinator_stats=excluded.coordinator_stats,
                 worker_stats=excluded.worker_stats""",
            (ing_id, schedule_id, run_id, ing_started, fields.get("finished_at"),
             fields.get("events_seen", 0), fields.get("findings_created", 0),
             fields.get("suggestions_created", 0),
             json.dumps(fields.get("peers_synced", [])),
             fields.get("summary_text", ""), fields.get("status", "running"),
             fields.get("cluster_build_seconds"), fields.get("model_load_seconds"),
             fields.get("inference_seconds"), fields.get("prompt_tokens"),
             fields.get("completion_tokens"), fields.get("total_tokens"),
             fields.get("tokens_per_second"),
             json.dumps(fields.get("coordinator_stats", {})),
             json.dumps(fields.get("worker_stats", []))))
        c.commit()

    _save_ingestion(status="running")

    # 1. Cheap pre-check — skip the whole cluster build if there's
    # nothing new to ingest.
    try:
        events = _sentinel_get(f"/api/events?limit=500")
    except (urllib.error.URLError, ConnectionError, OSError) as e:
        _log(f"Sentinel unreachable at {SENTINEL_LOCAL_URL}: {e}")
        _save_ingestion(finished_at=now_iso(), status="skipped-no-sentinel")
        return "success", "Sentinel not running locally — nothing to ingest", log

    new_events = [e for e in events if e["id"] > checkpoint]
    if not new_events:
        _log("No new Sentinel activity since last checkpoint — skipping cluster build.")
        _save_ingestion(finished_at=now_iso(), status="success", events_seen=0)
        return "success", "No new Sentinel activity", log

    _log(f"{len(new_events)} new Sentinel event(s) since checkpoint {checkpoint}.")

    # 2. Pick workers from the schedule's own candidate pool by CURRENT
    # traffic light, not whatever was true when the schedule was
    # created — the whole point of "auto-populate based on the traffic
    # lights" is that this is re-evaluated every wakeup. Green
    # (verdict 'active') preferred; amber ('pending', "risky but could
    # try") used only if no green candidate is available right now; red
    # ('error', "leave alone") never auto-selected.
    var_overrides = dict(schedule["var_overrides"])
    pool = var_overrides.pop("worker_peer_pool", []) or []
    if not pool:
        _log("No worker peer pool configured on this schedule.")
        _save_ingestion(finished_at=now_iso(), status="failed", events_seen=len(new_events))
        return "failed", "No worker peer pool configured", log

    green, amber = [], []
    for p in pool:
        verdict = peers_routes.peer_verdict(p["peer_id"])
        if verdict == "active":
            green.append(p)
        elif verdict == "pending":
            amber.append(p)
        else:
            _log(f"Peer {p['peer_id']}: verdict={verdict or 'unreachable'} — excluded this cycle.")
    selected = green or amber
    if not selected:
        _log("No pool peer is currently green or amber (all red or unreachable) — skipping this cycle.")
        _save_ingestion(finished_at=now_iso(), status="success", events_seen=len(new_events))
        return "success", "No healthy worker peer available this cycle", log
    _log(f"Selected {len(selected)} worker(s) this cycle "
         f"({'green' if selected is green else 'amber (no green available)'}).")
    var_overrides["worker_peers"] = selected

    # 3. Build the ephemeral cluster.
    http_port = int(var_overrides.get("http_port", 8610))
    build_start = time.monotonic()
    build = tofu_engine.submit_build("distributed-llm", var_overrides, created_by="scheduler")
    build_id = build["id"]
    _log(f"tofu apply started: build {build_id}")
    for _ in range(240):  # up to 20 minutes
        time.sleep(5)
        build = tofu_engine.get_build(build_id)
        if build and build["status"] not in ("pending", "running"):
            break
    if not build or build["status"] != "success":
        _log(f"Cluster build failed: {build.get('status') if build else 'unknown'}")
        try:
            tofu_engine.run_tofu_destroy(build_id)
        except Exception:
            pass
        _save_ingestion(finished_at=now_iso(), status="failed", events_seen=len(new_events))
        return "failed", "Cluster build failed", log
    cluster_build_seconds = time.monotonic() - build_start
    _log(f"Cluster built successfully in {cluster_build_seconds:.1f}s.")

    findings_created = 0
    suggestions_created = 0
    summary_text = ""
    peers_synced: list[dict] = []
    max_event_id = checkpoint
    model_load_seconds = None
    inference_seconds = None
    tokens_per_second = None
    usage = {}
    coordinator_stats = {}
    worker_stats: list[dict] = []

    try:
        # 4. Wait for the coordinator to actually be ready — this is
        # the model-load wait, timed separately from the cluster build
        # above since "how long OpenTofu took" and "how long the model
        # took to load once the VMs existed" are two different things
        # worth telling apart on the Performance page.
        coordinator_url = f"http://127.0.0.1:{http_port}"
        load_start = time.monotonic()
        if not _wait_for_health(coordinator_url, timeout_s=360):
            _log("Coordinator never became healthy within 6 minutes.")
            raise RuntimeError("coordinator health check timed out")
        model_load_seconds = time.monotonic() - load_start
        _log(f"Coordinator healthy after {model_load_seconds:.1f}s — sending ingestion prompt.")

        # Resource-usage snapshot, right as inference is about to
        # start (peak-ish load for both roles) — coordinator is always
        # this host itself (the template never peer-places it), workers
        # are whichever peers got selected in step 2 above.
        try:
            coordinator_stats = host_stats.collect()
        except Exception:
            pass
        for p in selected:
            s = peers_routes.peer_stats(p["peer_id"])
            if s is not None:
                worker_stats.append({"peer_id": p["peer_id"], "stats": s})

        # 5. Prompt the model.
        inference_start = time.monotonic()
        parsed, usage = _run_ingestion_prompt(coordinator_url, new_events)
        inference_seconds = time.monotonic() - inference_start
        completion_tokens = usage.get("completion_tokens")
        if completion_tokens and inference_seconds > 0:
            tokens_per_second = completion_tokens / inference_seconds
        else:
            tokens_per_second = None
        _log(f"Inference took {inference_seconds:.1f}s"
             + (f", {completion_tokens} completion tokens "
                f"({tokens_per_second:.1f} tok/s)" if tokens_per_second else "") + ".")
        summary_text = parsed.get("summary", "")
        model_findings = parsed.get("findings", []) or []
        model_suggestions = parsed.get("suggestions", []) or []
        _log(f"Model returned {len(model_findings)} finding(s), "
             f"{len(model_suggestions)} suggestion(s).")

        # 6. Write back into this host's own Sentinel.
        source_doc = f"llm-ingest:{_HOSTNAME}"
        codes = []
        if model_findings:
            for i, f in enumerate(model_findings):
                f.setdefault("code", f"LLM-{schedule_id[:8]}-{int(time.time())}-{i}")
            resp = _sentinel_post("/api/kb/import",
                                   {"source_doc": source_doc, "findings": model_findings})
            codes = resp.get("codes", [])
            findings_created = resp.get("imported", 0)
            _log(f"Imported {findings_created} finding(s) into local Sentinel.")

        if model_suggestions and codes:
            items = []
            for s in model_suggestions:
                idx = s.get("finding_index")
                if idx is None or not (0 <= idx < len(codes)):
                    continue
                items.append({
                    "event_id": s.get("event_id"),
                    "finding_code": codes[idx],
                    "source_doc": source_doc,
                    "confidence": s.get("confidence", 0.5),
                    "text": s.get("text", ""),
                })
            if items:
                resp = _sentinel_post("/api/suggestions/import", {"items": items})
                suggestions_created = resp.get("imported", 0)
                _log(f"Imported {suggestions_created} suggestion(s) into local Sentinel.")

        # 7. Advance the checkpoint.
        max_event_id = max(e["id"] for e in new_events)

        # 8. Distribute to online peers.
        peers_synced = _distribute_to_peers(model_findings, model_suggestions, codes, source_doc, _log)

    except Exception as e:
        # Anything in steps 4-8 (most notably the coordinator health
        # check timing out) used to propagate straight past the
        # llm_ingestions save below and out of this function entirely
        # — the finally clause still tore the cluster down, but the
        # row stayed stuck at status='running' forever, which is
        # exactly the kind of thing that looks broken on a performance
        # history page. Save a real 'failed' row instead.
        _log(f"llm_ingest failed: {e}")
        _save_ingestion(finished_at=now_iso(), status="failed", events_seen=len(new_events),
                         cluster_build_seconds=cluster_build_seconds, model_load_seconds=model_load_seconds,
                         inference_seconds=inference_seconds, coordinator_stats=coordinator_stats,
                         worker_stats=worker_stats)
        return "failed", f"llm_ingest failed: {e}", log
    finally:
        # 9. Always tear the cluster down, regardless of steps 5-8.
        _log("Destroying ephemeral cluster...")
        try:
            tofu_engine.run_tofu_destroy(build_id)
            _log("Cluster destroyed.")
        except Exception as e:
            _log(f"WARNING: cluster destroy failed: {e}")

    db.get_db().execute(
        "UPDATE schedules SET sentinel_checkpoint_event_id=? WHERE id=?",
        (max_event_id, schedule_id))
    db.get_db().commit()

    _save_ingestion(finished_at=now_iso(), status="success", events_seen=len(new_events),
                     findings_created=findings_created, suggestions_created=suggestions_created,
                     peers_synced=peers_synced, summary_text=summary_text,
                     cluster_build_seconds=cluster_build_seconds, model_load_seconds=model_load_seconds,
                     inference_seconds=inference_seconds, prompt_tokens=usage.get("prompt_tokens"),
                     completion_tokens=usage.get("completion_tokens"), total_tokens=usage.get("total_tokens"),
                     tokens_per_second=tokens_per_second,
                     coordinator_stats=coordinator_stats, worker_stats=worker_stats)
    return "success", summary_text or f"Ingested {len(new_events)} event(s)", log


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _sentinel_get(path: str) -> list | dict:
    req = urllib.request.Request(SENTINEL_LOCAL_URL + path)
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read())


def _sentinel_post(path: str, body: dict) -> dict:
    data = json.dumps(body).encode()
    req = urllib.request.Request(
        SENTINEL_LOCAL_URL + path, data=data,
        headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.loads(r.read())


def _wait_for_health(base_url: str, timeout_s: int) -> bool:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        try:
            req = urllib.request.Request(base_url + "/health")
            with urllib.request.urlopen(req, timeout=5) as r:
                if r.status == 200:
                    return True
        except (urllib.error.URLError, ConnectionError, OSError, TimeoutError):
            pass
        time.sleep(5)
    return False


def _extract_json_object(text: str) -> dict:
    """First balanced {...} block in text — same brace-depth-tracking
    technique tofu_engine.py's _extract_balanced_block already uses for
    HCL, applied here to a model's chat-completion response, which may
    wrap its JSON in prose or a markdown fence despite being asked not
    to."""
    start = text.find("{")
    if start == -1:
        raise ValueError("no JSON object found in model response")
    depth = 0
    for i in range(start, len(text)):
        if text[i] == "{":
            depth += 1
        elif text[i] == "}":
            depth -= 1
            if depth == 0:
                return json.loads(text[start:i + 1])
    raise ValueError("unterminated JSON object in model response")


def _run_ingestion_prompt(coordinator_url: str, events: list[dict]) -> tuple[dict, dict]:
    """Returns (parsed_response, usage) — usage is llama-server's own
    OpenAI-compatible 'usage' object (prompt_tokens/completion_tokens/
    total_tokens) when present, or {} if this build of llama-server
    doesn't return one (not guaranteed across versions — read
    defensively, never assumed)."""
    prompt_lines = [
        "You are reviewing new log-intelligence events from Sentinel, "
        "a lab monitoring tool for a small virtualization platform.",
        "Each event below is a window of correlated log lines Sentinel "
        "flagged as worth a look. 'matched' true means Sentinel's own "
        "retrieval already found a known finding for it (finding_code "
        "set) — those need no new finding from you.",
        "For events with no confident match, if the window_text gives "
        "you enough to determine a likely root cause, produce a new "
        "finding. If you're not confident, omit it rather than guessing.",
        "",
        "Respond with EXACTLY one JSON object and nothing else — no "
        "prose before or after, no markdown fence:",
        '{"summary": "<2-4 sentence overview of what happened>",',
        ' "findings": [{"title": "...", "symptom": "...", "root_cause": "...", "fix": "..."}],',
        ' "suggestions": [{"event_id": <int>, "finding_index": <index into findings[] above>, '
        '"confidence": <0-1>, "text": "..."}]}',
        "",
        "Events:",
    ]
    for e in events:
        prompt_lines.append(json.dumps({
            "event_id": e["id"], "host": e.get("host", ""), "unit": e.get("unit", ""),
            "job": e.get("job", ""), "level": e.get("level", ""),
            "matched": bool(e.get("matched")), "flag_reason": e.get("flag_reason", ""),
            "window_text": (e.get("window_text") or "")[:800],
        }))
    prompt = "\n".join(prompt_lines)

    body = {
        "model": "default",
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.2,
    }
    data = json.dumps(body).encode()
    req = urllib.request.Request(
        coordinator_url + "/v1/chat/completions", data=data,
        headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=180) as r:
        resp = json.loads(r.read())
    content = resp["choices"][0]["message"]["content"]
    usage = resp.get("usage") or {}
    try:
        return _extract_json_object(content), usage
    except (ValueError, json.JSONDecodeError):
        return {"summary": content, "findings": [], "suggestions": []}, usage


def _distribute_to_peers(findings: list[dict], suggestions: list[dict], codes: list[str],
                          source_doc: str, log_fn) -> list[dict]:
    results = []
    for peer in peers_store.list_peers(status="approved"):
        entry = {"peer_id": peer["id"], "hostname": peer["hostname"]}
        try:
            payload_findings = list(findings)
            for f, code in zip(payload_findings, codes):
                f["code"] = code
            payload_suggestions = []
            for s in suggestions:
                idx = s.get("finding_index")
                if idx is None or not (0 <= idx < len(codes)):
                    continue
                payload_suggestions.append({
                    "event_id": s.get("event_id"), "finding_code": codes[idx],
                    "source_doc": source_doc,
                    "confidence": s.get("confidence", 0.5), "text": s.get("text", ""),
                })
            resp = peer_client.post(
                peer["api_url"] + "/v1/peers/sentinel-relay",
                {"source_doc": source_doc, "findings": payload_findings,
                 "suggestions": payload_suggestions},
                token=peer["remote_token"])
            if resp.status == 200:
                entry["status"] = resp.body.get("status", "synced")
            else:
                entry["status"] = f"error-{resp.status}"
        except peer_client.PeerUnreachable:
            entry["status"] = "unreachable"
        except Exception as e:
            entry["status"] = f"error: {e}"
        results.append(entry)
        log_fn(f"Peer {peer['hostname']}: {entry['status']}")
    return results
