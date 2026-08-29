"""
Scenario: Inter-Instance SSH

Proves:
  - Two instances can reach each other via the CloudCore shared keypair
  - The keypair is present at ~/.ssh/cloudcore_ed25519 on both instances
  - Instance A can SSH to instance B's private IP without a password
  - Instance B can SSH to instance A's private IP without a password
"""
from __future__ import annotations

import subprocess
import time
from pathlib import Path

from tests.lib.framework import assert_eq, req, vm_test
from tests.lib.helpers import (cleanup_by_prefix, delete_instance,
                                delete_vpc, make_vpc)

_PRIVKEY    = str(Path(__file__).parent.parent.parent / "api" / "keys" / "cloudcore_ed25519")
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


def _ssh(port: int, user: str, cmd: str) -> str:
    r = subprocess.run([
        "ssh", "-i", _PRIVKEY, "-p", str(port),
        "-o", "StrictHostKeyChecking=no", "-o", "ConnectTimeout=15",
        f"{user}@127.0.0.1", cmd,
    ], capture_output=True, text=True, timeout=30)
    if r.returncode != 0:
        raise AssertionError(f"SSH failed rc={r.returncode}: {r.stderr.strip()}")
    return r.stdout.strip()


class ScenarioInterInstanceSSH:
    def setUp(self):
        cleanup_by_prefix("instances", "sc-ssh-inst-")
        cleanup_by_prefix("vpcs", "sc-ssh-vpc")
        self.vpc = make_vpc("sc-ssh-vpc")

    @vm_test
    def test_01_launch_two_instances(self):
        for n in (1, 2):
            _, inst = req("POST", "/v1/instances", {
                "name":      f"sc-ssh-inst-{n}",
                "image_id":  "ubuntu-22.04",
                "flavor":    "standard.nano",
                "vpc_id":    self.vpc["id"],
                "subnet_id": "subnet-sc",
            }, expected=202)

        _, data = req("GET", "/v1/instances")
        for inst in [i for i in data["items"] if i["name"].startswith("sc-ssh-inst-")]:
            _poll_running(inst["id"])

    @vm_test
    def test_02_keypair_present_on_both(self):
        _, data = req("GET", "/v1/instances")
        instances = [i for i in data["items"]
                     if i["name"].startswith("sc-ssh-inst-") and i["status"] == "running"]
        if len(instances) < 2:
            raise AssertionError(f"need 2 running instances, got {len(instances)}")

        time.sleep(20)
        for inst in instances:
            out = _ssh(inst["ssh_port"], inst["ssh_user"],
                       "test -f ~/.ssh/cloudcore_ed25519 && echo present")
            if "present" not in out:
                raise AssertionError(
                    f"keypair missing on {inst['name']}: {out!r}")

    @vm_test
    def test_03_instance_a_can_ssh_to_b(self):
        _, data = req("GET", "/v1/instances")
        instances = sorted(
            [i for i in data["items"]
             if i["name"].startswith("sc-ssh-inst-") and i["status"] == "running"],
            key=lambda i: i["name"])
        if len(instances) < 2:
            raise AssertionError("need 2 running instances")

        inst_a, inst_b = instances[0], instances[1]
        ip_b = inst_b.get("private_ip")
        if not ip_b or ip_b == "10.0.2.15":
            # SLIRP — inter-instance SSH not possible without bridge
            raise AssertionError(
                "inter-instance SSH requires bridge networking (ccbr0). "
                "Both instances have SLIRP private_ip — run setup-network.sh first.")

        user = inst_a["ssh_user"]
        cmd  = (f"ssh -i ~/.ssh/cloudcore_ed25519 "
                f"-o StrictHostKeyChecking=no -o ConnectTimeout=10 "
                f"{user}@{ip_b} echo b-reachable")
        out = _ssh(inst_a["ssh_port"], user, cmd)
        if "b-reachable" not in out:
            raise AssertionError(f"A→B SSH failed: {out!r}")

    @vm_test
    def test_04_instance_b_can_ssh_to_a(self):
        _, data = req("GET", "/v1/instances")
        instances = sorted(
            [i for i in data["items"]
             if i["name"].startswith("sc-ssh-inst-") and i["status"] == "running"],
            key=lambda i: i["name"])
        if len(instances) < 2:
            raise AssertionError("need 2 running instances")

        inst_a, inst_b = instances[0], instances[1]
        ip_a = inst_a.get("private_ip")
        if not ip_a or ip_a == "10.0.2.15":
            raise AssertionError(
                "inter-instance SSH requires bridge networking (ccbr0).")

        user = inst_b["ssh_user"]
        cmd  = (f"ssh -i ~/.ssh/cloudcore_ed25519 "
                f"-o StrictHostKeyChecking=no -o ConnectTimeout=10 "
                f"{user}@{ip_a} echo a-reachable")
        out = _ssh(inst_b["ssh_port"], user, cmd)
        if "a-reachable" not in out:
            raise AssertionError(f"B→A SSH failed: {out!r}")

    @vm_test
    def test_05_teardown(self):
        _, data = req("GET", "/v1/instances")
        for inst in data["items"]:
            if inst["name"].startswith("sc-ssh-inst-"):
                delete_instance(inst["id"])
        delete_vpc(self.vpc["id"])
