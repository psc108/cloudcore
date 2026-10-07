from __future__ import annotations

import json
import logging
import os
import re
import shutil
import socket
import subprocess
import textwrap
from pathlib import Path
from typing import Optional

import threading
import time

import libvirt

import usb
import settings_store
from models import Instance, InstanceStatus

_port_lock = threading.Lock()

IMAGES_DIR = Path(__file__).parent / "images"
INSTANCES_DIR = Path(__file__).parent / "instances"
KEYS_DIR = Path(__file__).parent / "keys"
QEMU_URI = "qemu:///session"

_CC_PRIVKEY = KEYS_DIR / "cloudcore_ed25519"
_CC_PUBKEY  = KEYS_DIR / "cloudcore_ed25519.pub"

# Default SSH user per distro
_DISTRO_USER = {
    "ubuntu": "ubuntu",
    "debian": "debian",
    "rocky":  "rocky",
    "centos": "centos",
    "fedora": "fedora",
}


def get_cc_pubkey() -> str:
    return _CC_PUBKEY.read_text().strip() if _CC_PUBKEY.exists() else ""


def get_cc_privkey_path() -> str:
    return str(_CC_PRIVKEY)


def ssh_user_for_image(image_id: str) -> str:
    for distro, user in _DISTRO_USER.items():
        if distro in image_id:
            return user
    return "ubuntu"

# Port ranges for SLIRP host-to-guest forwarding
_SSH_PORT_START  = 12200
_SSH_PORT_END    = 12299
_HTTP_PORT_START = 12800
_HTTP_PORT_END   = 12899

# Per-VPC IP counter for unique simulated private IPs in SLIRP mode.
# Maps vpc_id → next host-bits offset (starts at 2; .0 = network, .1 = gateway).
_vpc_ip_counter: dict[str, int] = {}
_vpc_ip_lock = threading.Lock()


def init_vpc_ip_counters(vpc_cidr_map: dict[str, str], instances: list) -> None:
    """Seed per-VPC counters from existing instance records so restarts don't reuse IPs.

    vpc_cidr_map: {vpc_id: cidr_block}
    instances:    non-deleted Instance objects with private_ip set
    """
    import ipaddress
    with _vpc_ip_lock:
        for inst in instances:
            cidr = vpc_cidr_map.get(inst.vpc_id)
            if not cidr or not inst.private_ip or inst.private_ip == "10.0.2.15":
                continue
            try:
                net = ipaddress.ip_network(cidr, strict=False)
                offset = int(ipaddress.ip_address(inst.private_ip)) - int(net.network_address)
                if offset > 1:  # skip network/gateway offsets
                    _vpc_ip_counter[inst.vpc_id] = max(
                        _vpc_ip_counter.get(inst.vpc_id, 2), offset + 1
                    )
            except Exception:
                pass


def _allocate_slirp_ip(vpc_id: str, vpc_cidr: str) -> str:
    """Return a unique simulated private IP from the VPC's CIDR for a SLIRP instance."""
    import ipaddress
    with _vpc_ip_lock:
        offset = _vpc_ip_counter.get(vpc_id, 2)
        _vpc_ip_counter[vpc_id] = offset + 1
    net = ipaddress.ip_network(vpc_cidr, strict=False)
    # Allocate from the first /24 in the VPC (e.g. 10.10.0.0/16 → 10.10.0.x)
    host_ip = net.network_address + offset
    return str(host_ip)

# Bridge name and subnet for persistent networking (created by
# setup-network.sh, which is the single source of truth for both — keep
# in sync with that script's own BRIDGE/SUBNET values if either changes).
# Every bridged instance gets its real address from this subnet's DHCP
# pool regardless of any VPC/subnet object's own declared CIDR — callers
# needing "the CIDR real bridged instances are actually reachable on"
# (e.g. nfs.py's "vpc" share-client shorthand, F-041) should use this,
# not a VPC's cidr_block.
log = logging.getLogger(__name__)

BRIDGE_NAME = "ccbr0"
# llm-chat lab VMs (F2, api/setup-lab-network.sh): an isolated bridge with its
# own DHCP, selected by the instance tag network=lab. Never ccbr0 instead.
LAB_BRIDGE_NAME = "cclab0"
LEASE_FILES = (Path("/var/lib/misc/cloudcore-dnsmasq.leases"), Path("/var/lib/misc/cloudcore-lab-dnsmasq.leases"))


def bridge_cidr() -> str:
    """The real bridge subnet, e.g. "192.168.100.0/24" by default.

    Per-host configurable (network.bridge_subnet_octet setting) so two
    hosts paired for cross-host peering don't collide on the same
    subnet — see setup-network.sh, which must be run with the same
    octet for the two to actually agree. Read live (not cached) since
    it can change via a settings PUT without an API restart.
    """
    octet = settings_store.get("network.bridge_subnet_octet", 100)
    return f"192.168.{octet}.0/24"

# Image catalogue — entries exist regardless of whether the file is downloaded.
# 'available' is computed at runtime from disk presence.
IMAGE_CATALOGUE: list[dict] = [
    {
        "id": "ubuntu-22.04",
        "name": "Ubuntu 22.04 LTS",
        "distro": "ubuntu",
        "version": "22.04",
        "arch": "x86_64",
        "min_disk_gb": 10,
        "fetch_url": "https://cloud-images.ubuntu.com/jammy/current/jammy-server-cloudimg-amd64.img",
    },
    {
        "id": "ubuntu-24.04",
        "name": "Ubuntu 24.04 LTS",
        "distro": "ubuntu",
        "version": "24.04",
        "arch": "x86_64",
        "min_disk_gb": 10,
        "fetch_url": "https://cloud-images.ubuntu.com/noble/current/noble-server-cloudimg-amd64.img",
    },
    {
        "id": "debian-12",
        "name": "Debian 12 Bookworm",
        "distro": "debian",
        "version": "12",
        "arch": "x86_64",
        "min_disk_gb": 10,
        "fetch_url": "https://cloud.debian.org/images/cloud/bookworm/latest/debian-12-genericcloud-amd64.qcow2",
    },
    {
        "id": "rocky-9",
        "name": "Rocky Linux 9",
        "distro": "rocky",
        "version": "9",
        "arch": "x86_64",
        "min_disk_gb": 10,
        "fetch_url": "https://dl.rockylinux.org/pub/rocky/9/images/x86_64/Rocky-9-GenericCloud.latest.x86_64.qcow2",
    },
]


def _scrub_known_hosts(port: int) -> None:
    """Remove any stale known_hosts entry for 127.0.0.1:<port>."""
    subprocess.run(
        ["ssh-keygen", "-f", str(Path.home() / ".ssh" / "known_hosts"),
         "-R", f"[127.0.0.1]:{port}"],
        capture_output=True,
    )


def _free_port(start: int, end: int) -> int:
    """Find a free TCP port in [start, end]."""
    for port in range(start, end + 1):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            if s.connect_ex(("127.0.0.1", port)) != 0:
                return port
    raise RuntimeError(f"No free port in range {start}-{end}")


# Flavors: (vcpus, memory_mb, disk_gb)
# standard.xlarge added per direct request ("allow the use of a large
# model with a larger vm") — sized for examples/llm-chat's/
# distributed-llm's own Q8_0 pin (~7.17GB weights, split across a
# coordinator+worker) rather than an arbitrary round number: 8192MB
# leaves real headroom over "half the model + KV cache + RPC buffers"
# per node, confirmed against this project's real paired hosts'
# available RAM (~16GB / ~21GB via host_stats.py) at the time this was
# sized — see api/capacity_gate.py, which checks a peer's actual
# available RAM against a flavor's requirement before a build using it
# is even submitted, rather than relying on this number being right
# forever as hosts' other workloads change.
FLAVORS = {
    "standard.nano":   (1, 512,  5),
    "standard.small":  (1, 1024, 10),
    "standard.medium": (2, 2048, 20),
    "standard.large":  (4, 4096, 40),
    "standard.xlarge":  (6, 8192, 60),
    # standard.2xlarge -- sized for a dedicated testing host with real
    # 8 cores / 32GB free (found the coordinator's own default,
    # standard.large's 4GB, genuinely too tight once running a 14B
    # model alongside a Firecracker terminal session at once). 6 vCPU,
    # not 8 -- per direct correction, a flavor must never claim every
    # physical core a host has; 2 stay free for that host's own OS/
    # hypervisor overhead (scheduling, I/O, libvirt/qemu itself), same
    # don't-oversubscribe principle standard.large's own default was
    # already sized against, just as a margin rather than an exact
    # match. See api/capacity_gate.py's own HOST_RESERVED_CORES for the
    # same principle applied generally to live capacity checking, not
    # just this one flavor's static definition.
    "standard.2xlarge": (6, 16384, 100),
    # Few cores, much memory: a small LLM on a small host (llm-chat's lab
    # reader, 7B Q4 + 8k context in ~6GB). 2 vCPU, so a 4-core host keeps
    # 2 for itself (same rule as standard.2xlarge above).
    "memory.medium": (2, 8192, 40),
    # The same for a 14B (Q4 ~8.4GB + context): llm-chat's lab model on a
    # 4-core host, measured at ~80% of an 8-thread host's speed (C3).
    "memory.large": (2, 16384, 60),
}


def _conn() -> libvirt.virConnect:
    conn = libvirt.open(QEMU_URI)
    if conn is None:
        raise RuntimeError("Failed to connect to libvirt")
    return conn


def list_images() -> list[dict]:
    """Return catalogue entries annotated with whether the image file is present,
    then the custom images imported here (B2)."""
    available_stems = {p.stem for p in IMAGES_DIR.glob("*.qcow2")}
    return [
        {**img, "available": img["id"] in available_stems}
        for img in IMAGE_CATALOGUE
    ] + [{**m, "available": m["id"] in available_stems} for m in _custom_images()]


# Custom images (lfs-os-Phased-Implementation.md, B2): a disk built here,
# imported as a standalone image -- IMAGES_DIR/<id>.qcow2 plus <id>.json, which
# says how to boot it (firmware, disk bus) since it isn't a stock cloud image.
CUSTOM_IMAGE_ID_RE = re.compile(r"^[a-z0-9][a-z0-9.-]{1,40}$")
_OVMF_CODE = ("/usr/share/OVMF/OVMF_CODE_4M.fd", "/usr/share/OVMF/OVMF_CODE.fd")
_OVMF_VARS = ("/usr/share/OVMF/OVMF_VARS_4M.fd", "/usr/share/OVMF/OVMF_VARS.fd")


def _custom_images() -> list[dict]:
    out = []
    for meta in sorted(IMAGES_DIR.glob("*.json")):
        try:
            m = json.loads(meta.read_text())
        except (OSError, ValueError):
            continue
        if m.get("custom") and m.get("id") == meta.stem:
            out.append(m)
    return out


def image_meta(image_id: str) -> dict:
    """The catalogue entry or custom image's metadata ({} if neither)."""
    for img in IMAGE_CATALOGUE:
        if img["id"] == image_id:
            return img
    return next((m for m in _custom_images() if m["id"] == image_id), {})


def _ovmf() -> tuple[str, str]:
    code = next((c for c in _OVMF_CODE if Path(c).exists()), None)
    vars_ = next((v for v in _OVMF_VARS if Path(v).exists()), None)
    if not code or not vars_:
        raise RuntimeError("UEFI boot needs OVMF firmware on this host (apt install ovmf)")
    return code, vars_


def import_image(image_id: str, name: str, source: Instance, disk: str = "disk", snapshot: str = "",
                 firmware: str = "uefi", disk_bus: str = "virtio", description: str = "") -> dict:
    """Flatten one disk of a stopped instance -- as it is, or as one of its
    snapshots -- into a standalone image. The instance must be stopped: a
    running VM's disk is neither consistent nor readable (qemu's lock).
    The image is copied as it is, identity and all (F-232): a system meant to
    be imaged should leave /etc/machine-id empty, or its instances share one
    DHCP identity and fight over one address."""
    if not CUSTOM_IMAGE_ID_RE.match(image_id):
        raise ValueError("id: 2-41 of lowercase letters, digits, '.', '-', starting with a letter or digit")
    if image_meta(image_id) or (IMAGES_DIR / f"{image_id}.qcow2").exists():
        raise ValueError(f"an image called {image_id!r} already exists")
    if firmware not in ("uefi", "bios") or disk_bus not in ("virtio", "scsi"):
        raise ValueError("firmware is 'uefi' or 'bios'; disk_bus is 'virtio' or 'scsi'")
    if disk != "disk" and not re.fullmatch(r"data[0-2]", disk):
        raise ValueError("disk is 'disk' (the system disk) or 'data0'..'data2'")
    src = INSTANCES_DIR / source.id / f"{disk}.qcow2"
    if not src.exists():
        raise ValueError(f"instance {source.id} has no {disk} here")
    if snapshot and not any(s["name"] == snapshot for s in list_snapshots(source.id)):
        raise ValueError(f"instance {source.id} has no snapshot {snapshot!r}")
    conn = _conn()
    try:
        if conn.lookupByName(source.domain_name).isActive():
            raise RuntimeError("stop the instance first: a running VM's disk can't be read consistently")
    finally:
        conn.close()
    if firmware == "uefi":
        _ovmf()
    dest = IMAGES_DIR / f"{image_id}.qcow2"
    part = dest.with_name(dest.name + ".part")
    t0 = time.monotonic()
    cmd = ["qemu-img", "convert", "-O", "qcow2"] + (["-l", f"snapshot.name={snapshot}"] if snapshot else []) \
        + [str(src), str(part)]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        part.unlink(missing_ok=True)
        raise RuntimeError(f"qemu-img convert: {r.stderr.strip()[-300:]}")
    part.replace(dest)
    meta = {"id": image_id, "name": name or image_id, "distro": "custom", "version": "", "arch": "x86_64",
            "min_disk_gb": 1, "custom": True, "firmware": firmware, "disk_bus": disk_bus,
            "description": description[:500], "created_at": _now(),
            "source": {"instance_id": source.id, "instance_name": source.name, "disk": disk, "snapshot": snapshot},
            "size_bytes": dest.stat().st_size, "seconds": round(time.monotonic() - t0, 1)}
    tmp = IMAGES_DIR / f"{image_id}.json.tmp"
    tmp.write_text(json.dumps(meta, indent=1))
    tmp.replace(IMAGES_DIR / f"{image_id}.json")
    return meta


def delete_image(image_id: str) -> None:
    """A custom image only; the caller checks no instance is built on it."""
    m = next((x for x in _custom_images() if x["id"] == image_id), None)
    if not m:
        raise KeyError(image_id)
    (IMAGES_DIR / f"{image_id}.qcow2").unlink(missing_ok=True)
    (IMAGES_DIR / f"{image_id}.json").unlink(missing_ok=True)


def _base_image_path(image_id: str) -> Path:
    p = IMAGES_DIR / f"{image_id}.qcow2"
    if not p.exists():
        raise ValueError(f"Image '{image_id}' not found. Available: {list_images()}")
    return p


def _instance_dir(instance_id: str) -> Path:
    d = INSTANCES_DIR / instance_id
    d.mkdir(parents=True, exist_ok=True)
    return d


def _build_users_block(users: list, cc_pubkey: str) -> str:
    """Build the cloud-config users: block for additional users."""
    if not users:
        return ""
    # "- default" is required here: cloud-init's users module normally
    # creates the image's own default user (ubuntu) implicitly, but a
    # cloud-config that defines its own users: list REPLACES that implicit
    # behavior entirely unless the literal string "default" is included as
    # one of the list's own entries — confirmed directly, the hard way:
    # without this, adding any extra_users silently broke SSH access to
    # the ubuntu account on every affected instance (sshd rejected the
    # CloudCore keypair outright, "Permission denied (publickey)", despite
    # the same top-level ssh_authorized_keys: block that works fine when
    # extra_users is empty).
    lines = ["users:", "  - default"]
    for u in users:
        uname = u["username"]
        lines.append(f"  - name: {uname}")
        lines.append(f"    shell: /bin/bash")
        lines.append(f"    lock_passwd: {'false' if u.get('password_hash') else 'true'}")
        if u.get("password_hash"):
            lines.append(f"    passwd: {u['password_hash']}")
        if u.get("sudo", False):
            lines.append(f"    sudo: ALL=(ALL) NOPASSWD:ALL")
        keys = list(u.get("ssh_keys", []))
        if cc_pubkey:
            keys.append(cc_pubkey)
        if keys:
            lines.append("    ssh_authorized_keys:")
            for k in keys:
                lines.append(f"      - {k}")
    return "\n".join(lines)


def _build_write_files_block(ssh_user: str, cc_pubkey: str, cc_privkey: str, extra_users: list) -> str:
    """Build write_files + runcmd blocks for the CloudCore keypair on all users."""
    if not cc_privkey:
        return ""
    priv_indented = "\n".join("          " + l for l in cc_privkey.splitlines())
    all_users = [ssh_user] + [u["username"] for u in extra_users]

    write_entries = []
    runcmds = []
    for usr in all_users:
        # No `owner:` here deliberately: the write_files module runs before
        # users-groups creates {usr}, so an owner referencing that user
        # fails with "Unknown user or group". Files land root-owned and the
        # runcmd below (which runs after users-groups) chowns them -- the home
        # directory too (F-221): write_files makes /home/{usr} as root first,
        # and useradd leaves an existing home as it finds it.
        write_entries.append(f"""  - path: /home/{usr}/.ssh/cloudcore_ed25519
    permissions: '0600'
    content: |
{priv_indented}
  - path: /home/{usr}/.ssh/cloudcore_ed25519.pub
    permissions: '0644'
    content: |
          {cc_pubkey}""")
        runcmds.append(f"""  - |
    mkdir -p /home/{usr}/.ssh
    # F-227: useradd skipped /etc/skel because write_files had made the home first.
    cp -rn /etc/skel/. /home/{usr}/ && chown -R {usr}:{usr} /home/{usr}
    grep -qF 'cloudcore_ed25519' /home/{usr}/.ssh/config 2>/dev/null || printf '\\nHost *\\n  IdentityFile ~/.ssh/cloudcore_ed25519\\n  StrictHostKeyChecking no\\n' >> /home/{usr}/.ssh/config
    chown {usr}:{usr} /home/{usr} /home/{usr}/.ssh /home/{usr}/.ssh/config /home/{usr}/.ssh/cloudcore_ed25519 /home/{usr}/.ssh/cloudcore_ed25519.pub
    chmod 700 /home/{usr}/.ssh
    chmod 600 /home/{usr}/.ssh/config /home/{usr}/.ssh/cloudcore_ed25519""")

    return "write_files:\n" + "\n".join(write_entries) + "\nruncmd:\n" + "\n".join(runcmds)


_MERGED_LIST_KEYS = ("packages", "write_files", "runcmd", "bootcmd", "ssh_authorized_keys")


def _merge_user_data(base_cloud_config: str, extra_user_data: Optional[str]) -> str:
    """Merge CloudCore's own cloud-config (SSH key injection) with a caller-
    supplied user_data document into a single #cloud-config.

    cloud-init's own multi-part merge semantics are not something to lean
    on here — we merge explicitly so the result is deterministic and
    testable. List-valued keys that matter (packages, write_files, runcmd,
    bootcmd, ssh_authorized_keys) are concatenated, CloudCore's entries
    first. A caller document that isn't #cloud-config (e.g. a raw shell
    script) is dropped into write_files and invoked via runcmd instead of
    being silently ignored.
    """
    import yaml

    base = yaml.safe_load(base_cloud_config.split("#cloud-config", 1)[-1]) or {}
    extra_user_data = (extra_user_data or "").strip()

    if not extra_user_data:
        merged = base
    elif extra_user_data.startswith("#cloud-config"):
        extra = yaml.safe_load(extra_user_data.split("#cloud-config", 1)[-1]) or {}
        merged = dict(base)
        for key in _MERGED_LIST_KEYS:
            if key in extra or key in base:
                merged[key] = list(base.get(key) or []) + list(extra.get(key) or [])
        for key, value in extra.items():
            if key not in _MERGED_LIST_KEYS:
                merged[key] = value
    else:
        merged = dict(base)
        merged["write_files"] = list(base.get("write_files") or []) + [{
            "path": "/var/lib/cloud/user-supplied-script.sh",
            "permissions": "0755",
            "content": extra_user_data,
        }]
        merged["runcmd"] = list(base.get("runcmd") or []) + ["/var/lib/cloud/user-supplied-script.sh"]

    return "#cloud-config\n" + yaml.safe_dump(merged, default_flow_style=False, sort_keys=False)


def _cloud_init_iso(instance_dir: Path, instance_name: str, image_id: str,
                    user_data: Optional[str], extra_users: Optional[list] = None,
                    untrusted: bool = False) -> Path:
    """Build a cloud-init NoCloud ISO, injecting the CloudCore inter-instance keypair
    and merging in any caller-supplied user_data (packages/write_files/runcmd/...).
    untrusted (lab VMs): the public key only, so the host can still reach the VM,
    never the private key -- model-written commands run there, and students
    have root on theirs (F-235)."""
    extra_users = extra_users or []
    meta_data = f"instance-id: {instance_name}\nlocal-hostname: {instance_name}\n"

    cc_pubkey  = get_cc_pubkey()
    cc_privkey = "" if untrusted else (_CC_PRIVKEY.read_text().strip() if _CC_PRIVKEY.exists() else "")
    ssh_user   = ssh_user_for_image(image_id)

    users_block = _build_users_block(extra_users, cc_pubkey)
    write_files_block = _build_write_files_block(ssh_user, cc_pubkey, cc_privkey, extra_users)

    base_cloud_config = f"""#cloud-config
ssh_authorized_keys:
  - {cc_pubkey}
{users_block}
{write_files_block}
"""

    merged_user_data = _merge_user_data(base_cloud_config, user_data)

    (instance_dir / "meta-data").write_text(meta_data)
    (instance_dir / "user-data").write_text(merged_user_data)
    iso_path = instance_dir / "cloud-init.iso"

    # Try genisoimage first, then xorriso
    for cmd in (
        ["genisoimage", "-output", str(iso_path), "-volid", "cidata",
         "-joliet", "-rock", str(instance_dir / "user-data"), str(instance_dir / "meta-data")],
        ["xorriso", "-as", "mkisofs", "-output", str(iso_path), "-volid", "cidata",
         "-joliet", "-rock", str(instance_dir / "user-data"), str(instance_dir / "meta-data")],
    ):
        if subprocess.run(["which", cmd[0]], capture_output=True).returncode == 0:
            subprocess.run(cmd, check=True, capture_output=True)
            return iso_path

    raise RuntimeError(
        "No ISO builder found. Install genisoimage: sudo apt install genisoimage"
    )


def _bridge_usable(name: str = BRIDGE_NAME) -> bool:
    """Return True only if the bridge exists AND /etc/qemu/bridge.conf permits it."""
    r = subprocess.run(["ip", "link", "show", name], capture_output=True)
    if r.returncode != 0:
        return False
    conf = Path("/etc/qemu/bridge.conf")
    if not conf.exists():
        return False
    return any(
        line.strip() in (f"allow {name}", "allow all")
        for line in conf.read_text().splitlines()
        if not line.strip().startswith("#")
    )


def _console_log_path(instance_id: str) -> Path:
    return INSTANCES_DIR / instance_id / "console.log"


def get_console_output(instance_id: str, lines: int = 200) -> str:
    """Return the last `lines` lines from the instance serial console log."""
    log = _console_log_path(instance_id)
    if not log.exists():
        return ""
    text = log.read_text(errors="replace")
    all_lines = text.splitlines()
    return "\n".join(all_lines[-lines:]) if len(all_lines) > lines else text


def _display_xml(display: bool) -> str:
    """B3: a graphical console -- virtio-gpu (a DRM device a Wayland compositor
    can use) and a VNC display on the host's loopback only. The dashboard
    reaches it through the API's ticketed WebSocket bridge; nothing else can."""
    if not display:
        return ""
    return ("<video><model type='virtio' heads='1' primary='yes'/></video>"
            "<graphics type='vnc' port='-1' autoport='yes' listen='127.0.0.1'>"
            "<listen type='address' address='127.0.0.1'/></graphics>")


def wants_display(instance: Instance) -> bool:
    return (instance.tags or {}).get("display") == "vnc" or image_meta(instance.image_id).get("display") == "vnc"


def vnc_port(domain_name: str) -> int:
    """The running domain's VNC port on 127.0.0.1, or 0."""
    import xml.etree.ElementTree as ET
    conn = _conn()
    try:
        dom = conn.lookupByName(domain_name)
        if not dom.isActive():
            return 0
        g = ET.fromstring(dom.XMLDesc()).find(".//graphics[@type='vnc']")
        return int(g.get("port", "0")) if g is not None and g.get("port", "-1").isdigit() else 0
    except libvirt.libvirtError:
        return 0
    finally:
        conn.close()


def screenshot_png(domain_name: str) -> bytes:
    """The running domain's screen as PNG (libvirt gives PPM; converted with
    the standard library, no imaging packages on the host)."""
    import struct
    import zlib
    conn = _conn()
    try:
        dom = conn.lookupByName(domain_name)
        if not dom.isActive():
            raise RuntimeError("the instance isn't running")
        stream = conn.newStream(0)
        mime = dom.screenshot(stream, 0, 0)
        chunks = []
        while True:
            data = stream.recv(1 << 20)
            if not data:
                break
            chunks.append(data)
        stream.finish()
    except libvirt.libvirtError as e:
        raise RuntimeError(f"libvirt: {e}") from e
    finally:
        conn.close()
    raw = b"".join(chunks)
    if mime == "image/png":
        return raw
    # PPM (P6): header "P6\n<w> <h>\n<max>\n" then RGB bytes.
    m = re.match(rb"P6\s+(?:#[^\n]*\n\s*)*(\d+)\s+(\d+)\s+(\d+)\s", raw)
    if not m:
        raise RuntimeError(f"unexpected screenshot format {mime!r}")
    w, h = int(m.group(1)), int(m.group(2))
    pix = raw[m.end():m.end() + w * h * 3]
    rows = b"".join(b"\x00" + pix[y * w * 3:(y + 1) * w * 3] for y in range(h))

    def chunk(kind: bytes, body: bytes) -> bytes:
        return struct.pack(">I", len(body)) + kind + body + struct.pack(">I", zlib.crc32(kind + body) & 0xFFFFFFFF)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(rows, 6)) + chunk(b"IEND", b""))


def _os_xml(firmware: dict | None) -> str:
    """BIOS by default; UEFI (B2) with OVMF and this instance's own variable store."""
    if not firmware:
        return "<os><type arch='x86_64' machine='pc'>hvm</type><boot dev='hd'/></os>"
    return ("<os><type arch='x86_64' machine='pc'>hvm</type>"
            f"<loader readonly='yes' type='pflash'>{firmware['code']}</loader>"
            f"<nvram>{firmware['vars']}</nvram><boot dev='hd'/></os>")


def _domain_xml_slirp(
    domain_name: str,
    vcpus: int,
    memory_mb: int,
    disk_path: Path,
    iso_path: Path,
    ssh_host_port: int,
    http_host_port: int,
    instance_id: str = "",
    usb_hostdev_xml: str = "",
    firmware: dict | None = None,
    display: bool = False,
) -> str:
    # seclabel type='none': disk/ISO images live under this repo's own
    # api/images|instances/ tree, not the standard /var/lib/libvirt/images/
    # libvirt's default AppArmor abstraction expects. A fresh Ubuntu 24.04
    # install hit one real AppArmor DENIED entry (dmesg) for qemu reading
    # the disk file during this same debugging session — not conclusively
    # pinned as the cause of every "Domain not found" error seen along the
    # way (the actual, confirmed cause of most of them turned out to be
    # the machine type below), but a real denial was observed at least
    # once, and disabling per-domain confinement for CloudCore's own
    # guests is a pure relaxation that can't newly break anything. Not a
    # host-wide AppArmor change — scoped to these guests only, appropriate
    # for a single-user lab/dev platform, not a multi-tenant one.
    #
    # machine='pc' (a version-less alias qemu resolves to whichever i440fx
    # machine type it actually ships) instead of a hardcoded
    # 'pc-i440fx-2.9': confirmed directly as the real root cause of the
    # "Domain not found" errors above — libvirtd rejected domain creation
    # outright with "unsupported configuration: Emulator ... does not
    # support machine type 'pc-i440fx-2.9'" on a fresh Ubuntu 24.04/qemu
    # 8.2.2 install, even though the identical qemu-system-x86 package
    # version still supports it on another host. Not worth chasing why
    # that specific old version string is unsupported on one build and not
    # another — 'pc' sidesteps the whole class of problem.
    memory_kib = memory_mb * 1024
    log_file = str(_console_log_path(instance_id)) if instance_id else ""
    log_elem = f"\n              <log file='{log_file}' append='on'/>" if log_file else ""
    os_xml = _os_xml(firmware)
    return textwrap.dedent(f"""\
        <domain type='kvm' xmlns:qemu='http://libvirt.org/schemas/domain/qemu/1.0'>
          <name>{domain_name}</name>
          <memory unit='KiB'>{memory_kib}</memory>
          <vcpu>{vcpus}</vcpu>
          {os_xml}
          <features><acpi/><apic/></features>
          <cpu mode='host-passthrough'/>
          <seclabel type='none'/>
          <devices>
            <disk type='file' device='disk'>
              <driver name='qemu' type='qcow2'/>
              <source file='{disk_path}'/>
              <target dev='vda' bus='virtio'/>
            </disk>
            <disk type='file' device='cdrom'>
              <driver name='qemu' type='raw'/>
              <source file='{iso_path}'/>
              <target dev='sda' bus='sata'/>
              <readonly/>
            </disk>
            <serial type='pty'>{log_elem}<target port='0'/></serial>
            <console type='pty'><target type='serial' port='0'/></console>
            {usb_hostdev_xml}{_display_xml(display)}
          </devices>
          <qemu:commandline>
            <qemu:arg value='-netdev'/>
            <qemu:arg value='user,id=ccnet0,hostfwd=tcp:127.0.0.1:{ssh_host_port}-:22,hostfwd=tcp:127.0.0.1:{http_host_port}-:80'/>
            <qemu:arg value='-device'/>
            <qemu:arg value='virtio-net-pci,netdev=ccnet0,bus=pci.0,addr=0x5'/>
          </qemu:commandline>
        </domain>
    """)


def _domain_xml_bridge(
    domain_name: str,
    vcpus: int,
    memory_mb: int,
    disk_path: Path,
    iso_path: Path,
    instance_id: str = "",
    usb_hostdev_xml: str = "",
    bridge: str = BRIDGE_NAME,
    scsi_disks: bool = False,
    data_disks: tuple = (),
    firmware: dict | None = None,
    display: bool = False,
) -> str:
    # seclabel type='none' — see _domain_xml_slirp's comment above.
    memory_kib = memory_mb * 1024
    log_file = str(_console_log_path(instance_id)) if instance_id else ""
    log_elem = f"\n              <log file='{log_file}' append='on'/>" if log_file else ""
    # F3 (lab VMs): disks on virtio-scsi, as on a typical server -- root
    # /dev/sda, blank data disks /dev/sdb, /dev/sdc ... -- so how-to answers'
    # device names are simply right. (The cloud-init CD-ROM is /dev/sr0 in
    # the guest either way; 'sdz' is only libvirt's name for it.)
    if scsi_disks:
        root_target, cdrom_target = "<target dev='sda' bus='scsi'/>", "sdz"
        extra = "<controller type='scsi' model='virtio-scsi'/>" + "".join(
            f"<disk type='file' device='disk'><driver name='qemu' type='qcow2'/><source file='{p}'/>"
            f"<target dev='sd{chr(ord('b') + i)}' bus='scsi'/></disk>" for i, p in enumerate(data_disks))
    else:
        root_target, cdrom_target, extra = "<target dev='vda' bus='virtio'/>", "sda", ""
    os_xml = _os_xml(firmware)
    return textwrap.dedent(f"""\
        <domain type='kvm'>
          <name>{domain_name}</name>
          <memory unit='KiB'>{memory_kib}</memory>
          <vcpu>{vcpus}</vcpu>
          {os_xml}
          <features><acpi/><apic/></features>
          <cpu mode='host-passthrough'/>
          <seclabel type='none'/>
          <devices>
            <disk type='file' device='disk'>
              <driver name='qemu' type='qcow2'/>
              <source file='{disk_path}'/>
              {root_target}
            </disk>
            <disk type='file' device='cdrom'>
              <driver name='qemu' type='raw'/>
              <source file='{iso_path}'/>
              <target dev='{cdrom_target}' bus='sata'/>
              <readonly/>
            </disk>
            {extra}
            <interface type='bridge'>
              <source bridge='{bridge}'/>
              <model type='virtio'/>
            </interface>
            <serial type='pty'>{log_elem}<target port='0'/></serial>
            <console type='pty'><target type='serial' port='0'/></console>
            {usb_hostdev_xml}{_display_xml(display)}
          </devices>
        </domain>
    """)



MAX_DATA_DISKS, MAX_DATA_DISK_GB = 3, 50


def _data_disk_sizes(instance: Instance) -> list[int]:
    """Blank data disks a lab VM asked for: the instance tag data_disks,
    e.g. "2,2" (GB each). Invalid values are refused, not guessed."""
    raw = str((instance.tags or {}).get("data_disks") or "").strip()
    if not raw:
        return []
    try:
        sizes = [int(s) for s in raw.split(",")]
    except ValueError:
        raise ValueError(f"data_disks must be comma-separated GB sizes, not {raw!r}") from None
    if len(sizes) > MAX_DATA_DISKS or any(s < 1 or s > MAX_DATA_DISK_GB for s in sizes):
        raise ValueError(f"data_disks: at most {MAX_DATA_DISKS} disks of 1-{MAX_DATA_DISK_GB} GB")
    return sizes


def _create_data_disks(instance: Instance, instance_dir: Path) -> tuple:
    paths = []
    for i, gb in enumerate(_data_disk_sizes(instance)):
        p = instance_dir / f"data{i}.qcow2"
        subprocess.run(["qemu-img", "create", "-f", "qcow2", str(p), f"{gb}G"], check=True, capture_output=True)
        paths.append(p)
    return tuple(paths)


def resize_disk(instance: Instance, target: str, size_gb: int) -> int:
    """Grow one of a running instance's disks live (root sda, or data sdb...),
    as a cloud provider's "enlarge volume" would; the guest sees the new size
    and its own tools (growpart, resize2fs, pvresize) do the rest. Refuses to
    shrink. Returns the new size in bytes."""
    if not re.fullmatch(r"(?:sd[a-d]|vda)", target or ""):
        raise ValueError("target must be sda-sdd (or vda)")
    if not (1 <= int(size_gb) <= 200):
        raise ValueError("size_gb must be 1-200")
    conn = _conn()
    try:
        dom = conn.lookupByName(instance.domain_name)
        current = dom.blockInfo(target)[0]
        new = int(size_gb) * 1024 ** 3
        if new <= current:
            raise ValueError(f"{target} is already {current // 1024 ** 3} GB; disks only grow")
        dom.blockResize(target, new, libvirt.VIR_DOMAIN_BLOCK_RESIZE_BYTES)
        return new
    finally:
        conn.close()


def create_instance(instance: Instance, vpc_cidr: str = "10.0.0.0/8") -> Instance:
    flavor = FLAVORS.get(instance.flavor)
    if flavor is None:
        raise ValueError(f"Unknown flavor '{instance.flavor}'. Available: {list(FLAVORS)}")

    vcpus, memory_mb, disk_gb = flavor
    base_image = _base_image_path(instance.image_id)
    instance_dir = _instance_dir(instance.id)
    disk_path = instance_dir / "disk.qcow2"
    domain_name = f"cc-{instance.id[:8]}"
    instance.domain_name = domain_name

    # Create a copy-on-write overlay from the base image
    subprocess.run(
        ["qemu-img", "create", "-f", "qcow2", "-b", str(base_image), "-F", "qcow2",
         str(disk_path), f"{disk_gb}G"],
        check=True, capture_output=True,
    )

    iso_path = _cloud_init_iso(instance_dir, instance.name, instance.image_id, instance.user_data, instance.users,
                               untrusted=(instance.tags or {}).get("network") == "lab")
    # F2: a lab VM goes on the isolated lab bridge or nowhere -- never on
    # ccbr0, and never SLIRP, where it would reach everything ccbr0 does.
    lab = (instance.tags or {}).get("network") == "lab"
    if lab and not _bridge_usable(LAB_BRIDGE_NAME):
        raise RuntimeError(f"this host has no isolated lab network ({LAB_BRIDGE_NAME}); "
                           "run api/setup-lab-network.sh")
    use_bridge = lab or _bridge_usable()
    data_disks = _create_data_disks(instance, instance_dir) if lab else ()
    # B2: how a custom image boots -- UEFI (OVMF, with this instance's own copy
    # of the variable store) and its disk bus; stock images are BIOS + virtio.
    meta = image_meta(instance.image_id)
    firmware = None
    if meta.get("firmware") == "uefi":
        code, vars_template = _ovmf()
        nvram = instance_dir / "OVMF_VARS.fd"
        if not nvram.exists():
            shutil.copyfile(vars_template, nvram)
        firmware = {"code": code, "vars": str(nvram)}
    scsi = lab or meta.get("disk_bus") == "scsi"

    # Re-validate USB devices here too, immediately before building XML —
    # never trust that a check done moments earlier (in the API handler)
    # still holds; the device could have been claimed by a concurrent
    # request in between.
    usb_hostdev_xml = ""
    if instance.usb_device_ids:
        with usb._usb_lock:
            for usb_id in instance.usb_device_ids:
                err = usb.validate_attachable(usb_id, instance.id)
                if err:
                    raise RuntimeError(f"USB passthrough failed: {err}")
            usb_hostdev_xml = "".join(usb.hostdev_xml(u) for u in instance.usb_device_ids)

    with _port_lock:
        if use_bridge:
            xml = _domain_xml_bridge(domain_name, vcpus, memory_mb, disk_path, iso_path,
                                     instance_id=instance.id, usb_hostdev_xml=usb_hostdev_xml,
                                     bridge=LAB_BRIDGE_NAME if lab else BRIDGE_NAME,
                                     scsi_disks=scsi, data_disks=data_disks, firmware=firmware,
                                     display=wants_display(instance))
            instance.ssh_host_port = 0
            instance.http_host_port = 0
            instance.private_ip = ""  # will be set from DHCP lease after boot
        else:
            ssh_host_port  = _free_port(_SSH_PORT_START,  _SSH_PORT_END)
            http_host_port = _free_port(_HTTP_PORT_START, _HTTP_PORT_END)
            _scrub_known_hosts(ssh_host_port)
            instance.ssh_host_port  = ssh_host_port
            instance.http_host_port = http_host_port
            # Allocate a unique simulated private IP from the VPC CIDR
            instance.private_ip = _allocate_slirp_ip(instance.vpc_id, vpc_cidr)
            xml = _domain_xml_slirp(domain_name, vcpus, memory_mb, disk_path, iso_path,
                                    ssh_host_port, http_host_port, instance_id=instance.id,
                                    usb_hostdev_xml=usb_hostdev_xml, firmware=firmware,
                                    display=wants_display(instance))

        conn = _conn()
        try:
            dom = conn.defineXML(xml)
            dom.create()
            instance.status = InstanceStatus.RUNNING
        except Exception as e:
            instance.status = InstanceStatus.ERROR
            raise RuntimeError(f"libvirt error: {e}") from e
        finally:
            conn.close()

    return instance


def sync_usb_devices(instance: Instance, added: list[str], removed: list[str]) -> None:
    """Hot-attach/detach USB devices on an existing domain.

    Uses AFFECT_CONFIG unconditionally (so the persistent domain
    definition always reflects the change, taking effect on next boot
    even if the domain is currently stopped) plus AFFECT_LIVE when the
    domain is actually running (so it takes effect immediately too, no
    restart needed). This is new ground for this codebase — nothing else
    in compute.py mutates a live domain's device list, only its power
    state — so it's deliberately narrow: one call per device, errors
    collected rather than raised immediately, so one failed device doesn't
    stop the rest from being applied.
    """
    if not added and not removed:
        return
    conn = _conn()
    errors = []
    try:
        dom = conn.lookupByName(instance.domain_name)
        flags = libvirt.VIR_DOMAIN_AFFECT_CONFIG
        if dom.isActive():
            flags |= libvirt.VIR_DOMAIN_AFFECT_LIVE
        for usb_id in removed:
            try:
                dom.detachDeviceFlags(usb.hostdev_xml(usb_id), flags)
            except libvirt.libvirtError as e:
                errors.append(f"detach {usb_id}: {e}")
        for usb_id in added:
            try:
                dom.attachDeviceFlags(usb.hostdev_xml(usb_id), flags)
            except libvirt.libvirtError as e:
                errors.append(f"attach {usb_id}: {e}")
    finally:
        conn.close()
    if errors:
        raise RuntimeError("USB device sync had errors: " + "; ".join(errors))


def start_domain(domain_name: str) -> None:
    """Start a defined-but-stopped libvirt domain (e.g. after daemon restart)."""
    conn = _conn()
    try:
        dom = conn.lookupByName(domain_name)
        if not dom.isActive():
            dom.create()
    except libvirt.libvirtError as e:
        raise RuntimeError(f"libvirt error: {e}") from e
    finally:
        conn.close()


def stop_domain(domain_name: str) -> None:
    """Force-stop (power-off) a running domain."""
    conn = _conn()
    try:
        dom = conn.lookupByName(domain_name)
        if dom.isActive():
            dom.destroy()
    except libvirt.libvirtError as e:
        raise RuntimeError(f"libvirt error: {e}") from e
    finally:
        conn.close()


# Keep old name for compatibility with any internal callers.
stop_instance = stop_domain


def reboot_domain(domain_name: str) -> None:
    """Send an ACPI reboot signal to a running domain."""
    conn = _conn()
    try:
        dom = conn.lookupByName(domain_name)
        if not dom.isActive():
            raise RuntimeError(f"Domain {domain_name!r} is not running")
        dom.reboot(0)
    except libvirt.libvirtError as e:
        raise RuntimeError(f"libvirt error: {e}") from e
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Disk snapshots (lfs-os-Phased-Implementation.md, B1): checkpoints for long
# builds. Every disk of the instance gets a qcow2 internal snapshot of the same
# name, taken with the domain shut off so the disks are consistent with each
# other and with themselves; qemu-img refuses an image a running qemu holds.
# The list lives in the instance's directory, snapshots.json.
# ---------------------------------------------------------------------------

SNAPSHOT_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
SHUTDOWN_TIMEOUT_S = 180


def _instance_disks(instance_id: str) -> list[Path]:
    d = INSTANCES_DIR / instance_id
    return [p for p in [d / "disk.qcow2", *sorted(d.glob("data*.qcow2"))] if p.exists()]


def _snapshot_index(instance_id: str) -> Path:
    return INSTANCES_DIR / instance_id / "snapshots.json"


def list_snapshots(instance_id: str) -> list[dict]:
    try:
        return json.loads(_snapshot_index(instance_id).read_text())
    except (OSError, ValueError):
        return []


def _save_snapshots(instance_id: str, snaps: list[dict]) -> None:
    path = _snapshot_index(instance_id)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(snaps, indent=1))
    tmp.replace(path)


def _shut_off(domain_name: str, force: bool) -> bool:
    """Shut the domain down cleanly (ACPI), waiting for it. Returns whether it
    was running. Raises if it doesn't stop in time and force is False."""
    conn = _conn()
    try:
        dom = conn.lookupByName(domain_name)
        if not dom.isActive():
            return False
        try:
            dom.shutdown()
        except libvirt.libvirtError as e:
            log.warning("ACPI shutdown of %s failed: %s", domain_name, e)
        deadline = time.monotonic() + SHUTDOWN_TIMEOUT_S
        while dom.isActive() and time.monotonic() < deadline:
            time.sleep(2)
        if dom.isActive():
            if not force:
                raise RuntimeError(f"{domain_name} didn't shut down within {SHUTDOWN_TIMEOUT_S}s; "
                                   "retry with force=true to power it off")
            dom.destroy()
        return True
    finally:
        conn.close()


def _qemu_img_snapshot(flag: str, disk: Path, name: str) -> None:
    r = subprocess.run(["qemu-img", "snapshot", flag, name, str(disk)], capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"qemu-img snapshot {flag} {name} {disk.name}: {r.stderr.strip()[-300:]}")


def create_snapshot(instance: Instance, name: str, description: str = "", force: bool = False) -> dict:
    """Shut down cleanly, snapshot every disk, start again if it was running."""
    if not SNAPSHOT_NAME_RE.match(name):
        raise ValueError("name: 1-64 of letters, digits, '.', '_', '-', starting with a letter or digit")
    if any(s["name"] == name for s in list_snapshots(instance.id)):
        raise ValueError(f"a snapshot called {name!r} already exists")
    disks = _instance_disks(instance.id)
    if not disks:
        raise RuntimeError("this instance has no disks here (is it on another host?)")
    t0 = time.monotonic()
    was_running = _shut_off(instance.domain_name, force)
    done: list[Path] = []
    try:
        for d in disks:
            _qemu_img_snapshot("-c", d, name)
            done.append(d)
    except RuntimeError:
        for d in done:  # all or nothing
            subprocess.run(["qemu-img", "snapshot", "-d", name, str(d)], capture_output=True)
        raise
    finally:
        if was_running:
            start_domain(instance.domain_name)
    snap = {"name": name, "description": description[:500], "created_at": _now(),
            "disks": [d.name for d in disks], "was_running": was_running,
            "seconds": round(time.monotonic() - t0, 1)}
    _save_snapshots(instance.id, list_snapshots(instance.id) + [snap])
    return snap


def restore_snapshot(instance: Instance, name: str, start: bool | None = None) -> dict:
    """Power off (the state now is being thrown away), revert every disk, then
    start if it was running -- or as `start` says."""
    snap = next((s for s in list_snapshots(instance.id) if s["name"] == name), None)
    if not snap:
        raise KeyError(name)
    conn = _conn()
    try:
        dom = conn.lookupByName(instance.domain_name)
        was_running = bool(dom.isActive())
        if was_running:
            dom.destroy()
    finally:
        conn.close()
    for disk in snap["disks"]:
        _qemu_img_snapshot("-a", INSTANCES_DIR / instance.id / disk, name)
    started = was_running if start is None else start
    if started:
        start_domain(instance.domain_name)
    return {**snap, "restored_at": _now(), "running": started}


def delete_snapshot(instance: Instance, name: str) -> None:
    snaps = list_snapshots(instance.id)
    snap = next((s for s in snaps if s["name"] == name), None)
    if not snap:
        raise KeyError(name)
    conn = _conn()
    try:
        running = conn.lookupByName(instance.domain_name).isActive()
    finally:
        conn.close()
    if running:
        raise RuntimeError("deleting a snapshot needs the instance stopped (qemu-img can't open a disk "
                           "a running VM holds)")
    for disk in snap["disks"]:
        _qemu_img_snapshot("-d", INSTANCES_DIR / instance.id / disk, name)
    _save_snapshots(instance.id, [s for s in snaps if s["name"] != name])


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def delete_instance(instance: Instance) -> None:
    conn = _conn()
    try:
        try:
            dom = conn.lookupByName(instance.domain_name)
            if dom.isActive():
                dom.destroy()
            # F-233: a UEFI domain (B2) has an NVRAM store, and a plain
            # undefine() refuses it; the flag is harmless for BIOS domains.
            dom.undefineFlags(libvirt.VIR_DOMAIN_UNDEFINE_NVRAM)
        except libvirt.libvirtError as e:
            # Only "no such domain" means already gone. Anything else used to
            # be swallowed here too, leaving the domain defined (F-233).
            if e.get_error_code() != libvirt.VIR_ERR_NO_DOMAIN:
                log.error("couldn't undefine %s: %s", instance.domain_name, e)
                raise RuntimeError(f"libvirt couldn't remove {instance.domain_name}: {e}") from e
    finally:
        conn.close()

    instance_dir = INSTANCES_DIR / instance.id
    if instance_dir.exists():
        shutil.rmtree(instance_dir)


def get_instance_status(domain_name: str) -> InstanceStatus:
    conn = _conn()
    try:
        dom = conn.lookupByName(domain_name)
        state, _ = dom.state()
        if state == libvirt.VIR_DOMAIN_RUNNING:
            return InstanceStatus.RUNNING
        elif state in (libvirt.VIR_DOMAIN_SHUTOFF, libvirt.VIR_DOMAIN_SHUTDOWN):
            return InstanceStatus.STOPPED
        return InstanceStatus.PENDING
    except libvirt.libvirtError:
        return InstanceStatus.DELETED
    finally:
        conn.close()


def get_instance_ip(domain_name: str) -> str:
    """Return guest IP: real DHCP IP for bridge instances, 10.0.2.15 for SLIRP."""
    conn = _conn()
    try:
        dom = conn.lookupByName(domain_name)
        state, _ = dom.state()
        if state != libvirt.VIR_DOMAIN_RUNNING:
            return ""
        # Get MAC address from domain XML to tell bridge-mode instances
        # (real DHCP-leased IP) apart from SLIRP-mode ones (fixed IP).
        import xml.etree.ElementTree as ET
        tree = ET.fromstring(dom.XMLDesc())
        mac_el = tree.find(".//interface[@type='bridge']/mac")
        if mac_el is None:
            return "10.0.2.15"
        mac = mac_el.get("address", "").lower()
        for lease_file in LEASE_FILES:
            try:
                leases = lease_file.read_text() if lease_file.exists() else ""
            except OSError as e:
                # An unreadable lease file must not fail the whole request
                # (found on the peer: the lab lease file was root-only).
                log.warning("can't read %s: %s", lease_file, e)
                continue
            if leases:
                for line in leases.splitlines():
                    parts = line.split()
                    # dnsmasq lease format: expiry mac ip hostname clientid
                    if len(parts) >= 3 and parts[1].lower() == mac:
                        return parts[2]
        # Bridge instance with no lease yet (called right after boot,
        # before DHCP completes) — return falsy so callers checking
        # `if not instance.private_ip` retry on a later poll instead of
        # getting stuck on the SLIRP placeholder forever.
        return ""
    except libvirt.libvirtError:
        return ""
    finally:
        conn.close()
