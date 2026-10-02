"""Lab-VM broker -- /v1/lab-vms (llm-chat-full-vm-Phased-Implementation.md, F1).

The llm-chat coordinator proves answers on throwaway full VMs, and gives each
student a personal one. It is itself a lab VM that runs model-written
commands, so it must never hold CloudCore's master token (F-201). This
broker is all it gets, with its own token (CLOUDCORE_LABVM_TOKEN):

- create a VM from a fixed template -- the caller chooses only a purpose
  (proof-target, proof-prober, student), a run id and a control public key;
  image, size, network and cloud-init are the broker's;
- list, get, touch (student activity) and delete *only* the VMs it created;
- quotas, and a reaper on this host that enforces lifetimes even if the
  coordinator dies: proofs 2 h; students 30 min idle / 4 h at most
  (user decisions, 2026-10-02).

It drives CloudCore's own API on 127.0.0.1:8080 with the master token, so
placement (recommend-placement over this host and every peer, from free
resources), peer proxying and teardown behave exactly as for any instance.
Reachable from guests on the examples listener (192.168.100.1:8083).
"""

from __future__ import annotations

import hmac
import ipaddress
import json
import logging
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from datetime import datetime, timedelta, timezone

from flask import Blueprint, jsonify, request

import capacity_gate
import cc_token
import compute
import db
import labnet_routes
import peer_client
import peers_store
import wireguard

log = logging.getLogger(__name__)

lab_vms_bp = Blueprint("lab_vms", __name__)

LAB_VM_REACHABLE_ENDPOINTS = {
    "lab_vms.create_lab_vm",
    "lab_vms.list_lab_vms",
    "lab_vms.get_lab_vm",
    "lab_vms.touch_lab_vm",
    "lab_vms.delete_lab_vm",
    "lab_vms.pair_lab_vm",
    "lab_vms.grow_lab_vm_disk",
}

_API = "http://127.0.0.1:8080"
IMAGE_ID = "ubuntu-22.04"
LAB_VPC, LAB_VPC_CIDR = "lab-vms", "10.250.0.0/16"
LAB_SUBNET, LAB_SUBNET_CIDR = "lab-vms-a", "10.250.0.0/24"
MAX_ACTIVE = 6
MAX_STUDENT = 2
REAP_INTERVAL_S = 60

# F4: what each purpose's VM needs at boot, installed by cloud-init while it
# boots (the image is the stock Ubuntu cloud image).
PACKAGES = {
    "proof-target": ["pamtester", "oathtool"],
    "proof-prober": ["python3-paramiko", "oathtool", "nfs-common", "dnsutils", "curl"],
    "student": ["pamtester", "oathtool"],
}

# F4: the lab's own control sshd, on its own port with its own config, so an
# answer that reconfigures the normal sshd (port, root login, PAM/MFA) can't
# cut off the runner. Key-only, no PAM (as the microVM's vsock control was).
_CONTROL_SSHD = """Port 1022
ListenAddress 0.0.0.0
HostKey /etc/ssh/ssh_host_ed25519_key
HostKey /etc/ssh/ssh_host_rsa_key
PermitRootLogin prohibit-password
PasswordAuthentication no
KbdInteractiveAuthentication no
UsePAM no
AllowUsers root student labctl
PidFile /run/sshd-control.pid
Subsystem sftp /usr/lib/openssh/sftp-server
X11Forwarding no
PrintMotd no
SetEnv PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
"""
_CONTROL_UNIT = """[Unit]
Description=Lab control sshd (port 1022)
After=network.target

[Service]
ExecStartPre=/bin/mkdir -p /run/sshd
ExecStartPre=/usr/sbin/sshd -t -f /etc/ssh/sshd_config_control
ExecStart=/usr/sbin/sshd -D -f /etc/ssh/sshd_config_control
Restart=always

[Install]
WantedBy=multi-user.target
"""


def _user_data(purpose: str, pubkey: str) -> str:
    """The fixed cloud-init template (the caller supplies only the key)."""
    def block(text: str, indent: int = 6) -> str:
        return "\n".join(" " * indent + line for line in text.rstrip("\n").split("\n"))
    pkgs = "".join(f"\n  - {p}" for p in PACKAGES.get(purpose, []))
    return f"""#cloud-config
package_update: true
packages:{pkgs}
write_files:
  - path: /etc/ssh/sshd_config_control
    permissions: "0644"
    content: |
{block(_CONTROL_SSHD)}
  - path: /etc/systemd/system/sshd-control.service
    permissions: "0644"
    content: |
{block(_CONTROL_UNIT)}
  - path: /root/.ssh/authorized_keys
    permissions: "0600"
    content: |
      {pubkey}
runcmd:
  # Not locked ("!"): sshd without PAM refuses locked accounts even for keys.
  - usermod -p '*' student
  - usermod -p '*' labctl
  - echo 'student ALL=(ALL) NOPASSWD:ALL' > /etc/sudoers.d/90-student && chmod 440 /etc/sudoers.d/90-student
  - systemctl daemon-reload
  - systemctl enable --now sshd-control.service
  # Saved while ufw is inactive, so an answer's "ufw enable" keeps it.
  - command -v ufw >/dev/null && ufw allow 1022/tcp comment lab-control || true
"""


# F3: blank data disks (GB) per purpose -- /dev/sdb, /dev/sdc in the guest,
# for partitioning/LVM/RAID answers; the prober needs none.
DATA_DISKS = {"proof-target": "2,2", "student": "2,2"}

# purpose -> (flavor candidates, largest first; max life; idle limit or None)
PURPOSES = {
    "proof-target": (["standard.large", "standard.medium"], timedelta(hours=2), None),
    "proof-prober": (["standard.small", "standard.nano"], timedelta(hours=2), None),
    "student": (["standard.large", "standard.medium"], timedelta(hours=4), timedelta(minutes=30)),
}
_PUBKEY_RE = re.compile(r"^(ssh-ed25519|ecdsa-sha2-nistp256|ssh-rsa) [A-Za-z0-9+/=]{40,800}( [\w@.:-]{0,64})?$")
_RUN_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,40}$")
_lock = threading.Lock()

_SCHEMA = """
CREATE TABLE IF NOT EXISTS lab_vms (
    id            TEXT PRIMARY KEY,
    name          TEXT NOT NULL,
    purpose       TEXT NOT NULL,
    run_id        TEXT NOT NULL,
    host_id       TEXT,
    flavor        TEXT NOT NULL,
    created_at    TEXT NOT NULL,
    expires_at    TEXT NOT NULL,
    idle_minutes  INTEGER,
    last_touch_at TEXT NOT NULL,
    deleted_at    TEXT,
    delete_reason TEXT
);
"""


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(t: datetime) -> str:
    return t.isoformat()


def _conn():
    conn = db.get_db()
    conn.executescript(_SCHEMA)
    return conn


def _problem(status: int, title: str, detail: str):
    return jsonify({"status": status, "title": title, "detail": detail}), status


@lab_vms_bp.before_request
def _require_token():
    """Only the broker token. (The master token never reaches the guest-
    facing listener -- server.py refuses it there -- and isn't needed:
    the dashboard has the full instance API.)"""
    expected = cc_token.labvm_token()
    auth = request.headers.get("Authorization", "")
    token = auth.removeprefix("Bearer ") if auth.startswith("Bearer ") else ""
    if expected and token and hmac.compare_digest(token, expected):
        return None
    return _problem(401, "Unauthorized", "the lab-VM broker token is required")


def _call(method: str, path: str, body: dict | None = None, timeout: float = 30.0) -> tuple[int, dict]:
    """CloudCore's own API with the master token, from this host only."""
    req = urllib.request.Request(
        _API + path, method=method, data=json.dumps(body).encode() if body is not None else None,
        headers={"Authorization": f"Bearer {cc_token.master_token()}", "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
            return resp.status, (json.loads(raw) if raw else {})
    except urllib.error.HTTPError as e:
        raw = e.read()
        try:
            return e.code, json.loads(raw) if raw else {}
        except ValueError:
            return e.code, {"detail": raw[:300].decode("utf-8", "replace")}


def _items(payload: dict) -> list:
    return payload.get("items", []) if isinstance(payload, dict) else []


def _ensure_network(peer_id: str | None) -> tuple[str, str]:
    """The lab VPC and subnet on the chosen host, created once, found by name."""
    tags = {"Project": "llm-chat", "ManagedBy": "lab-vm-broker", "Environment": "lab"}
    vpcs_path = f"/v1/peers/{peer_id}/vpcs" if peer_id else "/v1/vpcs"
    vpc = next((v for v in _items(_call("GET", vpcs_path)[1]) if v.get("name") == LAB_VPC), None)
    if not vpc:
        body = {"name": LAB_VPC, "cidr_block": LAB_VPC_CIDR, "tags": tags}
        if peer_id:
            body["peer_id"] = peer_id
        status, vpc = _call("POST", "/v1/vpcs", body)
        if status not in (200, 201):
            raise RuntimeError(f"creating the lab VPC failed ({status}): {vpc.get('detail')}")
    subnets_path = f"/v1/peers/{peer_id}/subnets" if peer_id else f"/v1/subnets?vpc_id={vpc['id']}"
    subnet = next((s for s in _items(_call("GET", subnets_path)[1])
                   if s.get("name") == LAB_SUBNET and s.get("vpc_id") == vpc["id"]), None)
    if not subnet:
        body = {"name": LAB_SUBNET, "vpc_id": vpc["id"], "cidr_block": LAB_SUBNET_CIDR, "tags": tags}
        if peer_id:
            body["peer_id"] = peer_id
        status, subnet = _call("POST", "/v1/subnets", body)
        if status not in (200, 201):
            raise RuntimeError(f"creating the lab subnet failed ({status}): {subnet.get('detail')}")
    return vpc["id"], subnet["id"]


def _caller_host(ip: str) -> str | None:
    """"" for this host, a peer id for a peer's bridge, None otherwise."""
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return None
    if addr.is_loopback or addr in ipaddress.ip_network(compute.bridge_cidr()):
        return ""
    for peer in peers_store.list_peers(status="approved"):
        subnet = peer.get("wg_bridge_subnet")
        if subnet and addr in ipaddress.ip_network(subnet, strict=False):
            return peer["id"]
        # A peer masquerades its guests' traffic into the WireGuard link, so
        # their requests arrive from that peer's transit address (found live:
        # the coordinator's arrived as 10.99.101.1, not 192.168.101.239).
        if subnet and ip == wireguard.peer_transit_ip(subnet):
            return peer["id"]
    return None


def _host_stats(peer_id: str | None) -> dict:
    status, stats = _call("GET", f"/v1/peers/{peer_id}/stats" if peer_id else "/v1/system/stats")
    if status != 200:
        return {}
    return stats.get("stats", stats) if isinstance(stats, dict) else {}


def _labnet(peer_id: str | None, method: str, a: str, b: str = "") -> tuple[bool, str]:
    """Pair/unpair on the host the VMs run on: the local helper, or the
    peer's /v1/labnet/pairs with that peer's token."""
    if not peer_id:
        return labnet_routes.helper(*(["pair", "add", a, b] if method == "pair" else ["unpair", a]))
    peer = peers_store.get_peer(peer_id)
    if not peer or peer.get("status") != "approved":
        return False, "peer not approved"
    try:
        if method == "pair":
            r = peer_client.post(peer["api_url"] + "/v1/labnet/pairs", {"a": a, "b": b}, token=peer["remote_token"])
        else:
            r = peer_client.delete(peer["api_url"] + "/v1/labnet/pairs?ip=" + urllib.parse.quote(a),
                                   token=peer["remote_token"])
    except peer_client.PeerUnreachable as e:
        return False, str(e)
    return r.status in (200, 201, 204), str(r.body)


def _row_dict(r) -> dict:
    return {k: r[k] for k in r.keys()}


def _active(conn) -> list:
    return conn.execute("SELECT * FROM lab_vms WHERE deleted_at IS NULL").fetchall()


@lab_vms_bp.post("/v1/lab-vms")
def create_lab_vm():
    body = request.get_json(force=True, silent=True) or {}
    purpose, run_id, pubkey = body.get("purpose"), str(body.get("run_id") or ""), str(body.get("public_key") or "").strip()
    if purpose not in PURPOSES:
        return _problem(400, "Bad Request", f"purpose must be one of {sorted(PURPOSES)}")
    if not _RUN_ID_RE.match(run_id):
        return _problem(400, "Bad Request", "run_id: 1-40 letters, digits, '-' or '_'")
    if not _PUBKEY_RE.match(pubkey):
        return _problem(400, "Bad Request", "public_key must be one OpenSSH public key line")
    flavors, max_life, idle = PURPOSES[purpose]
    with _lock:
        conn = _conn()
        active = _active(conn)
        if len(active) >= MAX_ACTIVE:
            return _problem(429, "Too Many Lab VMs", f"{len(active)} lab VMs already exist (limit {MAX_ACTIVE})")
        if purpose == "student" and sum(r["purpose"] == "student" for r in active) >= MAX_STUDENT:
            return _problem(429, "Too Many Lab VMs", f"{MAX_STUDENT} student machines already exist")
        # F2: on the caller's own host -- lab traffic never crosses the
        # WireGuard link, and lab VMs only go where an isolated lab
        # network exists. Not a named host: it follows the coordinator.
        host = _caller_host(request.remote_addr or "")
        if host is None:
            return _problem(403, "Forbidden", "the caller isn't on a CloudCore host's bridge")
        peer_id = host or None
        stats = _host_stats(peer_id)
        if not stats.get("lab_network"):
            return _problem(503, "No Lab Network",
                            "the caller's host has no isolated lab network (run api/setup-lab-network.sh there)")
        flavor = next((f for f in flavors if capacity_gate.affords(stats, f)), None)
        if not flavor:
            return _problem(503, "No Capacity", "the caller's host can't afford a lab VM right now")
        best = {"flavor": flavor, "hostname": "this host" if not peer_id else peer_id}
        try:
            vpc_id, subnet_id = _ensure_network(peer_id)
        except RuntimeError as e:
            return _problem(502, "Lab Network Unavailable", str(e))
        short = uuid.uuid4().hex[:8]
        name = f"labvm-{purpose.split('-')[-1]}-{short}"
        now = _now()
        req = {
            "name": name, "image_id": IMAGE_ID, "flavor": best["flavor"], "vpc_id": vpc_id, "subnet_id": subnet_id,
            # network=lab: compute attaches it to the isolated cclab0 bridge
            # or refuses to create it (F2).
            "tags": {"lab_vm": "true", "network": "lab", "purpose": purpose, "run_id": run_id,
                     "ManagedBy": "lab-vm-broker", "Project": "llm-chat", "Environment": "lab",
                     **({"data_disks": DATA_DISKS[purpose]} if purpose in DATA_DISKS else {})},
            # The control account: key-only, the caller's key. Everything else
            # the guest needs is installed by the advice runner over this.
            "users": [{"username": "labctl", "sudo": True, "ssh_keys": [pubkey]},
                      {"username": "student", "sudo": True, "ssh_keys": [pubkey]}],
            "user_data": _user_data(purpose, pubkey),
        }
        if peer_id:
            req["peer_id"] = peer_id
        status, inst = _call("POST", "/v1/instances", req, timeout=60)
        if status not in (200, 201, 202):
            return _problem(502, "Create Failed", f"CloudCore refused the instance ({status}): {inst.get('detail')}")
        conn.execute(
            "INSERT INTO lab_vms (id, name, purpose, run_id, host_id, flavor, created_at, expires_at, idle_minutes, "
            "last_touch_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (inst["id"], name, purpose, run_id, peer_id, best["flavor"], _iso(now), _iso(now + max_life),
             int(idle.total_seconds() // 60) if idle else None, _iso(now)))
        conn.commit()
    log.info("lab VM %s created for %s (%s on %s)", name, purpose, best["flavor"], best.get("hostname"))
    return jsonify(_describe(inst["id"])), 202


def _describe(vm_id: str) -> dict:
    conn = _conn()
    row = conn.execute("SELECT * FROM lab_vms WHERE id = ?", (vm_id,)).fetchone()
    out = _row_dict(row)
    if row["deleted_at"] is None:
        status, inst = _call("GET", f"/v1/instances/{vm_id}")
        if status == 200:
            out.update(status=inst.get("status"), ip=inst.get("private_ip") or "", ssh_user="labctl",
                       host=inst.get("host_hostname") or "", error=inst.get("error_message") or "")
        else:
            out.update(status="unknown", ip="", error=f"CloudCore returned {status}")
    else:
        out.update(status="deleted", ip="")
    return out


def _own(vm_id: str):
    row = _conn().execute("SELECT * FROM lab_vms WHERE id = ?", (vm_id,)).fetchone()
    return row


@lab_vms_bp.get("/v1/lab-vms")
def list_lab_vms():
    run_id = request.args.get("run_id", "")
    rows = _active(_conn())
    return jsonify({"items": [_row_dict(r) for r in rows if not run_id or r["run_id"] == run_id]})


@lab_vms_bp.get("/v1/lab-vms/<vm_id>")
def get_lab_vm(vm_id):
    if not _own(vm_id):
        return _problem(404, "Not Found", "no such lab VM")
    return jsonify(_describe(vm_id))


@lab_vms_bp.post("/v1/lab-vms/<vm_id>/touch")
def touch_lab_vm(vm_id):
    row = _own(vm_id)
    if not row or row["deleted_at"]:
        return _problem(404, "Not Found", "no such lab VM")
    conn = _conn()
    conn.execute("UPDATE lab_vms SET last_touch_at = ? WHERE id = ?", (_iso(_now()), vm_id))
    conn.commit()
    return jsonify(_describe(vm_id))


def _delete(vm_id: str, reason: str) -> tuple[bool, str]:
    row = _conn().execute("SELECT * FROM lab_vms WHERE id = ?", (vm_id,)).fetchone()
    ip = _describe(vm_id).get("ip") if row and row["deleted_at"] is None else ""
    if ip:
        ok, why = _labnet(row["host_id"], "unpair", ip)
        if not ok:
            log.warning("lab VM %s: unpairing %s failed: %s", vm_id, ip, why)
    status, body = _call("DELETE", f"/v1/instances/{vm_id}", timeout=60)
    if status in (200, 204, 404):
        conn = _conn()
        conn.execute("UPDATE lab_vms SET deleted_at = ?, delete_reason = ? WHERE id = ? AND deleted_at IS NULL",
                     (_iso(_now()), reason, vm_id))
        conn.commit()
        return True, ""
    return False, f"CloudCore returned {status}: {body.get('detail')}"


@lab_vms_bp.delete("/v1/lab-vms/<vm_id>")
def delete_lab_vm(vm_id):
    row = _own(vm_id)
    if not row:
        return _problem(404, "Not Found", "no such lab VM")
    if row["deleted_at"]:
        return "", 204
    ok, why = _delete(vm_id, "requested")
    return ("", 204) if ok else _problem(502, "Delete Failed", why)


@lab_vms_bp.post("/v1/lab-vms/<vm_id>/pair")
def pair_lab_vm(vm_id):
    """Let two lab VMs of the same run (target and prober) reach each other."""
    other = str((request.get_json(force=True, silent=True) or {}).get("with") or "")
    a, b = _own(vm_id), _own(other)
    if not a or not b or a["deleted_at"] or b["deleted_at"] or vm_id == other:
        return _problem(404, "Not Found", "both must be existing lab VMs")
    if a["run_id"] != b["run_id"] or (a["host_id"] or "") != (b["host_id"] or ""):
        return _problem(409, "Conflict", "only VMs of the same run on the same host can be paired")
    ia, ib = _describe(vm_id).get("ip"), _describe(other).get("ip")
    if not ia or not ib:
        return _problem(409, "Not Ready", "both VMs need an address first")
    ok, why = _labnet(a["host_id"], "pair", ia, ib)
    return (jsonify({"paired": [ia, ib]}), 201) if ok else _problem(502, "Pair Failed", why)


@lab_vms_bp.post("/v1/lab-vms/<vm_id>/grow-disk")
def grow_lab_vm_disk(vm_id):
    """F3: enlarge one of the VM's disks (root sda, data sdb/sdc) live, as a
    person would in their cloud console before following an answer about
    growing a filesystem."""
    row = _own(vm_id)
    if not row or row["deleted_at"]:
        return _problem(404, "Not Found", "no such lab VM")
    body = request.get_json(force=True, silent=True) or {}
    target = str(body.get("target") or "")
    if target not in ("sda", "sdb", "sdc"):
        return _problem(400, "Bad Request", "target must be sda, sdb or sdc")
    status, out = _call("POST", f"/v1/instances/{vm_id}/disks/{target}/resize",
                        {"size_gb": body.get("size_gb")}, timeout=60)
    return jsonify(out), status


def reap_once(now: datetime | None = None) -> list[str]:
    """Delete lab VMs past their lifetime or idle limit. Returns their ids."""
    now = now or _now()
    reaped = []
    for r in _active(_conn()):
        why = ""
        if now >= datetime.fromisoformat(r["expires_at"]):
            why = "lifetime reached"
        elif r["idle_minutes"] and now >= datetime.fromisoformat(r["last_touch_at"]) + timedelta(minutes=r["idle_minutes"]):
            why = "idle limit reached"
        if why:
            ok, err = _delete(r["id"], why)
            if ok:
                reaped.append(r["id"])
                log.info("lab VM %s reaped: %s", r["name"], why)
            else:
                log.warning("lab VM %s: reaping failed: %s", r["name"], err)
    return reaped


def _loop() -> None:
    while True:
        try:
            reap_once()
        except Exception:  # noqa: BLE001 -- the reaper must outlive any one bad row
            log.exception("lab VM reaper failed, continuing")
        time.sleep(REAP_INTERVAL_S)


def start() -> None:
    threading.Thread(target=_loop, name="lab-vm-reaper", daemon=True).start()
