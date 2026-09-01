"""OpenTofu build engine — template discovery, variable extraction, apply/destroy."""

from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path

import db

_running: dict[str, dict] = {}

EXAMPLES_DIR = Path(__file__).parent.parent / "examples"
PROVIDER_DIR = Path(__file__).parent.parent / "provider"

# Human-readable metadata for known example directories
_TEMPLATE_META = {
    "vpc-only": {
        "title": "VPC Only",
        "description": "VPC + two logical subnets (web/db).",
        "resources": ["vpc", "subnet"],
    },
    "compute-basic": {
        "title": "Basic Compute",
        "description": "VPC + subnets + security groups + single instance.",
        "resources": ["vpc", "subnet", "security_group", "instance"],
    },
    "load-balanced-web": {
        "title": "Load-Balanced Web (L7 ALB)",
        "description": "VPC + subnets + security groups + instance group + ALB.",
        "resources": ["vpc", "subnet", "security_group", "instance", "load_balancer"],
    },
    "network-lb": {
        "title": "Network Load Balancer (L4)",
        "description": "VPC + subnets + security groups + instance group + internal NLB.",
        "resources": ["vpc", "subnet", "security_group", "instance", "load_balancer"],
    },
    "full-stack": {
        "title": "Full Stack",
        "description": "VPC + subnets + security groups + web instance group + ALB. Uses all modules.",
        "resources": ["vpc", "subnet", "security_group", "instance", "load_balancer"],
    },
    "dns-with-compute": {
        "title": "DNS with Compute",
        "description": "VPC + subnets + instance + DNS zone + A record pointing to the instance.",
        "resources": ["vpc", "subnet", "security_group", "instance", "dns_zone", "dns_record"],
    },
    "nfs-shared-storage": {
        "title": "NFS Shared Storage",
        "description": "VPC + NFS server with two exports + two app instances.",
        "resources": ["vpc", "subnet", "security_group", "nfs_server", "instance"],
    },
    "openstack-services": {
        "title": "OpenStack Services Stack",
        "description": "VPC + 6 named instances (frontend, backend, mysql, keystone, rabbitmq, admin/NFS) + public frontend ALB + internal backend NLB.",
        "resources": ["vpc", "subnet", "security_group", "instance", "nfs_server", "load_balancer"],
    },
}


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------

def _save_build(build: dict) -> None:
    if build["status"] in ("pending", "running"):
        return
    c = db.get_db()
    c.execute("""INSERT INTO tofu_builds
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


# ---------------------------------------------------------------------------
# Template catalogue
# ---------------------------------------------------------------------------

def list_templates() -> list[dict]:
    templates = []
    for d in sorted(EXAMPLES_DIR.iterdir()):
        if not d.is_dir() or not any(d.glob("*.tf")):
            continue
        meta = _TEMPLATE_META.get(d.name, {})
        templates.append({
            "id": d.name,
            "filename": d.name,
            "title": meta.get("title", d.name.replace("-", " ").title()),
            "description": meta.get("description", ""),
            "resources": meta.get("resources", []),
        })
    return templates


# ---------------------------------------------------------------------------
# Variable extraction from variables.tf
# ---------------------------------------------------------------------------

def extract_template_vars(dir_name: str) -> dict:
    # Prefer variables.tf (best-practice layout); fall back to main.tf for legacy examples
    example_dir = EXAMPLES_DIR / dir_name
    path = example_dir / "variables.tf"
    if not path.exists():
        path = example_dir / "main.tf"
    if not path.exists():
        return _connection_vars()

    content = path.read_text()
    schema = {}

    # Match both single-line and multi-line variable blocks
    for block in re.finditer(
        r'variable\s+"(\w+)"\s*\{([^}]*)\}', content, re.DOTALL
    ):
        name = block.group(1)
        body = block.group(2)
        default_match = re.search(r'default\s*=\s*"([^"]*)"', body) or \
                        re.search(r'default\s*=\s*(\S+)', body)
        if default_match:
            schema[name] = {"default": default_match.group(1).strip(), "derived": False}
        else:
            schema[name] = {"default": "", "derived": False}

    schema.update(_connection_vars())
    return schema


def _connection_vars() -> dict:
    return {
        "cloudcore_api_url":   {"default": "http://127.0.0.1:8080", "derived": False},
        "cloudcore_api_token": {"default": "dev-token",             "derived": False},
    }


# ---------------------------------------------------------------------------
# Build lifecycle
# ---------------------------------------------------------------------------

def submit_build(dir_name: str, var_overrides: dict, created_by: str = "ui") -> dict:
    build_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc).isoformat()
    build = {
        "id": build_id,
        "template": dir_name,
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
    threading.Thread(target=_run_build, args=(build_id, var_overrides), daemon=True).start()
    return build


def mark_build_destroyed(build_id: str) -> None:
    db.get_db().execute("UPDATE tofu_builds SET status='destroyed' WHERE id=?", (build_id,))
    db.get_db().commit()


def get_build(build_id: str) -> dict | None:
    if build_id in _running:
        return _running[build_id]
    row = db.get_db().execute("SELECT * FROM tofu_builds WHERE id=?", (build_id,)).fetchone()
    return _build_from_row(row) if row else None


def list_builds() -> list[dict]:
    db_builds = [
        _build_from_row(r) for r in
        db.get_db().execute("SELECT * FROM tofu_builds ORDER BY created_at DESC").fetchall()
    ]
    running = sorted(_running.values(), key=lambda b: b["created_at"], reverse=True)
    seen = {b["id"] for b in running}
    return running + [b for b in db_builds if b["id"] not in seen]


# ---------------------------------------------------------------------------
# Execution
# ---------------------------------------------------------------------------

def _log(build: dict, line: str) -> None:
    ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
    build["log"].append(f"[{ts}] {line}")


def _snapshot(api_token: str) -> dict:
    import urllib.request
    base = "http://127.0.0.1:8080"
    headers = {"Authorization": f"Bearer {api_token}"}
    result = {}
    for rtype, path in (
        ("vpc",        "/v1/vpcs"),
        ("instance",   "/v1/instances"),
        ("lb",         "/v1/load-balancers"),
        ("dns_zone",   "/v1/dns/zones"),
        ("nfs_server", "/v1/nfs-servers"),
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

    api_token = var_overrides.get("cloudcore_api_token", "dev-token")
    try:
        snapshot_before = _snapshot(api_token)
    except Exception:
        snapshot_before = {}

    try:
        _log(build, f"Starting OpenTofu build: {build['template']}")
        _execute_tofu(build, var_overrides)
    except Exception as e:
        _log(build, f"ERROR: {e}")
        build["status"] = "failed"
        build["exit_code"] = -1
    finally:
        build["finished_at"] = datetime.now(timezone.utc).isoformat()
        if build["status"] == "success" and snapshot_before:
            try:
                snapshot_after = _snapshot(api_token)
                build["provisioned"] = _diff_snapshots(snapshot_before, snapshot_after)
            except Exception:
                pass
        _save_build(build)


def _find_tofu() -> str:
    for candidate in ("tofu", "opentofu"):
        if subprocess.run(["which", candidate], capture_output=True).returncode == 0:
            return candidate
    raise FileNotFoundError(
        "OpenTofu not found. Install from https://opentofu.org/docs/intro/install/ "
        "and ensure 'tofu' is on PATH."
    )


def _execute_tofu(build: dict, var_overrides: dict) -> None:
    work_dir = EXAMPLES_DIR / build["template"]
    if not work_dir.exists():
        raise FileNotFoundError(f"Example directory not found: {build['template']}")

    tofu = _find_tofu()

    # Build -var flags for connection vars; everything else goes via TF_VAR_ env
    api_url   = var_overrides.get("cloudcore_api_url",   "http://127.0.0.1:8080")
    api_token = var_overrides.get("cloudcore_api_token", "dev-token")

    tofurc = Path.home() / ".tofurc"
    env = {
        **os.environ,
        "CLOUDCORE_API_URL":   api_url,
        "CLOUDCORE_API_TOKEN": api_token,
        "TF_INPUT":            "0",
        "TF_IN_AUTOMATION":    "1",
    }
    if tofurc.exists():
        env["TF_CLI_CONFIG_FILE"] = str(tofurc)
    # Pass any extra user vars as TF_VAR_*
    for k, v in var_overrides.items():
        if k not in ("cloudcore_api_url", "cloudcore_api_token"):
            env[f"TF_VAR_{k}"] = str(v)

    def _run_cmd(cmd: list[str]) -> int:
        _log(build, f"$ {' '.join(cmd)}")
        _log(build, "─" * 60)
        proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, env=env, cwd=str(work_dir),
        )
        for line in proc.stdout:
            _log(build, line.rstrip())
        proc.wait()
        _log(build, "─" * 60)
        return proc.returncode

    # Skip tofu init when dev_overrides are active — it errors against the real registry
    using_dev_overrides = tofurc.exists() and "dev_overrides" in tofurc.read_text()
    if not using_dev_overrides:
        rc = _run_cmd([tofu, "init", "-no-color"])
        if rc != 0:
            build["exit_code"] = rc
            build["status"] = "failed"
            _log(build, f"tofu init failed (exit {rc})")
            return
    else:
        _log(build, "Skipping tofu init (dev_overrides active)")

    # tofu apply
    rc = _run_cmd([tofu, "apply", "-auto-approve", "-no-color"])
    build["exit_code"] = rc
    build["status"] = "success" if rc == 0 else "failed"
    _log(build, f"Finished with exit code {rc}")
