"""Security group enforcement via iptables.

Bridge instances (ccbr0): rules applied on the host using the instance MAC
address — same approach as AWS at the hypervisor level.

SLIRP instances (no bridge): rules injected into the VM via SSH using
iptables commands. Applied after the instance is running.

Rule schema (ingress/egress):
  {
    "protocol":  "tcp" | "udp" | "icmp" | "-1",   # -1 = all
    "from_port": int | null,
    "to_port":   int | null,
    "cidr":      "0.0.0.0/0" | specific CIDR,
    "description": str   # informational only
  }
"""
from __future__ import annotations

import logging
import subprocess
import xml.etree.ElementTree as ET
from typing import Optional

import libvirt

log = logging.getLogger(__name__)

_QEMU_URI = "qemu:///session"
_CHAIN_PREFIX = "CC-SG-"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _run(cmd: list[str], check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, check=check)


def _instance_mac(domain_name: str) -> Optional[str]:
    """Return the MAC address of the bridge interface for a domain, or None."""
    try:
        conn = libvirt.open(_QEMU_URI)
        dom = conn.lookupByName(domain_name)
        tree = ET.fromstring(dom.XMLDesc())
        conn.close()
        mac_el = tree.find(".//interface[@type='bridge']/mac")
        return mac_el.get("address", "").lower() if mac_el is not None else None
    except Exception:
        return None


def _chain_name(sg_id: str) -> str:
    # iptables chain names max 28 chars; use first 16 chars of id
    return f"{_CHAIN_PREFIX}{sg_id[:16]}"


def _iptables_rule_args(rule: dict, direction: str) -> list[list[str]]:
    """Convert a rule dict to one or more iptables argument lists (without -A/-D chain)."""
    proto = rule.get("protocol", "-1")
    cidr  = rule.get("cidr", "0.0.0.0/0")
    fp    = rule.get("from_port")
    tp    = rule.get("to_port")

    if proto == "-1":
        return [["-s" if direction == "ingress" else "-d", cidr, "-j", "ACCEPT"]]

    args = ["-p", proto, "-s" if direction == "ingress" else "-d", cidr]
    if fp is not None and tp is not None:
        if fp == tp:
            args += ["--dport", str(fp)]
        else:
            args += ["--dport", f"{fp}:{tp}"]
    args += ["-j", "ACCEPT"]
    return [args]


# ---------------------------------------------------------------------------
# Bridge enforcement (host iptables, keyed on MAC)
# ---------------------------------------------------------------------------

def _ensure_chain(chain: str) -> None:
    r = _run(["iptables", "-n", "--list", chain], check=False)
    if r.returncode != 0:
        _run(["iptables", "-N", chain])


def _flush_chain(chain: str) -> None:
    _run(["iptables", "-F", chain], check=False)


def _delete_chain(chain: str) -> None:
    _run(["iptables", "-F", chain], check=False)
    _run(["iptables", "-X", chain], check=False)


def _mac_jump_exists(chain: str, mac: str, parent: str) -> bool:
    r = _run(["iptables", "-n", "--list", parent, "--line-numbers"], check=False)
    return chain in r.stdout and mac.lower() in r.stdout.lower()


def apply_bridge(domain_name: str, sg_id: str,
                 ingress_rules: list, egress_rules: list) -> None:
    """Apply security group rules for a bridge-networked instance."""
    mac = _instance_mac(domain_name)
    if not mac:
        log.warning("sg.apply_bridge: no MAC found for %s, skipping", domain_name)
        return

    chain = _chain_name(sg_id + domain_name[:8])
    _ensure_chain(chain)
    _flush_chain(chain)

    # Default deny at end of chain
    for rule in ingress_rules:
        for args in _iptables_rule_args(rule, "ingress"):
            _run(["iptables", "-A", chain] + args, check=False)
    _run(["iptables", "-A", chain, "-j", "DROP"], check=False)

    # Jump from FORWARD chain for this MAC
    if not _mac_jump_exists(chain, mac, "FORWARD"):
        _run(["iptables", "-I", "FORWARD", "1",
              "-m", "mac", "--mac-source", mac, "-j", chain], check=False)

    log.info("sg.apply_bridge: applied %d ingress rules for %s (mac %s)",
             len(ingress_rules), domain_name, mac)


def remove_bridge(domain_name: str, sg_id: str) -> None:
    """Remove bridge iptables rules for an instance."""
    mac = _instance_mac(domain_name)
    chain = _chain_name(sg_id + domain_name[:8])
    if mac:
        _run(["iptables", "-D", "FORWARD",
              "-m", "mac", "--mac-source", mac, "-j", chain], check=False)
    _delete_chain(chain)


# ---------------------------------------------------------------------------
# SLIRP enforcement (in-VM iptables via SSH)
# ---------------------------------------------------------------------------

def _build_iptables_script(ingress_rules: list, egress_rules: list) -> str:
    lines = [
        "#!/bin/sh",
        "iptables -F INPUT",
        "iptables -F OUTPUT",
        "iptables -P INPUT DROP",
        "iptables -P OUTPUT ACCEPT",
        "iptables -A INPUT -m state --state ESTABLISHED,RELATED -j ACCEPT",
        "iptables -A INPUT -i lo -j ACCEPT",
    ]
    for rule in ingress_rules:
        proto = rule.get("protocol", "-1")
        cidr  = rule.get("cidr", "0.0.0.0/0")
        fp    = rule.get("from_port")
        tp    = rule.get("to_port")
        if proto == "-1":
            lines.append(f"iptables -A INPUT -s {cidr} -j ACCEPT")
        else:
            dport = ""
            if fp is not None and tp is not None:
                dport = f"--dport {fp}" if fp == tp else f"--dport {fp}:{tp}"
            lines.append(f"iptables -A INPUT -p {proto} -s {cidr} {dport} -j ACCEPT".strip())
    return "\n".join(lines)


def apply_slirp(instance, ingress_rules: list, egress_rules: list) -> None:
    """Apply security group rules inside a SLIRP instance via SSH."""
    if not instance.ssh_host_port:
        return
    script = _build_iptables_script(ingress_rules, egress_rules)
    try:
        import compute
        key = compute.get_cc_privkey_path()
        subprocess.run(
            ["ssh", "-i", key, "-p", str(instance.ssh_host_port),
             "-o", "StrictHostKeyChecking=no", "-o", "ConnectTimeout=10",
             f"{instance.ssh_user}@127.0.0.1",
             f"echo {subprocess.list2cmdline([script])} | sudo sh"],
            capture_output=True, text=True, timeout=30,
        )
        log.info("sg.apply_slirp: applied rules to instance %s", instance.id)
    except Exception as e:
        log.warning("sg.apply_slirp: failed for %s: %s", instance.id, e)


# ---------------------------------------------------------------------------
# Public interface — called from server.py
# ---------------------------------------------------------------------------

def apply(instance, ingress_rules: list, egress_rules: list) -> None:
    """Apply security group rules to an instance (bridge or SLIRP)."""
    if instance.domain_name and _instance_mac(instance.domain_name):
        apply_bridge(instance.domain_name, instance.id, ingress_rules, egress_rules)
    else:
        apply_slirp(instance, ingress_rules, egress_rules)


def remove(instance) -> None:
    """Remove all security group rules for an instance."""
    if instance.domain_name and _instance_mac(instance.domain_name):
        remove_bridge(instance.domain_name, instance.id)
    # SLIRP: rules live inside the VM; they vanish when the VM is destroyed
