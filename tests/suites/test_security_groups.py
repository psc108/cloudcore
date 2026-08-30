from __future__ import annotations

from tests.lib.framework import assert_eq, assert_in, assert_not_in, req
from tests.lib.helpers import (cleanup_by_prefix, cleanup_sgs_by_prefix,
                                delete_sg, delete_vpc,
                                make_sg, make_vpc)

PFX = "t-sg-"


class TestSecurityGroups:
    def setUp(self):
        cleanup_sgs_by_prefix(PFX)
        cleanup_by_prefix("vpcs", PFX + "vpc")
        self.vpc = make_vpc(PFX + "vpc")

    # ── Create ────────────────────────────────────────────────────────────────
    def test_create_returns_201(self):
        status, sg = req("POST", "/v1/security-groups",
                         {"name": PFX + "create", "vpc_id": self.vpc["id"]},
                         expected=201)
        assert_eq(status, 201, "create status")
        delete_sg(sg["id"])

    def test_create_name_stored(self):
        sg = make_sg(PFX + "name", self.vpc["id"])
        assert_eq(sg["name"], PFX + "name", "name stored")
        delete_sg(sg["id"])

    def test_create_vpc_id_stored(self):
        sg = make_sg(PFX + "vpcid", self.vpc["id"])
        assert_eq(sg["vpc_id"], self.vpc["id"], "vpc_id stored")
        delete_sg(sg["id"])

    def test_create_description_stored(self):
        sg = make_sg(PFX + "desc", self.vpc["id"], description="Web tier")
        assert_eq(sg["description"], "Web tier", "description stored")
        delete_sg(sg["id"])

    def test_create_id_present(self):
        sg = make_sg(PFX + "id", self.vpc["id"])
        assert_in("id", sg, "id present")
        if not sg["id"]:
            raise AssertionError("id is empty")
        delete_sg(sg["id"])

    def test_create_created_at_present(self):
        sg = make_sg(PFX + "ts", self.vpc["id"])
        assert_in("created_at", sg, "created_at present")
        delete_sg(sg["id"])

    def test_create_status_active(self):
        sg = make_sg(PFX + "status", self.vpc["id"])
        assert_eq(sg["status"], "active", "status active")
        delete_sg(sg["id"])

    def test_create_empty_rules(self):
        sg = make_sg(PFX + "emptyrules", self.vpc["id"])
        assert_eq(sg["ingress_rules"], [], "empty ingress")
        assert_eq(sg["egress_rules"],  [], "empty egress")
        delete_sg(sg["id"])

    def test_create_with_ingress_rule(self):
        sg = make_sg(PFX + "ingress", self.vpc["id"], ingress_rules=[
            {"protocol": "tcp", "from_port": 22, "to_port": 22, "cidr": "0.0.0.0/0"}
        ])
        assert_eq(len(sg["ingress_rules"]), 1, "one ingress rule")
        r = sg["ingress_rules"][0]
        assert_eq(r["protocol"],  "tcp",       "protocol")
        assert_eq(r["from_port"], 22,           "from_port")
        assert_eq(r["to_port"],   22,           "to_port")
        assert_eq(r["cidr"],      "0.0.0.0/0", "cidr")
        delete_sg(sg["id"])

    def test_create_with_all_traffic_rule(self):
        sg = make_sg(PFX + "alltraffic", self.vpc["id"], egress_rules=[
            {"protocol": "-1", "cidr": "0.0.0.0/0"}
        ])
        assert_eq(len(sg["egress_rules"]), 1, "one egress rule")
        assert_eq(sg["egress_rules"][0]["protocol"], "-1", "all-traffic protocol")
        delete_sg(sg["id"])

    def test_create_missing_name_returns_400(self):
        req("POST", "/v1/security-groups", {"vpc_id": self.vpc["id"]}, expected=400)

    def test_create_missing_vpc_id_returns_400(self):
        req("POST", "/v1/security-groups", {"name": PFX + "novpc"}, expected=400)

    def test_create_invalid_vpc_returns_404(self):
        req("POST", "/v1/security-groups",
            {"name": PFX + "badvpc", "vpc_id": "does-not-exist"}, expected=404)

    def test_create_duplicate_name_rejected(self):
        sg = make_sg(PFX + "dup", self.vpc["id"])
        req("POST", "/v1/security-groups",
            {"name": PFX + "dup", "vpc_id": self.vpc["id"]}, expected=409)
        delete_sg(sg["id"])

    def test_create_invalid_protocol_returns_400(self):
        req("POST", "/v1/security-groups", {
            "name": PFX + "badproto", "vpc_id": self.vpc["id"],
            "ingress_rules": [{"protocol": "sctp", "from_port": 80, "to_port": 80, "cidr": "0.0.0.0/0"}]
        }, expected=400)

    def test_create_missing_ports_for_tcp_returns_400(self):
        req("POST", "/v1/security-groups", {
            "name": PFX + "noports", "vpc_id": self.vpc["id"],
            "ingress_rules": [{"protocol": "tcp", "cidr": "0.0.0.0/0"}]
        }, expected=400)

    # ── Read ──────────────────────────────────────────────────────────────────
    def test_list_includes_created(self):
        sg = make_sg(PFX + "list", self.vpc["id"])
        _, data = req("GET", "/v1/security-groups")
        assert_in(sg["id"], [x["id"] for x in data["items"]], "sg in list")
        delete_sg(sg["id"])

    def test_list_excludes_deleted(self):
        sg = make_sg(PFX + "listdel", self.vpc["id"])
        delete_sg(sg["id"])
        _, data = req("GET", "/v1/security-groups")
        assert_not_in(sg["id"], [x["id"] for x in data["items"]], "deleted not in list")

    def test_get_by_id(self):
        sg = make_sg(PFX + "get", self.vpc["id"])
        _, got = req("GET", f"/v1/security-groups/{sg['id']}")
        assert_eq(got["id"], sg["id"], "id matches")
        delete_sg(sg["id"])

    def test_get_missing_returns_404(self):
        req("GET", "/v1/security-groups/does-not-exist", expected=404)

    # ── Update ────────────────────────────────────────────────────────────────
    def test_update_description(self):
        sg = make_sg(PFX + "upddesc", self.vpc["id"])
        _, u = req("PUT", f"/v1/security-groups/{sg['id']}",
                   {"description": "updated desc"})
        assert_eq(u["description"], "updated desc", "description updated")
        delete_sg(sg["id"])

    def test_update_adds_ingress_rule(self):
        sg = make_sg(PFX + "addrule", self.vpc["id"])
        _, u = req("PUT", f"/v1/security-groups/{sg['id']}", {
            "ingress_rules": [
                {"protocol": "tcp", "from_port": 80, "to_port": 80, "cidr": "0.0.0.0/0"}
            ],
            "egress_rules": [],
        })
        assert_eq(len(u["ingress_rules"]), 1, "one ingress rule after update")
        delete_sg(sg["id"])

    def test_update_removes_rule(self):
        sg = make_sg(PFX + "rmrule", self.vpc["id"], ingress_rules=[
            {"protocol": "tcp", "from_port": 22, "to_port": 22, "cidr": "0.0.0.0/0"},
            {"protocol": "tcp", "from_port": 80, "to_port": 80, "cidr": "0.0.0.0/0"},
        ])
        _, u = req("PUT", f"/v1/security-groups/{sg['id']}", {
            "ingress_rules": [
                {"protocol": "tcp", "from_port": 22, "to_port": 22, "cidr": "0.0.0.0/0"}
            ],
            "egress_rules": [],
        })
        assert_eq(len(u["ingress_rules"]), 1, "one rule after remove")
        delete_sg(sg["id"])

    def test_update_persists(self):
        sg = make_sg(PFX + "persist", self.vpc["id"])
        req("PUT", f"/v1/security-groups/{sg['id']}", {"description": "persisted"})
        _, got = req("GET", f"/v1/security-groups/{sg['id']}")
        assert_eq(got["description"], "persisted", "update persisted")
        delete_sg(sg["id"])

    def test_update_invalid_protocol_returns_400(self):
        sg = make_sg(PFX + "updproto", self.vpc["id"])
        req("PUT", f"/v1/security-groups/{sg['id']}", {
            "ingress_rules": [{"protocol": "gre", "cidr": "0.0.0.0/0"}],
            "egress_rules": [],
        }, expected=400)
        delete_sg(sg["id"])

    def test_update_missing_returns_404(self):
        req("PUT", "/v1/security-groups/does-not-exist",
            {"description": "x"}, expected=404)

    # ── Delete ────────────────────────────────────────────────────────────────
    def test_delete_returns_204(self):
        sg = make_sg(PFX + "del204", self.vpc["id"])
        status, _ = req("DELETE", f"/v1/security-groups/{sg['id']}", expected=204)
        assert_eq(status, 204, "delete status")

    def test_delete_removes_from_list(self):
        sg = make_sg(PFX + "del", self.vpc["id"])
        delete_sg(sg["id"])
        _, data = req("GET", "/v1/security-groups")
        assert_not_in(sg["id"], [x["id"] for x in data["items"]], "sg removed")

    def test_delete_missing_returns_404(self):
        req("DELETE", "/v1/security-groups/does-not-exist", expected=404)

    # ── Port range ────────────────────────────────────────────────────────────
    def test_port_range_stored(self):
        sg = make_sg(PFX + "portrange", self.vpc["id"], ingress_rules=[
            {"protocol": "tcp", "from_port": 8000, "to_port": 8080, "cidr": "10.0.0.0/8"}
        ])
        r = sg["ingress_rules"][0]
        assert_eq(r["from_port"], 8000,         "from_port range")
        assert_eq(r["to_port"],   8080,         "to_port range")
        assert_eq(r["cidr"],      "10.0.0.0/8", "cidr range")
        delete_sg(sg["id"])

    def test_from_port_greater_than_to_port_returns_400(self):
        req("POST", "/v1/security-groups", {
            "name": PFX + "badrange", "vpc_id": self.vpc["id"],
            "ingress_rules": [{"protocol": "tcp", "from_port": 443, "to_port": 80, "cidr": "0.0.0.0/0"}]
        }, expected=400)
