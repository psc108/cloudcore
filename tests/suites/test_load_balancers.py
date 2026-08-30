from __future__ import annotations

from tests.lib.framework import assert_eq, assert_in, assert_not_in, req
from tests.lib.helpers import (cleanup_by_prefix, delete_lb, delete_vpc,
                                make_lb, make_vpc)


class TestLoadBalancers:
    def setUp(self):
        cleanup_by_prefix("load-balancers", "t-lb-")
        cleanup_by_prefix("vpcs", "t-lb-vpc")
        self.vpc = make_vpc("t-lb-vpc")

    # ── Create ────────────────────────────────────────────────────────────────
    def test_create_returns_201(self):
        lb = make_lb("t-lb-create", self.vpc["id"])
        assert_eq(lb["status"], "active", "status")
        delete_lb(lb["id"])

    def test_create_name_stored(self):
        lb = make_lb("t-lb-name", self.vpc["id"])
        assert_eq(lb["name"], "t-lb-name", "name stored")
        delete_lb(lb["id"])

    def test_create_type_application_default(self):
        lb = make_lb("t-lb-typeapp", self.vpc["id"])
        assert_eq(lb["type"], "application", "type default application")
        delete_lb(lb["id"])

    def test_create_type_network(self):
        lb = make_lb("t-lb-typenet", self.vpc["id"], lb_type="network")
        assert_eq(lb["type"], "network", "type network stored")
        delete_lb(lb["id"])

    def test_create_vpc_id_stored(self):
        lb = make_lb("t-lb-vpc", self.vpc["id"])
        assert_eq(lb["vpc_id"], self.vpc["id"], "vpc_id stored")
        delete_lb(lb["id"])

    def test_create_internal_false_default(self):
        lb = make_lb("t-lb-extfacing", self.vpc["id"])
        assert_eq(lb["internal"], False, "internal false default")
        delete_lb(lb["id"])

    def test_create_internal_true_stored(self):
        _, lb = req("POST", "/v1/load-balancers",
                    {"name": "t-lb-internal", "vpc_id": self.vpc["id"],
                     "internal": True}, expected=201)
        assert_eq(lb["internal"], True, "internal true stored")
        delete_lb(lb["id"])

    def test_create_dns_name_set(self):
        lb = make_lb("t-lb-dnsname", self.vpc["id"])
        assert_eq(lb["dns_name"], "t-lb-dnsname.lb.cloudcore.local", "dns_name")
        delete_lb(lb["id"])

    def test_create_listen_port_in_range(self):
        lb = make_lb("t-lb-port", self.vpc["id"])
        if not (8200 <= lb["listen_port"] <= 8299):
            raise AssertionError(f"listen_port {lb['listen_port']} outside 8200-8299")
        delete_lb(lb["id"])

    def test_create_listen_port_is_integer(self):
        lb = make_lb("t-lb-porttype", self.vpc["id"])
        if not isinstance(lb["listen_port"], int):
            raise AssertionError(f"listen_port is not int: {type(lb['listen_port'])}")
        delete_lb(lb["id"])

    def test_create_tags_stored(self):
        _, lb = req("POST", "/v1/load-balancers",
                    {"name": "t-lb-tags", "vpc_id": self.vpc["id"],
                     "tags": {"env": "ci", "team": "ops"}}, expected=201)
        assert_eq(lb["tags"]["env"],  "ci",  "env tag")
        assert_eq(lb["tags"]["team"], "ops", "team tag")
        delete_lb(lb["id"])

    def test_create_id_present(self):
        lb = make_lb("t-lb-id", self.vpc["id"])
        assert_in("id", lb, "id field")
        if not lb["id"]:
            raise AssertionError("id is empty")
        delete_lb(lb["id"])

    def test_create_created_at_present(self):
        lb = make_lb("t-lb-ts", self.vpc["id"])
        assert_in("created_at", lb, "created_at present")
        delete_lb(lb["id"])

    def test_create_missing_name_returns_400(self):
        req("POST", "/v1/load-balancers", {"vpc_id": self.vpc["id"]}, expected=400)

    def test_create_duplicate_name_rejected(self):
        lb = make_lb("t-lb-dup", self.vpc["id"])
        req("POST", "/v1/load-balancers",
            {"name": "t-lb-dup", "vpc_id": self.vpc["id"]}, expected=409)
        delete_lb(lb["id"])

    # ── Read ──────────────────────────────────────────────────────────────────
    def test_list_includes_created(self):
        lb = make_lb("t-lb-list", self.vpc["id"])
        _, data = req("GET", "/v1/load-balancers")
        assert_in(lb["id"], [x["id"] for x in data["items"]], "lb in list")
        delete_lb(lb["id"])

    def test_list_excludes_deleted(self):
        lb = make_lb("t-lb-listdel", self.vpc["id"])
        delete_lb(lb["id"])
        _, data = req("GET", "/v1/load-balancers")
        assert_not_in(lb["id"], [x["id"] for x in data["items"]], "deleted not in list")

    def test_get_by_id(self):
        lb = make_lb("t-lb-get", self.vpc["id"])
        _, got = req("GET", f"/v1/load-balancers/{lb['id']}")
        assert_eq(got["id"], lb["id"], "id matches")
        delete_lb(lb["id"])

    def test_get_missing_returns_404(self):
        req("GET", "/v1/load-balancers/does-not-exist", expected=404)

    # ── Update ────────────────────────────────────────────────────────────────
    def test_update_tags(self):
        lb = make_lb("t-lb-updtags", self.vpc["id"])
        _, u = req("PUT", f"/v1/load-balancers/{lb['id']}", {"tags": {"env": "prod"}})
        assert_eq(u["tags"]["env"], "prod", "tag updated")
        delete_lb(lb["id"])

    def test_update_internal(self):
        lb = make_lb("t-lb-updint", self.vpc["id"])
        _, u = req("PUT", f"/v1/load-balancers/{lb['id']}", {"internal": True})
        assert_eq(u["internal"], True, "internal updated")
        delete_lb(lb["id"])

    def test_update_persists(self):
        lb = make_lb("t-lb-persist", self.vpc["id"])
        req("PUT", f"/v1/load-balancers/{lb['id']}", {"tags": {"k": "v"}})
        _, got = req("GET", f"/v1/load-balancers/{lb['id']}")
        assert_eq(got["tags"]["k"], "v", "update persisted")
        delete_lb(lb["id"])

    def test_update_preserves_backends(self):
        lb = make_lb("t-lb-updbk", self.vpc["id"])
        req("POST", f"/v1/load-balancers/{lb['id']}/backends",
            {"name": "srv-01", "address": "192.168.100.10", "port": 80}, expected=201)
        req("PUT", f"/v1/load-balancers/{lb['id']}", {"tags": {"k": "v"}})
        _, got = req("GET", f"/v1/load-balancers/{lb['id']}")
        assert_eq(len(got["backends"]), 1, "backend preserved after update")
        delete_lb(lb["id"])

    # ── Delete ────────────────────────────────────────────────────────────────
    def test_delete_returns_204(self):
        lb = make_lb("t-lb-del204", self.vpc["id"])
        status, _ = req("DELETE", f"/v1/load-balancers/{lb['id']}", expected=204)
        assert_eq(status, 204, "delete status")

    def test_delete_removes_from_list(self):
        lb = make_lb("t-lb-del", self.vpc["id"])
        delete_lb(lb["id"])
        _, data = req("GET", "/v1/load-balancers")
        assert_not_in(lb["id"], [x["id"] for x in data["items"]], "lb removed")

    def test_delete_missing_returns_404(self):
        req("DELETE", "/v1/load-balancers/does-not-exist", expected=404)

    # ── Backends ──────────────────────────────────────────────────────────────
    def test_add_backend_name_stored(self):
        lb = make_lb("t-lb-bkname", self.vpc["id"])
        _, u = req("POST", f"/v1/load-balancers/{lb['id']}/backends",
                   {"name": "srv-01", "address": "192.168.100.10", "port": 80},
                   expected=201)
        assert_in("srv-01", [b["name"] for b in u["backends"]], "backend name stored")
        delete_lb(lb["id"])

    def test_add_backend_address_stored(self):
        lb = make_lb("t-lb-bkaddr", self.vpc["id"])
        _, u = req("POST", f"/v1/load-balancers/{lb['id']}/backends",
                   {"name": "srv-01", "address": "192.168.100.10", "port": 80},
                   expected=201)
        bk = next(b for b in u["backends"] if b["name"] == "srv-01")
        assert_eq(bk["address"], "192.168.100.10", "address stored")
        delete_lb(lb["id"])

    def test_add_backend_port_stored_as_int(self):
        lb = make_lb("t-lb-bkport", self.vpc["id"])
        _, u = req("POST", f"/v1/load-balancers/{lb['id']}/backends",
                   {"name": "srv-01", "address": "192.168.100.10", "port": 8080},
                   expected=201)
        bk = next(b for b in u["backends"] if b["name"] == "srv-01")
        assert_eq(bk["port"], 8080, "port stored")
        if not isinstance(bk["port"], int):
            raise AssertionError(f"port is not int: {type(bk['port'])}")
        delete_lb(lb["id"])

    def test_add_backend_missing_name_returns_400(self):
        lb = make_lb("t-lb-bknoname", self.vpc["id"])
        req("POST", f"/v1/load-balancers/{lb['id']}/backends",
            {"address": "192.168.100.10", "port": 80}, expected=400)
        delete_lb(lb["id"])

    def test_add_backend_missing_address_returns_400(self):
        lb = make_lb("t-lb-bknoaddr", self.vpc["id"])
        req("POST", f"/v1/load-balancers/{lb['id']}/backends",
            {"name": "srv-01", "port": 80}, expected=400)
        delete_lb(lb["id"])

    def test_add_backend_missing_port_returns_400(self):
        lb = make_lb("t-lb-bknoport", self.vpc["id"])
        req("POST", f"/v1/load-balancers/{lb['id']}/backends",
            {"name": "srv-01", "address": "192.168.100.10"}, expected=400)
        delete_lb(lb["id"])

    def test_add_multiple_backends(self):
        lb = make_lb("t-lb-bkmulti", self.vpc["id"])
        for i in range(3):
            req("POST", f"/v1/load-balancers/{lb['id']}/backends",
                {"name": f"srv-{i}", "address": f"192.168.100.{10+i}", "port": 80},
                expected=201)
        _, got = req("GET", f"/v1/load-balancers/{lb['id']}")
        assert_eq(len(got["backends"]), 3, "three backends stored")
        delete_lb(lb["id"])

    def test_remove_backend(self):
        lb = make_lb("t-lb-rmbk", self.vpc["id"])
        req("POST", f"/v1/load-balancers/{lb['id']}/backends",
            {"name": "srv-rm", "address": "192.168.100.11", "port": 80}, expected=201)
        req("DELETE", f"/v1/load-balancers/{lb['id']}/backends/srv-rm", expected=204)
        _, got = req("GET", f"/v1/load-balancers/{lb['id']}")
        assert_not_in("srv-rm", [b["name"] for b in got["backends"]], "backend removed")
        delete_lb(lb["id"])

    def test_remove_one_of_multiple_backends(self):
        lb = make_lb("t-lb-rmmulti", self.vpc["id"])
        for name in ("keep", "remove"):
            req("POST", f"/v1/load-balancers/{lb['id']}/backends",
                {"name": name, "address": "192.168.100.10", "port": 80}, expected=201)
        req("DELETE", f"/v1/load-balancers/{lb['id']}/backends/remove", expected=204)
        _, got = req("GET", f"/v1/load-balancers/{lb['id']}")
        names = [b["name"] for b in got["backends"]]
        assert_not_in("remove", names, "removed backend gone")
        assert_in("keep", names, "kept backend remains")
        delete_lb(lb["id"])

    # ── Listeners ─────────────────────────────────────────────────────────────
    def test_listeners_empty_on_create(self):
        lb = make_lb("t-lb-lstempty", self.vpc["id"])
        assert_eq(lb["listeners"], [], "listeners empty on create")
        delete_lb(lb["id"])

    def test_add_listener_returns_201(self):
        lb = make_lb("t-lb-lstadd", self.vpc["id"])
        status, lst = req("POST", f"/v1/load-balancers/{lb['id']}/listeners",
                          {"port": 80, "protocol": "HTTP"}, expected=201)
        assert_eq(status, 201, "201 returned")
        assert_in("id", lst, "id in listener")
        delete_lb(lb["id"])

    def test_add_listener_port_stored(self):
        lb = make_lb("t-lb-lstport", self.vpc["id"])
        req("POST", f"/v1/load-balancers/{lb['id']}/listeners",
            {"port": 8080, "protocol": "HTTP"}, expected=201)
        _, got = req("GET", f"/v1/load-balancers/{lb['id']}")
        assert_in(8080, [l["port"] for l in got["listeners"]], "port stored")
        delete_lb(lb["id"])

    def test_add_listener_protocol_stored(self):
        lb = make_lb("t-lb-lstproto", self.vpc["id"])
        req("POST", f"/v1/load-balancers/{lb['id']}/listeners",
            {"port": 443, "protocol": "HTTPS"}, expected=201)
        _, got = req("GET", f"/v1/load-balancers/{lb['id']}")
        lst = next(l for l in got["listeners"] if l["port"] == 443)
        assert_eq(lst["protocol"], "HTTPS", "protocol stored")
        delete_lb(lb["id"])

    def test_add_listener_tcp_protocol(self):
        lb = make_lb("t-lb-lsttcp", self.vpc["id"])
        req("POST", f"/v1/load-balancers/{lb['id']}/listeners",
            {"port": 3306, "protocol": "TCP"}, expected=201)
        _, got = req("GET", f"/v1/load-balancers/{lb['id']}")
        lst = next(l for l in got["listeners"] if l["port"] == 3306)
        assert_eq(lst["protocol"], "TCP", "TCP protocol stored")
        delete_lb(lb["id"])

    def test_add_listener_default_action_stored(self):
        lb = make_lb("t-lb-lstaction", self.vpc["id"])
        req("POST", f"/v1/load-balancers/{lb['id']}/listeners",
            {"port": 80, "protocol": "HTTP", "default_action": "forward"}, expected=201)
        _, got = req("GET", f"/v1/load-balancers/{lb['id']}")
        lst = next(l for l in got["listeners"] if l["port"] == 80)
        assert_eq(lst["default_action"], "forward", "default_action stored")
        delete_lb(lb["id"])

    def test_add_listener_missing_port_returns_400(self):
        lb = make_lb("t-lb-lstnoport", self.vpc["id"])
        req("POST", f"/v1/load-balancers/{lb['id']}/listeners",
            {"protocol": "HTTP"}, expected=400)
        delete_lb(lb["id"])

    def test_add_listener_invalid_protocol_returns_400(self):
        lb = make_lb("t-lb-lstbadproto", self.vpc["id"])
        req("POST", f"/v1/load-balancers/{lb['id']}/listeners",
            {"port": 80, "protocol": "FTP"}, expected=400)
        delete_lb(lb["id"])

    def test_add_listener_duplicate_port_returns_409(self):
        lb = make_lb("t-lb-lstdup", self.vpc["id"])
        req("POST", f"/v1/load-balancers/{lb['id']}/listeners",
            {"port": 80, "protocol": "HTTP"}, expected=201)
        req("POST", f"/v1/load-balancers/{lb['id']}/listeners",
            {"port": 80, "protocol": "HTTP"}, expected=409)
        delete_lb(lb["id"])

    def test_add_multiple_listeners(self):
        lb = make_lb("t-lb-lstmulti", self.vpc["id"])
        for port in (80, 443, 8080):
            req("POST", f"/v1/load-balancers/{lb['id']}/listeners",
                {"port": port, "protocol": "HTTP"}, expected=201)
        _, got = req("GET", f"/v1/load-balancers/{lb['id']}")
        assert_eq(len(got["listeners"]), 3, "three listeners stored")
        delete_lb(lb["id"])

    def test_remove_listener(self):
        lb = make_lb("t-lb-lstrm", self.vpc["id"])
        _, lst = req("POST", f"/v1/load-balancers/{lb['id']}/listeners",
                     {"port": 80, "protocol": "HTTP"}, expected=201)
        req("DELETE", f"/v1/load-balancers/{lb['id']}/listeners/{lst['id']}", expected=204)
        _, got = req("GET", f"/v1/load-balancers/{lb['id']}")
        assert_eq(got["listeners"], [], "listener removed")
        delete_lb(lb["id"])

    def test_remove_listener_missing_returns_404(self):
        lb = make_lb("t-lb-lstrmno", self.vpc["id"])
        req("DELETE", f"/v1/load-balancers/{lb['id']}/listeners/does-not-exist", expected=404)
        delete_lb(lb["id"])

    def test_remove_one_of_multiple_listeners(self):
        lb = make_lb("t-lb-lstrmone", self.vpc["id"])
        _, l1 = req("POST", f"/v1/load-balancers/{lb['id']}/listeners",
                    {"port": 80, "protocol": "HTTP"}, expected=201)
        _, l2 = req("POST", f"/v1/load-balancers/{lb['id']}/listeners",
                    {"port": 443, "protocol": "HTTPS"}, expected=201)
        req("DELETE", f"/v1/load-balancers/{lb['id']}/listeners/{l1['id']}", expected=204)
        _, got = req("GET", f"/v1/load-balancers/{lb['id']}")
        ports = [l["port"] for l in got["listeners"]]
        assert_not_in(80, ports, "port 80 removed")
        assert_in(443, ports, "port 443 remains")
        delete_lb(lb["id"])

    # ── Health Check ──────────────────────────────────────────────────────────
    def test_health_check_empty_on_create(self):
        lb = make_lb("t-lb-hcempty", self.vpc["id"])
        assert_eq(lb["health_check"], {}, "health_check empty on create")
        delete_lb(lb["id"])

    def test_set_health_check_returns_200(self):
        lb = make_lb("t-lb-hcset", self.vpc["id"])
        status, hc = req("PUT", f"/v1/load-balancers/{lb['id']}/health-check",
                         {"protocol": "HTTP", "path": "/health", "interval": 10,
                          "healthy_threshold": 2, "unhealthy_threshold": 3}, expected=200)
        assert_eq(status, 200, "200 returned")
        assert_eq(hc["protocol"], "HTTP", "protocol stored")
        delete_lb(lb["id"])

    def test_health_check_path_stored(self):
        lb = make_lb("t-lb-hcpath", self.vpc["id"])
        req("PUT", f"/v1/load-balancers/{lb['id']}/health-check",
            {"protocol": "HTTP", "path": "/ping"}, expected=200)
        _, got = req("GET", f"/v1/load-balancers/{lb['id']}")
        assert_eq(got["health_check"]["path"], "/ping", "path stored")
        delete_lb(lb["id"])

    def test_health_check_interval_stored(self):
        lb = make_lb("t-lb-hcinterval", self.vpc["id"])
        req("PUT", f"/v1/load-balancers/{lb['id']}/health-check",
            {"protocol": "HTTP", "interval": 15}, expected=200)
        _, got = req("GET", f"/v1/load-balancers/{lb['id']}")
        assert_eq(got["health_check"]["interval"], 15, "interval stored")
        delete_lb(lb["id"])

    def test_health_check_thresholds_stored(self):
        lb = make_lb("t-lb-hcthresh", self.vpc["id"])
        req("PUT", f"/v1/load-balancers/{lb['id']}/health-check",
            {"protocol": "HTTP", "healthy_threshold": 3, "unhealthy_threshold": 5}, expected=200)
        _, got = req("GET", f"/v1/load-balancers/{lb['id']}")
        assert_eq(got["health_check"]["healthy_threshold"], 3, "healthy_threshold")
        assert_eq(got["health_check"]["unhealthy_threshold"], 5, "unhealthy_threshold")
        delete_lb(lb["id"])

    def test_health_check_tcp_protocol(self):
        lb = make_lb("t-lb-hctcp", self.vpc["id"])
        req("PUT", f"/v1/load-balancers/{lb['id']}/health-check",
            {"protocol": "TCP"}, expected=200)
        _, got = req("GET", f"/v1/load-balancers/{lb['id']}")
        assert_eq(got["health_check"]["protocol"], "TCP", "TCP protocol stored")
        delete_lb(lb["id"])

    def test_health_check_invalid_protocol_returns_400(self):
        lb = make_lb("t-lb-hcbadproto", self.vpc["id"])
        req("PUT", f"/v1/load-balancers/{lb['id']}/health-check",
            {"protocol": "FTP"}, expected=400)
        delete_lb(lb["id"])

    def test_health_check_persists(self):
        lb = make_lb("t-lb-hcpersist", self.vpc["id"])
        req("PUT", f"/v1/load-balancers/{lb['id']}/health-check",
            {"protocol": "HTTP", "path": "/ready", "interval": 20}, expected=200)
        _, got = req("GET", f"/v1/load-balancers/{lb['id']}")
        assert_eq(got["health_check"]["path"], "/ready", "persisted")
        delete_lb(lb["id"])

    def test_health_check_update_overwrites(self):
        lb = make_lb("t-lb-hcupdate", self.vpc["id"])
        req("PUT", f"/v1/load-balancers/{lb['id']}/health-check",
            {"protocol": "HTTP", "path": "/old"}, expected=200)
        req("PUT", f"/v1/load-balancers/{lb['id']}/health-check",
            {"protocol": "HTTP", "path": "/new"}, expected=200)
        _, got = req("GET", f"/v1/load-balancers/{lb['id']}")
        assert_eq(got["health_check"]["path"], "/new", "overwritten")
        delete_lb(lb["id"])

    def test_delete_health_check(self):
        lb = make_lb("t-lb-hcdel", self.vpc["id"])
        req("PUT", f"/v1/load-balancers/{lb['id']}/health-check",
            {"protocol": "HTTP"}, expected=200)
        req("DELETE", f"/v1/load-balancers/{lb['id']}/health-check", expected=204)
        _, got = req("GET", f"/v1/load-balancers/{lb['id']}")
        assert_eq(got["health_check"], {}, "health_check cleared")
        delete_lb(lb["id"])

    def test_health_check_missing_lb_returns_404(self):
        req("PUT", "/v1/load-balancers/does-not-exist/health-check",
            {"protocol": "HTTP"}, expected=404)
