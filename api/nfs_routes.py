"""NFS server API routes."""
from __future__ import annotations

import threading
from flask import Blueprint, request, jsonify

import nfs as nfs_compute
import nfs_store
import store as resource_store
from models import NfsServer

nfs_bp = Blueprint("nfs", __name__)


def _problem(status, title, detail):
    return jsonify({"status": status, "title": title, "detail": detail}), status


# ---------------------------------------------------------------------------
# NFS Servers
# ---------------------------------------------------------------------------

@nfs_bp.get("/v1/nfs-servers")
def list_nfs_servers():
    return jsonify({"items": [s.to_dict() for s in nfs_store.list_all()]})


@nfs_bp.post("/v1/nfs-servers")
def create_nfs_server():
    body = request.get_json(force=True) or {}
    name = body.get("name", "").strip()
    if not name:
        return _problem(400, "Bad Request", "name is required")
    for field in ("vpc_id",):
        if not body.get(field):
            return _problem(400, "Bad Request", f"{field} is required")
    if nfs_store.find_by_name(name):
        return _problem(409, "Conflict", f"NFS server '{name}' already exists")

    vpc = resource_store.get_vpc(body["vpc_id"])
    if not vpc:
        return _problem(404, "Not Found", f"VPC '{body['vpc_id']}' not found")

    shares = body.get("shares", [])
    for s in shares:
        if not s.get("name"):
            return _problem(400, "Bad Request", "each share requires a name")

    nfs = NfsServer(
        name=name,
        vpc_id=body["vpc_id"],
        flavor=body.get("flavor", "standard.medium"),
        disk_gb=int(body.get("disk_gb", 20)),
        shares=shares,
        tags=body.get("tags", {}),
    )
    nfs_store.put(nfs)

    def _launch():
        try:
            nfs_compute.create_nfs_server(nfs, vpc.cidr_block)
            ip = nfs_compute.get_nfs_server_ip(nfs.domain_name)
            nfs.private_ip = ip
        except Exception as e:
            from models import NfsServerStatus
            nfs.status = NfsServerStatus.ERROR
            import logging
            logging.getLogger(__name__).error("NFS server launch failed %s: %s", nfs.id, e)
        finally:
            nfs_store.put(nfs)

    threading.Thread(target=_launch, daemon=True).start()
    return jsonify(nfs.to_dict()), 202


@nfs_bp.get("/v1/nfs-servers/<nfs_id>")
def get_nfs_server(nfs_id):
    nfs = nfs_store.get(nfs_id)
    if not nfs:
        return _problem(404, "Not Found", f"NFS server '{nfs_id}' not found")
    if nfs.domain_name:
        nfs.status = nfs_compute.get_nfs_server_status(nfs.domain_name)
        if not nfs.private_ip:
            nfs.private_ip = nfs_compute.get_nfs_server_ip(nfs.domain_name)
        nfs_store.put(nfs)
    return jsonify(nfs.to_dict())


@nfs_bp.delete("/v1/nfs-servers/<nfs_id>")
def delete_nfs_server(nfs_id):
    nfs = nfs_store.get(nfs_id)
    if not nfs:
        return _problem(404, "Not Found", f"NFS server '{nfs_id}' not found")
    nfs_store.delete(nfs_id)

    def _destroy():
        try:
            nfs_compute.delete_nfs_server(nfs)
        except Exception as e:
            import logging
            logging.getLogger(__name__).error("NFS server delete failed %s: %s", nfs_id, e)

    threading.Thread(target=_destroy, daemon=True).start()
    return "", 204


# ---------------------------------------------------------------------------
# Shares (sub-resource)
# ---------------------------------------------------------------------------

@nfs_bp.post("/v1/nfs-servers/<nfs_id>/shares")
def add_share(nfs_id):
    nfs = nfs_store.get(nfs_id)
    if not nfs:
        return _problem(404, "Not Found", f"NFS server '{nfs_id}' not found")
    body = request.get_json(force=True) or {}
    share_name = body.get("name", "").strip()
    if not share_name:
        return _problem(400, "Bad Request", "name is required")
    if any(s["name"] == share_name for s in nfs.shares):
        return _problem(409, "Conflict", f"Share '{share_name}' already exists")

    share = {
        "name": share_name,
        "clients": body.get("clients", "vpc"),
        "path": f"/exports/{share_name}",
    }
    nfs.shares.append(share)
    nfs_store.put(nfs)

    if nfs.status.value == "running":
        def _reload():
            try:
                nfs_compute.reload_exports(nfs)
            except Exception as e:
                import logging
                logging.getLogger(__name__).error("exportfs reload failed %s: %s", nfs_id, e)
        threading.Thread(target=_reload, daemon=True).start()

    return jsonify(share), 201


@nfs_bp.delete("/v1/nfs-servers/<nfs_id>/shares/<share_name>")
def remove_share(nfs_id, share_name):
    nfs = nfs_store.get(nfs_id)
    if not nfs:
        return _problem(404, "Not Found", f"NFS server '{nfs_id}' not found")
    if not any(s["name"] == share_name for s in nfs.shares):
        return _problem(404, "Not Found", f"Share '{share_name}' not found")
    nfs.shares = [s for s in nfs.shares if s["name"] != share_name]
    nfs_store.put(nfs)

    if nfs.status.value == "running":
        def _reload():
            try:
                nfs_compute.reload_exports(nfs)
            except Exception as e:
                import logging
                logging.getLogger(__name__).error("exportfs reload failed %s: %s", nfs_id, e)
        threading.Thread(target=_reload, daemon=True).start()

    return "", 204


# ---------------------------------------------------------------------------
# Mount info helper — returns cloud-init snippet for an instance
# ---------------------------------------------------------------------------

@nfs_bp.get("/v1/nfs-servers/<nfs_id>/shares/<share_name>/mount-config")
def mount_config(nfs_id, share_name):
    nfs = nfs_store.get(nfs_id)
    if not nfs:
        return _problem(404, "Not Found", f"NFS server '{nfs_id}' not found")
    share = next((s for s in nfs.shares if s["name"] == share_name), None)
    if not share:
        return _problem(404, "Not Found", f"Share '{share_name}' not found")
    mount_point = f"/mnt/{share_name}"
    export_path = share.get("path") or f"/exports/{share_name}"
    return jsonify({
        "nfs_server_ip": nfs.private_ip,
        "export_path": export_path,
        "mount_point": mount_point,
        "mount_command": nfs_compute.mount_command(nfs, share_name, mount_point),
        "cloud_init_entry": nfs_compute.cloud_init_mount_entry(
            nfs.private_ip, share_name, mount_point),
    })
