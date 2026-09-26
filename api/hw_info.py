"""Static hardware inventory for the host CloudCore's own API (and, in
practice, Sentinel — the two always run on the same box in this
project's own deployments) is running on. Direct request: "there is no
where in the dashboard that provides details of the machine the
dashboard/sentinel is running on... from bios to cpu to memory
including versions if available, speeds etc."

Deliberately a separate module from host_stats.py, not an extension of
it: that module is live *performance* (load/memory-used-%/disk-used-%,
polled repeatedly for placement decisions); this one is static
*inventory* (what the machine actually is — model names, versions,
speeds), read once per request but not meaningfully expected to change
between requests short of a reboot. Every source here is either a
plain stdlib file read or a single read-only system command — same
"no new dependency, subprocess.check_output with a graceful fallback"
discipline about_routes.py already applies.

No new privilege requirement for anything in this file: `/sys/class/
dmi/id/*` (BIOS/board/chassis identity), `lscpu`, `lsblk`, `lspci`,
`/proc/meminfo`, and `/sys/class/net/*` are all world-readable on a
stock Ubuntu install, confirmed live on this host. Per-DIMM memory
detail (size/speed/manufacturer/part number per physical stick — the
one thing genuinely unavailable without it) additionally shells out to
`dmidecode`, which does need root; see api/setup-hwinfo.sh for the
narrowly-scoped sudoers grant that enables it, and _dimm_info() below
for the graceful all-or-nothing degradation when that grant isn't
installed on a given host.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

_DMI_DIR = Path("/sys/class/dmi/id")


def _cmd(*args: str) -> str:
    try:
        return subprocess.check_output(list(args), stderr=subprocess.DEVNULL, text=True, timeout=10)
    except Exception:
        return ""


def _dmi(field: str) -> str:
    """A single /sys/class/dmi/id/<field> value, or "" if absent/
    unreadable — a handful of fields (product_serial, board_serial,
    chassis_serial) are root-only on most distros even though the rest
    of this directory is world-readable; those are simply omitted
    rather than attempted, since dmidecode (already needed for DIMM
    detail) is the real path to serial numbers if ever wanted."""
    try:
        return (_DMI_DIR / field).read_text().strip()
    except OSError:
        return ""


def _bios_and_system() -> dict:
    return {
        "bios_vendor": _dmi("bios_vendor"),
        "bios_version": _dmi("bios_version"),
        "bios_date": _dmi("bios_date"),
        "system_vendor": _dmi("sys_vendor"),
        "product_name": _dmi("product_name"),
        "product_version": _dmi("product_version"),
        "board_vendor": _dmi("board_vendor"),
        "board_name": _dmi("board_name"),
        "board_version": _dmi("board_version"),
        "chassis_vendor": _dmi("chassis_vendor"),
        "chassis_type": _CHASSIS_TYPES.get(_dmi("chassis_type"), _dmi("chassis_type") or "Unknown"),
    }


# DMTF SMBIOS chassis-type codes -> human labels, the handful actually
# seen in practice (laptops, desktops, servers, VMs) — an unrecognized
# code still shows its raw number rather than failing.
_CHASSIS_TYPES = {
    "3": "Desktop", "4": "Low Profile Desktop", "6": "Mini Tower", "7": "Tower",
    "8": "Portable", "9": "Laptop", "10": "Notebook", "13": "All in One",
    "14": "Sub Notebook", "17": "Main Server Chassis", "23": "Rack Mount Server",
    "30": "Tablet", "31": "Convertible", "32": "Detachable",
}


def _cpu_info() -> dict:
    raw = _cmd("lscpu", "-J")
    fields: dict[str, str] = {}
    if raw:
        import json
        try:
            for row in json.loads(raw).get("lscpu", []):
                key = row.get("field", "").rstrip(":")
                if key:
                    fields[key] = row.get("data") or ""
        except (json.JSONDecodeError, AttributeError):
            pass

    vulnerabilities = {k[len("Vulnerability "):]: v for k, v in fields.items() if k.startswith("Vulnerability ")}

    def _f(key: str) -> str:
        return fields.get(key, "")

    def _int(key: str, default: int = 0) -> int:
        try:
            return int(_f(key))
        except ValueError:
            return default

    return {
        "model": _f("Model name"),
        "vendor": _f("Vendor ID"),
        "architecture": _f("Architecture"),
        "family": _f("CPU family"),
        "model_number": _f("Model"),
        "stepping": _f("Stepping"),
        "sockets": _int("Socket(s)", 1),
        "cores_per_socket": _int("Core(s) per socket", os.cpu_count() or 1),
        "threads_per_core": _int("Thread(s) per core", 1),
        "logical_cpus": os.cpu_count() or 0,
        "max_mhz": _f("CPU max MHz"),
        "min_mhz": _f("CPU min MHz"),
        "bogomips": _f("BogoMIPS"),
        "cache": {
            "l1d": _f("L1d cache"), "l1i": _f("L1i cache"),
            "l2": _f("L2 cache"), "l3": _f("L3 cache"),
        },
        "virtualization": _f("Virtualization"),
        "vulnerabilities": vulnerabilities,
    }


def _meminfo() -> dict:
    info: dict[str, int] = {}
    try:
        with open("/proc/meminfo") as fh:
            for line in fh:
                key, _, rest = line.partition(":")
                try:
                    info[key] = int(rest.strip().split()[0])
                except (ValueError, IndexError):
                    pass
    except OSError:
        pass
    total_kb = info.get("MemTotal", 0)
    return {
        "total_mb": round(total_kb / 1024) if total_kb else 0,
        "swap_total_mb": round(info.get("SwapTotal", 0) / 1024),
    }


def _dimm_info() -> tuple[list[dict], bool]:
    """Per-DIMM detail via `sudo -n dmidecode -t memory` — returns
    (dimms, available). `available=False` (empty list) whenever the
    sudoers grant isn't installed or dmidecode itself isn't present,
    letting the API response and the UI both distinguish "this host
    genuinely has no populated slots" (impossible) from "we're not
    allowed to ask" — the two look identical as an empty list alone."""
    raw = subprocess.run(
        ["sudo", "-n", "dmidecode", "-t", "memory"],
        capture_output=True, text=True, timeout=10,
    )
    if raw.returncode != 0:
        return [], False

    dimms: list[dict] = []
    for block in raw.stdout.split("\n\n"):
        lines = block.splitlines()
        # Every real dmidecode record is prefixed with its own
        # "Handle 0x.., DMI type N, M bytes" line, so "Memory Device"
        # is always the *second* line of the block, never the first —
        # confirmed live against this host's own real dmidecode output
        # after the first version of this check (which only looked at
        # line 0) silently skipped every genuine DIMM.
        if not any(line.strip() == "Memory Device" for line in lines):
            continue
        fields: dict[str, str] = {}
        for line in lines:
            key, sep, value = line.partition(":")
            if sep:
                fields[key.strip()] = value.strip()
        size = fields.get("Size", "")
        if not size or size.lower().startswith("no module"):
            continue  # an empty slot, not a populated DIMM
        dimms.append({
            "locator": fields.get("Locator", ""),
            "size": size,
            "type": fields.get("Type", ""),
            "speed": fields.get("Speed", ""),
            "configured_speed": fields.get("Configured Memory Speed", ""),
            "manufacturer": fields.get("Manufacturer", ""),
            "part_number": fields.get("Part Number", ""),
        })
    return dimms, True


def _disks() -> list[dict]:
    raw = _cmd("lsblk", "-J", "-b", "-o", "NAME,SIZE,TYPE,MODEL,VENDOR,ROTA,TRAN")
    if not raw:
        return []
    import json
    try:
        devices = json.loads(raw).get("blockdevices", [])
    except json.JSONDecodeError:
        return []
    disks = []
    for d in devices:
        if d.get("type") != "disk":
            continue
        size_bytes = d.get("size") or 0
        disks.append({
            "name": d.get("name", ""),
            "size_gb": round(int(size_bytes) / (1024 ** 3), 1) if size_bytes else 0,
            "model": (d.get("model") or "").strip(),
            "vendor": (d.get("vendor") or "").strip(),
            "media": "HDD" if d.get("rota") else ("NVMe" if d.get("tran") == "nvme" else "SSD"),
            "transport": d.get("tran") or "",
        })
    return disks


def _gpus() -> list[dict]:
    raw = _cmd("lspci", "-mm")
    gpus = []
    for line in raw.splitlines():
        if "VGA compatible controller" not in line and "3D controller" not in line and "Display controller" not in line:
            continue
        # lspci -mm: space-separated, double-quoted fields —
        # <slot> "<class>" "<vendor>" "<device>" [-r<rev>] ["<subvendor>" "<subdevice>"]
        parts = re.findall(r'"([^"]*)"|(\S+)', line)
        values = [a or b for a, b in parts]
        if len(values) >= 4:
            gpus.append({"vendor": values[2], "model": values[3]})
    return gpus


def _ethtool_driver_info(name: str) -> dict:
    """`ethtool -i <iface>` — driver name/version, firmware version, and
    the PCI/USB bus address, for both wired and wireless. Confirmed
    live: unlike plain `ethtool <iface>` (link speed/duplex/negotiated
    modes — that subcommand needs root on this host), `-i` specifically
    needs no privilege at all, so this needed no new sudoers grant."""
    raw = _cmd("ethtool", "-i", name)
    fields: dict[str, str] = {}
    for line in raw.splitlines():
        key, sep, value = line.partition(":")
        if sep:
            fields[key.strip()] = value.strip()
    return {
        "driver": fields.get("driver", ""),
        "driver_version": fields.get("version", ""),
        "firmware_version": fields.get("firmware-version", ""),
        "bus_info": fields.get("bus-info", ""),
    }


def _iw_link_info(name: str) -> dict:
    """SSID/frequency/signal/real negotiated bitrate for a wireless
    interface, via `iw dev <iface> link` + `iw dev <iface> info` — the
    sysfs `speed` file this module otherwise reads doesn't exist for
    WiFi at all (there's no fixed link speed the way a wired NIC has
    one), so this is the only way to show anything meaningful here.
    Needs the `iw` package (not `ethtool`, not root) — not installed on
    the host this was implemented against, so this specific function's
    real parsing is implementation-only, not yet live-confirmed against
    a genuine `iw` install; every other function in this module was
    verified against this host's own real output before being called
    done. Silently returns {} on any failure (not installed, not
    associated, unexpected output shape) rather than guessing."""
    if not shutil.which("iw"):
        return {}
    link = _cmd("iw", "dev", name, "link")
    if not link or "Not connected" in link:
        return {}
    result: dict[str, str] = {}
    for line in link.splitlines():
        line = line.strip()
        if line.startswith("SSID:"):
            result["ssid"] = line.partition(":")[2].strip()
        elif line.startswith("freq:"):
            result["frequency_mhz"] = line.partition(":")[2].strip()
        elif line.startswith("signal:"):
            result["signal_dbm"] = line.partition(":")[2].strip()
        elif line.startswith("tx bitrate:"):
            # e.g. "866.7 MBit/s VHT-MCS 9 80MHz short GI VHT-NSS 2" —
            # the numeric prefix is what's wanted, not a re-match of
            # "tx bitrate:" against a string that no longer contains it
            # (the partition below already stripped that prefix off).
            m = re.match(r"([\d.]+)", line.partition(":")[2].strip())
            if m:
                result["tx_bitrate_mbps"] = m.group(1)
    return result


def _network_interfaces() -> list[dict]:
    ifaces = []
    net_dir = Path("/sys/class/net")
    if not net_dir.is_dir():
        return ifaces
    for iface_dir in sorted(net_dir.iterdir()):
        name = iface_dir.name
        if name == "lo" or not (iface_dir / "device").exists():
            continue  # virtual (bridge/veth/tap/loopback) — no real backing device

        def _read(f: str) -> str:
            try:
                return (iface_dir / f).read_text().strip()
            except OSError:
                return ""

        wireless = (iface_dir / "phy80211").exists()
        speed = _read("speed")
        entry = {
            "name": name,
            "mac": _read("address"),
            "speed_mbps": speed if speed and speed != "-1" else "",
            "duplex": _read("duplex"),
            "operstate": _read("operstate"),
            "wireless": wireless,
            **_ethtool_driver_info(name),
        }
        if wireless:
            entry.update(_iw_link_info(name))
        ifaces.append(entry)
    return ifaces


def _os_info() -> dict:
    pretty_name = ""
    try:
        with open("/etc/os-release") as fh:
            for line in fh:
                if line.startswith("PRETTY_NAME="):
                    pretty_name = line.partition("=")[2].strip().strip('"')
                    break
    except OSError:
        pass
    uname = os.uname()
    return {
        "distro": pretty_name,
        "kernel": uname.release,
        "arch": uname.machine,
        "hostname": uname.nodename,
        "uptime": _cmd("uptime", "-p").strip(),
    }


def collect() -> dict:
    dimms, dmidecode_available = _dimm_info()
    return {
        "bios": _bios_and_system(),
        "cpu": _cpu_info(),
        "memory": {**_meminfo(), "dimms": dimms, "dimm_detail_available": dmidecode_available},
        "disks": _disks(),
        "gpus": _gpus(),
        "network": _network_interfaces(),
        "os": _os_info(),
    }
