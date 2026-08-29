from __future__ import annotations

import time
import urllib.parse

from tests.lib.framework import assert_eq, assert_in, assert_not_in, req
from tests.lib.helpers import (cleanup_by_prefix, cleanup_dns_zones_by_prefix,
                                delete_dns_zone, delete_lb, delete_vpc,
                                make_dns_zone, make_lb, make_vpc)

_ZE = lambda z: urllib.parse.quote(z, safe="")


class TestDNS:
    def setUp(self):
        cleanup_dns_zones_by_prefix("t-zone-")
        cleanup_by_prefix("load-balancers", "t-dns-lb-")
        cleanup_by_prefix("vpcs", "t-dns-vpc")
        self.vpc = make_vpc("t-dns-vpc")

    # ── Built-in zones ────────────────────────────────────────────────────────
    def test_builtin_zones_exist(self):
        _, data = req("GET", "/v1/dns/zones")
        names = [z["name"] for z in data["items"]]
        assert_in("instances.cloudcore.local", names, "instances zone")
        assert_in("lb.cloudcore.local",        names, "lb zone")

    def test_builtin_zones_flagged(self):
        _, data = req("GET", "/v1/dns/zones")
        for z in data["items"]:
            if z["name"] in ("instances.cloudcore.local", "lb.cloudcore.local"):
                assert_eq(z["builtin"], True, f"{z['name']} builtin flag")

    def test_builtin_zone_delete_blocked(self):
        req("DELETE", f"/v1/dns/zones/{_ZE('instances.cloudcore.local')}",
            expected=400)
        req("DELETE", f"/v1/dns/zones/{_ZE('lb.cloudcore.local')}",
            expected=400)

    # ── Zone CRUD ─────────────────────────────────────────────────────────────
    def test_create_zone_name_stored(self):
        z = make_dns_zone("t-zone-name.cloudcore.local")
        assert_eq(z["name"], "t-zone-name.cloudcore.local", "zone name")
        delete_dns_zone("t-zone-name.cloudcore.local")

    def test_create_zone_returns_201(self):
        status, _ = req("POST", "/v1/dns/zones",
                        {"name": "t-zone-201.cloudcore.local"}, expected=201)
        assert_eq(status, 201, "create status")
        delete_dns_zone("t-zone-201.cloudcore.local")

    def test_create_zone_created_at_present(self):
        z = make_dns_zone("t-zone-ts.cloudcore.local")
        assert_in("created_at", z, "created_at present")
        delete_dns_zone("t-zone-ts.cloudcore.local")

    def test_create_zone_record_count_zero(self):
        z = make_dns_zone("t-zone-count.cloudcore.local")
        assert_eq(z["record_count"], 0, "initial record_count")
        delete_dns_zone("t-zone-count.cloudcore.local")

    def test_create_zone_missing_name_returns_400(self):
        req("POST", "/v1/dns/zones", {}, expected=400)

    def test_create_zone_duplicate_rejected(self):
        make_dns_zone("t-zone-dup.cloudcore.local")
        req("POST", "/v1/dns/zones",
            {"name": "t-zone-dup.cloudcore.local"}, expected=409)
        delete_dns_zone("t-zone-dup.cloudcore.local")

    def test_list_zones_includes_created(self):
        make_dns_zone("t-zone-list.cloudcore.local")
        _, data = req("GET", "/v1/dns/zones")
        names = [z["name"] for z in data["items"]]
        assert_in("t-zone-list.cloudcore.local", names, "zone in list")
        delete_dns_zone("t-zone-list.cloudcore.local")

    def test_delete_zone_returns_204(self):
        make_dns_zone("t-zone-del204.cloudcore.local")
        status, _ = req("DELETE",
            f"/v1/dns/zones/{_ZE('t-zone-del204.cloudcore.local')}",
            expected=204)
        assert_eq(status, 204, "delete status")

    def test_delete_zone_missing_returns_404(self):
        req("DELETE", f"/v1/dns/zones/{_ZE('no-such.zone')}", expected=404)

    # ── Record CRUD ───────────────────────────────────────────────────────────
    def test_create_a_record_fields(self):
        make_dns_zone("t-zone-arec.cloudcore.local")
        _, r = req("POST",
            f"/v1/dns/zones/{_ZE('t-zone-arec.cloudcore.local')}/records",
            {"name": "host1", "type": "A", "value": "192.168.100.10", "ttl": 60},
            expected=201)
        assert_eq(r["name"],  "host1",                             "name")
        assert_eq(r["type"],  "A",                                 "type")
        assert_eq(r["value"], "192.168.100.10",                    "value")
        assert_eq(r["ttl"],   60,                                  "ttl")
        assert_eq(r["fqdn"],  "host1.t-zone-arec.cloudcore.local", "fqdn")
        delete_dns_zone("t-zone-arec.cloudcore.local")

    def test_create_record_default_ttl(self):
        make_dns_zone("t-zone-ttl.cloudcore.local")
        _, r = req("POST",
            f"/v1/dns/zones/{_ZE('t-zone-ttl.cloudcore.local')}/records",
            {"name": "h", "type": "A", "value": "1.2.3.4"}, expected=201)
        assert_eq(r["ttl"], 300, "default ttl 300")
        delete_dns_zone("t-zone-ttl.cloudcore.local")

    def test_create_record_created_at_present(self):
        make_dns_zone("t-zone-rects.cloudcore.local")
        _, r = req("POST",
            f"/v1/dns/zones/{_ZE('t-zone-rects.cloudcore.local')}/records",
            {"name": "h", "type": "A", "value": "1.2.3.4"}, expected=201)
        assert_in("created_at", r, "created_at present")
        delete_dns_zone("t-zone-rects.cloudcore.local")

    def test_create_record_resource_type_manual(self):
        make_dns_zone("t-zone-manual.cloudcore.local")
        _, r = req("POST",
            f"/v1/dns/zones/{_ZE('t-zone-manual.cloudcore.local')}/records",
            {"name": "h", "type": "A", "value": "1.2.3.4"}, expected=201)
        assert_eq(r["resource_type"], "manual", "resource_type manual")
        delete_dns_zone("t-zone-manual.cloudcore.local")

    def test_create_cname_record(self):
        make_dns_zone("t-zone-cname.cloudcore.local")
        _, r = req("POST",
            f"/v1/dns/zones/{_ZE('t-zone-cname.cloudcore.local')}/records",
            {"name": "www", "type": "CNAME",
             "value": "host1.t-zone-cname.cloudcore.local"}, expected=201)
        assert_eq(r["type"], "CNAME", "cname type")
        delete_dns_zone("t-zone-cname.cloudcore.local")

    def test_create_txt_record(self):
        make_dns_zone("t-zone-txt.cloudcore.local")
        _, r = req("POST",
            f"/v1/dns/zones/{_ZE('t-zone-txt.cloudcore.local')}/records",
            {"name": "info", "type": "TXT", "value": "v=spf1 ~all"}, expected=201)
        assert_eq(r["type"], "TXT", "txt type")
        delete_dns_zone("t-zone-txt.cloudcore.local")

    def test_create_record_invalid_type_rejected(self):
        make_dns_zone("t-zone-badtype.cloudcore.local")
        req("POST",
            f"/v1/dns/zones/{_ZE('t-zone-badtype.cloudcore.local')}/records",
            {"name": "x", "type": "MX", "value": "mail.example.com"}, expected=400)
        delete_dns_zone("t-zone-badtype.cloudcore.local")

    def test_create_record_missing_value_returns_400(self):
        make_dns_zone("t-zone-badval.cloudcore.local")
        req("POST",
            f"/v1/dns/zones/{_ZE('t-zone-badval.cloudcore.local')}/records",
            {"name": "x", "type": "A"}, expected=400)
        delete_dns_zone("t-zone-badval.cloudcore.local")

    def test_create_record_missing_name_returns_400(self):
        make_dns_zone("t-zone-badname.cloudcore.local")
        req("POST",
            f"/v1/dns/zones/{_ZE('t-zone-badname.cloudcore.local')}/records",
            {"type": "A", "value": "1.2.3.4"}, expected=400)
        delete_dns_zone("t-zone-badname.cloudcore.local")

    def test_upsert_updates_existing_record(self):
        make_dns_zone("t-zone-upsert.cloudcore.local")
        zenc = _ZE("t-zone-upsert.cloudcore.local")
        req("POST", f"/v1/dns/zones/{zenc}/records",
            {"name": "h", "type": "A", "value": "1.2.3.4"}, expected=201)
        req("POST", f"/v1/dns/zones/{zenc}/records",
            {"name": "h", "type": "A", "value": "5.6.7.8"}, expected=201)
        _, data = req("GET", f"/v1/dns/zones/{zenc}/records")
        assert_eq(len(data["items"]), 1, "upsert — still one record")
        assert_eq(data["items"][0]["value"], "5.6.7.8", "value updated")
        delete_dns_zone("t-zone-upsert.cloudcore.local")

    def test_list_records(self):
        make_dns_zone("t-zone-listrec.cloudcore.local")
        zenc = _ZE("t-zone-listrec.cloudcore.local")
        req("POST", f"/v1/dns/zones/{zenc}/records",
            {"name": "a", "type": "A", "value": "1.2.3.4"}, expected=201)
        req("POST", f"/v1/dns/zones/{zenc}/records",
            {"name": "b", "type": "A", "value": "5.6.7.8"}, expected=201)
        _, data = req("GET", f"/v1/dns/zones/{zenc}/records")
        assert_eq(len(data["items"]), 2, "two records")
        delete_dns_zone("t-zone-listrec.cloudcore.local")

    def test_record_count_increments(self):
        make_dns_zone("t-zone-rcnt.cloudcore.local")
        zenc = _ZE("t-zone-rcnt.cloudcore.local")
        req("POST", f"/v1/dns/zones/{zenc}/records",
            {"name": "h", "type": "A", "value": "1.2.3.4"}, expected=201)
        _, data = req("GET", "/v1/dns/zones")
        z = next(z for z in data["items"] if z["name"] == "t-zone-rcnt.cloudcore.local")
        assert_eq(z["record_count"], 1, "record_count incremented")
        delete_dns_zone("t-zone-rcnt.cloudcore.local")

    def test_delete_record(self):
        make_dns_zone("t-zone-delrec.cloudcore.local")
        zenc = _ZE("t-zone-delrec.cloudcore.local")
        req("POST", f"/v1/dns/zones/{zenc}/records",
            {"name": "todel", "type": "A", "value": "1.2.3.4"}, expected=201)
        req("DELETE", f"/v1/dns/zones/{zenc}/records/todel/A", expected=204)
        _, data = req("GET", f"/v1/dns/zones/{zenc}/records")
        assert_eq(len(data["items"]), 0, "record removed")
        delete_dns_zone("t-zone-delrec.cloudcore.local")

    def test_delete_missing_record_returns_404(self):
        make_dns_zone("t-zone-delmiss.cloudcore.local")
        zenc = _ZE("t-zone-delmiss.cloudcore.local")
        req("DELETE", f"/v1/dns/zones/{zenc}/records/nobody/A", expected=404)
        delete_dns_zone("t-zone-delmiss.cloudcore.local")

    def test_list_records_missing_zone_returns_404(self):
        req("GET", f"/v1/dns/zones/{_ZE('no-such.zone')}/records", expected=404)

    # ── Auto-registration ─────────────────────────────────────────────────────
    def test_lb_auto_registers_dns(self):
        lb = make_lb("t-dns-lb-auto", self.vpc["id"])
        _, data = req("GET", f"/v1/dns/zones/{_ZE('lb.cloudcore.local')}/records")
        assert_in("t-dns-lb-auto", [r["name"] for r in data["items"]],
                  "lb auto-registered")
        delete_lb(lb["id"])

    def test_lb_dns_record_value_is_loopback(self):
        lb = make_lb("t-dns-lb-val", self.vpc["id"])
        _, data = req("GET", f"/v1/dns/zones/{_ZE('lb.cloudcore.local')}/records")
        r = next((r for r in data["items"] if r["name"] == "t-dns-lb-val"), None)
        if r is None:
            raise AssertionError("lb record not found")
        assert_eq(r["value"], "127.0.0.1", "lb A record value")
        delete_lb(lb["id"])

    def test_lb_dns_resource_type_is_lb(self):
        lb = make_lb("t-dns-lb-rtype", self.vpc["id"])
        _, data = req("GET", f"/v1/dns/zones/{_ZE('lb.cloudcore.local')}/records")
        r = next((r for r in data["items"] if r["name"] == "t-dns-lb-rtype"), None)
        if r is None:
            raise AssertionError("lb record not found")
        assert_eq(r["resource_type"], "lb", "resource_type lb")
        assert_eq(r["resource_id"],   lb["id"], "resource_id matches lb id")
        delete_lb(lb["id"])

    def test_lb_delete_removes_dns(self):
        lb = make_lb("t-dns-lb-rm", self.vpc["id"])
        delete_lb(lb["id"])
        time.sleep(0.5)
        _, data = req("GET", f"/v1/dns/zones/{_ZE('lb.cloudcore.local')}/records")
        assert_not_in("t-dns-lb-rm", [r["name"] for r in data["items"]],
                      "DNS record removed after LB delete")
