"""USB host device discovery and passthrough helpers.

Devices are never persisted — they're discovered live from the host's
actual USB tree (/sys/bus/usb/devices/) on every call, the same way
compute.list_images() reflects whatever's actually on disk rather than a
stored catalogue. Attachment state (which instance, if any, owns a given
device) is derived by scanning instances for membership in
usb_device_ids, not tracked separately — mirrors the existing security
group detach-conflict check in sg_routes.py.

Safety: a device is blocked from attachment if it's HID (checked at the
interface level, not just the device level — a composite keyboard/mouse
commonly reports class 00 at the device level and only declares class 03
on one of its interfaces), a hub, or name-matches a small denylist that
catches vendor-specific-class input/auth devices an interface-class check
can't see (e.g. fingerprint readers, reported as class 0xff). This exists
specifically so a CloudCore host that's also someone's workstation can't
be told to hand its own keyboard/mouse to a VM.
"""
from __future__ import annotations

import subprocess
import threading
from pathlib import Path

import store as resource_store

_USB_SYSFS = Path("/sys/bus/usb/devices")

_HID_CLASS = "03"
_HUB_CLASS = "09"

_NAME_DENYLIST = ("keyboard", "mouse", "touchpad", "trackpad", "fingerprint")

# Class code alone can't identify a WiFi adapter — most USB WiFi dongles
# (including the RTL8812AU this platform's sniffer template targets)
# report a vendor-specific device class (0xff), indistinguishable by class
# from any other vendor-specific gadget. Description keywords are the only
# reliable signal available from sysfs/lsusb. "wireless" alone is excluded
# unless paired with an explicit WiFi marker, since combo Bluetooth radios
# (e.g. "Intel Corp. Bluetooth wireless interface") also say "wireless".
_WIFI_MARKERS = ("wifi", "wi-fi", "wlan", "802.11")


def _looks_like_wifi_adapter(description: str) -> bool:
    lowered = description.lower()
    if any(m in lowered for m in _WIFI_MARKERS):
        return True
    return "wireless" in lowered and "bluetooth" not in lowered

# Guards check-then-claim so two concurrent requests can't both be told
# the same device is free. Mirrors compute._port_lock.
_usb_lock = threading.Lock()


def _read(path: Path) -> str:
    try:
        return path.read_text().strip()
    except (FileNotFoundError, OSError):
        return ""


def _lsusb_description(vendor_id: str, product_id: str) -> str:
    """Look up a device's name from the usb.ids database via `lsusb -d`.

    Some devices (e.g. the fingerprint reader that motivated this
    fallback) report empty product/manufacturer strings via sysfs at
    runtime, even though lsusb's static usb.ids lookup knows exactly what
    they are — relying on sysfs strings alone silently produces devices
    with no name to keyword-match against.
    """
    try:
        out = subprocess.run(
            ["lsusb", "-d", f"{vendor_id}:{product_id}"],
            capture_output=True, text=True, timeout=5,
        ).stdout.strip()
    except (FileNotFoundError, subprocess.SubprocessError, OSError):
        return ""
    marker = f"ID {vendor_id}:{product_id} "
    idx = out.find(marker)
    if idx == -1:
        return ""
    return out[idx + len(marker):].splitlines()[0].strip()


def _interface_classes(device_dir: Path) -> set[str]:
    """Interface classes for a device. sysfs stores these as sibling dirs
    named "<device>:<config>.<iface>" next to the device dir itself."""
    classes = set()
    for iface_dir in device_dir.parent.glob(f"{device_dir.name}:*"):
        cls = _read(iface_dir / "bInterfaceClass")
        if cls:
            classes.add(cls)
    return classes


def _is_blocked(device_class: str, interface_classes: set[str], description: str) -> tuple[bool, str | None]:
    if device_class == _HID_CLASS or _HID_CLASS in interface_classes:
        return True, "HID device (keyboard/mouse/similar) — refused to prevent locking out the host"
    if device_class == _HUB_CLASS:
        return True, "USB hub — not a passthrough-able device"
    lowered = description.lower()
    for kw in _NAME_DENYLIST:
        if kw in lowered:
            return True, f"blocked by name match ('{kw}') — likely an input or auth device"
    return False, None


def _attached_instance_map() -> dict[str, str]:
    """usb_id -> instance_id for every instance currently referencing it."""
    attached: dict[str, str] = {}
    for inst in resource_store.list_instances():
        for usb_id in inst.usb_device_ids:
            attached[usb_id] = inst.id
    return attached


def list_usb_devices() -> list[dict]:
    """Discover USB devices physically attached to the host right now.

    Blocked devices are still returned (with blocked=True and a reason)
    rather than hidden, so it's discoverable *why* something can't be
    attached instead of it just mysteriously not appearing.
    """
    attached = _attached_instance_map()
    devices = []
    if not _USB_SYSFS.is_dir():
        return devices
    for entry in sorted(_USB_SYSFS.glob("*")):
        vendor_file = entry / "idVendor"
        if not vendor_file.exists():
            continue  # interface-only dirs (e.g. "1-5.4.2:1.0") have no idVendor
        vendor_id = _read(vendor_file)
        product_id = _read(entry / "idProduct")
        if not vendor_id or not product_id:
            continue
        usb_id = f"{vendor_id}:{product_id}"
        device_class = _read(entry / "bDeviceClass")
        product = _read(entry / "product")
        manufacturer = _read(entry / "manufacturer")
        description = (
            _lsusb_description(vendor_id, product_id)
            or f"{manufacturer} {product}".strip()
            or f"{usb_id} device"
        )
        interface_classes = _interface_classes(entry)
        blocked, reason = _is_blocked(device_class, interface_classes, description)
        devices.append({
            "id": usb_id,
            "vendor_id": vendor_id,
            "product_id": product_id,
            "description": description,
            "bus": _read(entry / "busnum"),
            "device": _read(entry / "devnum"),
            "blocked": blocked,
            "block_reason": reason,
            "attached_to": attached.get(usb_id),
            "likely_wifi_adapter": _looks_like_wifi_adapter(description),
        })
    return devices


def validate_attachable(usb_id: str, requesting_instance_id: str | None) -> str | None:
    """Return an error string if usb_id cannot be attached, else None.

    Call under _usb_lock so the check-then-claim is atomic against
    concurrent requests targeting the same device. requesting_instance_id
    is the instance that WOULD own the device after this call succeeds —
    pass None at instance-create time (nothing can already be attached to
    an instance that doesn't exist yet); pass the instance's own id on
    update, so a device it already owns doesn't read as a conflict with
    itself.
    """
    devices = {d["id"]: d for d in list_usb_devices()}
    dev = devices.get(usb_id)
    if not dev:
        return f"USB device '{usb_id}' not found on this host"
    if dev["blocked"]:
        return f"USB device '{usb_id}' cannot be attached: {dev['block_reason']}"
    owner = dev["attached_to"]
    if owner and owner != requesting_instance_id:
        return f"USB device '{usb_id}' is already attached to instance '{owner}'"
    return None


def hostdev_xml(usb_id: str) -> str:
    """<hostdev> fragment for one device. managed='yes' makes libvirt
    auto-detach it from the host driver before the guest starts and
    auto-reattach it once the guest releases it — no manual
    virsh nodedev-detach needed, and no VFIO/IOMMU kernel config required
    (that's only for PCI passthrough; USB hostdev goes through QEMU's own
    usb-host device model)."""
    vendor_id, product_id = usb_id.split(":")
    return (
        "<hostdev mode='subsystem' type='usb' managed='yes'>"
        f"<source><vendor id='0x{vendor_id}'/><product id='0x{product_id}'/></source>"
        "</hostdev>"
    )
