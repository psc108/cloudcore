"""Full-VM backend for the advice runner (llm-chat-full-vm-Phased-Implementation.md, F4).

Same interface the runner uses on a microvm.MicroVM -- boot(), ssh_client(user),
vcpu_count / mem_size_mib / scratch_mib, wait_exit(), teardown() -- but the
machine is a throwaway CloudCore VM from the lab-VM broker (/v1/lab-vms, F1),
on the isolated lab network (F2), with real disks (F3).

Differences the runner accounts for:
- reboots_in_place: a reboot restarts the same VM; the runner reconnects
  instead of rebuilding a microVM from its saved disk.
- target_addr: the address the prober uses for the target (its lab address),
  known after boot; on the microVM it was the fixed pair-bridge 172.30.0.2.
- control: SSH to the VM's own control sshd on port 1022 (key-only, no PAM),
  which an answer's sshd changes can't touch (the microVM used vsock).
- not keepable (yet): proof VMs are destroyed at the end of the run.

Usage, matching run_advice's make_vm / make_prober / pair_bridges:

    labs = FullVMLabs(broker_url, broker_token)
    run_advice(answer, labs.make_target, make_prober=labs.make_prober,
               pair_bridges=(labs.new_run, labs.end_run), ...)
"""

from __future__ import annotations

import json
import shutil
import socket
import subprocess
import tempfile
import threading
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

import paramiko

CONTROL_PORT = 1022
# flavor -> (vcpus, MiB, root GiB), as api/compute.py FLAVORS
_FLAVORS = {"standard.nano": (1, 512, 5), "standard.small": (1, 1024, 10), "standard.medium": (2, 2048, 20),
            "standard.large": (4, 4096, 40), "standard.xlarge": (6, 8192, 60), "standard.2xlarge": (6, 16384, 100)}


class BootError(RuntimeError):
    pass


class _Broker:
    def __init__(self, url: str, token: str):
        self.url, self.token = url.rstrip("/"), token

    def call(self, method: str, path: str, body: dict | None = None, timeout: float = 60) -> tuple[int, dict]:
        req = urllib.request.Request(self.url + path, method=method,
                                     data=json.dumps(body).encode() if body is not None else None,
                                     headers={"Authorization": f"Bearer {self.token}",
                                              "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read()
                return resp.status, (json.loads(raw) if raw else {})
        except urllib.error.HTTPError as e:
            raw = e.read()
            try:
                return e.code, json.loads(raw) if raw else {}
            except ValueError:
                return e.code, {"detail": raw[:300].decode("utf-8", "replace")}


def _port_open(ip: str, port: int, timeout: float = 3.0) -> bool:
    try:
        with socket.create_connection((ip, port), timeout=timeout):
            return True
    except OSError:
        return False


class FullVM:
    reboots_in_place = True
    keepable = False

    def __init__(self, broker: _Broker, purpose: str, run_id: str, pair_with: "FullVM | None" = None,
                 boot_timeout_s: int = 600):
        self.broker, self.purpose, self.run_id, self.pair_with = broker, purpose, run_id, pair_with
        self.boot_timeout_s = boot_timeout_s
        self.id = ""
        self.ip = ""
        self.flavor = ""
        self.vcpu_count = self.mem_size_mib = self.scratch_mib = 0
        self._dir = Path(tempfile.mkdtemp(prefix="fullvm-"))
        self.private_key_path = str(self._dir / "id_ed25519")
        self._prestart: threading.Thread | None = None
        self._prestart_error: Exception | None = None
        self.jail_dir = str(self._dir)  # the runner's microVM-only reboot path never uses it here

    @property
    def target_addr(self) -> str:
        return self.ip

    def _wait(self, what: str, ok, deadline: float, every: float = 3.0):
        while time.monotonic() < deadline:
            value = ok()
            if value:
                return value
            time.sleep(every)
        raise BootError(f"{self.purpose} VM {self.id or '?'}: timed out waiting for {what}")

    def boot(self) -> None:
        if self._prestart is not None:
            # Started early (FullVMLabs prefetch): wait for it, then pair.
            self._prestart.join()
            if self._prestart_error:
                raise self._prestart_error
        else:
            self._boot_vm()
        if self.pair_with is not None:
            st, d = self.broker.call("POST", f"/v1/lab-vms/{self.id}/pair", {"with": self.pair_with.id})
            if st != 201:
                raise BootError(f"pairing the prober with its target failed ({st}): {d.get('detail')}")

    def start_early(self) -> None:
        """Begin booting now, in the background; boot() then just waits."""
        def run():
            try:
                self._boot_vm()
            except Exception as e:  # noqa: BLE001 -- re-raised from boot()
                self._prestart_error = e
        self._prestart = threading.Thread(target=run, name=f"fullvm-{self.purpose}", daemon=True)
        self._prestart.start()

    def _boot_vm(self) -> None:
        subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", f"lab-{self.run_id}",
                        "-f", self.private_key_path], check=True)
        pub = Path(self.private_key_path + ".pub").read_text().strip()
        status, vm = self.broker.call("POST", "/v1/lab-vms",
                                      {"purpose": self.purpose, "run_id": self.run_id, "public_key": pub})
        if status != 202:
            raise BootError(f"the lab-VM broker refused a {self.purpose} VM ({status}): {vm.get('detail')}")
        self.id, self.flavor = vm["id"], vm.get("flavor", "")
        self.vcpu_count, self.mem_size_mib, root_gb = _FLAVORS.get(self.flavor, (0, 0, 0))
        self.scratch_mib = root_gb * 1024
        deadline = time.monotonic() + self.boot_timeout_s

        def addr():
            st, d = self.broker.call("GET", f"/v1/lab-vms/{self.id}")
            if st == 200 and d.get("status") == "error":
                raise BootError(f"{self.purpose} VM failed to start: {d.get('error')}")
            return d.get("ip") if st == 200 and d.get("status") == "running" else ""
        self.ip = self._wait("an address", addr, deadline)
        # cloud-init installs the packages and starts the control sshd; the
        # control port answering means it has got that far.
        self._wait("the control sshd (port 1022)", lambda: _port_open(self.ip, CONTROL_PORT), deadline)
        client = self._wait("a control login", self._try_login, deadline, every=5.0)
        try:
            _, out, _ = client.exec_command("cloud-init status --wait >/dev/null 2>&1; cloud-init status", timeout=600)
            state = out.read().decode().strip()
        finally:
            client.close()
        if "error" in state:
            raise BootError(f"{self.purpose} VM's cloud-init reported: {state}")

    def _try_login(self):
        try:
            return self.ssh_client("root")
        except (paramiko.SSHException, OSError):
            return None

    def ssh_client(self, username: str = "student") -> paramiko.SSHClient:
        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        client.connect(self.ip, port=CONTROL_PORT, username=username, key_filename=self.private_key_path,
                       timeout=15, banner_timeout=15, auth_timeout=15, look_for_keys=False, allow_agent=False)
        return client

    def wait_exit(self, timeout_s: float) -> bool:
        """After a reboot: wait for the VM to go down and come back up."""
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline and _port_open(self.ip, CONTROL_PORT, 2):
            time.sleep(2)
        while time.monotonic() < deadline + 240:
            if _port_open(self.ip, CONTROL_PORT, 3) and self._try_login_close():
                return True
            time.sleep(3)
        return False

    def _try_login_close(self) -> bool:
        c = self._try_login()
        if c is None:
            return False
        c.close()
        return True

    def grow_disk(self, target: str, size_gb: int) -> tuple[bool, str]:
        st, d = self.broker.call("POST", f"/v1/lab-vms/{self.id}/grow-disk", {"target": target, "size_gb": size_gb})
        return st == 200, d.get("detail", "") if st != 200 else ""

    def teardown(self) -> None:
        if self._prestart is not None:
            self._prestart.join(timeout=self.boot_timeout_s)
        try:
            if self.id:
                self.broker.call("DELETE", f"/v1/lab-vms/{self.id}")
        finally:
            shutil.rmtree(self._dir, ignore_errors=True)


class FullVMLabs:
    """make_target / make_prober / pair_bridges for run_advice."""

    def __init__(self, broker_url: str, broker_token: str, prefetch_prober: bool = True):
        self.broker = _Broker(broker_url, broker_token)
        self.prefetch_prober = prefetch_prober
        self._targets: dict[str, FullVM] = {}
        self._probers: dict[str, FullVM] = {}

    def new_run(self) -> str:
        return "run-" + uuid.uuid4().hex[:10]

    def end_run(self, run_key: str) -> None:
        self._targets.pop(run_key, None)
        unused = self._probers.pop(run_key, None)
        if unused is not None:
            unused.teardown()  # prefetched but never handed out

    def make_target(self, pair_bridge: str = "", scratch_from: str = "") -> FullVM:
        run_key = pair_bridge or self.new_run()
        vm = FullVM(self.broker, "proof-target", run_key)
        self._targets[run_key] = vm
        if self.prefetch_prober and pair_bridge and run_key not in self._probers:
            # The prober boots alongside the target instead of after it.
            prober = FullVM(self.broker, "proof-prober", run_key, pair_with=vm)
            prober.start_early()
            self._probers[run_key] = prober
        return vm

    def make_prober(self, run_key: str) -> FullVM:
        prober = self._probers.pop(run_key, None)
        if prober is not None:
            return prober
        return FullVM(self.broker, "proof-prober", run_key, pair_with=self._targets.get(run_key))
