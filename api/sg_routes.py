"""Security group API routes — /v1/security-groups"""
from __future__ import annotations

from flask import Blueprint, request, jsonify

import sg_store
import store as resource_store
from models import SecurityGroup

sg_bp = Blueprint("sg", __name__)

_VALID_PROTOCOLS = {"tcp", "udp", "icmp", "-1"}


def _problem(status, title, detail):
    return jsonify({"status": status, "title": title, "detail": detail}), status


def _validate_rules(rules: list) -> str | None:
    """Return an error string if any rule is invalid, else None."""
    for r in rules:
        proto = r.get("protocol", "-1")
        if proto not in _VALID_PROTOCOLS:
            return f"protocol must be one of: {', '.join(sorted(_VALID_PROTOCOLS))}"
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


@sg_bp.get("/v1/security-groups")
def list_sgs():
    return jsonify({"items": [sg.to_dict() for sg in sg_store.list_all()]})


@sg_bp.post("/v1/security-groups")
def create_sg():
    body = request.get_json(force=True) or {}
    name = body.get("name", "").strip()
    if not name:
        return _problem(400, "Bad Request", "name is required")
    if not body.get("vpc_id"):
        return _problem(400, "Bad Request", "vpc_id is required")
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
    return jsonify(sg.to_dict()), 201


@sg_bp.get("/v1/security-groups/<sg_id>")
def get_sg(sg_id):
    sg = sg_store.get(sg_id)
    if not sg:
        return _problem(404, "Not Found", f"Security group '{sg_id}' not found")
    return jsonify(sg.to_dict())


@sg_bp.put("/v1/security-groups/<sg_id>")
def update_sg(sg_id):
    sg = sg_store.get(sg_id)
    if not sg:
        return _problem(404, "Not Found", f"Security group '{sg_id}' not found")
    body = request.get_json(force=True) or {}

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

    # Re-apply rules to any running instances that reference this SG
    _reapply_to_instances(sg)

    return jsonify(sg.to_dict())


@sg_bp.delete("/v1/security-groups/<sg_id>")
def delete_sg(sg_id):
    if not sg_store.delete(sg_id):
        return _problem(404, "Not Found", f"Security group '{sg_id}' not found")
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
