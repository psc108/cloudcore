from __future__ import annotations

import functools
import os
import threading
from flask import Flask, request, jsonify, abort, send_from_directory

import store
import compute
import lb as lb_backend
import dns as dns_store
import dns_server
import build_engine
import db
import nfs_store
import sg_store
import sg as sg_enforce
import ipaddress
from models import VPC, Instance, LoadBalancer, InstanceStatus, Subnet, InternetGateway, RouteTable
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
    active_instances = store.list_instances_by_vpc(vpc_id)
    if active_instances:
        return problem(409, "Conflict",
            f"VPC '{vpc_id}' has {len(active_instances)} active instance(s) — delete them first")
    active_lbs = [lb for lb in store.list_lbs() if lb.vpc_id == vpc_id]
    if active_lbs:
        return problem(409, "Conflict",
            f"VPC '{vpc_id}' has {len(active_lbs)} active load balancer(s) — delete them first")
    active_sgs = [sg for sg in sg_store.list_security_groups()
                  if sg.vpc_id == vpc_id and sg.status.value == "active"]
    if active_sgs:
        return problem(409, "Conflict",
            f"VPC '{vpc_id}' has {len(active_sgs)} active security group(s) — delete them first")
    active_subnets = store.list_subnets_by_vpc(vpc_id)
    if active_subnets:
        return problem(409, "Conflict",
            f"VPC '{vpc_id}' has {len(active_subnets)} active subnet(s) — delete them first")
    if not store.delete_vpc(vpc_id):
        return problem(404, "Not Found", f"VPC '{vpc_id}' not found")
    return "", 204


# ---------------------------------------------------------------------------
# Subnets
# ---------------------------------------------------------------------------

def _cidr_contained(parent_cidr: str, child_cidr: str) -> bool:
    try:
        parent = ipaddress.ip_network(parent_cidr, strict=False)
        child = ipaddress.ip_network(child_cidr, strict=False)
        return child.subnet_of(parent)
    except ValueError:
        return False


@app.get("/v1/subnets")
@require_auth
def list_subnets():
    vpc_id = request.args.get("vpc_id")
    if vpc_id:
        return jsonify({"items": [s.to_dict() for s in store.list_subnets_by_vpc(vpc_id)]})
    return jsonify({"items": [s.to_dict() for s in store.list_subnets()]})


@app.post("/v1/subnets")
@require_auth
def create_subnet():
    body = request.get_json(force=True) or {}
    for field in ("name", "vpc_id", "cidr_block"):
        if not body.get(field):
            return problem(400, "Bad Request", f"'{field}' is required")

    vpc = store.get_vpc(body["vpc_id"])
    if not vpc:
        return problem(404, "Not Found", f"VPC '{body['vpc_id']}' not found")

    if not _cidr_contained(vpc.cidr_block, body["cidr_block"]):
        return problem(400, "Bad Request",
            f"Subnet CIDR '{body['cidr_block']}' is not contained within VPC CIDR '{vpc.cidr_block}'")

    if store.find_subnet_by_name(body["name"]):
        return problem(409, "Conflict", f"Subnet '{body['name']}' already exists")

    subnet = Subnet(
        name=body["name"],
        vpc_id=body["vpc_id"],
        cidr_block=body["cidr_block"],
        public=bool(body.get("public", False)),
        zone=body.get("zone", "a"),
        tags=body.get("tags", {}),
    )
    store.put_subnet(subnet)
    return jsonify(subnet.to_dict()), 201


@app.get("/v1/subnets/<subnet_id>")
@require_auth
def get_subnet(subnet_id):
    subnet = store.get_subnet(subnet_id)
    if not subnet:
        return problem(404, "Not Found", f"Subnet '{subnet_id}' not found")
    return jsonify(subnet.to_dict())


@app.put("/v1/subnets/<subnet_id>")
@require_auth
def update_subnet(subnet_id):
    subnet = store.get_subnet(subnet_id)
    if not subnet:
        return problem(404, "Not Found", f"Subnet '{subnet_id}' not found")
    body = request.get_json(force=True) or {}
    subnet.name = body.get("name", subnet.name)
    subnet.public = bool(body.get("public", subnet.public))
    subnet.zone = body.get("zone", subnet.zone)
    subnet.tags = body.get("tags", subnet.tags)
    store.put_subnet(subnet)
    return jsonify(subnet.to_dict())


@app.delete("/v1/subnets/<subnet_id>")
@require_auth
def delete_subnet(subnet_id):
    subnet = store.get_subnet(subnet_id)
    if not subnet:
        return problem(404, "Not Found", f"Subnet '{subnet_id}' not found")
    active_instances = [i for i in store.list_instances_by_vpc(subnet.vpc_id)
                        if i.subnet_id == subnet_id]
    if active_instances:
        return problem(409, "Conflict",
            f"Subnet '{subnet_id}' has {len(active_instances)} active instance(s) — delete them first")
    if not store.delete_subnet(subnet_id):
        return problem(404, "Not Found", f"Subnet '{subnet_id}' not found")
    return "", 204


# ---------------------------------------------------------------------------
# Internet Gateways
# ---------------------------------------------------------------------------

@app.get("/v1/internet-gateways")
@require_auth
def list_igws():
    vpc_id = request.args.get("vpc_id")
    if vpc_id:
        return jsonify({"items": [g.to_dict() for g in store.list_igws_by_vpc(vpc_id)]})
    return jsonify({"items": [g.to_dict() for g in store.list_igws()]})


@app.post("/v1/internet-gateways")
@require_auth
def create_igw():
    body = request.get_json(force=True) or {}
    for field in ("name", "vpc_id"):
        if not body.get(field):
            return problem(400, "Bad Request", f"'{field}' is required")
    if not store.get_vpc(body["vpc_id"]):
        return problem(404, "Not Found", f"VPC '{body['vpc_id']}' not found")
    if store.find_igw_by_name(body["name"]):
        return problem(409, "Conflict", f"Internet gateway '{body['name']}' already exists")
    igw = InternetGateway(
        name=body["name"], vpc_id=body["vpc_id"], tags=body.get("tags", {}),
    )
    store.put_igw(igw)
    return jsonify(igw.to_dict()), 201


@app.get("/v1/internet-gateways/<igw_id>")
@require_auth
def get_igw(igw_id):
    igw = store.get_igw(igw_id)
    if not igw:
        return problem(404, "Not Found", f"Internet gateway '{igw_id}' not found")
    return jsonify(igw.to_dict())


@app.put("/v1/internet-gateways/<igw_id>")
@require_auth
def update_igw(igw_id):
    igw = store.get_igw(igw_id)
    if not igw:
        return problem(404, "Not Found", f"Internet gateway '{igw_id}' not found")
    body = request.get_json(force=True) or {}
    igw.name = body.get("name", igw.name)
    igw.tags = body.get("tags", igw.tags)
    store.put_igw(igw)
    return jsonify(igw.to_dict())


@app.delete("/v1/internet-gateways/<igw_id>")
@require_auth
def delete_igw(igw_id):
    if not store.delete_igw(igw_id):
        return problem(404, "Not Found", f"Internet gateway '{igw_id}' not found")
    return "", 204


# ---------------------------------------------------------------------------
# Route Tables
# ---------------------------------------------------------------------------

@app.get("/v1/route-tables")
@require_auth
def list_route_tables():
    vpc_id = request.args.get("vpc_id")
    if vpc_id:
        return jsonify({"items": [rt.to_dict() for rt in store.list_route_tables_by_vpc(vpc_id)]})
    return jsonify({"items": [rt.to_dict() for rt in store.list_route_tables()]})


@app.post("/v1/route-tables")
@require_auth
def create_route_table():
    body = request.get_json(force=True) or {}
    for field in ("name", "vpc_id"):
        if not body.get(field):
            return problem(400, "Bad Request", f"'{field}' is required")
    if not store.get_vpc(body["vpc_id"]):
        return problem(404, "Not Found", f"VPC '{body['vpc_id']}' not found")
    if store.find_route_table_by_name(body["name"]):
        return problem(409, "Conflict", f"Route table '{body['name']}' already exists")
    rt = RouteTable(
        name=body["name"],
        vpc_id=body["vpc_id"],
        subnet_ids=body.get("subnet_ids", []),
        routes=body.get("routes", []),
        tags=body.get("tags", {}),
    )
    store.put_route_table(rt)
    return jsonify(rt.to_dict()), 201


@app.get("/v1/route-tables/<rt_id>")
@require_auth
def get_route_table(rt_id):
    rt = store.get_route_table(rt_id)
    if not rt:
        return problem(404, "Not Found", f"Route table '{rt_id}' not found")
    return jsonify(rt.to_dict())


@app.put("/v1/route-tables/<rt_id>")
@require_auth
def update_route_table(rt_id):
    rt = store.get_route_table(rt_id)
    if not rt:
        return problem(404, "Not Found", f"Route table '{rt_id}' not found")
    body = request.get_json(force=True) or {}
    rt.name = body.get("name", rt.name)
    rt.subnet_ids = body.get("subnet_ids", rt.subnet_ids)
    rt.routes = body.get("routes", rt.routes)
    rt.tags = body.get("tags", rt.tags)
    store.put_route_table(rt)
    return jsonify(rt.to_dict())


@app.delete("/v1/route-tables/<rt_id>")
@require_auth
def delete_route_table(rt_id):
    if not store.delete_route_table(rt_id):
        return problem(404, "Not Found", f"Route table '{rt_id}' not found")
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

    vpc = store.get_vpc(instance.vpc_id)
    vpc_cidr = vpc.cidr_block if vpc else "10.0.0.0/8"

    # Launch VM asynchronously so the API returns immediately
    def _launch():
        try:
            compute.create_instance(instance, vpc_cidr=vpc_cidr)
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
            # Reload any LBs in the same VPC so they pick up the new instance
            vpc_instances = store.list_instances_by_vpc(instance.vpc_id)
            for lb in store.list_lbs():
                if lb.vpc_id == instance.vpc_id:
                    try:
                        lb_backend.reload(lb, vpc_instances=vpc_instances)
                    except Exception as lb_err:
                        app.logger.warning("LB reload failed for %s: %s", lb.id, lb_err)
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

    # Reload LBs in the same VPC — deleted instance is already excluded from the query
    for lb in store.list_lbs():
        if lb.vpc_id == instance.vpc_id:
            try:
                lb_backend.reload(lb, vpc_instances=store.list_instances_by_vpc(instance.vpc_id))
            except Exception as lb_err:
                app.logger.warning("LB reload failed after instance delete %s: %s", instance_id, lb_err)

    def _destroy():
        try:
            compute.delete_instance(instance)
        except Exception as e:
            app.logger.error("Failed to delete instance %s: %s", instance_id, e)

    threading.Thread(target=_destroy, daemon=True).start()
    return "", 204


@app.post("/v1/instances/<instance_id>/stop")
@require_auth
def stop_instance(instance_id):
    instance = store.get_instance(instance_id)
    if not instance:
        return problem(404, "Not Found", f"Instance '{instance_id}' not found")
    if instance.status != InstanceStatus.RUNNING:
        return problem(409, "Conflict",
            f"Instance '{instance_id}' is {instance.status.value}, not running")
    if not instance.domain_name:
        return problem(409, "Conflict", "Instance has no associated domain — cannot stop")
    try:
        compute.stop_domain(instance.domain_name)
    except Exception as e:
        return problem(500, "Internal Server Error", str(e))
    instance.status = InstanceStatus.STOPPED
    store.put_instance(instance)
    for lb in store.list_lbs():
        if lb.vpc_id == instance.vpc_id:
            try:
                lb_backend.reload(lb, vpc_instances=store.list_instances_by_vpc(instance.vpc_id))
            except Exception as e:
                app.logger.warning("LB reload after stop %s: %s", instance_id, e)
    return jsonify(instance.to_dict())


@app.post("/v1/instances/<instance_id>/start")
@require_auth
def start_instance(instance_id):
    instance = store.get_instance(instance_id)
    if not instance:
        return problem(404, "Not Found", f"Instance '{instance_id}' not found")
    if instance.status not in (InstanceStatus.STOPPED,):
        return problem(409, "Conflict",
            f"Instance '{instance_id}' is {instance.status.value}, not stopped")
    if not instance.domain_name:
        return problem(409, "Conflict", "Instance has no associated domain — cannot start")
    try:
        compute.start_domain(instance.domain_name)
    except Exception as e:
        return problem(500, "Internal Server Error", str(e))
    instance.status = InstanceStatus.RUNNING
    store.put_instance(instance)
    for lb in store.list_lbs():
        if lb.vpc_id == instance.vpc_id:
            try:
                lb_backend.reload(lb, vpc_instances=store.list_instances_by_vpc(instance.vpc_id))
            except Exception as e:
                app.logger.warning("LB reload after start %s: %s", instance_id, e)
    return jsonify(instance.to_dict())


@app.post("/v1/instances/<instance_id>/reboot")
@require_auth
def reboot_instance(instance_id):
    instance = store.get_instance(instance_id)
    if not instance:
        return problem(404, "Not Found", f"Instance '{instance_id}' not found")
    if instance.status != InstanceStatus.RUNNING:
        return problem(409, "Conflict",
            f"Instance '{instance_id}' is {instance.status.value}, not running")
    if not instance.domain_name:
        return problem(409, "Conflict", "Instance has no associated domain — cannot reboot")
    try:
        compute.reboot_domain(instance.domain_name)
    except Exception as e:
        return problem(500, "Internal Server Error", str(e))
    return jsonify(instance.to_dict())


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
        lb.listen_port = lb_backend.start(
            lb, vpc_instances=store.list_instances_by_vpc(lb.vpc_id))
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
        lb_backend.reload(lb, vpc_instances=store.list_instances_by_vpc(lb.vpc_id))
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
        lb_backend.reload(lb, vpc_instances=store.list_instances_by_vpc(lb.vpc_id))
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
        lb_backend.reload(lb, vpc_instances=store.list_instances_by_vpc(lb.vpc_id))
    except Exception as e:
        app.logger.error("HAProxy reload failed for %s: %s", lb_id, e)
    store.put_lb(lb)
    return "", 204


@app.post("/v1/load-balancers/<lb_id>/listeners")
@require_auth
def add_listener(lb_id):
    lb = store.get_lb(lb_id)
    if not lb:
        return problem(404, "Not Found", f"Load balancer '{lb_id}' not found")
    body = request.get_json(force=True) or {}
    port = body.get("port")
    if not port:
        return problem(400, "Bad Request", "port is required")
    try:
        port = int(port)
    except (ValueError, TypeError):
        return problem(400, "Bad Request", "port must be an integer")
    if port < 1 or port > 65535:
        return problem(400, "Bad Request", "port must be between 1 and 65535")
    protocol = body.get("protocol", "HTTP").upper()
    if protocol not in ("HTTP", "HTTPS", "TCP"):
        return problem(400, "Bad Request", "protocol must be HTTP, HTTPS or TCP")
    if any(l["port"] == port for l in lb.listeners):
        return problem(409, "Conflict", f"Listener on port {port} already exists")
    from models import new_id
    listener = {
        "id": new_id(),
        "port": port,
        "protocol": protocol,
        "default_action": body.get("default_action", "forward"),
    }
    lb.listeners.append(listener)
    try:
        lb_backend.reload(lb, vpc_instances=store.list_instances_by_vpc(lb.vpc_id))
    except Exception as e:
        app.logger.error("HAProxy reload failed for %s: %s", lb_id, e)
    store.put_lb(lb)
    return jsonify(listener), 201


@app.delete("/v1/load-balancers/<lb_id>/listeners/<listener_id>")
@require_auth
def remove_listener(lb_id, listener_id):
    lb = store.get_lb(lb_id)
    if not lb:
        return problem(404, "Not Found", f"Load balancer '{lb_id}' not found")
    if not any(l["id"] == listener_id for l in lb.listeners):
        return problem(404, "Not Found", f"Listener '{listener_id}' not found")
    lb.listeners = [l for l in lb.listeners if l["id"] != listener_id]
    try:
        lb_backend.reload(lb, vpc_instances=store.list_instances_by_vpc(lb.vpc_id))
    except Exception as e:
        app.logger.error("HAProxy reload failed for %s: %s", lb_id, e)
    store.put_lb(lb)
    return "", 204


@app.put("/v1/load-balancers/<lb_id>/health-check")
@require_auth
def set_health_check(lb_id):
    lb = store.get_lb(lb_id)
    if not lb:
        return problem(404, "Not Found", f"Load balancer '{lb_id}' not found")
    body = request.get_json(force=True) or {}
    protocol = body.get("protocol", "HTTP").upper()
    if protocol not in ("HTTP", "TCP"):
        return problem(400, "Bad Request", "protocol must be HTTP or TCP")
    interval = body.get("interval", 30)
    try:
        interval = int(interval)
    except (ValueError, TypeError):
        return problem(400, "Bad Request", "interval must be an integer")
    lb.health_check = {
        "protocol": protocol,
        "path": body.get("path", "/") if protocol == "HTTP" else "",
        "interval": interval,
        "healthy_threshold": int(body.get("healthy_threshold", 2)),
        "unhealthy_threshold": int(body.get("unhealthy_threshold", 3)),
    }
    try:
        lb_backend.reload(lb, vpc_instances=store.list_instances_by_vpc(lb.vpc_id))
    except Exception as e:
        app.logger.error("HAProxy reload failed for %s: %s", lb_id, e)
    store.put_lb(lb)
    return jsonify(lb.health_check), 200


@app.delete("/v1/load-balancers/<lb_id>/health-check")
@require_auth
def delete_health_check(lb_id):
    lb = store.get_lb(lb_id)
    if not lb:
        return problem(404, "Not Found", f"Load balancer '{lb_id}' not found")
    lb.health_check = {}
    try:
        lb_backend.reload(lb, vpc_instances=store.list_instances_by_vpc(lb.vpc_id))
    except Exception as e:
        app.logger.error("HAProxy reload failed for %s: %s", lb_id, e)
    store.put_lb(lb)
    return "", 204


@app.get("/v1/load-balancers/<lb_id>/health")
@require_auth
def lb_backend_health(lb_id):
    lb = store.get_lb(lb_id)
    if not lb:
        return problem(404, "Not Found", f"Load balancer '{lb_id}' not found")
    backends = lb_backend.get_health(lb_id)
    healthy  = sum(1 for b in backends if b["healthy"])
    return jsonify({
        "lb_id":    lb_id,
        "running":  lb_backend.is_running(lb_id),
        "backends": backends,
        "summary":  {"total": len(backends), "healthy": healthy, "unhealthy": len(backends) - healthy},
    })


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
         "-o", "StrictHostKeyChecking=no",
         "-o", "UserKnownHostsFile=/dev/null",
         "-o", "ConnectTimeout=10",
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
    vpcs         = [v.to_dict() for v in store.list_vpcs()         if v.status != "deleted"]
    instances    = [i.to_dict() for i in store.list_instances()    if i.status.value not in ("deleted",)]
    lbs          = [l.to_dict() for l in store.list_lbs()          if l.status != "deleted"]
    subnets      = [s.to_dict() for s in store.list_subnets()]
    igws         = [g.to_dict() for g in store.list_igws()]
    route_tables = [r.to_dict() for r in store.list_route_tables()]
    zones        = dns_store.list_zones()
    nfs_servers  = [n.to_dict() for n in nfs_store.list_all()]
    return jsonify({
        "vpcs":          vpcs,
        "instances":     instances,
        "load_balancers": lbs,
        "subnets":       subnets,
        "internet_gateways": igws,
        "route_tables":  route_tables,
        "dns_zones":     zones,
        "nfs_servers":   nfs_servers,
        "summary": {
            "vpcs":             len(vpcs),
            "subnets":          len(subnets),
            "internet_gateways": len(igws),
            "instances":        len(instances),
            "instances_running": sum(1 for i in instances if i["status"] == "running"),
            "load_balancers":   len(lbs),
            "dns_zones":        len(zones),
            "dns_records":      sum(z["record_count"] for z in zones),
            "nfs_servers":      len(nfs_servers),
            "dns_resolver":     f"127.0.0.1:{dns_server.PORT}",
        },
    })


# ---------------------------------------------------------------------------
# Startup reconciliation
# ---------------------------------------------------------------------------

def reconcile():
    """Reconcile persisted state against live libvirt domains and HAProxy processes."""
    app.logger.info("Reconciling state...")

    # Seed SLIRP IP counters from existing instance records so new instances after
    # a server restart don't get the same IPs as existing ones.
    vpc_cidr_map = {v.id: v.cidr_block for v in store.list_vpcs()}
    compute.init_vpc_ip_counters(vpc_cidr_map, store.list_instances())

    for instance in store.list_instances():
        if not instance.domain_name:
            continue
        live = compute.get_instance_status(instance.domain_name)
        if live in (InstanceStatus.DELETED, InstanceStatus.STOPPED):
            if instance.status == InstanceStatus.RUNNING:
                # Domain was lost (libvirtd restart) — try to bring it back up
                app.logger.info("  restarting domain %s (was running, now %s)",
                                instance.domain_name, live.value)
                try:
                    compute.start_domain(instance.domain_name)
                    live = InstanceStatus.RUNNING
                except Exception as e:
                    app.logger.warning("  could not restart %s: %s", instance.domain_name, e)
                    live = InstanceStatus.STOPPED
            if live != InstanceStatus.RUNNING:
                instance.status = InstanceStatus.STOPPED
                instance.private_ip = ""
        if live == InstanceStatus.RUNNING:
            instance.status = live
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
            vpc_instances = store.list_instances_by_vpc(lb.vpc_id)
            if lb_backend.is_running(lb.id):
                lb_backend.reload(lb, vpc_instances=vpc_instances)
            else:
                lb.listen_port = lb_backend.start(lb, vpc_instances=vpc_instances)
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
    dns_server.start()
    app.run(host="127.0.0.1", port=8080, debug=False)
