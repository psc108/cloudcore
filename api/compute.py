from __future__ import annotations

import os
import shutil
import socket
import subprocess
import textwrap
from pathlib import Path
from typing import Optional

import threading

import libvirt

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

# Port range for SSH forwarding (SLIRP path)
_SSH_PORT_START = 12200
_SSH_PORT_END = 12299

# Bridge name for persistent networking (created by setup-network.sh)
BRIDGE_NAME = "ccbr0"

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
FLAVORS = {
    "standard.nano":   (1, 512,  5),
    "standard.small":  (1, 1024, 10),
    "standard.medium": (2, 2048, 20),
    "standard.large":  (4, 4096, 40),
}


def _conn() -> libvirt.virConnect:
    conn = libvirt.open(QEMU_URI)
    if conn is None:
        raise RuntimeError("Failed to connect to libvirt")
    return conn


def list_images() -> list[dict]:
    """Return catalogue entries annotated with whether the image file is present."""
    available_stems = {p.stem for p in IMAGES_DIR.glob("*.qcow2")}
    return [
        {**img, "available": img["id"] in available_stems}
        for img in IMAGE_CATALOGUE
    ]


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
    lines = ["users:"]
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
        write_entries.append(f"""  - path: /home/{usr}/.ssh/cloudcore_ed25519
    permissions: '0600'
    owner: '{usr}:{usr}'
    content: |
{priv_indented}
  - path: /home/{usr}/.ssh/cloudcore_ed25519.pub
    permissions: '0644'
    owner: '{usr}:{usr}'
    content: |
          {cc_pubkey}""")
        runcmds.append(f"""  - |
    mkdir -p /home/{usr}/.ssh
    grep -qF 'cloudcore_ed25519' /home/{usr}/.ssh/config 2>/dev/null || printf '\\nHost *\\n  IdentityFile ~/.ssh/cloudcore_ed25519\\n  StrictHostKeyChecking no\\n' >> /home/{usr}/.ssh/config
    chown {usr}:{usr} /home/{usr}/.ssh/config /home/{usr}/.ssh/cloudcore_ed25519 /home/{usr}/.ssh/cloudcore_ed25519.pub
    chmod 600 /home/{usr}/.ssh/config /home/{usr}/.ssh/cloudcore_ed25519""")

    return "write_files:\n" + "\n".join(write_entries) + "\nruncmd:\n" + "\n".join(runcmds)


def _cloud_init_iso(instance_dir: Path, instance_name: str, image_id: str,
                    user_data: Optional[str], extra_users: Optional[list] = None) -> Path:
    """Build a cloud-init NoCloud ISO, injecting the CloudCore inter-instance keypair."""
    extra_users = extra_users or []
    meta_data = f"instance-id: {instance_name}\nlocal-hostname: {instance_name}\n"

    cc_pubkey  = get_cc_pubkey()
    cc_privkey = _CC_PRIVKEY.read_text().strip() if _CC_PRIVKEY.exists() else ""
    ssh_user   = ssh_user_for_image(image_id)

    # Collect any user-supplied authorized_keys from raw user_data
    extra_keys: list[str] = []
    if user_data:
        import yaml
        try:
            parsed = yaml.safe_load(user_data.split("#cloud-config", 1)[-1])
            if isinstance(parsed, dict):
                extra_keys = parsed.get("ssh_authorized_keys", [])
        except Exception:
            pass

    all_keys = extra_keys + ([cc_pubkey] if cc_pubkey else [])
    keys_yaml = "\n".join(f"  - {k}" for k in all_keys)

    users_block = _build_users_block(extra_users, cc_pubkey)
    write_files_block = _build_write_files_block(ssh_user, cc_pubkey, cc_privkey, extra_users)

    user_data = f"""#cloud-config
ssh_authorized_keys:
{keys_yaml}
{users_block}
{write_files_block}
"""

    (instance_dir / "meta-data").write_text(meta_data)
    (instance_dir / "user-data").write_text(user_data)
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


def _bridge_usable() -> bool:
    """Return True only if ccbr0 exists AND /etc/qemu/bridge.conf permits it."""
    r = subprocess.run(["ip", "link", "show", BRIDGE_NAME], capture_output=True)
    if r.returncode != 0:
        return False
    conf = Path("/etc/qemu/bridge.conf")
    if not conf.exists():
        return False
    return any(
        line.strip() in (f"allow {BRIDGE_NAME}", "allow all")
        for line in conf.read_text().splitlines()
        if not line.strip().startswith("#")
    )


def _domain_xml_slirp(
    domain_name: str,
    vcpus: int,
    memory_mb: int,
    disk_path: Path,
    iso_path: Path,
    ssh_host_port: int,
) -> str:
    memory_kib = memory_mb * 1024
    return textwrap.dedent(f"""\
        <domain type='kvm' xmlns:qemu='http://libvirt.org/schemas/domain/qemu/1.0'>
          <name>{domain_name}</name>
          <memory unit='KiB'>{memory_kib}</memory>
          <vcpu>{vcpus}</vcpu>
          <os>
            <type arch='x86_64' machine='pc-i440fx-2.9'>hvm</type>
            <boot dev='hd'/>
          </os>
          <features><acpi/><apic/></features>
          <cpu mode='host-passthrough'/>
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
            <serial type='pty'><target port='0'/></serial>
            <console type='pty'><target type='serial' port='0'/></console>
          </devices>
          <qemu:commandline>
            <qemu:arg value='-netdev'/>
            <qemu:arg value='user,id=ccnet0,hostfwd=tcp:127.0.0.1:{ssh_host_port}-:22'/>
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
) -> str:
    memory_kib = memory_mb * 1024
    return textwrap.dedent(f"""\
        <domain type='kvm'>
          <name>{domain_name}</name>
          <memory unit='KiB'>{memory_kib}</memory>
          <vcpu>{vcpus}</vcpu>
          <os>
            <type arch='x86_64' machine='pc-i440fx-2.9'>hvm</type>
            <boot dev='hd'/>
          </os>
          <features><acpi/><apic/></features>
          <cpu mode='host-passthrough'/>
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
            <interface type='bridge'>
              <source bridge='{BRIDGE_NAME}'/>
              <model type='virtio'/>
            </interface>
            <serial type='pty'><target port='0'/></serial>
            <console type='pty'><target type='serial' port='0'/></console>
          </devices>
        </domain>
    """)



def create_instance(instance: Instance) -> Instance:
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

    iso_path = _cloud_init_iso(instance_dir, instance.name, instance.image_id, instance.user_data, instance.users)
    use_bridge = _bridge_usable()

    with _port_lock:
        if use_bridge:
            xml = _domain_xml_bridge(domain_name, vcpus, memory_mb, disk_path, iso_path)
            instance.ssh_host_port = 0
            instance.private_ip = ""  # will be set from DHCP lease after boot
        else:
            ssh_host_port = _free_port(_SSH_PORT_START, _SSH_PORT_END)
            _scrub_known_hosts(ssh_host_port)
            instance.ssh_host_port = ssh_host_port
            instance.private_ip = "10.0.2.15"  # SLIRP convention
            xml = _domain_xml_slirp(domain_name, vcpus, memory_mb, disk_path, iso_path, ssh_host_port)

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


def stop_instance(domain_name: str) -> None:
    conn = _conn()
    try:
        dom = conn.lookupByName(domain_name)
        if dom.isActive():
            dom.destroy()
    except libvirt.libvirtError as e:
        raise RuntimeError(f"libvirt error: {e}") from e
    finally:
        conn.close()


def delete_instance(instance: Instance) -> None:
    conn = _conn()
    try:
        try:
            dom = conn.lookupByName(instance.domain_name)
            if dom.isActive():
                dom.destroy()
            dom.undefine()
        except libvirt.libvirtError:
            pass  # already gone
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
        # Try bridge DHCP lease first
        lease_file = Path("/var/lib/misc/cloudcore-dnsmasq.leases")
        if lease_file.exists():
            # Get MAC address from domain XML
            import xml.etree.ElementTree as ET
            tree = ET.fromstring(dom.XMLDesc())
            mac_el = tree.find(".//interface[@type='bridge']/mac")
            if mac_el is not None:
                mac = mac_el.get("address", "").lower()
                for line in lease_file.read_text().splitlines():
                    parts = line.split()
                    # dnsmasq lease format: expiry mac ip hostname clientid
                    if len(parts) >= 3 and parts[1].lower() == mac:
                        return parts[2]
        return "10.0.2.15"
    except libvirt.libvirtError:
        return ""
    finally:
        conn.close()
