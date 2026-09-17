"""Build engine — template parsing, resource provisioning, playbook execution."""

from __future__ import annotations

import os
import re
import subprocess
import threading
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import json
import yaml

import random
import db
import failure_queue

API_TOKEN = os.environ.get("CLOUDCORE_API_TOKEN", "dev-token")

# In-memory cache for running builds (log lines appended live)
# Completed builds are read back from SQLite
_running: dict[str, dict] = {}


def _new_suffix() -> str:
    """6-digit zero-padded random suffix for unique resource names."""
    return f"{random.randint(0, 999999):06d}"


def load_builds() -> None:
    pass  # persistence is now handled by SQLite via db.init()


def _save_build(build: dict) -> None:
    if build["status"] in ("pending", "running"):
        return
    c = db.get_db()
    c.execute("""INSERT INTO builds
        (id,template,status,created_at,started_at,finished_at,created_by,
         var_overrides,log,exit_code,provisioned)
        VALUES (?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(id) DO UPDATE SET
            status=excluded.status, started_at=excluded.started_at,
            finished_at=excluded.finished_at, log=excluded.log,
            exit_code=excluded.exit_code, provisioned=excluded.provisioned""",
        (build["id"], build["template"], build["status"], build["created_at"],
         build.get("started_at"), build.get("finished_at"), build.get("created_by", "ui"),
         json.dumps(build.get("var_overrides", {})), json.dumps(build.get("log", [])),
         build.get("exit_code"), json.dumps(build.get("provisioned", []))))
    c.commit()
    # Remove from running cache once persisted
    _running.pop(build["id"], None)


def _build_from_row(row) -> dict:
    return {
        "id": row["id"], "template": row["template"], "status": row["status"],
        "created_at": row["created_at"], "started_at": row["started_at"],
        "finished_at": row["finished_at"], "created_by": row["created_by"],
        "var_overrides": json.loads(row["var_overrides"]),
        "log": json.loads(row["log"]),
        "exit_code": row["exit_code"],
        "provisioned": json.loads(row["provisioned"]),
    }


EXAMPLES_DIR   = Path(__file__).parent.parent / "ansible" / "examples"
INVENTORY_FILE = Path(__file__).parent.parent / "ansible" / "inventory.ini"
COLLECTION_PATH = Path(__file__).parent.parent / "ansible" / "collections"


# ---------------------------------------------------------------------------
# Template catalogue
# ---------------------------------------------------------------------------

_TEMPLATE_META = {
    "01-vpc-only.yml":           {"title": "VPC Only",                    "description": "Create a single VPC.",                                          "resources": ["vpc"]},
    "02-compute-basic.yml":      {"title": "Basic Compute",               "description": "VPC + single instance, waits for running.",                     "resources": ["vpc", "instance"]},
    "03-compute-with-dns.yml":   {"title": "Compute with DNS",            "description": "VPC + instance + DNS zone + A record.",                         "resources": ["vpc", "instance", "dns_zone", "dns_record"]},
    "04-load-balanced-web.yml":  {"title": "Load-Balanced Web (L7 ALB)",  "description": "VPC + 2 instances + application load balancer with backends.",   "resources": ["vpc", "instance", "load_balancer"]},
    "05-network-lb.yml":         {"title": "Network Load Balancer (L4)",  "description": "VPC + instances + internal L4 NLB.",                            "resources": ["vpc", "instance", "load_balancer"]},
    "06-full-stack.yml":         {"title": "Full Stack",                  "description": "VPC + 3 instances + ALB + DNS zone + CNAME.",                   "resources": ["vpc", "instance", "load_balancer", "dns_zone", "dns_record"]},
    "07-nfs-shared-storage.yml": {"title": "NFS Shared Storage",           "description": "VPC + NFS server (LVM data disk) + 2 instances with shared mount.", "resources": ["vpc", "nfs_server", "instance"]},
    "08-openstack-services.yml": {"title": "OpenStack Services Stack",       "description": "VPC + 6 named instances + admin/NFS + frontend ALB + backend NLB.",  "resources": ["vpc", "instance", "nfs_server", "load_balancer"]},
    "09-ghidra-workstation.yml": {"title": "Ghidra Workstation",            "description": "VPC + security group + XFCE desktop with Ghidra, browser-accessible via noVNC through a network LB.", "resources": ["vpc", "security_group", "instance", "load_balancer"]},
    "10-kiwix-library.yml":      {"title": "Kiwix Library",                "description": "VPC + security group + instance serving an offline Kiwix content library over HTTP through a load balancer.", "resources": ["vpc", "security_group", "instance", "load_balancer"]},
    "11-wifi-sniffer.yml":       {"title": "WiFi Sniffer",                 "description": "VPC + security group + instance running Kismet + aircrack-ng, driven by a passed-through USB WiFi adapter.", "resources": ["vpc", "security_group", "instance", "load_balancer"]},
    "12-ha-frontend-lb.yml":     {"title": "HA Frontend Load Balancer",    "description": "VPC + security groups + HA frontend instance group + 2 ProxySQL/NGINX/Keepalived nodes sharing a floating VIP + MySQL Group Replication + RabbitMQ + Keystone + backend application tier + shared NFS storage + DNS records.", "resources": ["vpc", "security_group", "instance", "nfs_server", "dns_record"]},
    "13-distributed-llm.yml":   {"title": "Distributed LLM Inference",   "description": "VPC + coordinator instance + one RPC worker per peer, splitting a 7B GGUF model's layers across hosts via llama.cpp's RPC backend, served through a load balancer.", "resources": ["vpc", "security_group", "instance", "load_balancer"]},
    "14-llm-chat.yml":          {"title": "LLM Chat",                    "description": "Same distributed llama.cpp coordinator + RPC worker(s) as Distributed LLM Inference, but for an interactive human chat session — open the load balancer's own address in a browser for llama-server's own built-in chat Web UI.", "resources": ["vpc", "security_group", "instance", "load_balancer"]},
}


def list_templates() -> list[dict]:
    templates = []
    for filename in sorted(EXAMPLES_DIR.glob("*.yml")):
        if filename.name not in _TEMPLATE_META:
            continue
        meta = _TEMPLATE_META[filename.name]
        templates.append({
            "id": filename.stem,
            "filename": filename.name,
            "title": meta.get("title", filename.stem),
            "description": meta.get("description", ""),
            "resources": meta.get("resources", []),
        })
    return templates


# ---------------------------------------------------------------------------
# Variable extraction — what does a template need?
# ---------------------------------------------------------------------------

def _has_jinja(val) -> bool:
    """Return True if val (or any string inside a list/dict) contains a Jinja2 expression."""
    if isinstance(val, str):
        return "{{" in val
    if isinstance(val, list):
        return any(_has_jinja(v) for v in val)
    if isinstance(val, dict):
        return any(_has_jinja(v) for v in val.values())
    return False


def extract_template_vars(filename: str) -> dict[str, Any]:
    """Parse a playbook's vars block and return the variable schema with defaults."""
    path = EXAMPLES_DIR / filename
    if not path.exists():
        raise FileNotFoundError(f"Template not found: {filename}")

    with open(path) as f:
        plays = yaml.safe_load(f)

    if not plays or not isinstance(plays, list):
        return {}

    play = plays[0]
    raw_vars = play.get("vars", {})

    # Resolve vars that reference group_vars defaults
    group_defaults = _load_group_defaults()

    schema: dict[str, Any] = {}
    for key, val in raw_vars.items():
        if _has_jinja(val):
            resolved = _resolve_jinja_var(val, {**group_defaults, **raw_vars}) if isinstance(val, str) else val
            schema[key] = {"default": resolved, "derived": True}
        else:
            schema[key] = {"default": val, "derived": False}

    # Always expose the connection vars and project/env targeting vars
    schema["default_project"]      = {"default": group_defaults.get("default_project",      "cloudcore-examples"),       "derived": False}
    schema["default_environment"]  = {"default": group_defaults.get("default_environment",  "dev"),                      "derived": False}
    schema["cloudcore_api_url"]    = {"default": group_defaults.get("cloudcore_api_url",    "http://127.0.0.1:8080"),    "derived": False}
    schema["cloudcore_api_token"]  = {"default": group_defaults.get("cloudcore_api_token",  "dev-token"),                "derived": False}
    schema["build_suffix"]         = {"default": _new_suffix(),                                                           "derived": False}

    return schema


def _load_group_defaults() -> dict:
    gv_path = EXAMPLES_DIR / "group_vars" / "all.yml"
    if not gv_path.exists():
        return {}
    with open(gv_path) as f:
        raw = yaml.safe_load(f) or {}
    # Strip Jinja2 expressions — return literal defaults only
    result = {}
    for k, v in raw.items():
        if isinstance(v, str) and "{{" in v:
            # Extract the default() value if present
            m = re.search(r"default\('([^']+)'", v)
            if m:
                result[k] = m.group(1)
        else:
            result[k] = v
    return result


def _resolve_jinja_var(expr: str, context: dict) -> str:
    """Best-effort single-level Jinja2 variable resolution."""
    def replacer(m):
        inner = m.group(1).strip()
        return str(context.get(inner, m.group(0)))
    return re.sub(r"\{\{\s*(\w+)\s*\}\}", replacer, expr)


# ---------------------------------------------------------------------------
# Build submission
# ---------------------------------------------------------------------------

def submit_build(template_filename: str, var_overrides: dict, created_by: str = "ui") -> dict:
    """Create a build record and start execution in a background thread."""
    build_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc).isoformat()

    build = {
        "id": build_id,
        "template": template_filename,
        "status": "pending",
        "created_at": now,
        "started_at": None,
        "finished_at": None,
        "created_by": created_by,
        "var_overrides": var_overrides,
        "log": [],
        "exit_code": None,
        "provisioned": [],
    }
    _running[build_id] = build

    thread = threading.Thread(target=_run_build, args=(build_id, var_overrides), daemon=True)
    thread.start()
    return build


def mark_build_destroyed(build_id: str) -> None:
    db.get_db().execute(
        "UPDATE builds SET status='destroyed' WHERE id=?", (build_id,))
    db.get_db().commit()


def get_build(build_id: str) -> dict | None:
    if build_id in _running:
        return _running[build_id]
    row = db.get_db().execute("SELECT * FROM builds WHERE id=?", (build_id,)).fetchone()
    return _build_from_row(row) if row else None


def list_builds() -> list[dict]:
    db_builds = [
        _build_from_row(r) for r in
        db.get_db().execute("SELECT * FROM builds ORDER BY created_at DESC").fetchall()
    ]
    running = sorted(_running.values(), key=lambda b: b["created_at"], reverse=True)
    # running builds come first, then completed from DB
    seen = {b["id"] for b in running}
    return running + [b for b in db_builds if b["id"] not in seen]


# ---------------------------------------------------------------------------
# Build execution
# ---------------------------------------------------------------------------

def _log(build: dict, line: str) -> None:
    ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
    build["log"].append(f"[{ts}] {line}")


def _snapshot(api_token: str) -> dict:
    """Capture current resource IDs across all types."""
    import urllib.request
    base = "http://127.0.0.1:8080"
    headers = {"Authorization": f"Bearer {api_token}"}
    result = {}
    for rtype, path in (
        ("vpc",            "/v1/vpcs"),
        ("instance",       "/v1/instances"),
        ("lb",             "/v1/load-balancers"),
        ("dns_zone",       "/v1/dns/zones"),
        ("nfs_server",     "/v1/nfs-servers"),
        ("security_group", "/v1/security-groups"),
    ):
        req = urllib.request.Request(base + path, headers=headers)
        with urllib.request.urlopen(req, timeout=5) as r:
            data = json.loads(r.read())
        result[rtype] = {
            item["id" if "id" in item else "name"]: item.get("name", item.get("id", ""))
            for item in data.get("items", [])
        }
    return result


def _diff_snapshots(before: dict, after: dict) -> list[dict]:
    """Return list of {type, id, name} for resources present in after but not before."""
    provisioned = []
    for rtype, after_items in after.items():
        before_ids = set(before.get(rtype, {}).keys())
        for rid, name in after_items.items():
            if rid not in before_ids:
                provisioned.append({"type": rtype, "id": rid, "name": name})
    return provisioned


def _run_build(build_id: str, var_overrides: dict) -> None:
    build = _running[build_id]
    build["status"] = "running"
    build["started_at"] = datetime.now(timezone.utc).isoformat()

    api_token = var_overrides.get("cloudcore_api_token") or _load_group_defaults().get("cloudcore_api_token", "dev-token")
    try:
        snapshot_before = _snapshot(api_token)
    except Exception:
        snapshot_before = {}

    try:
        _log(build, f"Starting build: {build['template']}")
        _execute_playbook(build, var_overrides)
    except Exception as e:
        _log(build, f"ERROR: {e}")
        build["status"] = "failed"
        build["exit_code"] = -1
    finally:
        build["finished_at"] = datetime.now(timezone.utc).isoformat()
        if snapshot_before:
            try:
                snapshot_after = _snapshot(api_token)
                build["provisioned"] = _diff_snapshots(snapshot_before, snapshot_after)
            except Exception:
                pass
        if build["status"] == "failed":
            _cleanup_failed_build(build, api_token)
        _save_build(build)


# Deletion-safe ordering for auto-cleanup below — instances first (they
# depend on the network scaffolding around them), then mid-tier
# resources, then security groups, then the VPC last, since a VPC
# delete is refused (409) while anything else inside it still exists.
_DESTROY_ORDER = {"instance": 0, "lb": 1, "nfs_server": 1, "dns_zone": 1,
                   "security_group": 2, "vpc": 3}
_DESTROY_PATH = {
    "vpc":            lambda i: f"/v1/vpcs/{i}",
    "instance":       lambda i: f"/v1/instances/{i}",
    "lb":             lambda i: f"/v1/load-balancers/{i}",
    "dns_zone":       lambda i: f"/v1/dns/zones/{i}",
    "nfs_server":     lambda i: f"/v1/nfs-servers/{i}",
    "security_group": lambda i: f"/v1/security-groups/{i}",
}


def _cleanup_failed_build(build: dict, api_token: str) -> None:
    """Auto-destroy whatever a failed build managed to create before
    it failed, and queue its own log for the next llm_ingest wakeup to
    analyze — per direct request: "we need to look at removing
    orphaned resources when we allow/commit a destroy. we leave too
    many resources left over when builds fail." Safe to destroy
    immediately rather than waiting for a human to notice, since it's
    the build's own LOG that actually gets inspected afterwards (kept
    deliberately, queued for LLM analysis below), not live access to
    whatever got created."""
    provisioned = sorted(build.get("provisioned") or [],
                          key=lambda r: _DESTROY_ORDER.get(r["type"], 1))
    if provisioned:
        _log(build, f"Auto-destroying {len(provisioned)} resource(s) left by this failed build...")
        for r in provisioned:
            path_fn = _DESTROY_PATH.get(r["type"])
            if not path_fn:
                continue
            try:
                req = urllib.request.Request(
                    f"http://127.0.0.1:8080{path_fn(r['id'])}",
                    headers={"Authorization": f"Bearer {api_token or API_TOKEN}"}, method="DELETE")
                with urllib.request.urlopen(req, timeout=15) as resp:
                    _log(build, f"  destroyed {r['type']} {r['name']} ({resp.status})")
            except Exception as e:
                _log(build, f"  WARNING: failed to auto-destroy {r['type']} {r['name']}: {e}")

    try:
        failure_queue.queue_failure(
            "ansible", build["id"], build["template"],
            build.get("var_overrides", {}), build.get("log", []), build.get("exit_code"))
    except Exception as e:
        _log(build, f"WARNING: failed to queue build log for LLM analysis: {e}")


def _build_extra_vars(var_overrides: dict) -> dict:
    """Merge group_var defaults with any user overrides, ensuring build_suffix is always set."""
    defaults = _load_group_defaults()
    merged = {**defaults, **var_overrides}
    if not merged.get("build_suffix"):
        merged["build_suffix"] = _new_suffix()
    return merged


def _execute_playbook(build: dict, var_overrides: dict) -> None:
    playbook_path = EXAMPLES_DIR / build["template"]
    if not playbook_path.exists():
        raise FileNotFoundError(f"Playbook not found: {build['template']}")

    extra_vars = _build_extra_vars(var_overrides)

    # Write extra-vars to a temp file to avoid shell escaping issues
    import json, tempfile, os
    with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
        json.dump(extra_vars, f)
        vars_file = f.name

    env = {
        **os.environ,
        "ANSIBLE_COLLECTIONS_PATH": str(Path.home() / ".ansible" / "collections"),
        "ANSIBLE_STDOUT_CALLBACK": "default",
        "ANSIBLE_FORCE_COLOR": "0",
        "PYTHONUNBUFFERED": "1",
    }

    cmd = [
        "ansible-playbook",
        str(playbook_path),
        "-i", str(INVENTORY_FILE),
        "--extra-vars", f"@{vars_file}",
    ]

    # In SLIRP/no-bridge environments VMs never reach 'running' — skip wait tasks
    try:
        import sys
        sys.path.insert(0, str(Path(__file__).parent))
        import compute
        if not compute._bridge_usable():
            cmd += ["--skip-tags", "wait"]
    except Exception:
        pass

    _log(build, f"Command: {' '.join(cmd)}")
    _log(build, "─" * 60)

    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            env=env,
        )

        for line in proc.stdout:
            _log(build, line.rstrip())

        proc.wait()
        build["exit_code"] = proc.returncode
        build["status"] = "success" if proc.returncode == 0 else "failed"
        _log(build, "─" * 60)
        _log(build, f"Finished with exit code {proc.returncode}")

    finally:
        try:
            os.unlink(vars_file)
        except OSError:
            pass
