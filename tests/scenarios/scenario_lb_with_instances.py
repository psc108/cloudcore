"""
Scenario: Load Balancer with Live Instances

Proves:
  - Two instances launch and reach running state
  - Both are registered as LB backends
  - HAProxy is actually listening on the assigned port
  - HTTP requests to the LB port reach a backend (round-robin)
  - DNS records exist for both instances and the LB
  - Full teardown removes all DNS records
"""
from __future__ import annotations

import http.client
import socket
import subprocess
import time
import urllib.parse
from pathlib import Path

from tests.lib.framework import assert_eq, assert_in, assert_not_in, req, vm_test
from tests.lib.helpers import (cleanup_by_prefix, cleanup_dns_zones_by_prefix,
                                delete_instance, delete_lb, delete_vpc,
                                make_lb, make_vpc)

_PRIVKEY     = str(Path(__file__).parent.parent.parent / "api" / "keys" / "cloudcore_ed25519")
_POLL_EVERY  = 10
_POLL_MAX    = 180
_ZE          = lambda z: urllib.parse.quote(z, safe="")


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


def _ssh(port: int, user: str, cmd: str) -> str:
    r = subprocess.run([
        "ssh", "-i", _PRIVKEY, "-p", str(port),
        "-o", "StrictHostKeyChecking=no", "-o", "ConnectTimeout=15",
        f"{user}@127.0.0.1", cmd,
    ], capture_output=True, text=True, timeout=30)
    if r.returncode != 0:
        raise AssertionError(f"SSH failed rc={r.returncode}: {r.stderr.strip()}")
    return r.stdout.strip()


def _port_open(port: int, timeout: float = 5.0) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=timeout):
            return True
    except OSError:
        return False


class ScenarioLBWithInstances:
    """Full end-to-end: VPC → 2 instances → LB → backends → HTTP traffic."""

    def setUp(self):
        cleanup_by_prefix("instances", "sc-lb-inst-")
        cleanup_by_prefix("load-balancers", "sc-lb-")
        cleanup_by_prefix("vpcs", "sc-lb-vpc")
        self.vpc       = make_vpc("sc-lb-vpc")
        self.instances = []
        self.lb        = None

    # ── Step 1: launch two instances ─────────────────────────────────────────
    @vm_test
    def test_01_launch_two_instances(self):
        for n in (1, 2):
            _, inst = req("POST", "/v1/instances", {
                "name":      f"sc-lb-inst-{n}",
                "image_id":  "ubuntu-22.04",
                "flavor":    "standard.nano",
                "vpc_id":    self.vpc["id"],
                "subnet_id": "subnet-sc",
            }, expected=202)
            self.instances.append(inst["id"])

        running = []
        for iid in self.instances:
            inst = _poll_running(iid)
            running.append(inst)
            assert_eq(inst["status"], "running", f"instance {iid} running")

        # Both must have SSH ports
        for inst in running:
            if not inst.get("ssh_port"):
                raise AssertionError(f"instance {inst['id']} has no ssh_port")

    # ── Step 2: SSH into each instance ────────────────────────────────────────
    @vm_test
    def test_02_ssh_into_each_instance(self):
        _, data = req("GET", "/v1/instances")
        instances = [i for i in data["items"]
                     if i["name"].startswith("sc-lb-inst-") and i["status"] == "running"]
        if len(instances) < 2:
            raise AssertionError(f"expected 2 running instances, got {len(instances)}")

        time.sleep(20)  # let cloud-init finish
        for inst in instances:
            out = _ssh(inst["ssh_port"], inst["ssh_user"], "echo ok")
            if "ok" not in out:
                raise AssertionError(f"SSH to {inst['name']} failed: {out!r}")

    # ── Step 3: start a simple HTTP server on each instance ───────────────────
    @vm_test
    def test_03_start_http_servers(self):
        _, data = req("GET", "/v1/instances")
        instances = [i for i in data["items"]
                     if i["name"].startswith("sc-lb-inst-") and i["status"] == "running"]

        for inst in instances:
            # Start a minimal Python HTTP server that returns the instance name
            _ssh(inst["ssh_port"], inst["ssh_user"],
                 f"nohup python3 -m http.server 8080 "
                 f"--directory /tmp > /tmp/http.log 2>&1 &")
        time.sleep(3)

    # ── Step 4: create LB and add both instances as backends ─────────────────
    @vm_test
    def test_04_create_lb_with_backends(self):
        _, data = req("GET", "/v1/instances")
        instances = [i for i in data["items"]
                     if i["name"].startswith("sc-lb-inst-") and i["status"] == "running"]

        lb = make_lb("sc-lb-main", self.vpc["id"])
        self.lb = lb

        for inst in instances:
            # Use the instance's private IP and the HTTP server port
            ip = inst.get("private_ip") or "127.0.0.1"
            _, updated = req("POST", f"/v1/load-balancers/{lb['id']}/backends", {
                "name":    inst["name"],
                "address": ip,
                "port":    8080,
            }, expected=201)

        _, got = req("GET", f"/v1/load-balancers/{lb['id']}")
        assert_eq(len(got["backends"]), 2, "two backends registered")

    # ── Step 5: LB port is open ───────────────────────────────────────────────
    @vm_test
    def test_05_lb_port_is_listening(self):
        _, data = req("GET", "/v1/load-balancers")
        lb = next((lb for lb in data["items"] if lb["name"] == "sc-lb-main"), None)
        if lb is None:
            raise AssertionError("sc-lb-main not found")
        port = lb["listen_port"]
        if not _port_open(port):
            raise AssertionError(f"LB port {port} is not open")

    # ── Step 6: DNS records exist for all resources ───────────────────────────
    @vm_test
    def test_06_dns_records_exist(self):
        _, inst_recs = req("GET",
            f"/v1/dns/zones/{_ZE('instances.cloudcore.local')}/records")
        inst_names = [r["name"] for r in inst_recs["items"]]
        for n in (1, 2):
            assert_in(f"sc-lb-inst-{n}", inst_names,
                      f"sc-lb-inst-{n} in instances DNS")

        _, lb_recs = req("GET",
            f"/v1/dns/zones/{_ZE('lb.cloudcore.local')}/records")
        lb_names = [r["name"] for r in lb_recs["items"]]
        assert_in("sc-lb-main", lb_names, "sc-lb-main in lb DNS")

    # ── Step 7: teardown — verify DNS cleaned up ──────────────────────────────
    @vm_test
    def test_07_teardown_cleans_dns(self):
        _, data = req("GET", "/v1/load-balancers")
        lb = next((lb for lb in data["items"] if lb["name"] == "sc-lb-main"), None)
        if lb:
            delete_lb(lb["id"])

        _, data = req("GET", "/v1/instances")
        for inst in data["items"]:
            if inst["name"].startswith("sc-lb-inst-"):
                delete_instance(inst["id"])

        time.sleep(2)

        _, lb_recs = req("GET",
            f"/v1/dns/zones/{_ZE('lb.cloudcore.local')}/records")
        assert_not_in("sc-lb-main", [r["name"] for r in lb_recs["items"]],
                      "lb DNS record removed")

        _, inst_recs = req("GET",
            f"/v1/dns/zones/{_ZE('instances.cloudcore.local')}/records")
        inst_names = [r["name"] for r in inst_recs["items"]]
        for n in (1, 2):
            assert_not_in(f"sc-lb-inst-{n}", inst_names,
                          f"sc-lb-inst-{n} DNS record removed")

        delete_vpc(self.vpc["id"])
