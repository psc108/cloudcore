"""
microvm.py -- one jailed Firecracker microVM, shared by every llm-chat
service that boots one (Stage 10 of llm-chat-sandbox-extensions-Phased-
Implementation.md). sandbox_terminal.py uses it for Terminal sessions;
verify_proxy.py uses it for per-run non-Python execution (Stage 12).

Two things changed from the Stage 5 launcher this replaces:

1. The VMM runs under jailer, not directly as root. jailer chroots it to
   CHROOT_BASE/firecracker/<id>/root, drops it to the unprivileged
   fcrunner uid/gid, and puts it in its own cgroup v2 group with a hard
   memory and CPU ceiling. The guest-to-host boundary (KVM + the
   bridge's iptables policy) is unchanged; this adds a host-side layer
   in case the VMM process itself is ever compromised.

2. The golden rootfs is attached read-only and shared by every session
   (hard-linked into each chroot, never copied), with a small sparse
   per-session scratch drive as the only writable disk. The guest's
   /sbin/overlay-init (baked into the rootfs by
   api/build-firecracker-rootfs.sh) overlays the two before handing
   off to systemd. This replaces a full 768MB copy per session, which
   is what makes a microVM per Run (Stage 12) affordable at all.

   root="snapshot" (llm-chat-lab-sandbox L13) keeps the same sharing but
   makes the root a real ext4 filesystem: the guest's /sbin/snapshot-init
   builds a device-mapper snapshot of the golden image with the scratch
   drive as its persistent copy-on-write store, then hands off to systemd. An overlay root can't be NFS-exported, hold Docker's
   storage or a swap file; a snapshot can. The prober and Terminal keep
   the overlay: a full snapshot store invalidates the whole root, while
   a full overlay only refuses writes.

The golden rootfs and kernel must be root-owned and not writable by
fcrunner: a jailed VMM reaches them through hard links, i.e. the same
inode, so if fcrunner owned them a compromised VMM could rewrite the
shared golden image for every later session.

The caller must run as root: it creates TAP devices and runs jailer.
"""
from __future__ import annotations

import fcntl
import ipaddress
import json
import os
import pwd
import shutil
import signal
import socket
import subprocess
import threading
import time
import uuid
from http.client import HTTPConnection

import paramiko

FC_BIN = "/opt/firecracker/firecracker"
JAILER_BIN = "/opt/firecracker/jailer"
KERNEL_PATH = "/opt/firecracker/vmlinux"
GOLDEN_ROOTFS = "/opt/firecracker/golden-rootfs.ext4"
JAIL_USER = "fcrunner"
CHROOT_BASE = "/srv/jailer"
# Private SSH keys live here, deliberately OUTSIDE any chroot, so the
# jailed VMM (running as fcrunner) can never read the key that logs into
# its own guest.
KEY_DIR = os.path.join(CHROOT_BASE, "sessions")
# jailer places each microVM's cgroup under this parent. It must not be
# a cgroup the calling service itself lives in (cgroup v2's
# no-internal-processes rule), so it hangs off the root.
CGROUP_PARENT = "fcsandbox"
CGROUP_ROOT = "/sys/fs/cgroup"

# Memory the VMM process needs on top of guest RAM (device emulation,
# its own heap). Sized with headroom: exceeding memory.max OOM-kills
# the whole VMM, which is the failure we want for a runaway, not for a
# normal guest.
VMM_OVERHEAD_MIB = 128

# Paths as the jailed VMM sees them, relative to its chroot.
_IN_JAIL_KERNEL = "/vmlinux"
_IN_JAIL_ROOTFS = "/rootfs.ext4"
_IN_JAIL_SCRATCH = "/scratch.ext4"


def _spare_path(i: int) -> str:
    return f"/spare{i}.img"
_IN_JAIL_API_SOCK = "/run/firecracker.socket"
_IN_JAIL_VSOCK = "/run/control.vsock"

# Control channel (llm-chat-lab-sandbox L2): SSH runs over virtio-vsock to
# a separate, PAM-free sshd in the guest (sshd-control, launched per
# connection by socat), never over the guest NIC. Guest firewall rules,
# routing, PAM and sshd_config -- all things lab advice changes -- cannot
# cut the session off. Port 22 on eth0 stays the student's own sshd.
GUEST_CID = 3
CONTROL_VSOCK_PORT = 1022


class BootError(Exception):
    pass


class CapacityError(BootError):
    """The coordinator can't give a new VM even its floor right now."""


# -- Sizing from real resources (llm-chat-lab-sandbox L3) ---------------------
#
# No fixed per-VM size tied to any machine: each VM asks for a target and
# gets whatever the coordinator can actually spare, down to a floor, below
# which it's refused with a clear reason. The ledger is the kernel's own:
# every live VM's cgroup under CGROUP_PARENT carries memory.max (what it was
# promised) and memory.current (what it uses), so the Terminal service and
# verify-proxy see each other's VMs with no shared state of their own.

# Kept free for the coordinator's own services (llama-server, verify-proxy,
# the page cache kiwix/NFS lean on) on top of what they use right now.
HOST_RESERVE_MIB = int(os.environ.get("VM_HOST_RESERVE_MIB", "768"))
DISK_RESERVE_MIB = int(os.environ.get("VM_DISK_RESERVE_MIB", "4096"))
_PLAN_LOCK_PATH = "/run/fcsandbox-plan.lock"


class VmSizing:
    """What a kind of VM would like and the least it can usefully run with."""

    def __init__(self, mem_target_mib: int, mem_floor_mib: int,
                 vcpu_target: int, scratch_target_mib: int, scratch_floor_mib: int):
        self.mem_target_mib = mem_target_mib
        self.mem_floor_mib = mem_floor_mib
        self.vcpu_target = vcpu_target
        self.scratch_target_mib = scratch_target_mib
        self.scratch_floor_mib = scratch_floor_mib


def _meminfo_mib() -> tuple[int, int]:
    vals = {}
    with open("/proc/meminfo") as f:
        for line in f:
            key, rest = line.split(":", 1)
            vals[key] = int(rest.split()[0]) // 1024
    return vals["MemTotal"], vals["MemAvailable"]


def _vm_memory_ledger_mib() -> tuple[int, int]:
    """(promised, in use) across every live VM cgroup, in MiB."""
    promised = in_use = 0
    base = os.path.join(CGROUP_ROOT, CGROUP_PARENT)
    try:
        entries = [e for e in os.scandir(base) if e.is_dir()]
    except OSError:
        return 0, 0
    for e in entries:
        try:
            with open(os.path.join(e.path, "memory.max")) as f:
                raw = f.read().strip()
            with open(os.path.join(e.path, "memory.current")) as f:
                cur = int(f.read().strip())
        except (OSError, ValueError):
            continue
        if raw != "max":
            promised += int(raw) // (1024 * 1024)
        in_use += cur // (1024 * 1024)
    return promised, in_use


def plan_vm_size(sizing: VmSizing) -> tuple[int, int, int]:
    """(vcpus, mem MiB, scratch MiB) for one new VM from what this host has
    free right now. Raises CapacityError below the floors. Call with the
    plan lock held (MicroVM.boot does) so two starts can't both spend the
    same memory."""
    total, available = _meminfo_mib()
    promised, in_use = _vm_memory_ledger_mib()
    # Everything that isn't a VM: the coordinator's services, page cache
    # pressure, kernel. VM memory is counted at its promise, not its use,
    # since guests grow into it.
    non_vm = max(0, (total - available) - in_use)
    spare = total - non_vm - HOST_RESERVE_MIB - promised - VMM_OVERHEAD_MIB
    mem = min(sizing.mem_target_mib, spare) // 128 * 128
    if mem < sizing.mem_floor_mib:
        raise CapacityError(
            f"not enough free memory for another sandbox right now ({max(spare, 0)}MB spare, "
            f"{sizing.mem_floor_mib}MB needed) -- try again when a session ends")
    # Never hand one VM every core the coordinator has.
    cpus = os.cpu_count() or 1
    vcpus = max(1, min(sizing.vcpu_target, cpus - 1 if cpus > 1 else 1))
    st = os.statvfs(CHROOT_BASE)
    disk_free = st.f_bavail * st.f_frsize // (1024 * 1024)
    scratch = min(sizing.scratch_target_mib, disk_free - DISK_RESERVE_MIB)
    if scratch < sizing.scratch_floor_mib:
        raise CapacityError(
            f"not enough free disk for another sandbox right now ({max(disk_free, 0)}MB free)")
    return vcpus, mem, scratch


# Two services start VMs on the sandbox bridge, each with its own in-process
# pool, so the subnet is split: the Terminal takes the lower part and the
# Linux Help advice runner (llm-chat-lab-sandbox L4) the top ADVICE_SLOTS.
ADVICE_SLOTS = 16


class IpPool:
    """Thread-safe allocator over one bridge subnet. The first 8 and last
    4 host addresses are held back (gateway, dnsmasq and future
    fixed-address uses), same as the Stage 5 pool this replaces.
    `part` picks the Terminal's or the advice runner's share of a subnet
    both use; "all" (per-run VMs, alone on their own bridge) takes it all."""

    def __init__(self, cidr: str, part: str = "all"):
        net = ipaddress.ip_network(cidr)
        hosts = [str(ip) for ip in net.hosts()]
        self.gateway = hosts[0]
        self.prefixlen = net.prefixlen
        self.netmask = str(net.netmask)
        usable = hosts[8:-4]
        if part == "terminal":
            usable = usable[:-ADVICE_SLOTS]
        elif part == "advice":
            usable = usable[-ADVICE_SLOTS:]
        elif part != "all":
            raise ValueError(f"unknown pool part {part!r}")
        self._pool = usable
        self._in_use: set[str] = set()
        self._lock = threading.Lock()

    def alloc(self) -> str | None:
        with self._lock:
            for ip in self._pool:
                if ip not in self._in_use:
                    self._in_use.add(ip)
                    return ip
        return None

    def release(self, ip: str | None) -> None:
        if ip is None:
            return
        with self._lock:
            self._in_use.discard(ip)


def _jail_ids() -> tuple[int, int]:
    pw = pwd.getpwnam(JAIL_USER)
    return pw.pw_uid, pw.pw_gid


def _link_or_copy(src: str, dst: str) -> None:
    """Hard link when src and dst share a filesystem (the normal case on
    a single-disk coordinator), otherwise a real copy with a warning --
    correct either way, just slower."""
    try:
        os.link(src, dst)
    except OSError as e:
        print(f"microvm: hard link {src} -> {dst} failed ({e}); copying instead", flush=True)
        shutil.copyfile(src, dst)
        os.chmod(dst, 0o644)


class MicroVM:
    """One jailed Firecracker microVM: a fresh TAP on `bridge`, the shared
    read-only golden rootfs plus a fresh scratch drive, a fresh ephemeral
    SSH keypair delivered via MMDS, and the jailed VMM process. Nothing is
    shared with or reused by any other session.

    `owner` is a short tag ("term", "run") prefixed onto the jail id and
    the TAP name, so each service's startup sweep (sweep_orphans) only
    ever touches its own leftovers, never another service's live VMs."""

    def __init__(self, owner: str, pool: IpPool, bridge: str,
                 vcpu_count: int = 1, mem_size_mib: int = 256,
                 scratch_mib: int = 1024, boot_timeout_s: int = 20,
                 extra_boot_args: str = "", sizing: VmSizing | None = None,
                 isolate: bool = True, pair_bridge: str = "", scratch_from: str = "",
                 root: str = "overlay", spare_disks_mib: tuple[int, ...] = ()):
        """With `sizing`, vcpu_count/mem_size_mib/scratch_mib are only
        placeholders: boot() replaces them with plan_vm_size()'s answer.

        L8 (the advice runner's prober): `pair_bridge` adds a second NIC,
        eth1, on a private per-run bridge shared only with a prober VM (no
        uplink, no host address); `isolate=False` is for a VM whose only
        NIC is on such a bridge. `scratch_from` boots from a preserved
        scratch disk instead of a fresh one: a reboot that keeps state.

        L13: `root="snapshot"` (see the module docstring); `spare_disks_mib`
        attaches blank sparse disks after the scratch drive (vdc, vdd, ...;
        the image also names the first two /dev/sdb and /dev/sdc) for disk
        advice -- partitioning, LVM, RAID -- to work on."""
        if root not in ("overlay", "snapshot"):
            raise ValueError(f"root must be 'overlay' or 'snapshot', not {root!r}")
        self.root = root
        self.spare_disks_mib = tuple(spare_disks_mib)
        self.owner = owner
        self.sizing = sizing
        self.isolate = isolate
        self.pair_bridge = pair_bridge
        self.scratch_from = scratch_from
        self.pool = pool
        self.bridge = bridge
        self.vcpu_count = vcpu_count
        self.mem_size_mib = mem_size_mib
        self.scratch_mib = scratch_mib
        self.boot_timeout_s = boot_timeout_s
        self.extra_boot_args = extra_boot_args

        short = uuid.uuid4().hex[:12]
        self.session_id = f"{owner}-{short}"
        # IFNAMSIZ is 16 including the NUL: "fc" + owner[:4] + "-" + 8 hex fits.
        self.tap_name = f"fc{owner[:4]}-{short[:8]}"
        self.pair_tap = f"fp{owner[:4]}-{short[:8]}" if pair_bridge else ""
        self._short = short
        self.ip: str | None = None

        self.jail_dir = os.path.join(CHROOT_BASE, "firecracker", self.session_id)
        self.chroot = os.path.join(self.jail_dir, "root")
        self.api_sock = self.chroot + _IN_JAIL_API_SOCK
        self.key_dir = os.path.join(KEY_DIR, self.session_id)
        self.private_key_path = os.path.join(self.key_dir, "id_ed25519")
        self.log_path = os.path.join(self.key_dir, "firecracker.log")
        self.cgroup_dir = os.path.join(CGROUP_ROOT, CGROUP_PARENT, self.session_id)
        self.public_key_text = ""
        self.proc: subprocess.Popen | None = None
        self._log_fh = None

    # -- Firecracker API --------------------------------------------------

    def _api(self, method: str, path: str, body: dict | None = None):
        conn = HTTPConnection("localhost", timeout=5)
        conn.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        conn.sock.connect(self.api_sock)
        data = json.dumps(body).encode() if body is not None else None
        conn.request(method, path, body=data, headers={"Content-Type": "application/json"})
        resp = conn.getresponse()
        raw = resp.read()
        conn.close()
        if resp.status >= 300:
            raise BootError(f"Firecracker API {method} {path} -> {resp.status}: {raw!r}")
        return raw

    def _log_tail(self, n: int = 5) -> str:
        try:
            with open(self.log_path, errors="replace") as f:
                lines = [ln.strip() for ln in f.readlines() if ln.strip()]
            return " | ".join(lines[-n:])
        except OSError:
            return ""

    # -- lifecycle ----------------------------------------------------------

    def _prepare_chroot(self, uid: int, gid: int) -> None:
        os.makedirs(self.chroot, mode=0o755, exist_ok=True)
        _link_or_copy(KERNEL_PATH, self.chroot + _IN_JAIL_KERNEL)
        _link_or_copy(GOLDEN_ROOTFS, self.chroot + _IN_JAIL_ROOTFS)

        # Sparse: only blocks the guest actually writes cost real disk.
        scratch = self.chroot + _IN_JAIL_SCRATCH
        if self.scratch_from:
            os.rename(self.scratch_from, scratch)
            self.scratch_mib = os.path.getsize(scratch) // (1024 * 1024)
        else:
            with open(scratch, "wb") as f:
                f.truncate(self.scratch_mib * 1024 * 1024)
            if self.root == "overlay":
                # A snapshot store needs no filesystem: all zeroes is an
                # empty persistent store.
                subprocess.run(["mkfs.ext4", "-q", "-F", scratch], check=True,
                               stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        os.chown(scratch, uid, gid)
        os.chmod(scratch, 0o600)
        for i, mib in enumerate(self.spare_disks_mib):
            spare = self.chroot + _spare_path(i)
            if self.scratch_from and os.path.exists(self.scratch_from + f".spare{i}"):
                os.rename(self.scratch_from + f".spare{i}", spare)
            else:
                with open(spare, "wb") as f:
                    f.truncate(mib * 1024 * 1024)
            os.chown(spare, uid, gid)
            os.chmod(spare, 0o600)

    def boot(self) -> None:
        uid, gid = _jail_ids()
        os.makedirs(self.key_dir, mode=0o700, exist_ok=True)

        self.ip = self.pool.alloc()
        if self.ip is None:
            raise BootError("No sandbox IP available (concurrent session limit reached)")

        # Held from sizing until this VM's cgroup (and so its memory.max)
        # exists, across processes: the Terminal service and verify-proxy
        # both start VMs on this host.
        with open(_PLAN_LOCK_PATH, "w") as plan_lock:
            fcntl.flock(plan_lock, fcntl.LOCK_EX)
            if self.sizing is not None:
                self.vcpu_count, self.mem_size_mib, self.scratch_mib = plan_vm_size(self.sizing)
            self._boot_locked(uid, gid)

        self._wait_for_ssh()

    def _boot_locked(self, uid: int, gid: int) -> None:
        self._prepare_chroot(uid, gid)

        subprocess.run(["ssh-keygen", "-t", "ed25519", "-N", "", "-q",
                        "-f", self.private_key_path], check=True)
        with open(self.private_key_path + ".pub") as f:
            self.public_key_text = f.read().strip()
        os.chmod(self.private_key_path, 0o600)

        # Owned by the jail user: the VMM opens /dev/net/tun and attaches
        # to this TAP after jailer has already dropped root, so a
        # root-owned TAP would fail with EPERM at /network-interfaces.
        subprocess.run(["ip", "tuntap", "add", self.tap_name, "mode", "tap",
                        "user", str(uid), "group", str(gid)], check=True)
        subprocess.run(["ip", "link", "set", self.tap_name, "master", self.bridge], check=True)
        # Port isolation: isolated ports can't exchange frames with each
        # other, only with the bridge itself (the gateway/NAT). One VM can
        # never reach another, whether or not br_netfilter is loaded to send
        # bridged traffic through the FORWARD rules.
        if self.isolate:
            subprocess.run(["bridge", "link", "set", "dev", self.tap_name, "isolated", "on"], check=True)
        subprocess.run(["ip", "link", "set", self.tap_name, "up"], check=True)
        if self.pair_tap:
            # Deliberately not isolated: the pair bridge exists so this VM and
            # its prober can reach each other, and nothing else is on it.
            subprocess.run(["ip", "tuntap", "add", self.pair_tap, "mode", "tap",
                            "user", str(uid), "group", str(gid)], check=True)
            subprocess.run(["ip", "link", "set", self.pair_tap, "master", self.pair_bridge], check=True)
            subprocess.run(["ip", "link", "set", self.pair_tap, "up"], check=True)

        mem_max = (self.mem_size_mib + VMM_OVERHEAD_MIB) * 1024 * 1024
        cpu_max = f"{self.vcpu_count * 100000} 100000"
        self._log_fh = open(self.log_path, "ab")
        self.proc = subprocess.Popen(
            [JAILER_BIN,
             "--id", self.session_id,
             "--exec-file", FC_BIN,
             "--uid", str(uid), "--gid", str(gid),
             "--chroot-base-dir", CHROOT_BASE,
             "--cgroup-version", "2",
             "--parent-cgroup", CGROUP_PARENT,
             "--cgroup", f"memory.max={mem_max}",
             "--cgroup", f"cpu.max={cpu_max}",
             "--resource-limit", "no-file=1024",
             "--", "--api-sock", _IN_JAIL_API_SOCK],
            stdin=subprocess.DEVNULL, stdout=self._log_fh, stderr=subprocess.STDOUT,
            start_new_session=True,
        )

        deadline = time.monotonic() + 5
        while not os.path.exists(self.api_sock):
            if self.proc.poll() is not None:
                raise BootError(f"jailer exited with {self.proc.returncode}: {self._log_tail()}")
            if time.monotonic() > deadline:
                raise BootError(f"Firecracker API socket never appeared: {self._log_tail()}")
            time.sleep(0.05)

        # Firecracker appends "root=/dev/vda ro" itself for a read-only
        # root drive; init= hands PID 1 to the overlay or snapshot step first.
        boot_args = (f"console=ttyS0 reboot=k panic=1 pci=off "
                     f"init=/sbin/{self.root}-init "
                     f"ip={self.ip}::{self.pool.gateway}:{self.pool.netmask}::eth0:off:{self.pool.gateway}"
                     + (f" {self.extra_boot_args}" if self.extra_boot_args else ""))
        self._api("PUT", "/boot-source", {
            "kernel_image_path": _IN_JAIL_KERNEL,
            "boot_args": boot_args,
        })
        self._api("PUT", "/drives/rootfs", {
            "drive_id": "rootfs",
            "path_on_host": _IN_JAIL_ROOTFS,
            "is_root_device": True,
            "is_read_only": True,
        })
        self._api("PUT", "/drives/scratch", {
            "drive_id": "scratch",
            "path_on_host": _IN_JAIL_SCRATCH,
            "is_root_device": False,
            "is_read_only": False,
        })
        for i in range(len(self.spare_disks_mib)):
            self._api("PUT", f"/drives/spare{i}", {
                "drive_id": f"spare{i}",
                "path_on_host": _spare_path(i),
                "is_root_device": False,
                "is_read_only": False,
            })
        # One MAC per VM, derived from its (pool-unique) IP. A fixed MAC
        # shared by every VM made concurrent sessions on one bridge steal
        # each other's frames: the bridge learns the MAC on whichever tap
        # spoke last (found live with a Terminal and an advice run at once).
        mac = "AA:FC:" + ":".join(f"{int(o):02X}" for o in self.ip.split("."))
        self._api("PUT", "/network-interfaces/eth0", {
            "iface_id": "eth0",
            "guest_mac": mac,
            "host_dev_name": self.tap_name,
        })
        if self.pair_tap:
            self._api("PUT", "/network-interfaces/eth1", {
                "iface_id": "eth1",
                "guest_mac": "AA:FD:" + ":".join(self._short[i:i + 2] for i in range(0, 8, 2)).upper(),
                "host_dev_name": self.pair_tap,
            })
        self._api("PUT", "/vsock", {
            "guest_cid": GUEST_CID,
            "uds_path": _IN_JAIL_VSOCK,
        })
        self._api("PUT", "/machine-config", {
            "vcpu_count": self.vcpu_count,
            "mem_size_mib": self.mem_size_mib,
        })
        # MMDS V1 carries only the PUBLIC half of this session's key; the
        # private half never leaves KEY_DIR.
        self._api("PUT", "/mmds/config", {
            "version": "V1",
            "network_interfaces": ["eth0"],
        })
        self._api("PUT", "/mmds", {
            "latest": {"meta-data": {"public-key": self.public_key_text}}
        })
        self._api("PUT", "/actions", {"action_type": "InstanceStart"})

    def _control_socket(self, timeout: float = 10.0) -> socket.socket:
        """A connected stream to the guest's control sshd: Firecracker's
        host-side vsock protocol is "CONNECT <port>\\n" on the UDS, answered
        by "OK <host port>\\n" once the guest is listening."""
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(timeout)
        try:
            s.connect(self.chroot + _IN_JAIL_VSOCK)
            s.sendall(f"CONNECT {CONTROL_VSOCK_PORT}\n".encode())
            reply = b""
            while not reply.endswith(b"\n") and len(reply) < 64:
                chunk = s.recv(1)
                if not chunk:
                    break
                reply += chunk
            if not reply.startswith(b"OK "):
                raise OSError(f"vsock CONNECT refused: {reply!r}")
        except OSError:
            s.close()
            raise
        s.settimeout(None)
        return s

    def _wait_for_ssh(self) -> None:
        deadline = time.monotonic() + self.boot_timeout_s
        while time.monotonic() < deadline:
            if self.proc.poll() is not None:
                raise BootError(f"Firecracker process exited during boot: {self._log_tail()}")
            try:
                s = self._control_socket(timeout=0.5)
                # The SSH banner proves sshd-control itself is up, not just socat.
                s.settimeout(2)
                banner = s.recv(4)
                s.close()
                if banner == b"SSH-":
                    return
            except OSError:
                pass
            time.sleep(0.2)
        raise BootError(f"Guest control channel not up within {self.boot_timeout_s}s")

    def alive(self) -> bool:
        """False once the VMM has exited for any reason. Also reaps it,
        so a crashed VMM doesn't linger as a zombie until teardown."""
        if getattr(self, "_attached", False):
            # Owned by another process: alive while its jail still exists.
            return os.path.exists(self.chroot + _IN_JAIL_API_SOCK)
        return self.proc is not None and self.proc.poll() is None

    def ssh_client(self, username: str = "student") -> paramiko.SSHClient:
        """SSH over the vsock control channel. `username` is "student" for
        the Terminal and the advice steps, "root" for checks that must not
        depend on the guest's sudo configuration."""
        # sshd-control can answer a moment before this session's key has
        # landed from MMDS (found live: the first root login right after
        # boot failed once, the next succeeded), so authentication is
        # retried briefly; anything else fails at once.
        deadline = time.monotonic() + 15
        while True:
            client = paramiko.SSHClient()
            client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
            try:
                client.connect(
                    hostname=self.ip, port=22, username=username,
                    key_filename=self.private_key_path,
                    timeout=10, banner_timeout=10,
                    sock=self._control_socket(),
                    look_for_keys=False, allow_agent=False,
                )
                return client
            except paramiko.AuthenticationException:
                client.close()
                if time.monotonic() > deadline:
                    raise
                time.sleep(0.5)

    @classmethod
    def attached(cls, session_id: str, ip: str = "") -> "MicroVM":
        """A handle on a VM another process booted and still owns (a kept
        lab machine): enough to open SSH over its control channel, nothing
        that could tear it down."""
        vm = cls.__new__(cls)
        vm.session_id = session_id
        vm.ip = ip
        vm.jail_dir = os.path.join(CHROOT_BASE, "firecracker", session_id)
        vm.chroot = os.path.join(vm.jail_dir, "root")
        vm.key_dir = os.path.join(KEY_DIR, session_id)
        vm.private_key_path = os.path.join(vm.key_dir, "id_ed25519")
        vm.proc = None
        vm._attached = True
        return vm

    def wait_exit(self, timeout_s: float) -> bool:
        """True once the VMM has exited (a guest reboot with reboot=k ends
        it), False if it's still running after `timeout_s`."""
        try:
            self.proc.wait(timeout=timeout_s)
            return True
        except subprocess.TimeoutExpired:
            return False

    def take_scratch(self, dest: str) -> None:
        """Move this (stopped) VM's scratch disk -- everything the guest
        wrote -- to `dest`, for a new VM to boot from (scratch_from).
        Spare disks travel with it as `dest`.spareN."""
        os.rename(self.chroot + _IN_JAIL_SCRATCH, dest)
        for i in range(len(self.spare_disks_mib)):
            os.rename(self.chroot + _spare_path(i), dest + f".spare{i}")

    def teardown(self) -> None:
        if self.proc is not None and self.proc.poll() is None:
            try:
                self.proc.send_signal(signal.SIGTERM)
                self.proc.wait(timeout=3)
            except (subprocess.TimeoutExpired, OSError):
                try:
                    self.proc.kill()
                    self.proc.wait(timeout=3)
                except (subprocess.TimeoutExpired, OSError):
                    pass
        if self._log_fh is not None:
            try:
                self._log_fh.close()
            except OSError:
                pass
        _remove_jail(self.session_id, self.tap_name)
        if self.pair_tap:
            subprocess.run(["ip", "link", "del", self.pair_tap],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
        shutil.rmtree(self.key_dir, ignore_errors=True)
        self.pool.release(self.ip)


def _remove_jail(session_id: str, tap_name: str | None) -> None:
    """Best-effort removal of everything one microVM leaves on the host:
    any process still in its cgroup, the cgroup itself, the chroot tree,
    and its TAP. Safe to call on a half-built or already-removed jail."""
    cg = os.path.join(CGROUP_ROOT, CGROUP_PARENT, session_id)
    if os.path.isdir(cg):
        try:
            with open(os.path.join(cg, "cgroup.procs")) as f:
                for line in f:
                    try:
                        os.kill(int(line), signal.SIGKILL)
                    except (ValueError, ProcessLookupError):
                        pass
        except OSError:
            pass
        # rmdir only succeeds once the killed processes have actually
        # left the cgroup, which is asynchronous.
        for _ in range(20):
            try:
                os.rmdir(cg)
                break
            except FileNotFoundError:
                break
            except OSError:
                time.sleep(0.1)
    shutil.rmtree(os.path.join(CHROOT_BASE, "firecracker", session_id), ignore_errors=True)
    if tap_name:
        subprocess.run(["ip", "link", "del", tap_name],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def sweep_orphans(owner: str) -> int:
    """Removes every jail, key dir and TAP left behind by `owner`'s
    previous process. jailer moves each VMM out of the calling service's
    own cgroup, so systemd stopping or restarting the service does NOT
    kill its microVMs. Called once at service start, before any new
    session exists, so everything tagged with `owner` is by definition
    orphaned. Returns how many jails were removed."""
    prefix = f"{owner}-"
    removed = 0
    jails = os.path.join(CHROOT_BASE, "firecracker")
    cg_parent = os.path.join(CGROUP_ROOT, CGROUP_PARENT)
    ids: set[str] = set()
    for base in (jails, cg_parent, KEY_DIR):
        try:
            ids.update(n for n in os.listdir(base) if n.startswith(prefix))
        except FileNotFoundError:
            pass
    for sid in ids:
        _remove_jail(sid, None)
        shutil.rmtree(os.path.join(KEY_DIR, sid), ignore_errors=True)
        removed += 1

    # Its TAPs and pair TAPs; pair bridges only when sweeping the advice
    # runner's own VMs -- another service's restart must never delete a
    # bridge a live advice run is using (found live).
    prefixes = (f"fc{owner[:4]}-", f"fp{owner[:4]}-") + ((PAIR_BRIDGE_PREFIX,) if owner == "advc" else ())
    try:
        for name in os.listdir("/sys/class/net"):
            if name.startswith(prefixes):
                subprocess.run(["ip", "link", "del", name],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
    except FileNotFoundError:
        pass
    return removed


# L8: each advice run's target and prober share a private bridge of their
# own -- no uplink, no host address, nothing else attached -- so the prober
# can test the target from outside without either reaching anything else.
PAIR_BRIDGE_PREFIX = "fcpair"
PAIR_SUBNET = "172.30.0.0/24"


def create_pair_bridge() -> str:
    name = f"{PAIR_BRIDGE_PREFIX}{uuid.uuid4().hex[:8]}"
    subprocess.run(["ip", "link", "add", name, "type", "bridge"], check=True)
    subprocess.run(["sysctl", "-qw", f"net.ipv6.conf.{name}.disable_ipv6=1"], check=False)
    subprocess.run(["ip", "link", "set", name, "up"], check=True)
    return name


def delete_pair_bridge(name: str) -> None:
    subprocess.run(["ip", "link", "del", name],
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
