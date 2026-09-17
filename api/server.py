from __future__ import annotations

import functools
import os
import threading
import time
from flask import Flask, request, jsonify, abort, send_from_directory, g

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
import usb
import identity
import discovery
import settings_store
import peer_listener
import peer_client
import peers_store
from models import (
    VPC, VPCStatus, Instance, LoadBalancer, InstanceStatus, Subnet, SubnetStatus,
    InternetGateway, RouteTable, now_iso,
)
from build_manager_routes import bm as build_manager_blueprint
from nfs_routes import nfs_bp
from sg_routes import sg_bp
from editor_routes import editor_bp
from about_routes import about_bp
from tofu_routes import tofu_bp
from usb_routes import usb_bp
from help_routes import help_bp
from settings_routes import settings_bp
from peers_routes import peers_bp, PEER_REACHABLE_ENDPOINTS
from stats_routes import stats_bp
from scheduler_routes import scheduler_bp
import scheduler

UI_DIR   = os.path.join(os.path.dirname(__file__), "..", "ui")
app = Flask(__name__)
app.register_blueprint(build_manager_blueprint)
app.register_blueprint(nfs_bp)
app.register_blueprint(sg_bp)
app.register_blueprint(editor_bp)
app.register_blueprint(about_bp)
app.register_blueprint(tofu_bp)
app.register_blueprint(usb_bp)
app.register_blueprint(help_bp)
app.register_blueprint(settings_bp)
app.register_blueprint(peers_bp)
app.register_blueprint(stats_bp)
app.register_blueprint(scheduler_bp)
API_TOKEN = os.environ.get("CLOUDCORE_API_TOKEN", "dev-token")


@app.after_request
def _cors(response):
    response.headers["Access-Control-Allow-Origin"]  = "*"
    response.headers["Access-Control-Allow-Headers"] = "Authorization, Content-Type"
    response.headers["Access-Control-Allow-Methods"] = "GET, POST, PUT, DELETE, OPTIONS"
    return response


# Instance CRUD lives directly on `app` (not a blueprint), so its Flask
# endpoint names are just the view function names — this is server.py's
# own contribution to the peer-reachable allowlist, alongside
# peers_routes.py's PEER_REACHABLE_ENDPOINTS (the pairing bootstrap
# routes). Kept as a separate set here, close to the routes it actually
# names, rather than folding into the peers blueprint's own set.
#
# list_vpcs/list_subnets: read-only, added so a peer's own dashboard can
# populate its Build Manager's peer_vpc_id/peer_subnet_id pickers from
# this host's real catalogue instead of expecting a user to already
# know an id that exists on a host they've never directly browsed (see
# peers_routes.py's list_peer_vpcs/list_peer_subnets, the calling side
# of this same proxy). No more sensitive than create_instance already
# being reachable here — a leaked/guessed peer token could already
# create/delete VMs through this same gate.
#
# create/get/update/delete_vpc, create/get/update/delete_subnet: VPCs
# and subnets can now be peer-placed too, not just instances (per
# direct request — "do we have to limit resource placement to just
# instances?"), so a peer's proxied create/read/update/delete needs to
# actually reach these on the executing host, same as instance CRUD
# already does.
_PEER_REACHABLE_LOCAL_ENDPOINTS = {
    "create_instance", "get_instance", "update_instance", "delete_instance",
    "list_vpcs", "list_subnets",
    "create_vpc", "get_vpc", "update_vpc", "delete_vpc",
    "create_subnet", "get_subnet", "update_subnet", "delete_subnet",
}


@app.before_request
def _peer_bind_gate():
    # This app is served on two binds: the original, dashboard-facing
    # 127.0.0.1:8080 (unaffected by this check — SERVER_PORT there is
    # never the peer port) and, only while discovery.enabled is true, a
    # second, network-reachable bind on network.peer_listener_port
    # (api/peer_listener.py). A request that actually arrived on that
    # second bind is only allowed through if its route is explicitly
    # peer-reachable (PEER_REACHABLE_ENDPOINTS | _PEER_REACHABLE_LOCAL_ENDPOINTS)
    # — everything else 403s there, regardless of any token presented,
    # so a leaked/guessed peer token still can't reach settings, builds,
    # NFS file upload, or any other dashboard-only route through it.
    # SERVER_PORT comes from the accepting socket, not anything a
    # client can influence.
    if request.environ.get("SERVER_PORT") == str(discovery.peer_listener_port()):
        if request.endpoint not in (PEER_REACHABLE_ENDPOINTS | _PEER_REACHABLE_LOCAL_ENDPOINTS):
            abort(403)


@app.get("/")
def ui():
    # No caching — index.html bundles the entire dashboard (all of
    # ui/src/js/*.js inlined by ui/build.sh), so a browser holding a
    # stale cached copy silently keeps running old JS indefinitely after
    # any dashboard fix ships, with no visible error — found directly
    # chasing a report that a just-fixed bug ("No SSH port" shown for
    # instances with a valid private_ip) was still happening: the fix
    # was already on disk and already correct, the browser just never
    # re-fetched it. send_from_directory's default Cache-Control let
    # that happen; explicitly disabling it here means a normal refresh
    # (not a hard-refresh) always picks up the latest dashboard.
    resp = send_from_directory(UI_DIR, "index.html")
    resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    resp.headers["Pragma"] = "no-cache"
    return resp


@app.get("/vendor/<path:filename>")
def ui_vendor(filename):
    # CodeMirror/xterm.js, vendored locally (ui/vendor/) rather than
    # loaded from cdnjs.cloudflare.com/cdn.jsdelivr.net — the Dashboard
    # itself must not need internet access any more than the example
    # templates it drives do. Filenames are pinned, versioned release
    # assets (e.g. codemirror.min.js), so the default long-lived cache
    # is fine here, unlike index.html's own deliberate no-cache above.
    return send_from_directory(os.path.join(UI_DIR, "vendor"), filename)


def require_auth(f):
    @functools.wraps(f)
    def wrapper(*args, **kwargs):
        auth = request.headers.get("Authorization", "")
        if auth == f"Bearer {API_TOKEN}":
            return f(*args, **kwargs)
        # Also accept a valid approved peer's own token — this is what
        # lets a remote peer call into the instance CRUD routes below on
        # its own behalf (Stage 5 remote-instance proxying). Doesn't by
        # itself widen what's *reachable*: the before_request gate below
        # still restricts the network-facing peer bind to an explicit
        # allowlist regardless of whether the token presented is valid.
        token = auth.removeprefix("Bearer ") if auth.startswith("Bearer ") else ""
        peer = peers_store.find_peer_by_local_token(token) if token else None
        if peer:
            g.peer = peer
            return f(*args, **kwargs)
        abort(401)
    return wrapper


def problem(status: int, title: str, detail: str):
    return jsonify({"status": status, "title": title, "detail": detail}), status


# ---------------------------------------------------------------------------
# VPCs
# ---------------------------------------------------------------------------

def _vpc_dict(vpc: VPC) -> dict:
    """vpc.to_dict() plus host_hostname when this VPC lives on a peer —
    same convenience _instance_dict() already provides for instances."""
    d = vpc.to_dict()
    if vpc.host_id:
        peer = peers_store.get_peer(vpc.host_id)
        d["host_hostname"] = peer["hostname"] if peer else ""
    return d


@app.get("/v1/vpcs")
@require_auth
def list_vpcs():
    return jsonify({"items": [_vpc_dict(v) for v in store.list_vpcs()]})


@app.post("/v1/vpcs")
@require_auth
def create_vpc():
    body = request.get_json(force=True) or {}
    name = body.get("name", "").strip()
    if not name:
        return problem(400, "Bad Request", "name is required")

    # Checked before the local name-uniqueness lookup below (unlike
    # create_instance's own ordering) — a name collision only means
    # anything against whichever host's catalogue the resource is
    # actually landing in, and that's the peer's own create endpoint's
    # job to enforce, not this host's.
    peer_id = body.get("peer_id")
    if peer_id:
        return _create_remote_vpc(peer_id, body)

    if store.find_vpc_by_name(name):
        return problem(409, "Conflict", f"VPC '{name}' already exists")

    vpc = VPC(
        name=name,
        cidr_block=body.get("cidr_block", "10.0.0.0/16"),
        dns_support=body.get("dns_support", True),
        tags=body.get("tags", {}),
    )
    store.put_vpc(vpc)
    return jsonify(_vpc_dict(vpc)), 201


def _create_remote_vpc(peer_id: str, body: dict):
    """peer_id was set on the create request — proxy to that peer's own
    /v1/vpcs instead of provisioning locally, keeping only a local
    wrapper row (host_id set) that later reads/updates/deletes proxy
    through in turn. Same pattern as _create_remote_instance."""
    peer = peers_store.get_peer(peer_id)
    if not peer or peer["status"] != "approved":
        return problem(400, "Bad Request", f"'{peer_id}' is not an approved peer")

    remote_body = {k: v for k, v in body.items() if k != "peer_id"}
    try:
        resp = peer_client.post(peer["api_url"] + "/v1/vpcs", remote_body, token=peer["remote_token"])
    except peer_client.PeerUnreachable as e:
        return problem(502, "Bad Gateway", f"Could not reach peer '{peer['hostname']}': {e}")
    if resp.status != 201:
        detail = resp.body if isinstance(resp.body, dict) else {}
        return problem(resp.status, "Bad Gateway",
                        f"Peer '{peer['hostname']}' rejected the create: {detail.get('detail', resp.body)}")

    remote = resp.body
    vpc = VPC(
        id=remote.get("id"), name=remote.get("name", body.get("name", "")),
        cidr_block=remote.get("cidr_block", ""), dns_support=remote.get("dns_support", True),
        created_at=remote.get("created_at", now_iso()), tags=remote.get("tags", {}),
        host_id=peer_id,
    )
    store.put_vpc(vpc)
    return jsonify(_vpc_dict(vpc)), 201


@app.get("/v1/vpcs/<vpc_id>")
@require_auth
def get_vpc(vpc_id):
    vpc = store.get_vpc(vpc_id)
    if not vpc:
        return problem(404, "Not Found", f"VPC '{vpc_id}' not found")
    if vpc.host_id:
        _refresh_remote_vpc(vpc)
    return jsonify(_vpc_dict(vpc))


def _refresh_remote_vpc(vpc: VPC) -> None:
    """Live-refresh a peer-placed VPC's mutable fields from the peer's
    own record. Unlike instances, a VPC has no meaningfully distinct
    "unreachable" operational state (its cidr_block/dns_support don't
    drift the way a VM's status/IP do over its own boot lifecycle) — on
    an unreachable peer this just silently keeps the last-known cached
    values rather than inventing a new status value for every one of
    VPC/Subnet/SecurityGroup's still-two-value status enums. A genuine
    404 (deleted directly on the peer) still marks it DELETED here,
    same as the local path's own deletion convention."""
    peer = peers_store.get_peer(vpc.host_id)
    if not peer or peer["status"] != "approved":
        return
    try:
        resp = peer_client.get(peer["api_url"] + f"/v1/vpcs/{vpc.id}", token=peer["remote_token"])
    except peer_client.PeerUnreachable:
        return
    if resp.status == 404:
        vpc.status = VPCStatus.DELETED
        store.put_vpc(vpc)
        return
    if resp.status != 200:
        return
    remote = resp.body
    vpc.name = remote.get("name", vpc.name)
    vpc.dns_support = remote.get("dns_support", vpc.dns_support)
    vpc.tags = remote.get("tags", vpc.tags)
    store.put_vpc(vpc)


@app.put("/v1/vpcs/<vpc_id>")
@require_auth
def update_vpc(vpc_id):
    vpc = store.get_vpc(vpc_id)
    if not vpc:
        return problem(404, "Not Found", f"VPC '{vpc_id}' not found")
    body = request.get_json(force=True) or {}
    if vpc.host_id:
        return _update_remote_vpc(vpc, body)
    vpc.name = body.get("name", vpc.name)
    vpc.dns_support = body.get("dns_support", vpc.dns_support)
    vpc.tags = body.get("tags", vpc.tags)
    store.put_vpc(vpc)
    return jsonify(_vpc_dict(vpc))


def _update_remote_vpc(vpc: VPC, body: dict):
    peer = peers_store.get_peer(vpc.host_id)
    if not peer or peer["status"] != "approved":
        return problem(502, "Bad Gateway",
                        "This VPC's peer is not currently approved/reachable — update not applied.")
    try:
        resp = peer_client.put(peer["api_url"] + f"/v1/vpcs/{vpc.id}", body, token=peer["remote_token"])
    except peer_client.PeerUnreachable as e:
        return problem(502, "Bad Gateway", f"Could not reach peer '{peer['hostname']}': {e} — update not applied.")
    if resp.status != 200:
        detail = resp.body if isinstance(resp.body, dict) else {}
        return problem(resp.status, "Bad Gateway",
                        f"Peer '{peer['hostname']}' rejected the update: {detail.get('detail', resp.body)}")
    remote = resp.body
    vpc.name = remote.get("name", vpc.name)
    vpc.dns_support = remote.get("dns_support", vpc.dns_support)
    vpc.tags = remote.get("tags", vpc.tags)
    store.put_vpc(vpc)
    return jsonify(_vpc_dict(vpc))


@app.delete("/v1/vpcs/<vpc_id>")
@require_auth
def delete_vpc(vpc_id):
    vpc = store.get_vpc(vpc_id)
    if not vpc:
        return problem(404, "Not Found", f"VPC '{vpc_id}' not found")
    if vpc.host_id:
        return _delete_remote_vpc(vpc)

    active_instances = store.list_instances_by_vpc(vpc_id)
    if active_instances:
        return problem(409, "Conflict",
            f"VPC '{vpc_id}' has {len(active_instances)} active instance(s) — delete them first")
    active_lbs = [lb for lb in store.list_lbs() if lb.vpc_id == vpc_id]
    if active_lbs:
        return problem(409, "Conflict",
            f"VPC '{vpc_id}' has {len(active_lbs)} active load balancer(s) — delete them first")
    active_sgs = [sg for sg in sg_store.list_all()
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


def _delete_remote_vpc(vpc: VPC):
    """Proxy the delete to the peer this VPC actually lives on.
    Deliberately refuses (not silently discards the local record) if
    the peer can't be reached right now — same reasoning as
    _delete_remote_instance."""
    peer = peers_store.get_peer(vpc.host_id)
    if not peer or peer["status"] != "approved":
        return problem(502, "Bad Gateway",
                        "This VPC's peer is not currently approved/reachable — "
                        "the remote VPC was NOT deleted, local record kept.")
    try:
        resp = peer_client.delete(peer["api_url"] + f"/v1/vpcs/{vpc.id}", token=peer["remote_token"])
    except peer_client.PeerUnreachable as e:
        return problem(502, "Bad Gateway",
                        f"Could not reach peer '{peer['hostname']}': {e} — "
                        "the remote VPC was NOT deleted, local record kept.")
    if resp.status not in (204, 404):
        detail = resp.body if isinstance(resp.body, dict) else {}
        return problem(resp.status, "Bad Gateway",
                        f"Peer '{peer['hostname']}' rejected the delete: {detail.get('detail', resp.body)}")
    store.delete_vpc(vpc.id)
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


def _subnet_dict(subnet: Subnet) -> dict:
    d = subnet.to_dict()
    if subnet.host_id:
        peer = peers_store.get_peer(subnet.host_id)
        d["host_hostname"] = peer["hostname"] if peer else ""
    return d


@app.get("/v1/subnets")
@require_auth
def list_subnets():
    vpc_id = request.args.get("vpc_id")
    if vpc_id:
        return jsonify({"items": [_subnet_dict(s) for s in store.list_subnets_by_vpc(vpc_id)]})
    return jsonify({"items": [_subnet_dict(s) for s in store.list_subnets()]})


@app.post("/v1/subnets")
@require_auth
def create_subnet():
    body = request.get_json(force=True) or {}
    for field in ("name", "vpc_id", "cidr_block"):
        if not body.get(field):
            return problem(400, "Bad Request", f"'{field}' is required")

    # Checked before any local vpc_id/cidr-containment lookup below —
    # a peer-placed subnet's own vpc_id is opaque to this host (it's
    # either a pre-existing VPC on the peer, or one just peer-placed
    # there in the same apply), same reasoning as create_vpc's own
    # peer_id-first ordering.
    peer_id = body.get("peer_id")
    if peer_id:
        return _create_remote_subnet(peer_id, body)

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
    return jsonify(_subnet_dict(subnet)), 201


def _create_remote_subnet(peer_id: str, body: dict):
    peer = peers_store.get_peer(peer_id)
    if not peer or peer["status"] != "approved":
        return problem(400, "Bad Request", f"'{peer_id}' is not an approved peer")

    remote_body = {k: v for k, v in body.items() if k != "peer_id"}
    try:
        resp = peer_client.post(peer["api_url"] + "/v1/subnets", remote_body, token=peer["remote_token"])
    except peer_client.PeerUnreachable as e:
        return problem(502, "Bad Gateway", f"Could not reach peer '{peer['hostname']}': {e}")
    if resp.status != 201:
        detail = resp.body if isinstance(resp.body, dict) else {}
        return problem(resp.status, "Bad Gateway",
                        f"Peer '{peer['hostname']}' rejected the create: {detail.get('detail', resp.body)}")

    remote = resp.body
    subnet = Subnet(
        id=remote.get("id"), name=remote.get("name", body.get("name", "")),
        vpc_id=remote.get("vpc_id", body.get("vpc_id", "")),
        cidr_block=remote.get("cidr_block", body.get("cidr_block", "")),
        public=remote.get("public", bool(body.get("public", False))),
        zone=remote.get("zone", body.get("zone", "a")),
        created_at=remote.get("created_at", now_iso()), tags=remote.get("tags", {}),
        host_id=peer_id,
    )
    store.put_subnet(subnet)
    return jsonify(_subnet_dict(subnet)), 201


@app.get("/v1/subnets/<subnet_id>")
@require_auth
def get_subnet(subnet_id):
    subnet = store.get_subnet(subnet_id)
    if not subnet:
        return problem(404, "Not Found", f"Subnet '{subnet_id}' not found")
    if subnet.host_id:
        _refresh_remote_subnet(subnet)
    return jsonify(_subnet_dict(subnet))


def _refresh_remote_subnet(subnet: Subnet) -> None:
    """Same reasoning as _refresh_remote_vpc — no distinct "unreachable"
    status, silently keeps cached values when the peer can't be reached."""
    peer = peers_store.get_peer(subnet.host_id)
    if not peer or peer["status"] != "approved":
        return
    try:
        resp = peer_client.get(peer["api_url"] + f"/v1/subnets/{subnet.id}", token=peer["remote_token"])
    except peer_client.PeerUnreachable:
        return
    if resp.status == 404:
        subnet.status = SubnetStatus.DELETED
        store.put_subnet(subnet)
        return
    if resp.status != 200:
        return
    remote = resp.body
    subnet.name = remote.get("name", subnet.name)
    subnet.public = remote.get("public", subnet.public)
    subnet.zone = remote.get("zone", subnet.zone)
    subnet.tags = remote.get("tags", subnet.tags)
    store.put_subnet(subnet)


@app.put("/v1/subnets/<subnet_id>")
@require_auth
def update_subnet(subnet_id):
    subnet = store.get_subnet(subnet_id)
    if not subnet:
        return problem(404, "Not Found", f"Subnet '{subnet_id}' not found")
    body = request.get_json(force=True) or {}
    if subnet.host_id:
        return _update_remote_subnet(subnet, body)
    subnet.name = body.get("name", subnet.name)
    subnet.public = bool(body.get("public", subnet.public))
    subnet.zone = body.get("zone", subnet.zone)
    subnet.tags = body.get("tags", subnet.tags)
    store.put_subnet(subnet)
    return jsonify(_subnet_dict(subnet))


def _update_remote_subnet(subnet: Subnet, body: dict):
    peer = peers_store.get_peer(subnet.host_id)
    if not peer or peer["status"] != "approved":
        return problem(502, "Bad Gateway",
                        "This subnet's peer is not currently approved/reachable — update not applied.")
    try:
        resp = peer_client.put(peer["api_url"] + f"/v1/subnets/{subnet.id}", body, token=peer["remote_token"])
    except peer_client.PeerUnreachable as e:
        return problem(502, "Bad Gateway", f"Could not reach peer '{peer['hostname']}': {e} — update not applied.")
    if resp.status != 200:
        detail = resp.body if isinstance(resp.body, dict) else {}
        return problem(resp.status, "Bad Gateway",
                        f"Peer '{peer['hostname']}' rejected the update: {detail.get('detail', resp.body)}")
    remote = resp.body
    subnet.name = remote.get("name", subnet.name)
    subnet.public = remote.get("public", subnet.public)
    subnet.zone = remote.get("zone", subnet.zone)
    subnet.tags = remote.get("tags", subnet.tags)
    store.put_subnet(subnet)
    return jsonify(_subnet_dict(subnet))


@app.delete("/v1/subnets/<subnet_id>")
@require_auth
def delete_subnet(subnet_id):
    subnet = store.get_subnet(subnet_id)
    if not subnet:
        return problem(404, "Not Found", f"Subnet '{subnet_id}' not found")
    if subnet.host_id:
        return _delete_remote_subnet(subnet)
    active_instances = [i for i in store.list_instances_by_vpc(subnet.vpc_id)
                        if i.subnet_id == subnet_id]
    if active_instances:
        return problem(409, "Conflict",
            f"Subnet '{subnet_id}' has {len(active_instances)} active instance(s) — delete them first")
    if not store.delete_subnet(subnet_id):
        return problem(404, "Not Found", f"Subnet '{subnet_id}' not found")
    return "", 204


def _delete_remote_subnet(subnet: Subnet):
    peer = peers_store.get_peer(subnet.host_id)
    if not peer or peer["status"] != "approved":
        return problem(502, "Bad Gateway",
                        "This subnet's peer is not currently approved/reachable — "
                        "the remote subnet was NOT deleted, local record kept.")
    try:
        resp = peer_client.delete(peer["api_url"] + f"/v1/subnets/{subnet.id}", token=peer["remote_token"])
    except peer_client.PeerUnreachable as e:
        return problem(502, "Bad Gateway",
                        f"Could not reach peer '{peer['hostname']}': {e} — "
                        "the remote subnet was NOT deleted, local record kept.")
    if resp.status not in (204, 404):
        detail = resp.body if isinstance(resp.body, dict) else {}
        return problem(resp.status, "Bad Gateway",
                        f"Peer '{peer['hostname']}' rejected the delete: {detail.get('detail', resp.body)}")
    store.delete_subnet(subnet.id)
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


def _instance_dict(instance: Instance) -> dict:
    """instance.to_dict() plus host_hostname when this instance lives
    on a peer — a Terraform-plan-time convenience (so `plan`/`show`
    output shows which physical host it landed on), not stored on the
    Instance itself since models.py has no DB access of its own."""
    d = instance.to_dict()
    if instance.host_id:
        peer = peers_store.get_peer(instance.host_id)
        d["host_hostname"] = peer["hostname"] if peer else ""
    return d


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
    return jsonify({"items": [_instance_dict(i) for i in store.list_instances()]})


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

    peer_id = body.get("peer_id")
    if peer_id:
        return _create_remote_instance(peer_id, body)

    # Enforced on whichever host actually runs this function: for a
    # peer-placed instance that's the remote peer's own server.py, after
    # _create_remote_instance forwards the request with peer_id stripped
    # (see below) — so this catches a security group id that only exists
    # on the *originating* host before the instance is ever created.
    # Previously an unresolved id was accepted here and silently
    # contributed zero rules at launch (sg_routes._merged_rules skips any
    # id sg_store can't find) — apply_bridge() still installs a
    # DROP-terminated chain regardless, so the instance came up completely
    # unreachable with no error anywhere (found running the cross-host
    # load-balanced-web demo: a locally-created SG id was passed through
    # to a peer-placed instance, which built and booted "successfully"
    # but never answered a ping or an HTTP request).
    for sg_id in (body.get("security_group_ids") or []):
        if not sg_store.get(sg_id):
            return problem(400, "Bad Request",
                f"security group '{sg_id}' not found — for a peer-placed instance, "
                "security_group_ids must reference a security group that exists on "
                "the target peer, not the originating host")

    # `or []`, not `.get(key, [])`: Terraform sends an explicit JSON null
    # for an unset Optional list attribute, which a plain default doesn't
    # catch since the key is present — same bug class already fixed in
    # the LB listener/target-group code earlier this session.
    usb_device_ids = body.get("usb_device_ids") or []

    # `compute.create_instance()`/`_cloud_init_iso()` have always supported
    # baking extra users (with NOPASSWD sudo and the CloudCore keypair) into
    # an instance's cloud-init at boot via `instance.users` — but this
    # create endpoint never read a "users" key from the request body, so
    # the only way to populate it was POST /v1/instances/{id}/users, which
    # is too late (the cloud-init ISO is already built) and only applies
    # live via SSH in SLIRP mode anyway. Wire it through here so it's usable
    # at create time for bridge-mode instances too.
    users = body.get("users") or []
    for u in users:
        if not isinstance(u, dict) or not u.get("username", "").strip():
            return problem(400, "Bad Request", "each entry in users requires a non-empty username")
    users = [{
        "username": u["username"].strip(),
        "sudo": bool(u.get("sudo", False)),
        "ssh_keys": u.get("ssh_keys") or [],
        "password_hash": u.get("password_hash", ""),
    } for u in users]

    with usb._usb_lock:
        for usb_id in usb_device_ids:
            err = usb.validate_attachable(usb_id, None)
            if err:
                return problem(409, "Conflict", err)

        instance = Instance(
            name=name,
            image_id=body["image_id"],
            flavor=body["flavor"],
            vpc_id=body["vpc_id"],
            subnet_id=body["subnet_id"],
            security_group_ids=body.get("security_group_ids") or [],
            usb_device_ids=usb_device_ids,
            user_data=body.get("user_data"),
            ssh_user=compute.ssh_user_for_image(body["image_id"]),
            tags=body.get("tags", {}),
            users=users,
        )
        store.put_instance(instance)

    vpc = store.get_vpc(instance.vpc_id)
    vpc_cidr = vpc.cidr_block if vpc else "10.0.0.0/8"

    # Launch VM asynchronously so the API returns immediately
    def _launch():
        try:
            compute.create_instance(instance, vpc_cidr=vpc_cidr)
        except Exception as e:
            # The VM itself never came up — this is a genuine instance-level
            # creation failure.
            from models import InstanceStatus
            instance.status = InstanceStatus.ERROR
            instance.error_message = str(e)
            app.logger.error("Failed to create instance %s: %s", instance.id, e)
            store.put_instance(instance)
            return

        # From here the VM exists and is starting up — a failure in one of
        # these steps degrades the instance (missing DNS record or security
        # group enforcement) but shouldn't be reported as instance creation
        # having failed outright: get_instance()'s live libvirt status check
        # would silently overwrite an ERROR status here on the very next
        # poll anyway (it trusts libvirt over the stored value), so setting
        # it would just be misleading — error_message is what actually
        # survives and is worth surfacing.
        try:
            ip = compute.get_instance_ip(instance.domain_name)
            instance.private_ip = ip
            dns_store.upsert_record(
                "instances.cloudcore.internal", instance.name, "A",
                ip or "127.0.0.1", resource_type="instance", resource_id=instance.id,
            )
            dns_server.reload()
            # Apply security group rules once the VM is up
            if instance.security_group_ids:
                from sg_routes import _merged_rules
                ingress, egress = _merged_rules(instance.security_group_ids)
                sg_enforce.apply(instance, ingress, egress)
        except Exception as e:
            instance.error_message = f"Post-launch step failed (instance is otherwise up): {e}"
            app.logger.error("Post-launch step failed for instance %s: %s", instance.id, e)

        store.put_instance(instance)
        # Reload LBs after put_instance so http_host_port is in the DB
        vpc_instances = store.list_instances_by_vpc(instance.vpc_id)
        for lb in store.list_lbs():
            if lb.vpc_id == instance.vpc_id:
                try:
                    lb_backend.reload(lb, vpc_instances=vpc_instances)
                except Exception as lb_err:
                    app.logger.warning("LB reload failed for %s: %s", lb.id, lb_err)

    threading.Thread(target=_launch, daemon=True).start()
    return jsonify(_instance_dict(instance)), 202


def _create_remote_instance(peer_id: str, body: dict):
    """peer_id was set on the create request — proxy to that peer's own
    /v1/instances instead of provisioning locally, keeping only a local
    wrapper row (host_id set) that later reads/deletes proxy through in
    turn. vpc_id/subnet_id/image_id/etc are deliberately NOT validated
    against this host's own catalogue here — they're opaque IDs the
    peer's own API validates against its own catalogue, exactly as if a
    normal local request had landed there directly."""
    peer = peers_store.get_peer(peer_id)
    if not peer or peer["status"] != "approved":
        return problem(400, "Bad Request", f"'{peer_id}' is not an approved peer")

    remote_body = {k: v for k, v in body.items() if k != "peer_id"}
    try:
        resp = peer_client.post(peer["api_url"] + "/v1/instances", remote_body, token=peer["remote_token"])
    except peer_client.PeerUnreachable as e:
        return problem(502, "Bad Gateway", f"Could not reach peer '{peer['hostname']}': {e}")
    if resp.status not in (200, 202):
        detail = resp.body if isinstance(resp.body, dict) else {}
        return problem(resp.status, "Bad Gateway",
                        f"Peer '{peer['hostname']}' rejected the create: {detail.get('detail', resp.body)}")

    remote = resp.body
    instance = Instance(
        id=remote["id"], name=remote.get("name", body.get("name", "")),
        image_id=body.get("image_id", ""), flavor=body.get("flavor", ""),
        vpc_id=body.get("vpc_id", ""), subnet_id=body.get("subnet_id", ""),
        security_group_ids=body.get("security_group_ids") or [],
        tags=body.get("tags") or {}, host_id=peer_id,
    )
    try:
        instance.status = InstanceStatus(remote.get("status", "pending"))
    except ValueError:
        instance.status = InstanceStatus.PENDING
    instance.private_ip = remote.get("private_ip", "")
    instance.public_ip = remote.get("public_ip", "")
    store.put_instance(instance)

    # Local instances get their DB-stored status kept current by their own
    # background _launch() thread, independent of anyone calling GET — the
    # LIST endpoint (/v1/instances) just reads whatever's already in the DB,
    # never live-refreshing on its own. Remote instances had no equivalent
    # (only an individual GET on this exact id ever refreshed one), which a
    # real Ansible run surfaced directly: its own idempotency/polling check
    # goes through find_by_name() -> the LIST endpoint, so it never saw a
    # remote instance leave "pending" no matter how long it waited. This
    # mirrors the same background-refresh pattern for remote instances.
    def _poll_remote():
        for _ in range(24):  # ~2 minutes at 5s apart — generous, not indefinite
            time.sleep(5)
            current = store.get_instance(instance.id)
            if current is None or current.status not in (InstanceStatus.PENDING, InstanceStatus.RUNNING):
                return  # deleted locally, or already reached a genuinely terminal status
            if current.status == InstanceStatus.RUNNING and current.private_ip:
                return  # already fully settled — nothing left to refresh
            try:
                poll_resp = peer_client.get(peer["api_url"] + f"/v1/instances/{instance.id}", token=peer["remote_token"])
            except peer_client.PeerUnreachable:
                continue
            if poll_resp.status != 200:
                continue
            poll_remote = poll_resp.body
            try:
                current.status = InstanceStatus(poll_remote.get("status", current.status.value))
            except ValueError:
                pass
            current.private_ip = poll_remote.get("private_ip", current.private_ip)
            current.public_ip = poll_remote.get("public_ip", current.public_ip)
            current.error_message = poll_remote.get("error_message", current.error_message)
            store.put_instance(current)
            # Same race the Go provider's own Create() already guards
            # against: a bridged instance can report status=running
            # before its DHCP lease (and thus private_ip) is actually
            # known — keep polling until both agree, not just status.
            if current.status not in (InstanceStatus.PENDING, InstanceStatus.RUNNING):
                return
            if current.status == InstanceStatus.RUNNING and current.private_ip:
                return

    threading.Thread(target=_poll_remote, daemon=True).start()
    return jsonify(_instance_dict(instance)), 202


@app.get("/v1/instances/<instance_id>")
@require_auth
def get_instance(instance_id):
    instance = store.get_instance(instance_id)
    if not instance:
        return problem(404, "Not Found", f"Instance '{instance_id}' not found")
    if instance.host_id:
        return _get_remote_instance(instance)
    # Refresh status and IP from libvirt, but only update — never auto-delete
    if instance.domain_name:
        live_status = compute.get_instance_status(instance.domain_name)
        # Only update status if libvirt confirms a real state change
        if live_status != InstanceStatus.DELETED:
            instance.status = live_status
        if not instance.private_ip and live_status == InstanceStatus.RUNNING:
            instance.private_ip = compute.get_instance_ip(instance.domain_name)
            if instance.private_ip:
                # The DNS A-record registered at launch time falls back to
                # "127.0.0.1" when the real (bridged) IP isn't known yet
                # (same DHCP-timing gap as private_ip itself) — but unlike
                # private_ip, nothing else ever re-registers it. Piggyback
                # on this same self-correcting refresh so the DNS record
                # doesn't stay wrong for the instance's whole lifetime.
                dns_store.upsert_record(
                    "instances.cloudcore.internal", instance.name, "A",
                    instance.private_ip, resource_type="instance", resource_id=instance.id,
                )
                dns_server.reload()
        store.put_instance(instance)
    return jsonify(_instance_dict(instance))


def _get_remote_instance(instance: Instance):
    """Live-proxy a read to the peer this instance actually lives on —
    same "refresh from the real source of truth on every read" pattern
    the local path already uses (compute.get_instance_status() there,
    the peer's own API here). Distinguishes "the VM itself is broken"
    (whatever status the peer reports) from "can't currently reach the
    host it's on" (UNREACHABLE) — deliberately not the same thing."""
    peer = peers_store.get_peer(instance.host_id)
    if not peer or peer["status"] != "approved":
        instance.status = InstanceStatus.UNREACHABLE
        store.put_instance(instance)
        return jsonify(_instance_dict(instance))
    try:
        resp = peer_client.get(peer["api_url"] + f"/v1/instances/{instance.id}", token=peer["remote_token"])
    except peer_client.PeerUnreachable:
        instance.status = InstanceStatus.UNREACHABLE
        store.put_instance(instance)
        return jsonify(_instance_dict(instance))
    if resp.status == 404:
        instance.status = InstanceStatus.DELETED
        store.put_instance(instance)
        return jsonify(_instance_dict(instance))
    if resp.status != 200:
        instance.status = InstanceStatus.UNREACHABLE
        store.put_instance(instance)
        return jsonify(_instance_dict(instance))

    remote = resp.body
    try:
        instance.status = InstanceStatus(remote.get("status", instance.status.value))
    except ValueError:
        pass
    instance.private_ip = remote.get("private_ip", instance.private_ip)
    instance.public_ip = remote.get("public_ip", instance.public_ip)
    instance.error_message = remote.get("error_message", instance.error_message)
    store.put_instance(instance)
    return jsonify(_instance_dict(instance))


@app.put("/v1/instances/<instance_id>")
@require_auth
def update_instance(instance_id):
    instance = store.get_instance(instance_id)
    if not instance:
        return problem(404, "Not Found", f"Instance '{instance_id}' not found")
    if instance.host_id:
        return _update_remote_instance(instance, request.get_json(force=True) or {})
    body = request.get_json(force=True) or {}
    instance.name = body.get("name", instance.name)
    instance.tags = body.get("tags", instance.tags)

    added, removed = [], []
    if "usb_device_ids" in body:
        # `or []`, not a bare index: Terraform sends this key with an
        # explicit JSON null whenever the attribute is left unset in HCL
        # (confirmed directly — the Go provider does this on every
        # Update() call, not just when a change is actually intended for
        # this field) — same bug class as the LB listener/target-group
        # code fixed earlier this session, just a different call site.
        new_ids = body["usb_device_ids"] or []
        with usb._usb_lock:
            for usb_id in new_ids:
                err = usb.validate_attachable(usb_id, instance.id)
                if err:
                    return problem(409, "Conflict", err)
            old_ids = set(instance.usb_device_ids)
            added = list(set(new_ids) - old_ids)
            removed = list(old_ids - set(new_ids))
            instance.usb_device_ids = new_ids
            store.put_instance(instance)
    else:
        store.put_instance(instance)

    if (added or removed) and instance.domain_name:
        try:
            compute.sync_usb_devices(instance, added, removed)
        except Exception as e:
            return problem(500, "Internal Server Error", str(e))

    return jsonify(_instance_dict(instance))


def _update_remote_instance(instance: Instance, body: dict):
    """Proxy name/tags/usb_device_ids updates to the peer this instance
    actually lives on — without this, USB sync would silently no-op
    (this wrapper row's own domain_name is always empty, so the local
    update path's `if ... and instance.domain_name:` guard would never
    fire) and name/tags edits would only ever touch the local wrapper,
    never the real instance, a real state-drift bug."""
    peer = peers_store.get_peer(instance.host_id)
    if not peer or peer["status"] != "approved":
        return problem(502, "Bad Gateway",
                        "This instance's peer is not currently approved/reachable — update not applied.")
    try:
        resp = peer_client.put(peer["api_url"] + f"/v1/instances/{instance.id}", body, token=peer["remote_token"])
    except peer_client.PeerUnreachable as e:
        return problem(502, "Bad Gateway", f"Could not reach peer '{peer['hostname']}': {e} — update not applied.")
    if resp.status != 200:
        detail = resp.body if isinstance(resp.body, dict) else {}
        return problem(resp.status, "Bad Gateway",
                        f"Peer '{peer['hostname']}' rejected the update: {detail.get('detail', resp.body)}")

    remote = resp.body
    instance.name = remote.get("name", instance.name)
    instance.tags = remote.get("tags", instance.tags)
    instance.usb_device_ids = remote.get("usb_device_ids", instance.usb_device_ids)
    store.put_instance(instance)
    return jsonify(_instance_dict(instance))


@app.delete("/v1/instances/<instance_id>")
@require_auth
def delete_instance(instance_id):
    instance = store.get_instance(instance_id)
    if not instance:
        return problem(404, "Not Found", f"Instance '{instance_id}' not found")
    if instance.host_id:
        return _delete_remote_instance(instance)

    # Mark deleted immediately so list excludes it before async teardown completes
    store.delete_instance_record(instance_id)
    dns_store.delete_records_for_resource(instance_id)
    dns_server.reload()
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


def _delete_remote_instance(instance: Instance):
    """Proxy the delete to the peer this instance actually lives on.
    Deliberately refuses (not silently discards the local record) if
    the peer can't be reached right now — the alternative would let a
    remote VM leak indefinitely with no local record pointing at it."""
    peer = peers_store.get_peer(instance.host_id)
    if not peer or peer["status"] != "approved":
        return problem(502, "Bad Gateway",
                        "This instance's peer is not currently approved/reachable — "
                        "the remote instance was NOT deleted, local record kept.")
    try:
        resp = peer_client.delete(peer["api_url"] + f"/v1/instances/{instance.id}", token=peer["remote_token"])
    except peer_client.PeerUnreachable as e:
        return problem(502, "Bad Gateway",
                        f"Could not reach peer '{peer['hostname']}': {e} — "
                        "the remote instance was NOT deleted, local record kept.")
    if resp.status not in (204, 404):
        detail = resp.body if isinstance(resp.body, dict) else {}
        return problem(resp.status, "Bad Gateway",
                        f"Peer '{peer['hostname']}' rejected the delete: {detail.get('detail', resp.body)}")
    store.delete_instance_record(instance.id)
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
    return jsonify(_instance_dict(instance))


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
    return jsonify(_instance_dict(instance))


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
    return jsonify(_instance_dict(instance))


@app.get("/v1/instances/<instance_id>/console")
@require_auth
def instance_console(instance_id):
    instance = store.get_instance(instance_id)
    if not instance:
        return problem(404, "Not Found", f"Instance '{instance_id}' not found")
    lines = request.args.get("lines", 200, type=int)
    output = compute.get_console_output(instance_id, lines=lines)
    if not output and not compute._console_log_path(instance_id).exists():
        return problem(404, "Not Found",
            "Console log not available — instance was created before serial logging was added "
            "or has not written any output yet")
    return jsonify({"instance_id": instance_id, "output": output})


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
        dns_name=f"{name}.lb.cloudcore.internal",
        backends=body.get("backends", []),
        sticky_sessions=bool(body.get("sticky_sessions", False)),
        cookie_name=body.get("cookie_name", "SERVERID"),
        deletion_protection=bool(body.get("deletion_protection", False)),
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
            "lb.cloudcore.internal", lb.name, "A", "127.0.0.1",
            resource_type="lb", resource_id=lb.id,
        )
        dns_server.reload()
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
    lb.sticky_sessions = bool(body.get("sticky_sessions", lb.sticky_sessions))
    lb.cookie_name = body.get("cookie_name", lb.cookie_name)
    lb.deletion_protection = bool(body.get("deletion_protection", lb.deletion_protection))
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
    lb = store.get_lb(lb_id)
    if not lb:
        return problem(404, "Not Found", f"Load balancer '{lb_id}' not found")
    if lb.deletion_protection:
        return problem(409, "Conflict",
            f"Load balancer '{lb_id}' has deletion protection enabled — disable it first")
    store.delete_lb(lb_id)
    lb_backend.stop(lb_id)
    dns_store.delete_records_for_resource(lb_id)
    dns_server.reload()
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


@app.get("/v1/load-balancers/<lb_id>/listeners")
@require_auth
def list_listeners(lb_id):
    lb = store.get_lb(lb_id)
    if not lb:
        return problem(404, "Not Found", f"Load balancer '{lb_id}' not found")
    return jsonify({"items": lb.listeners})


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
    protocol = body.get("protocol", "http")
    if protocol.lower() not in ("http", "https", "tcp"):
        return problem(400, "Bad Request", "protocol must be http, https or tcp")
    if any(l["port"] == port for l in lb.listeners):
        return problem(409, "Conflict", f"Listener on port {port} already exists")
    from models import new_id
    listener = {
        "id": new_id(),
        "lb_id": lb_id,
        "port": port,
        "protocol": protocol,
        "target_group_id": body.get("target_group_id", ""),
        # `or []`, not `.get(..., [])`: Terraform sends this key with an
        # explicit JSON null when a caller leaves an Optional+Computed
        # list attribute unset, so a plain default only fires when the
        # key is absent entirely — it isn't here.
        "routing_rules": body.get("routing_rules") or [],
        "default_action": body.get("default_action", "forward"),
        "status": "active",
    }
    lb.listeners.append(listener)
    try:
        lb_backend.reload(lb, vpc_instances=store.list_instances_by_vpc(lb.vpc_id))
    except Exception as e:
        app.logger.error("HAProxy reload failed for %s: %s", lb_id, e)
    store.put_lb(lb)
    return jsonify(listener), 201


@app.get("/v1/load-balancers/<lb_id>/listeners/<listener_id>")
@require_auth
def get_listener(lb_id, listener_id):
    lb = store.get_lb(lb_id)
    if not lb:
        return problem(404, "Not Found", f"Load balancer '{lb_id}' not found")
    lst = next((l for l in lb.listeners if l["id"] == listener_id), None)
    if not lst:
        return problem(404, "Not Found", f"Listener '{listener_id}' not found")
    return jsonify(lst)


@app.put("/v1/load-balancers/<lb_id>/listeners/<listener_id>")
@require_auth
def update_listener(lb_id, listener_id):
    lb = store.get_lb(lb_id)
    if not lb:
        return problem(404, "Not Found", f"Load balancer '{lb_id}' not found")
    lst = next((l for l in lb.listeners if l["id"] == listener_id), None)
    if not lst:
        return problem(404, "Not Found", f"Listener '{listener_id}' not found")
    body = request.get_json(force=True) or {}
    lst["target_group_id"] = body.get("target_group_id", lst.get("target_group_id", ""))
    lst["routing_rules"]   = body.get("routing_rules") or lst.get("routing_rules") or []
    lst["default_action"]  = body.get("default_action", lst.get("default_action", "forward"))
    try:
        lb_backend.reload(lb, vpc_instances=store.list_instances_by_vpc(lb.vpc_id))
    except Exception as e:
        app.logger.error("HAProxy reload failed for %s: %s", lb_id, e)
    store.put_lb(lb)
    return jsonify(lst)


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


# ---------------------------------------------------------------------------
# Target Groups
# ---------------------------------------------------------------------------

def _find_tg(lb: LoadBalancer, tg_id: str) -> dict | None:
    return next((t for t in lb.target_groups if t["id"] == tg_id), None)


def _resolve_tg_backends_for_api(tg: dict, instances: list) -> list[dict]:
    """Resolve TG target instance_ids to address:port for the API response."""
    inst_map = {i.id: i for i in instances}
    result = []
    for t in (tg.get("targets") or []):
        inst = inst_map.get(t["instance_id"])
        if inst:
            port = t.get("port") or tg.get("port", 80)
            addr = "127.0.0.1" if inst.http_host_port else (inst.private_ip or "")
            result.append({**t, "address": addr, "resolved_port": inst.http_host_port or port})
    return result


@app.get("/v1/load-balancers/<lb_id>/target-groups")
@require_auth
def list_target_groups(lb_id):
    lb = store.get_lb(lb_id)
    if not lb:
        return problem(404, "Not Found", f"Load balancer '{lb_id}' not found")
    return jsonify({"items": lb.target_groups})


@app.post("/v1/load-balancers/<lb_id>/target-groups")
@require_auth
def create_target_group(lb_id):
    lb = store.get_lb(lb_id)
    if not lb:
        return problem(404, "Not Found", f"Load balancer '{lb_id}' not found")
    body = request.get_json(force=True) or {}
    name = body.get("name", "").strip()
    if not name:
        return problem(400, "Bad Request", "name is required")
    if not body.get("port"):
        return problem(400, "Bad Request", "port is required")
    if any(t["name"] == name for t in lb.target_groups):
        return problem(409, "Conflict", f"Target group '{name}' already exists on this LB")
    from models import new_id
    # `or 30`/`or 2`, not `.get(key, 30)`: the Terraform provider's
    # health_check is a nested object whose own per-field Computed
    # defaults only apply when the *object itself* isn't null — leaving
    # health_check out of a cloudcore_lb_target_group block entirely
    # (the common case) means the provider sends its Go zero-value
    # struct instead, i.e. explicit interval/threshold 0s, not an
    # absent key .get()'s default could catch. 0 is never a valid
    # health-check interval or threshold, so treating a falsy incoming
    # value as "not really set" is safe here (found live: haproxy
    # rejected the resulting config outright with "invalid value 0 for
    # argument 'inter'" — an F-092-adjacent target group came up with
    # zero real backends, then with an interval that broke every
    # backend on the whole LB, while wiring the missing backend
    # registration step into examples/load-balanced-web).
    hc = body.get("health_check", {})
    tg = {
        "id": new_id(),
        "lb_id": lb_id,
        "name": name,
        "port": int(body["port"]),
        "protocol": body.get("protocol", "http").lower(),
        "targets": body.get("targets") or [],
        "health_check": {
            "path": hc.get("path") or "/",
            "interval": int(hc.get("interval") or 30),
            "healthy_threshold": int(hc.get("healthy_threshold") or 2),
            "unhealthy_threshold": int(hc.get("unhealthy_threshold") or 2),
        },
        "status": "active",
    }
    lb.target_groups.append(tg)
    try:
        lb_backend.reload(lb, vpc_instances=store.list_instances_by_vpc(lb.vpc_id))
    except Exception as e:
        app.logger.error("HAProxy reload failed for %s: %s", lb_id, e)
    store.put_lb(lb)
    return jsonify(tg), 201


@app.get("/v1/load-balancers/<lb_id>/target-groups/<tg_id>")
@require_auth
def get_target_group(lb_id, tg_id):
    lb = store.get_lb(lb_id)
    if not lb:
        return problem(404, "Not Found", f"Load balancer '{lb_id}' not found")
    tg = _find_tg(lb, tg_id)
    if not tg:
        return problem(404, "Not Found", f"Target group '{tg_id}' not found")
    return jsonify(tg)


@app.put("/v1/load-balancers/<lb_id>/target-groups/<tg_id>")
@require_auth
def update_target_group(lb_id, tg_id):
    lb = store.get_lb(lb_id)
    if not lb:
        return problem(404, "Not Found", f"Load balancer '{lb_id}' not found")
    tg = _find_tg(lb, tg_id)
    if not tg:
        return problem(404, "Not Found", f"Target group '{tg_id}' not found")
    body = request.get_json(force=True) or {}
    tg["name"]    = body.get("name", tg["name"])
    tg["port"]    = int(body.get("port", tg["port"]))
    tg["protocol"] = body.get("protocol", tg["protocol"])
    tg["targets"] = body.get("targets", tg["targets"])
    if "health_check" in body:
        # Same "or", not ".get(key, existing)" reasoning as create_target_group:
        # a falsy incoming value (the provider's zero-value struct sent for
        # an unconfigured health_check) means fall back, not "explicitly
        # wants 0" — 0 is never valid for any of these fields.
        # Chained `or`s all the way to the literal default, not a single
        # fallback to the stored value: that value can itself already be
        # a stale 0 from before this fix (this exact PUT is what's used
        # to force a stuck-at-0 target group back to a sane value).
        hc = body["health_check"]
        tg["health_check"] = {
            "path": hc.get("path") or tg["health_check"].get("path") or "/",
            "interval": int(hc.get("interval") or tg["health_check"].get("interval") or 30),
            "healthy_threshold": int(hc.get("healthy_threshold") or tg["health_check"].get("healthy_threshold") or 2),
            "unhealthy_threshold": int(hc.get("unhealthy_threshold") or tg["health_check"].get("unhealthy_threshold") or 2),
        }
    try:
        lb_backend.reload(lb, vpc_instances=store.list_instances_by_vpc(lb.vpc_id))
    except Exception as e:
        app.logger.error("HAProxy reload failed for %s: %s", lb_id, e)
    store.put_lb(lb)
    return jsonify(tg)


@app.delete("/v1/load-balancers/<lb_id>/target-groups/<tg_id>")
@require_auth
def delete_target_group(lb_id, tg_id):
    lb = store.get_lb(lb_id)
    if not lb:
        return problem(404, "Not Found", f"Load balancer '{lb_id}' not found")
    if not _find_tg(lb, tg_id):
        return problem(404, "Not Found", f"Target group '{tg_id}' not found")
    in_use = [l for l in lb.listeners
              if l.get("target_group_id") == tg_id
              or any(r.get("target_group_id") == tg_id for r in (l.get("routing_rules") or []))]
    if in_use:
        return problem(409, "Conflict",
            f"Target group '{tg_id}' is referenced by {len(in_use)} listener(s) — remove references first")
    lb.target_groups = [t for t in lb.target_groups if t["id"] != tg_id]
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
    if rtype not in ("A", "CNAME", "TXT", "MX", "PTR"):
        return problem(400, "Bad Request", "type must be A, CNAME, TXT, MX, or PTR")
    rec = dns_store.upsert_record(zone_name, name, rtype, value,
                                  ttl=int(body.get("ttl", 300)))
    dns_server.reload()
    return jsonify(rec), 201


@app.delete("/v1/dns/zones/<path:zone_name>/records/<name>/<rtype>")
@require_auth
def dns_delete_record(zone_name, name, rtype):
    if not dns_store.delete_record(zone_name, name, rtype):
        return problem(404, "Not Found", f"Record '{name}/{rtype}' not found in zone '{zone_name}'")
    dns_server.reload()
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
                "instances.cloudcore.internal", instance.name, "A",
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
                "lb.cloudcore.internal", lb.name, "A", "127.0.0.1",
                resource_type="lb", resource_id=lb.id,
            )
        except Exception as e:
            app.logger.error("  lb %s reconcile failed: %s", lb.name, e)

    app.logger.info("Reconciliation complete.")


if __name__ == "__main__":
    db.init()
    identity.ensure_peer_keypair()
    peer_listener.init(app)
    if settings_store.get("discovery.enabled", False):
        # Resume advertising (and the peer listener) across a restart —
        # a host that already opted in shouldn't silently go dark just
        # because the process bounced; both only ever stop via an
        # explicit settings PUT, not implicitly.
        discovery.advertise()
        peer_listener.start(discovery.peer_listener_port())
    dns_store.load()
    reconcile()
    dns_server.start()
    scheduler.start()
    # threaded=True: an NFS file upload (api/nfs.py's upload_file) holds
    # its request open for as long as the transfer takes — without this,
    # Werkzeug's single-threaded dev server would stall every other
    # request (dashboard polling included) for the whole duration.
    app.run(host="127.0.0.1", port=8080, debug=False, threaded=True)
