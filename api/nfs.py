"""NFS server provisioning — KVM VM with a dedicated data disk."""
from __future__ import annotations

import subprocess
import textwrap
from pathlib import Path

import libvirt

from models import NfsServer, NfsServerStatus
import compute  # reuse helpers: _conn, _free_port, _bridge_usable, FLAVORS, etc.

VOLUMES_DIR = Path(__file__).parent / "volumes"
VOLUMES_DIR.mkdir(exist_ok=True)

_NFS_IMAGE_ID = "ubuntu-22.04"
_SSH_PORT_START = 12300
_SSH_PORT_END   = 12399


def _data_disk_path(nfs_id: str) -> Path:
    return VOLUMES_DIR / f"nfs-{nfs_id}.qcow2"


def _cloud_init_iso(nfs: NfsServer, vpc_cidr: str) -> Path:
    """Build cloud-init ISO that installs nfs-kernel-server, sets up LVM and exports."""
    instance_dir = compute.INSTANCES_DIR / f"nfs-{nfs.id}"
    instance_dir.mkdir(parents=True, exist_ok=True)

    cc_pubkey  = compute.get_cc_pubkey()
    cc_privkey = (compute.KEYS_DIR / "cloudcore_ed25519").read_text().strip() \
                 if (compute.KEYS_DIR / "cloudcore_ed25519").exists() else ""

    shares_exports = "\n".join(
        _export_line(s, vpc_cidr) for s in nfs.shares
    )
    exports_cmds = "\n  ".join(
        f"- echo '{_export_line_raw(s, vpc_cidr)}' >> /etc/exports"
        for s in nfs.shares
    ) or "- true"

    priv_indented = "\n".join("      " + l for l in cc_privkey.splitlines())

    user_data = f"""#cloud-config
hostname: {nfs.name}
ssh_authorized_keys:
  - {cc_pubkey}

packages:
  - nfs-kernel-server
  - lvm2

write_files:
  - path: /home/ubuntu/.ssh/cloudcore_ed25519
    permissions: '0600'
    owner: 'ubuntu:ubuntu'
    content: |
{priv_indented}
  - path: /home/ubuntu/.ssh/cloudcore_ed25519.pub
    permissions: '0644'
    owner: 'ubuntu:ubuntu'
    content: |
      {cc_pubkey}

runcmd:
  - pvcreate /dev/vdb
  - vgcreate nfsvg /dev/vdb
  - lvcreate -l 100%FREE -n nfslv nfsvg
  - mkfs.ext4 /dev/nfsvg/nfslv
  - mkdir -p /exports
  - echo '/dev/nfsvg/nfslv /exports ext4 defaults 0 2' >> /etc/fstab
  - mount -a
  {_mkdir_cmds(nfs.shares)}
  - truncate -s0 /etc/exports
  {exports_cmds}
  - exportfs -ra
  - systemctl enable --now nfs-kernel-server
  - |
    mkdir -p /home/ubuntu/.ssh
    grep -qF 'cloudcore_ed25519' /home/ubuntu/.ssh/config 2>/dev/null || printf '\\nHost *\\n  IdentityFile ~/.ssh/cloudcore_ed25519\\n  StrictHostKeyChecking no\\n' >> /home/ubuntu/.ssh/config
    chown ubuntu:ubuntu /home/ubuntu/.ssh/config /home/ubuntu/.ssh/cloudcore_ed25519 /home/ubuntu/.ssh/cloudcore_ed25519.pub
    chmod 600 /home/ubuntu/.ssh/config /home/ubuntu/.ssh/cloudcore_ed25519
"""

    meta_data = f"instance-id: {nfs.name}\nlocal-hostname: {nfs.name}\n"
    (instance_dir / "user-data").write_text(user_data)
    (instance_dir / "meta-data").write_text(meta_data)
    iso_path = instance_dir / "cloud-init.iso"

    for cmd in (
        ["genisoimage", "-output", str(iso_path), "-volid", "cidata",
         "-joliet", "-rock", str(instance_dir / "user-data"), str(instance_dir / "meta-data")],
        ["xorriso", "-as", "mkisofs", "-output", str(iso_path), "-volid", "cidata",
         "-joliet", "-rock", str(instance_dir / "user-data"), str(instance_dir / "meta-data")],
    ):
        if subprocess.run(["which", cmd[0]], capture_output=True).returncode == 0:
            subprocess.run(cmd, check=True, capture_output=True)
            return iso_path

    raise RuntimeError("No ISO builder found. Install genisoimage: sudo apt install genisoimage")


def _export_line_raw(share: dict, vpc_cidr: str) -> str:
    """Single /etc/exports line with no indentation, safe for shell echo."""
    clients = share.get("clients", "vpc")
    if clients == "vpc":
        host_spec = vpc_cidr
    elif isinstance(clients, list):
        host_spec = " ".join(clients)
    else:
        host_spec = str(clients)
    path = f"/exports/{share['name']}"
    return f"{path} {host_spec}(rw,sync,no_subtree_check,no_root_squash)"


def _export_line(share: dict, vpc_cidr: str) -> str:
    """Used by reload_exports — returns a plain exports line."""
    return _export_line_raw(share, vpc_cidr)


def _mkdir_cmds(shares: list) -> str:
    return "\n  ".join(
        f"- mkdir -p /exports/{s['name']} && chmod 777 /exports/{s['name']}"
        for s in shares
    ) or "- true"


def _domain_xml(nfs: NfsServer, disk_path: Path, data_disk_path: Path,
                iso_path: Path, vcpus: int, memory_mb: int,
                ssh_host_port: int = 0) -> str:
    memory_kib = memory_mb * 1024
    use_bridge = compute._bridge_usable()

    net_xml = (
        f"""    <interface type='bridge'>
              <source bridge='{compute.BRIDGE_NAME}'/>
              <model type='virtio'/>
            </interface>"""
        if use_bridge else
        ""  # injected via qemu:commandline below
    )

    ns = "xmlns:qemu='http://libvirt.org/schemas/domain/qemu/1.0'" if not use_bridge else ""
    qemu_cmd = textwrap.dedent(f"""
          <qemu:commandline>
            <qemu:arg value='-netdev'/>
            <qemu:arg value='user,id=ccnet0,hostfwd=tcp:127.0.0.1:{ssh_host_port}-:22'/>
            <qemu:arg value='-device'/>
            <qemu:arg value='virtio-net-pci,netdev=ccnet0,bus=pci.0,addr=0x7'/>
          </qemu:commandline>""") if not use_bridge else ""

    return textwrap.dedent(f"""\
        <domain type='kvm' {ns}>
          <name>{nfs.domain_name}</name>
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
            <disk type='file' device='disk'>
              <driver name='qemu' type='qcow2'/>
              <source file='{data_disk_path}'/>
              <target dev='vdb' bus='virtio'/>
            </disk>
            <disk type='file' device='cdrom'>
              <driver name='qemu' type='raw'/>
              <source file='{iso_path}'/>
              <target dev='sda' bus='sata'/>
              <readonly/>
            </disk>
            {net_xml}
            <memballoon model='none'/>
            <serial type='pty'><target port='0'/></serial>
            <console type='pty'><target type='serial' port='0'/></console>
          </devices>{qemu_cmd}
        </domain>
    """)


def create_nfs_server(nfs: NfsServer, vpc_cidr: str) -> NfsServer:
    flavor = compute.FLAVORS.get(nfs.flavor)
    if flavor is None:
        raise ValueError(f"Unknown flavor '{nfs.flavor}'")

    vcpus, memory_mb, _ = flavor
    base_image = compute._base_image_path(_NFS_IMAGE_ID)
    instance_dir = compute.INSTANCES_DIR / f"nfs-{nfs.id}"
    instance_dir.mkdir(parents=True, exist_ok=True)

    os_disk = instance_dir / "disk.qcow2"
    data_disk = _data_disk_path(nfs.id)
    domain_name = f"ccnfs-{nfs.id[:8]}"
    nfs.domain_name = domain_name

    # OS disk — CoW overlay on base image
    subprocess.run(
        ["qemu-img", "create", "-f", "qcow2", "-b", str(base_image), "-F", "qcow2",
         str(os_disk), "10G"],
        check=True, capture_output=True,
    )
    # Data disk — blank qcow2, sized by user
    subprocess.run(
        ["qemu-img", "create", "-f", "qcow2", str(data_disk), f"{nfs.disk_gb}G"],
        check=True, capture_output=True,
    )

    iso_path = _cloud_init_iso(nfs, vpc_cidr)
    use_bridge = compute._bridge_usable()

    with compute._port_lock:
        if use_bridge:
            nfs.ssh_host_port = 0
            nfs.private_ip = ""
        else:
            nfs.ssh_host_port = compute._free_port(_SSH_PORT_START, _SSH_PORT_END)
            nfs.private_ip = "10.0.2.15"

        xml = _domain_xml(nfs, os_disk, data_disk, iso_path, vcpus, memory_mb, nfs.ssh_host_port)

        conn = compute._conn()
        try:
            dom = conn.defineXML(xml)
            dom.create()
            nfs.status = NfsServerStatus.RUNNING
        except Exception as e:
            nfs.status = NfsServerStatus.ERROR
            raise RuntimeError(f"libvirt error: {e}") from e
        finally:
            conn.close()

    return nfs


def delete_nfs_server(nfs: NfsServer) -> None:
    import shutil
    conn = compute._conn()
    try:
        try:
            dom = conn.lookupByName(nfs.domain_name)
            if dom.isActive():
                dom.destroy()
            dom.undefine()
        except libvirt.libvirtError:
            pass
    finally:
        conn.close()

    instance_dir = compute.INSTANCES_DIR / f"nfs-{nfs.id}"
    if instance_dir.exists():
        shutil.rmtree(instance_dir)

    data_disk = _data_disk_path(nfs.id)
    if data_disk.exists():
        data_disk.unlink()


def get_nfs_server_ip(domain_name: str) -> str:
    return compute.get_instance_ip(domain_name)


def get_nfs_server_status(domain_name: str) -> NfsServerStatus:
    from models import InstanceStatus
    s = compute.get_instance_status(domain_name)
    mapping = {
        InstanceStatus.RUNNING: NfsServerStatus.RUNNING,
        InstanceStatus.STOPPED: NfsServerStatus.STOPPED,
        InstanceStatus.PENDING: NfsServerStatus.PENDING,
        InstanceStatus.DELETED: NfsServerStatus.DELETED,
        InstanceStatus.ERROR:   NfsServerStatus.ERROR,
    }
    return mapping.get(s, NfsServerStatus.ERROR)


def reload_exports(nfs: NfsServer) -> None:
    """Re-write /etc/exports and reload on a running NFS server via SSH."""
    if not nfs.ssh_host_port and not nfs.private_ip:
        raise RuntimeError("NFS server has no reachable SSH endpoint")

    vpc_cidr = _get_vpc_cidr(nfs.vpc_id)
    exports_content = "\n".join(_export_line(s, vpc_cidr) for s in nfs.shares) + "\n"

    key = compute.get_cc_privkey_path()
    port = nfs.ssh_host_port if nfs.ssh_host_port else 22
    host = "127.0.0.1" if nfs.ssh_host_port else nfs.private_ip

    import subprocess
    cmd = [
        "ssh", "-i", key, "-p", str(port),
        "-o", "StrictHostKeyChecking=no", "-o", "ConnectTimeout=10",
        f"ubuntu@{host}",
        f"echo '{exports_content}' | sudo tee /etc/exports > /dev/null && sudo exportfs -ra",
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip())


def _get_vpc_cidr(vpc_id: str) -> str:
    import store
    vpc = store.get_vpc(vpc_id)
    return vpc.cidr_block if vpc else "10.0.0.0/8"


def mount_command(nfs: NfsServer, share_name: str, mount_point: str) -> str:
    """Return the mount command string for use in cloud-init or live SSH."""
    return f"mount -t nfs {nfs.private_ip}:/exports/{share_name} {mount_point}"


def cloud_init_mount_entry(nfs_ip: str, share_name: str, mount_point: str) -> dict:
    """Return a cloud-init mounts: list entry."""
    return [f"{nfs_ip}:/exports/{share_name}", mount_point, "nfs",
            "defaults,_netdev,nofail", "0", "0"]
