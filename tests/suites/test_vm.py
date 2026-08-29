from __future__ import annotations

import subprocess
import time
from pathlib import Path

from tests.lib.framework import assert_eq, assert_in, assert_not_in, req, vm_test
from tests.lib.helpers import (cleanup_by_prefix, delete_instance,
                                delete_vpc, make_vpc)

import urllib.parse

_KEYS_DIR    = Path(__file__).parent.parent.parent / "api" / "keys"
_PRIVKEY     = str(_KEYS_DIR / "cloudcore_ed25519")
_POLL_EVERY  = 10   # seconds
_POLL_MAX    = 180  # seconds


def _poll_running(instance_id: str) -> dict:
    deadline = time.time() + _POLL_MAX
    while time.time() < deadline:
        _, inst = req("GET", f"/v1/instances/{instance_id}")
        if inst["status"] == "running":
            return inst
        if inst["status"] == "error":
            raise AssertionError("instance entered error state")
        time.sleep(_POLL_EVERY)
    raise AssertionError(f"instance not running after {_POLL_MAX}s")


def _ssh(port: int, user: str, command: str) -> str:
    result = subprocess.run([
        "ssh", "-i", _PRIVKEY, "-p", str(port),
        "-o", "StrictHostKeyChecking=no",
        "-o", "ConnectTimeout=15",
        f"{user}@127.0.0.1", command,
    ], capture_output=True, text=True, timeout=30)
    if result.returncode != 0:
        raise AssertionError(
            f"SSH failed rc={result.returncode}: {result.stderr.strip()}")
    return result.stdout.strip()


class TestVM:
    def setUp(self):
        cleanup_by_prefix("instances", "t-vm-")
        cleanup_by_prefix("vpcs", "t-vm-vpc")
        self.vpc = make_vpc("t-vm-vpc")

    @vm_test
    def test_launch_and_terminate(self):
        _, inst = req("POST", "/v1/instances", {
            "name": "t-vm-basic", "image_id": "ubuntu-22.04",
            "flavor": "standard.nano", "vpc_id": self.vpc["id"],
            "subnet_id": "subnet-vm-test",
        }, expected=202)
        inst = _poll_running(inst["id"])
        assert_eq(inst["status"], "running", "status running")
        if not inst.get("ssh_port"):
            raise AssertionError("no ssh_port assigned")
        delete_instance(inst["id"])

    @vm_test
    def test_ssh_keypair_works(self):
        _, inst = req("POST", "/v1/instances", {
            "name": "t-vm-ssh", "image_id": "ubuntu-22.04",
            "flavor": "standard.nano", "vpc_id": self.vpc["id"],
            "subnet_id": "subnet-vm-test",
        }, expected=202)
        inst = _poll_running(inst["id"])
        time.sleep(20)  # let cloud-init finish
        out = _ssh(inst["ssh_port"], inst["ssh_user"], "echo cloudcore-ok")
        if "cloudcore-ok" not in out:
            raise AssertionError(f"unexpected SSH output: {out!r}")
        delete_instance(inst["id"])

    @vm_test
    def test_user_added_before_launch(self):
        _, inst = req("POST", "/v1/instances", {
            "name": "t-vm-user", "image_id": "ubuntu-22.04",
            "flavor": "standard.nano", "vpc_id": self.vpc["id"],
            "subnet_id": "subnet-vm-test",
        }, expected=202)
        iid = inst["id"]
        req("POST", f"/v1/instances/{iid}/users",
            {"username": "testuser", "sudo": False}, expected=201)
        inst = _poll_running(iid)
        time.sleep(20)
        out = _ssh(inst["ssh_port"], "testuser", "echo user-ok")
        if "user-ok" not in out:
            raise AssertionError(f"unexpected SSH output: {out!r}")
        delete_instance(iid)

    @vm_test
    def test_instance_dns_auto_registered(self):
        _, inst = req("POST", "/v1/instances", {
            "name": "t-vm-dns", "image_id": "ubuntu-22.04",
            "flavor": "standard.nano", "vpc_id": self.vpc["id"],
            "subnet_id": "subnet-vm-test",
        }, expected=202)
        iid  = inst["id"]
        inst = _poll_running(iid)
        zenc = urllib.parse.quote("instances.cloudcore.local", safe="")
        _, data = req("GET", f"/v1/dns/zones/{zenc}/records")
        assert_in("t-vm-dns", [r["name"] for r in data["items"]],
                  "instance auto-registered in DNS")
        delete_instance(iid)
        time.sleep(2)
        _, data = req("GET", f"/v1/dns/zones/{zenc}/records")
        assert_not_in("t-vm-dns", [r["name"] for r in data["items"]],
                      "DNS record removed after delete")

    @vm_test
    def test_flavors(self):
        """Verify each flavor launches without error."""
        for flavor in ("standard.nano", "standard.small"):
            _, inst = req("POST", "/v1/instances", {
                "name": f"t-vm-{flavor.split('.')[1]}",
                "image_id": "ubuntu-22.04",
                "flavor": flavor,
                "vpc_id": self.vpc["id"],
                "subnet_id": "subnet-vm-test",
            }, expected=202)
            inst = _poll_running(inst["id"])
            assert_eq(inst["flavor"], flavor, f"flavor {flavor}")
            delete_instance(inst["id"])
