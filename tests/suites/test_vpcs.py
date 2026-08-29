from __future__ import annotations

from tests.lib.framework import assert_eq, assert_in, assert_not_in, req
from tests.lib.helpers import cleanup_by_prefix, delete_vpc, make_vpc


class TestVPCs:
    def setUp(self):
        cleanup_by_prefix("vpcs", "t-vpc-")

    # ── Create ────────────────────────────────────────────────────────────────
    def test_create_returns_201(self):
        v = make_vpc("t-vpc-create")
        assert_eq(v["status"], "active", "status")
        delete_vpc(v["id"])

    def test_create_name_stored(self):
        v = make_vpc("t-vpc-name")
        assert_eq(v["name"], "t-vpc-name", "name")
        delete_vpc(v["id"])

    def test_create_cidr_stored(self):
        v = make_vpc("t-vpc-cidr", cidr="172.16.0.0/12")
        assert_eq(v["cidr_block"], "172.16.0.0/12", "cidr stored")
        delete_vpc(v["id"])

    def test_create_cidr_default(self):
        v = make_vpc("t-vpc-cidrdef")
        assert_eq(v["cidr_block"], "10.99.0.0/16", "cidr default")
        delete_vpc(v["id"])

    def test_create_dns_support_true(self):
        v = make_vpc("t-vpc-dnst")
        assert_eq(v["dns_support"], True, "dns_support true")
        delete_vpc(v["id"])

    def test_create_dns_support_false(self):
        _, v = req("POST", "/v1/vpcs",
                   {"name": "t-vpc-dnsf", "dns_support": False}, expected=201)
        assert_eq(v["dns_support"], False, "dns_support false")
        delete_vpc(v["id"])

    def test_create_tags_stored(self):
        v = make_vpc("t-vpc-tags", tags={"owner": "test", "env": "ci"})
        assert_eq(v["tags"]["owner"], "test", "owner tag")
        assert_eq(v["tags"]["env"],   "ci",   "env tag")
        delete_vpc(v["id"])

    def test_create_id_present(self):
        v = make_vpc("t-vpc-id")
        assert_in("id", v, "id field present")
        if not v["id"]:
            raise AssertionError("id is empty")
        delete_vpc(v["id"])

    def test_create_created_at_present(self):
        v = make_vpc("t-vpc-ts")
        assert_in("created_at", v, "created_at present")
        if not v["created_at"]:
            raise AssertionError("created_at is empty")
        delete_vpc(v["id"])

    def test_create_missing_name_returns_400(self):
        req("POST", "/v1/vpcs", {"cidr_block": "10.0.0.0/16"}, expected=400)

    def test_create_duplicate_name_rejected(self):
        v = make_vpc("t-vpc-dup")
        req("POST", "/v1/vpcs", {"name": "t-vpc-dup"}, expected=409)
        delete_vpc(v["id"])

    # ── Read ──────────────────────────────────────────────────────────────────
    def test_list_includes_created(self):
        v = make_vpc("t-vpc-list")
        _, data = req("GET", "/v1/vpcs")
        assert_in(v["id"], [x["id"] for x in data["items"]], "vpc in list")
        delete_vpc(v["id"])

    def test_list_excludes_deleted(self):
        v = make_vpc("t-vpc-listdel")
        delete_vpc(v["id"])
        _, data = req("GET", "/v1/vpcs")
        assert_not_in(v["id"], [x["id"] for x in data["items"]], "deleted vpc not in list")

    def test_get_by_id(self):
        v = make_vpc("t-vpc-get")
        _, got = req("GET", f"/v1/vpcs/{v['id']}")
        assert_eq(got["id"],   v["id"],   "id matches")
        assert_eq(got["name"], v["name"], "name matches")
        delete_vpc(v["id"])

    def test_get_missing_returns_404(self):
        req("GET", "/v1/vpcs/does-not-exist", expected=404)

    # ── Update ────────────────────────────────────────────────────────────────
    def test_update_tags(self):
        v = make_vpc("t-vpc-updtags")
        _, u = req("PUT", f"/v1/vpcs/{v['id']}", {"tags": {"env": "prod"}})
        assert_eq(u["tags"]["env"], "prod", "tag updated")
        delete_vpc(v["id"])

    def test_update_name(self):
        v = make_vpc("t-vpc-updname")
        _, u = req("PUT", f"/v1/vpcs/{v['id']}", {"name": "t-vpc-updname-new"})
        assert_eq(u["name"], "t-vpc-updname-new", "name updated")
        delete_vpc(v["id"])

    def test_update_dns_support(self):
        v = make_vpc("t-vpc-upddns")
        _, u = req("PUT", f"/v1/vpcs/{v['id']}", {"dns_support": False})
        assert_eq(u["dns_support"], False, "dns_support updated")
        delete_vpc(v["id"])

    def test_update_persists(self):
        v = make_vpc("t-vpc-persist")
        req("PUT", f"/v1/vpcs/{v['id']}", {"tags": {"k": "v"}})
        _, got = req("GET", f"/v1/vpcs/{v['id']}")
        assert_eq(got["tags"]["k"], "v", "update persisted")
        delete_vpc(v["id"])

    # ── Delete ────────────────────────────────────────────────────────────────
    def test_delete_returns_204(self):
        v = make_vpc("t-vpc-del204")
        status, _ = req("DELETE", f"/v1/vpcs/{v['id']}", expected=204)
        assert_eq(status, 204, "delete status")

    def test_delete_removes_from_list(self):
        v = make_vpc("t-vpc-del")
        delete_vpc(v["id"])
        _, data = req("GET", "/v1/vpcs")
        assert_not_in(v["id"], [x["id"] for x in data["items"]], "vpc removed")

    def test_delete_missing_returns_404(self):
        req("DELETE", "/v1/vpcs/does-not-exist", expected=404)
