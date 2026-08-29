"""
Scenario: DNS Lifecycle

Proves:
  - Instance DNS record appears when instance reaches running state
  - Instance DNS record value matches the instance's private_ip
  - LB DNS record appears immediately on creation
  - Adding/removing LB backends does not affect DNS record
  - Instance DNS record is removed when instance is terminated
  - LB DNS record is removed when LB is deleted
  - Custom zone and manual records are independent of resource lifecycle
"""
from __future__ import annotations

import time
import urllib.parse

from tests.lib.framework import assert_eq, assert_in, assert_not_in, req, vm_test
from tests.lib.helpers import (cleanup_by_prefix, cleanup_dns_zones_by_prefix,
                                delete_dns_zone, delete_instance, delete_lb,
                                delete_vpc, make_dns_zone, make_lb, make_vpc)

_ZE         = lambda z: urllib.parse.quote(z, safe="")
_POLL_EVERY = 10
_POLL_MAX   = 180


def _poll_running(instance_id: str) -> dict:
    deadline = time.time() + _POLL_MAX
    while time.time() < deadline:
        _, inst = req("GET", f"/v1/instances/{instance_id}")
        if inst["status"] == "running":
            return inst
        if inst["status"] == "error":
            raise AssertionError(f"instance {instance_id} entered error state")
        time.sleep(_POLL_EVERY)
    raise AssertionError(f"instance {instance_id} not running after {_POLL_MAX}s")


class ScenarioDNSLifecycle:
    def setUp(self):
        cleanup_by_prefix("instances", "sc-dns-inst-")
        cleanup_by_prefix("load-balancers", "sc-dns-lb-")
        cleanup_by_prefix("vpcs", "sc-dns-vpc")
        cleanup_dns_zones_by_prefix("sc-dns-zone-")
        self.vpc = make_vpc("sc-dns-vpc")

    # ── LB DNS (no VM needed) ─────────────────────────────────────────────────
    def test_01_lb_dns_record_created_immediately(self):
        lb = make_lb("sc-dns-lb-main", self.vpc["id"])
        _, data = req("GET", f"/v1/dns/zones/{_ZE('lb.cloudcore.local')}/records")
        names = [r["name"] for r in data["items"]]
        assert_in("sc-dns-lb-main", names, "lb DNS record created on LB create")
        delete_lb(lb["id"])

    def test_02_lb_dns_record_value_is_loopback(self):
        lb = make_lb("sc-dns-lb-val", self.vpc["id"])
        _, data = req("GET", f"/v1/dns/zones/{_ZE('lb.cloudcore.local')}/records")
        r = next((r for r in data["items"] if r["name"] == "sc-dns-lb-val"), None)
        if r is None:
            raise AssertionError("lb DNS record not found")
        assert_eq(r["value"], "127.0.0.1", "lb A record value is loopback")
        delete_lb(lb["id"])

    def test_03_lb_dns_record_has_correct_resource_id(self):
        lb = make_lb("sc-dns-lb-rid", self.vpc["id"])
        _, data = req("GET", f"/v1/dns/zones/{_ZE('lb.cloudcore.local')}/records")
        r = next((r for r in data["items"] if r["name"] == "sc-dns-lb-rid"), None)
        if r is None:
            raise AssertionError("lb DNS record not found")
        assert_eq(r["resource_id"], lb["id"], "resource_id matches lb id")
        delete_lb(lb["id"])

    def test_04_adding_backends_does_not_change_lb_dns(self):
        lb = make_lb("sc-dns-lb-bk", self.vpc["id"])
        req("POST", f"/v1/load-balancers/{lb['id']}/backends",
            {"name": "srv-01", "address": "192.168.100.10", "port": 80}, expected=201)
        _, data = req("GET", f"/v1/dns/zones/{_ZE('lb.cloudcore.local')}/records")
        recs = [r for r in data["items"] if r["name"] == "sc-dns-lb-bk"]
        assert_eq(len(recs), 1, "still exactly one DNS record after adding backend")
        assert_eq(recs[0]["value"], "127.0.0.1", "value unchanged after backend add")
        delete_lb(lb["id"])

    def test_05_lb_dns_record_removed_on_delete(self):
        lb = make_lb("sc-dns-lb-rm", self.vpc["id"])
        delete_lb(lb["id"])
        time.sleep(0.5)
        _, data = req("GET", f"/v1/dns/zones/{_ZE('lb.cloudcore.local')}/records")
        assert_not_in("sc-dns-lb-rm", [r["name"] for r in data["items"]],
                      "lb DNS record removed on delete")

    # ── Custom zone is independent ────────────────────────────────────────────
    def test_06_custom_zone_unaffected_by_lb_lifecycle(self):
        make_dns_zone("sc-dns-zone-custom.cloudcore.local")
        zenc = _ZE("sc-dns-zone-custom.cloudcore.local")
        req("POST", f"/v1/dns/zones/{zenc}/records",
            {"name": "myhost", "type": "A", "value": "10.0.0.1"}, expected=201)

        lb = make_lb("sc-dns-lb-custom", self.vpc["id"])
        delete_lb(lb["id"])
        time.sleep(0.5)

        _, data = req("GET", f"/v1/dns/zones/{zenc}/records")
        assert_in("myhost", [r["name"] for r in data["items"]],
                  "custom zone record unaffected by lb lifecycle")
        delete_dns_zone("sc-dns-zone-custom.cloudcore.local")

    # ── Instance DNS (VM required) ────────────────────────────────────────────
    @vm_test
    def test_07_instance_dns_record_appears_on_running(self):
        _, inst = req("POST", "/v1/instances", {
            "name":      "sc-dns-inst-main",
            "image_id":  "ubuntu-22.04",
            "flavor":    "standard.nano",
            "vpc_id":    self.vpc["id"],
            "subnet_id": "subnet-sc",
        }, expected=202)
        inst = _poll_running(inst["id"])

        _, data = req("GET",
            f"/v1/dns/zones/{_ZE('instances.cloudcore.local')}/records")
        names = [r["name"] for r in data["items"]]
        assert_in("sc-dns-inst-main", names, "instance DNS record created on running")

    @vm_test
    def test_08_instance_dns_record_value_matches_ip(self):
        _, data = req("GET", "/v1/instances")
        inst = next((i for i in data["items"]
                     if i["name"] == "sc-dns-inst-main"), None)
        if inst is None:
            raise AssertionError("sc-dns-inst-main not found — run test_07 first")

        _, recs = req("GET",
            f"/v1/dns/zones/{_ZE('instances.cloudcore.local')}/records")
        r = next((r for r in recs["items"] if r["name"] == "sc-dns-inst-main"), None)
        if r is None:
            raise AssertionError("instance DNS record not found")

        expected_ip = inst["private_ip"] or "127.0.0.1"
        assert_eq(r["value"], expected_ip, "DNS record value matches private_ip")

    @vm_test
    def test_09_instance_dns_record_removed_on_terminate(self):
        _, data = req("GET", "/v1/instances")
        inst = next((i for i in data["items"]
                     if i["name"] == "sc-dns-inst-main"), None)
        if inst is None:
            raise AssertionError("sc-dns-inst-main not found")

        delete_instance(inst["id"])
        time.sleep(2)

        _, recs = req("GET",
            f"/v1/dns/zones/{_ZE('instances.cloudcore.local')}/records")
        assert_not_in("sc-dns-inst-main", [r["name"] for r in recs["items"]],
                      "instance DNS record removed on terminate")
        delete_vpc(self.vpc["id"])
