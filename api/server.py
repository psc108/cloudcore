from __future__ import annotations

import functools
import os
import threading
from flask import Flask, request, jsonify, abort, send_from_directory

import store
import compute
import lb as lb_backend
import dns as dns_store
import build_engine
import db
import nfs_store
import sg_store
import sg as sg_enforce
from models import VPC, Instance, LoadBalancer, InstanceStatus
from build_manager_routes import bm as build_manager_blueprint
from nfs_routes import nfs_bp
from sg_routes import sg_bp
from editor_routes import editor_bp
from about_routes import about_bp
from tofu_routes import tofu_bp

UI_DIR   = os.path.join(os.path.dirname(__file__), "..", "ui")
HELP_FILE = os.path.join(os.path.dirname(__file__), "..", "HELP.md")
app = Flask(__name__)
app.register_blueprint(build_manager_blueprint)
app.register_blueprint(nfs_bp)
app.register_blueprint(sg_bp)
app.register_blueprint(editor_bp)
app.register_blueprint(about_bp)
app.register_blueprint(tofu_bp)
API_TOKEN = os.environ.get("CLOUDCORE_API_TOKEN", "dev-token")


@app.after_request
def _cors(response):
    response.headers["Access-Control-Allow-Origin"]  = "*"
    response.headers["Access-Control-Allow-Headers"] = "Authorization, Content-Type"
    response.headers["Access-Control-Allow-Methods"] = "GET, POST, PUT, DELETE, OPTIONS"
    return response


@app.get("/")
def ui():
    return send_from_directory(UI_DIR, "index.html")


@app.get("/help")
def help_doc():
    try:
        with open(HELP_FILE) as f:
            return f.read(), 200, {"Content-Type": "text/markdown; charset=utf-8"}
    except FileNotFoundError:
        return problem(404, "Not Found", "HELP.md not found")


def require_auth(f):
    @functools.wraps(f)
    def wrapper(*args, **kwargs):
        auth = request.headers.get("Authorization", "")
        if auth != f"Bearer {API_TOKEN}":
            abort(401)
        return f(*args, **kwargs)
    return wrapper


def problem(status: int, title: str, detail: str):
    return jsonify({"status": status, "title": title, "detail": detail}), status


# ---------------------------------------------------------------------------
# VPCs
# ---------------------------------------------------------------------------

@app.get("/v1/vpcs")
@require_auth
def list_vpcs():
    return jsonify({"items": [v.to_dict() for v in store.list_vpcs()]})


@app.post("/v1/vpcs")
@require_auth
def create_vpc():
    body = request.get_json(force=True) or {}
    name = body.get("name", "").strip()
    if not name:
        return problem(400, "Bad Request", "name is required")
    if store.find_vpc_by_name(name):
        return problem(409, "Conflict", f"VPC '{name}' already exists")

    vpc = VPC(
        name=name,
        cidr_block=body.get("cidr_block", "10.0.0.0/16"),
        dns_support=body.get("dns_support", True),
        tags=body.get("tags", {}),
    )
    store.put_vpc(vpc)
    return jsonify(vpc.to_dict()), 201


@app.get("/v1/vpcs/<vpc_id>")
@require_auth
def get_vpc(vpc_id):
    vpc = store.get_vpc(vpc_id)
    if not vpc:
        return problem(404, "Not Found", f"VPC '{vpc_id}' not found")
    return jsonify(vpc.to_dict())


@app.put("/v1/vpcs/<vpc_id>")
@require_auth
def update_vpc(vpc_id):
    vpc = store.get_vpc(vpc_id)
    if not vpc:
        return problem(404, "Not Found", f"VPC '{vpc_id}' not found")
    body = request.get_json(force=True) or {}
    vpc.name = body.get("name", vpc.name)
    vpc.dns_support = body.get("dns_support", vpc.dns_support)
    vpc.tags = body.get("tags", vpc.tags)
    store.put_vpc(vpc)
    return jsonify(vpc.to_dict())


@app.delete("/v1/vpcs/<vpc_id>")
@require_auth
def delete_vpc(vpc_id):
    if not store.delete_vpc(vpc_id):
        return problem(404, "Not Found", f"VPC '{vpc_id}' not found")
    return "", 204


# ---------------------------------------------------------------------------
# Instances
# ---------------------------------------------------------------------------

@app.get("/v1/instances")
@require_auth
def list_instances():
    return jsonify({"items": [i.to_dict() for i in store.list_instances()]})


@app.post("/v1/instances")
@require_auth
def create_instance():
    body = request.get_json(force=True) or {}
    name = body.get("name", "").strip()
    if not name:
        return problem(400, "Bad Request", "name is required")
    for field in ("image_id", "flavor", "vpc_id", "subnet_id"):
        if not body.get(field):
            return problem(400, "Bad Request", f"{field} is required")
    if store.find_instance_by_name(name):
        return problem(409, "Conflict", f"Instance '{name}' already exists")

    instance = Instance(
        name=name,
        image_id=body["image_id"],
        flavor=body["flavor"],
        vpc_id=body["vpc_id"],
        subnet_id=body["subnet_id"],
        security_group_ids=body.get("security_group_ids", []),
        user_data=body.get("user_data"),
        ssh_user=compute.ssh_user_for_image(body["image_id"]),
        tags=body.get("tags", {}),
    )
    store.put_instance(instance)

    # Launch VM asynchronously so the API returns immediately
    def _launch():
        try:
            compute.create_instance(instance)
            ip = compute.get_instance_ip(instance.domain_name)
            instance.private_ip = ip
            dns_store.upsert_record(
                "instances.cloudcore.local", instance.name, "A",
                ip or "127.0.0.1", resource_type="instance", resource_id=instance.id,
            )
            # Apply security group rules once the VM is up
            if instance.security_group_ids:
                from sg_routes import _merged_rules
                ingress, egress = _merged_rules(instance.security_group_ids)
                sg_enforce.apply(instance, ingress, egress)
        except Exception as e:
            from models import InstanceStatus
            instance.status = InstanceStatus.ERROR
            app.logger.error("Failed to create instance %s: %s", instance.id, e)
        finally:
            store.put_instance(instance)

    threading.Thread(target=_launch, daemon=True).start()
    return jsonify(instance.to_dict()), 202


@app.get("/v1/instances/<instance_id>")
@require_auth
def get_instance(instance_id):
    instance = store.get_instance(instance_id)
    if not instance:
        return problem(404, "Not Found", f"Instance '{instance_id}' not found")
    # Refresh status and IP from libvirt, but only update — never auto-delete
    if instance.domain_name:
        live_status = compute.get_instance_status(instance.domain_name)
        # Only update status if libvirt confirms a real state change
        if live_status != InstanceStatus.DELETED:
            instance.status = live_status
        if not instance.private_ip and live_status == InstanceStatus.RUNNING:
            instance.private_ip = compute.get_instance_ip(instance.domain_name)
        store.put_instance(instance)
    return jsonify(instance.to_dict())


@app.put("/v1/instances/<instance_id>")
@require_auth
def update_instance(instance_id):
    instance = store.get_instance(instance_id)
    if not instance:
        return problem(404, "Not Found", f"Instance '{instance_id}' not found")
    body = request.get_json(force=True) or {}
    instance.name = body.get("name", instance.name)
    instance.tags = body.get("tags", instance.tags)
    store.put_instance(instance)
    return jsonify(instance.to_dict())


@app.delete("/v1/instances/<instance_id>")
@require_auth
def delete_instance(instance_id):
    instance = store.get_instance(instance_id)
    if not instance:
        return problem(404, "Not Found", f"Instance '{instance_id}' not found")

    # Mark deleted immediately so list excludes it before async teardown completes
    store.delete_instance_record(instance_id)
    dns_store.delete_records_for_resource(instance_id)
    sg_enforce.remove(instance)

    def _destroy():
        try:
            compute.delete_instance(instance)
        except Exception as e:
            app.logger.error("Failed to delete instance %s: %s", instance_id, e)

    threading.Thread(target=_destroy, daemon=True).start()
    return "", 204


# ---------------------------------------------------------------------------
# Load Balancers
# ---------------------------------------------------------------------------

@app.get("/v1/load-balancers")
@require_auth
def list_lbs():
    return jsonify({"items": [lb.to_dict() for lb in store.list_lbs()]})


@app.post("/v1/load-balancers")
@require_auth
def create_lb():
    body = request.get_json(force=True) or {}
    name = body.get("name", "").strip()
    if not name:
        return problem(400, "Bad Request", "name is required")
    if store.find_lb_by_name(name):
        return problem(409, "Conflict", f"Load balancer '{name}' already exists")

    lb = LoadBalancer(
        name=name,
        type=body.get("type", "application"),
        vpc_id=body.get("vpc_id", ""),
        subnet_ids=body.get("subnet_ids", []),
        internal=body.get("internal", False),
        dns_name=f"{name}.lb.cloudcore.local",
        backends=body.get("backends", []),
        tags=body.get("tags", {}),
    )
    try:
        lb.listen_port = lb_backend.start(lb)
    except Exception as e:
        app.logger.error("HAProxy start failed for %s: %s", lb.id, e)
    store.put_lb(lb)
    try:
        dns_store.upsert_record(
            "lb.cloudcore.local", lb.name, "A", "127.0.0.1",
            resource_type="lb", resource_id=lb.id,
        )
    except Exception as e:
        app.logger.error("DNS registration failed for lb %s: %s", lb.id, e)
    return jsonify(lb.to_dict()), 201


@app.get("/v1/load-balancers/<lb_id>")
@require_auth
def get_lb(lb_id):
    lb = store.get_lb(lb_id)
    if not lb:
        return problem(404, "Not Found", f"Load balancer '{lb_id}' not found")
    return jsonify(lb.to_dict())


@app.put("/v1/load-balancers/<lb_id>")
@require_auth
def update_lb(lb_id):
    lb = store.get_lb(lb_id)
    if not lb:
        return problem(404, "Not Found", f"Load balancer '{lb_id}' not found")
    body = request.get_json(force=True) or {}
    lb.name = body.get("name", lb.name)
    lb.subnet_ids = body.get("subnet_ids", lb.subnet_ids)
    lb.internal = body.get("internal", lb.internal)
    lb.backends = body.get("backends", lb.backends)
    lb.tags = body.get("tags", lb.tags)
    try:
        lb_backend.reload(lb)
    except Exception as e:
        app.logger.error("HAProxy reload failed for %s: %s", lb_id, e)
    store.put_lb(lb)
    return jsonify(lb.to_dict())


@app.delete("/v1/load-balancers/<lb_id>")
@require_auth
def delete_lb(lb_id):
    if not store.delete_lb(lb_id):
        return problem(404, "Not Found", f"Load balancer '{lb_id}' not found")
    lb_backend.stop(lb_id)
    dns_store.delete_records_for_resource(lb_id)
    return "", 204


@app.post("/v1/load-balancers/<lb_id>/backends")
@require_auth
def add_backend(lb_id):
    lb = store.get_lb(lb_id)
    if not lb:
        return problem(404, "Not Found", f"Load balancer '{lb_id}' not found")
    body = request.get_json(force=True) or {}
    for field in ("name", "address", "port"):
        if not body.get(field):
            return problem(400, "Bad Request", f"{field} is required")
    lb.backends.append({"name": body["name"], "address": body["address"], "port": int(body["port"])})
    try:
        lb_backend.reload(lb)
    except Exception as e:
        app.logger.error("HAProxy reload failed for %s: %s", lb_id, e)
    store.put_lb(lb)
    return jsonify(lb.to_dict()), 201


@app.delete("/v1/load-balancers/<lb_id>/backends/<backend_name>")
@require_auth
def remove_backend(lb_id, backend_name):
    lb = store.get_lb(lb_id)
    if not lb:
        return problem(404, "Not Found", f"Load balancer '{lb_id}' not found")
    lb.backends = [b for b in lb.backends if b["name"] != backend_name]
    try:
        lb_backend.reload(lb)
    except Exception as e:
        app.logger.error("HAProxy reload failed for %s: %s", lb_id, e)
    store.put_lb(lb)
    return "", 204



# ---------------------------------------------------------------------------
# Instance users
# ---------------------------------------------------------------------------

@app.get("/v1/instances/<instance_id>/users")
@require_auth
def list_instance_users(instance_id):
    instance = store.get_instance(instance_id)
    if not instance:
        return problem(404, "Not Found", f"Instance '{instance_id}' not found")
    return jsonify({"items": instance.users})


@app.post("/v1/instances/<instance_id>/users")
@require_auth
def add_instance_user(instance_id):
    instance = store.get_instance(instance_id)
    if not instance:
        return problem(404, "Not Found", f"Instance '{instance_id}' not found")
    body = request.get_json(force=True) or {}
    username = body.get("username", "").strip()
    if not username:
        return problem(400, "Bad Request", "username is required")
    if any(u["username"] == username for u in instance.users):
        return problem(409, "Conflict", f"User '{username}' already exists on this instance")

    user_entry = {
        "username": username,
        "sudo": bool(body.get("sudo", False)),
        "ssh_keys": body.get("ssh_keys", []),
        "password_hash": body.get("password_hash", ""),
    }
    instance.users.append(user_entry)
    store.put_instance(instance)

    # If instance is running, apply via SSH immediately
    if instance.status.value == "running" and instance.ssh_host_port:
        def _apply():
            try:
                _ssh_add_user(instance, user_entry)
            except Exception as e:
                app.logger.error("Failed to add user %s to %s via SSH: %s", username, instance_id, e)
        import threading
        threading.Thread(target=_apply, daemon=True).start()

    return jsonify(user_entry), 201


@app.delete("/v1/instances/<instance_id>/users/<username>")
@require_auth
def remove_instance_user(instance_id, username):
    instance = store.get_instance(instance_id)
    if not instance:
        return problem(404, "Not Found", f"Instance '{instance_id}' not found")
    if not any(u["username"] == username for u in instance.users):
        return problem(404, "Not Found", f"User '{username}' not found on this instance")
    instance.users = [u for u in instance.users if u["username"] != username]
    store.put_instance(instance)

    if instance.status.value == "running" and instance.ssh_host_port:
        def _remove():
            try:
                _ssh_remove_user(instance, username)
            except Exception as e:
                app.logger.error("Failed to remove user %s from %s via SSH: %s", username, instance_id, e)
        import threading
        threading.Thread(target=_remove, daemon=True).start()

    return "", 204


def _ssh_run(instance, cmd: str) -> str:
    """Run a command on a running instance via SSH using the CloudCore key."""
    key = compute.get_cc_privkey_path()
    result = __import__("subprocess").run(
        ["ssh", "-i", key, "-p", str(instance.ssh_host_port),
         "-o", "StrictHostKeyChecking=no", "-o", "ConnectTimeout=10",
         f"{instance.ssh_user}@127.0.0.1", cmd],
        capture_output=True, text=True, timeout=30,
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip())
    return result.stdout.strip()


def _ssh_add_user(instance, user_entry: dict) -> None:
    uname = user_entry["username"]
    ph    = user_entry.get("password_hash", "")
    sudo  = user_entry.get("sudo", False)
    keys  = list(user_entry.get("ssh_keys", []))
    cc_pub = compute.get_cc_pubkey()
    if cc_pub:
        keys.append(cc_pub)

    cmds = [
        f"sudo useradd -m -s /bin/bash {uname} 2>/dev/null || true",
        f"sudo mkdir -p /home/{uname}/.ssh",
        f"sudo chmod 700 /home/{uname}/.ssh",
    ]
    if ph:
        cmds.append(f"echo '{uname}:{ph}' | sudo chpasswd -e")
    if sudo:
        cmds.append(f"echo '{uname} ALL=(ALL) NOPASSWD:ALL' | sudo tee /etc/sudoers.d/{uname} > /dev/null")
    for k in keys:
        cmds.append(f"echo '{k}' | sudo tee -a /home/{uname}/.ssh/authorized_keys > /dev/null")
    cmds += [
        f"sudo chmod 600 /home/{uname}/.ssh/authorized_keys",
        f"sudo chown -R {uname}:{uname} /home/{uname}/.ssh",
    ]
    _ssh_run(instance, " && ".join(cmds))


def _ssh_remove_user(instance, username: str) -> None:
    _ssh_run(instance, f"sudo userdel -r {username} 2>/dev/null || true && sudo rm -f /etc/sudoers.d/{username}")

# ---------------------------------------------------------------------------
# SSH key (inter-instance)
# ---------------------------------------------------------------------------

@app.get("/v1/ssh-key")
@require_auth
def get_ssh_key():
    pubkey = compute.get_cc_pubkey()
    if not pubkey:
        return problem(404, "Not Found", "CloudCore keypair not generated yet")
    return jsonify({"public_key": pubkey, "key_path": compute.get_cc_privkey_path()})


# ---------------------------------------------------------------------------
# Images (read-only catalogue)
# ---------------------------------------------------------------------------

@app.get("/v1/images")
@require_auth
def list_images():
    return jsonify({"items": compute.list_images()})


# ---------------------------------------------------------------------------
# DNS
# ---------------------------------------------------------------------------

@app.get("/v1/dns/zones")
@require_auth
def dns_list_zones():
    return jsonify({"items": dns_store.list_zones()})


@app.post("/v1/dns/zones")
@require_auth
def dns_create_zone():
    body = request.get_json(force=True) or {}
    name = body.get("name", "").strip()
    if not name:
        return problem(400, "Bad Request", "name is required")
    try:
        zone = dns_store.create_zone(name)
        return jsonify(zone), 201
    except ValueError as e:
        return problem(409, "Conflict", str(e))


@app.delete("/v1/dns/zones/<path:zone_name>")
@require_auth
def dns_delete_zone(zone_name):
    if zone_name in dns_store.BUILTIN_ZONES:
        return problem(400, "Bad Request", f"Cannot delete built-in zone '{zone_name}'")
    if not dns_store.delete_zone(zone_name):
        return problem(404, "Not Found", f"Zone '{zone_name}' not found")
    return "", 204


@app.get("/v1/dns/zones/<path:zone_name>/records")
@require_auth
def dns_list_records(zone_name):
    if dns_store.get_zone(zone_name) is None:
        return problem(404, "Not Found", f"Zone '{zone_name}' not found")
    return jsonify({"items": dns_store.list_records(zone_name)})


@app.post("/v1/dns/zones/<path:zone_name>/records")
@require_auth
def dns_create_record(zone_name):
    if dns_store.get_zone(zone_name) is None:
        return problem(404, "Not Found", f"Zone '{zone_name}' not found")
    body = request.get_json(force=True) or {}
    name  = body.get("name", "").strip()
    rtype = body.get("type", "A").strip().upper()
    value = body.get("value", "").strip()
    if not name or not value:
        return problem(400, "Bad Request", "name and value are required")
    if rtype not in ("A", "CNAME", "TXT"):
        return problem(400, "Bad Request", "type must be A, CNAME or TXT")
    rec = dns_store.upsert_record(zone_name, name, rtype, value,
                                  ttl=int(body.get("ttl", 300)))
    return jsonify(rec), 201


@app.delete("/v1/dns/zones/<path:zone_name>/records/<name>/<rtype>")
@require_auth
def dns_delete_record(zone_name, name, rtype):
    if not dns_store.delete_record(zone_name, name, rtype):
        return problem(404, "Not Found", f"Record '{name}/{rtype}' not found in zone '{zone_name}'")
    return "", 204


# ---------------------------------------------------------------------------
# Dashboard
# ---------------------------------------------------------------------------

@app.get("/v1/dashboard")
@require_auth
def dashboard():
    vpcs      = [v.to_dict() for v in store.list_vpcs()    if v.status != "deleted"]
    instances = [i.to_dict() for i in store.list_instances() if i.status.value not in ("deleted",)]
    lbs       = [l.to_dict() for l in store.list_lbs()     if l.status != "deleted"]
    zones     = dns_store.list_zones()
    nfs_servers = [n.to_dict() for n in nfs_store.list_all()]
    return jsonify({
        "vpcs":      vpcs,
        "instances": instances,
        "load_balancers": lbs,
        "dns_zones": zones,
        "nfs_servers": nfs_servers,
        "summary": {
            "vpcs":           len(vpcs),
            "instances":      len(instances),
            "instances_running": sum(1 for i in instances if i["status"] == "running"),
            "load_balancers": len(lbs),
            "dns_zones":      len(zones),
            "dns_records":    sum(z["record_count"] for z in zones),
            "nfs_servers":    len(nfs_servers),
        },
    })


# ---------------------------------------------------------------------------
# Startup reconciliation
# ---------------------------------------------------------------------------

def reconcile():
    """Reconcile persisted state against live libvirt domains and HAProxy processes."""
    app.logger.info("Reconciling state...")

    for instance in store.list_instances():
        if not instance.domain_name:
            continue
        live = compute.get_instance_status(instance.domain_name)
        if live == InstanceStatus.DELETED:
            instance.status = InstanceStatus.STOPPED
            instance.private_ip = ""
        else:
            instance.status = live
            if live == InstanceStatus.RUNNING:
                if not instance.private_ip:
                    instance.private_ip = compute.get_instance_ip(instance.domain_name)
                dns_store.upsert_record(
                    "instances.cloudcore.local", instance.name, "A",
                    instance.private_ip or "127.0.0.1",
                    resource_type="instance", resource_id=instance.id,
                )
        store.put_instance(instance)

    for lb in store.list_lbs():
        try:
            lb.listen_port = lb_backend.start(lb)
            store.put_lb(lb)
            dns_store.upsert_record(
                "lb.cloudcore.local", lb.name, "A", "127.0.0.1",
                resource_type="lb", resource_id=lb.id,
            )
        except Exception as e:
            app.logger.error("  lb %s reconcile failed: %s", lb.name, e)

    app.logger.info("Reconciliation complete.")


if __name__ == "__main__":
    db.init()
    dns_store.load()
    reconcile()
    app.run(host="127.0.0.1", port=8080, debug=False)
