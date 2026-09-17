"""Security group API routes — /v1/security-groups"""
from __future__ import annotations

from flask import Blueprint, request, jsonify

import peer_client
import peers_store
import sg_store
import store as resource_store
from models import SecurityGroup, SecurityGroupStatus, now_iso

sg_bp = Blueprint("sg", __name__)

_VALID_PROTOCOLS = {"tcp", "udp", "icmp", "-1"}


def _problem(status, title, detail):
    return jsonify({"status": status, "title": title, "detail": detail}), status


def _validate_rules(rules: list) -> str | None:
    """Return an error string if any rule is invalid, else None."""
    import ipaddress
    for r in rules:
        proto = r.get("protocol", "-1")
        if proto not in _VALID_PROTOCOLS:
            return f"protocol must be one of: {', '.join(sorted(_VALID_PROTOCOLS))}"
        has_cidr    = bool(r.get("cidr"))
        has_cidr_v6 = bool(r.get("cidr_ipv6"))
        has_sg      = bool(r.get("source_sg_id"))
        if not has_cidr and not has_cidr_v6 and not has_sg:
            return "each rule must have 'cidr', 'cidr_ipv6', or 'source_sg_id'"
        if has_sg and (has_cidr or has_cidr_v6):
            return "a rule cannot specify both 'source_sg_id' and a CIDR"
        if has_cidr_v6:
            try:
                ipaddress.ip_network(r["cidr_ipv6"], strict=False)
            except ValueError:
                return f"cidr_ipv6 '{r['cidr_ipv6']}' is not a valid IPv6 CIDR"
        if proto != "-1":
            fp = r.get("from_port")
            tp = r.get("to_port")
            if fp is None or tp is None:
                return "from_port and to_port are required for non-all-traffic rules"
            if not (0 <= int(fp) <= 65535 and 0 <= int(tp) <= 65535):
                return "port numbers must be 0–65535"
            if int(fp) > int(tp):
                return "from_port must be <= to_port"
    return None


def _sg_dict(sg: SecurityGroup) -> dict:
    d = sg.to_dict()
    if sg.host_id:
        peer = peers_store.get_peer(sg.host_id)
        d["host_hostname"] = peer["hostname"] if peer else ""
    return d


@sg_bp.get("/v1/security-groups")
def list_sgs():
    return jsonify({"items": [_sg_dict(sg) for sg in sg_store.list_all()]})


@sg_bp.post("/v1/security-groups")
def create_sg():
    body = request.get_json(force=True) or {}
    name = body.get("name", "").strip()
    if not name:
        return _problem(400, "Bad Request", "name is required")
    if not body.get("vpc_id"):
        return _problem(400, "Bad Request", "vpc_id is required")

    # Checked before the local vpc_id lookup below — a peer-placed
    # security group's own vpc_id is opaque to this host, same
    # reasoning as server.py's create_vpc/create_subnet.
    peer_id = body.get("peer_id")
    if peer_id:
        return _create_remote_sg(peer_id, body)

    if not resource_store.get_vpc(body["vpc_id"]):
        return _problem(404, "Not Found", f"VPC '{body['vpc_id']}' not found")
    if sg_store.find_by_name(name):
        return _problem(409, "Conflict", f"Security group '{name}' already exists")

    ingress = body.get("ingress_rules", [])
    egress  = body.get("egress_rules",  [])
    err = _validate_rules(ingress) or _validate_rules(egress)
    if err:
        return _problem(400, "Bad Request", err)

    sg = SecurityGroup(
        name=name,
        description=body.get("description", ""),
        vpc_id=body["vpc_id"],
        ingress_rules=ingress,
        egress_rules=egress,
        tags=body.get("tags", {}),
    )
    sg_store.put(sg)
    return jsonify(_sg_dict(sg)), 201


def _create_remote_sg(peer_id: str, body: dict):
    peer = peers_store.get_peer(peer_id)
    if not peer or peer["status"] != "approved":
        return _problem(400, "Bad Request", f"'{peer_id}' is not an approved peer")

    remote_body = {k: v for k, v in body.items() if k != "peer_id"}
    try:
        resp = peer_client.post(peer["api_url"] + "/v1/security-groups", remote_body, token=peer["remote_token"])
    except peer_client.PeerUnreachable as e:
        return _problem(502, "Bad Gateway", f"Could not reach peer '{peer['hostname']}': {e}")
    if resp.status != 201:
        detail = resp.body if isinstance(resp.body, dict) else {}
        return _problem(resp.status, "Bad Gateway",
                         f"Peer '{peer['hostname']}' rejected the create: {detail.get('detail', resp.body)}")

    remote = resp.body
    sg = SecurityGroup(
        id=remote.get("id"), name=remote.get("name", body.get("name", "")),
        description=remote.get("description", body.get("description", "")),
        vpc_id=remote.get("vpc_id", body.get("vpc_id", "")),
        ingress_rules=remote.get("ingress_rules", body.get("ingress_rules", [])),
        egress_rules=remote.get("egress_rules", body.get("egress_rules", [])),
        created_at=remote.get("created_at", now_iso()), tags=remote.get("tags", {}),
        host_id=peer_id,
    )
    sg_store.put(sg)
    return jsonify(_sg_dict(sg)), 201


@sg_bp.get("/v1/security-groups/<sg_id>")
def get_sg(sg_id):
    sg = sg_store.get(sg_id)
    if not sg:
        return _problem(404, "Not Found", f"Security group '{sg_id}' not found")
    if sg.host_id:
        _refresh_remote_sg(sg)
    return jsonify(_sg_dict(sg))


def _refresh_remote_sg(sg: SecurityGroup) -> None:
    """Same reasoning as server.py's _refresh_remote_vpc — no distinct
    "unreachable" status, silently keeps cached values when the peer
    can't be reached; a genuine 404 marks it DELETED."""
    peer = peers_store.get_peer(sg.host_id)
    if not peer or peer["status"] != "approved":
        return
    try:
        resp = peer_client.get(peer["api_url"] + f"/v1/security-groups/{sg.id}", token=peer["remote_token"])
    except peer_client.PeerUnreachable:
        return
    if resp.status == 404:
        sg.status = SecurityGroupStatus.DELETED
        sg_store.put(sg)
        return
    if resp.status != 200:
        return
    remote = resp.body
    sg.description   = remote.get("description", sg.description)
    sg.ingress_rules = remote.get("ingress_rules", sg.ingress_rules)
    sg.egress_rules  = remote.get("egress_rules", sg.egress_rules)
    sg.tags          = remote.get("tags", sg.tags)
    sg_store.put(sg)


@sg_bp.put("/v1/security-groups/<sg_id>")
def update_sg(sg_id):
    sg = sg_store.get(sg_id)
    if not sg:
        return _problem(404, "Not Found", f"Security group '{sg_id}' not found")
    body = request.get_json(force=True) or {}

    if sg.host_id:
        return _update_remote_sg(sg, body)

    ingress = body.get("ingress_rules", sg.ingress_rules)
    egress  = body.get("egress_rules",  sg.egress_rules)
    err = _validate_rules(ingress) or _validate_rules(egress)
    if err:
        return _problem(400, "Bad Request", err)

    sg.description   = body.get("description",   sg.description)
    sg.ingress_rules = ingress
    sg.egress_rules  = egress
    sg.tags          = body.get("tags", sg.tags)
    sg_store.put(sg)

    # Re-apply rules to any running instances that reference this SG —
    # only ever meaningful for a local SG: enforcement is iptables on
    # THIS host, and a peer-placed SG's actual enforcement happens on
    # the peer itself when it applies the proxied update below.
    _reapply_to_instances(sg)

    return jsonify(_sg_dict(sg))


def _update_remote_sg(sg: SecurityGroup, body: dict):
    peer = peers_store.get_peer(sg.host_id)
    if not peer or peer["status"] != "approved":
        return _problem(502, "Bad Gateway",
                         "This security group's peer is not currently approved/reachable — update not applied.")
    try:
        resp = peer_client.put(peer["api_url"] + f"/v1/security-groups/{sg.id}", body, token=peer["remote_token"])
    except peer_client.PeerUnreachable as e:
        return _problem(502, "Bad Gateway", f"Could not reach peer '{peer['hostname']}': {e} — update not applied.")
    if resp.status != 200:
        detail = resp.body if isinstance(resp.body, dict) else {}
        return _problem(resp.status, "Bad Gateway",
                         f"Peer '{peer['hostname']}' rejected the update: {detail.get('detail', resp.body)}")
    remote = resp.body
    sg.description   = remote.get("description", sg.description)
    sg.ingress_rules = remote.get("ingress_rules", sg.ingress_rules)
    sg.egress_rules  = remote.get("egress_rules", sg.egress_rules)
    sg.tags          = remote.get("tags", sg.tags)
    sg_store.put(sg)
    return jsonify(_sg_dict(sg))


@sg_bp.delete("/v1/security-groups/<sg_id>")
def delete_sg(sg_id):
    sg = sg_store.get(sg_id)
    if not sg:
        return _problem(404, "Not Found", f"Security group '{sg_id}' not found")
    if sg.host_id:
        return _delete_remote_sg(sg)
    attached = [i for i in resource_store.list_instances() if sg_id in i.security_group_ids]
    if attached:
        names = ", ".join(i.name for i in attached[:3])
        return _problem(409, "Conflict",
            f"Security group is attached to {len(attached)} instance(s) ({names}) — detach first")
    if not sg_store.delete(sg_id):
        return _problem(404, "Not Found", f"Security group '{sg_id}' not found")
    return "", 204


def _delete_remote_sg(sg: SecurityGroup):
    peer = peers_store.get_peer(sg.host_id)
    if not peer or peer["status"] != "approved":
        return _problem(502, "Bad Gateway",
                         "This security group's peer is not currently approved/reachable — "
                         "the remote security group was NOT deleted, local record kept.")
    try:
        resp = peer_client.delete(peer["api_url"] + f"/v1/security-groups/{sg.id}", token=peer["remote_token"])
    except peer_client.PeerUnreachable as e:
        return _problem(502, "Bad Gateway",
                         f"Could not reach peer '{peer['hostname']}': {e} — "
                         "the remote security group was NOT deleted, local record kept.")
    if resp.status not in (204, 404):
        detail = resp.body if isinstance(resp.body, dict) else {}
        return _problem(resp.status, "Bad Gateway",
                         f"Peer '{peer['hostname']}' rejected the delete: {detail.get('detail', resp.body)}")
    sg_store.delete(sg.id)
    return "", 204


def _reapply_to_instances(sg: SecurityGroup) -> None:
    """Re-apply updated rules to all running instances that reference this SG."""
    import threading
    import sg as sg_enforce
    from models import InstanceStatus

    def _apply():
        for inst in resource_store.list_instances():
            if sg.id in inst.security_group_ids and inst.status == InstanceStatus.RUNNING:
                # Merge rules from all SGs attached to this instance
                all_ingress, all_egress = _merged_rules(inst.security_group_ids)
                sg_enforce.apply(inst, all_ingress, all_egress)

    threading.Thread(target=_apply, daemon=True).start()


def _merged_rules(sg_ids: list) -> tuple[list, list]:
    """Merge ingress/egress rules from a list of SG IDs."""
    ingress, egress = [], []
    for sg_id in sg_ids:
        sg = sg_store.get(sg_id)
        if sg:
            ingress.extend(sg.ingress_rules)
            egress.extend(sg.egress_rules)
    return ingress, egress
