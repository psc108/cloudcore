"""Manages a local dnsmasq instance serving CloudCore DNS zones.

Listens on 127.0.0.1:5353 (no root required). Config is regenerated from
the DNS store and dnsmasq is reloaded (SIGHUP) after every record change.

To resolve from the host:
    dig @127.0.0.1 -p 5353 instance-name.instances.cloudcore.internal
"""
from __future__ import annotations

import logging
import os
import signal
import subprocess
from pathlib import Path
from typing import Optional

import db

log = logging.getLogger(__name__)

_DNS_DIR = Path(__file__).parent / "dns"
_DNS_DIR.mkdir(exist_ok=True)

_CONF_PATH = _DNS_DIR / "dnsmasq.conf"
_PID_PATH  = _DNS_DIR / "dnsmasq.pid"
PORT = 5353


def _generate_config() -> str:
    lines = [
        f"port={PORT}",
        "listen-address=127.0.0.1",
        "bind-interfaces",
        "no-dhcp-interface=",
        "no-hosts",
        "no-resolv",
        "domain-needed",
        f"pid-file={_PID_PATH}",
        "",
    ]
    rows = db.get_db().execute(
        "SELECT * FROM dns_records ORDER BY zone_name, name, type"
    ).fetchall()

    for row in rows:
        name  = row["name"]
        zone  = row["zone_name"]
        rtype = row["type"].upper()
        value = row["value"]
        fqdn  = f"{name}.{zone}" if name != "@" else zone

        if rtype in ("A", "AAAA"):
            lines.append(f"host-record={fqdn},{value}")
        elif rtype == "CNAME":
            lines.append(f"cname={fqdn},{value}")
        elif rtype == "TXT":
            escaped = value.replace('"', '\\"')
            lines.append(f'txt-record={fqdn},"{escaped}"')
        elif rtype == "MX":
            # value format: "<priority> <hostname>"
            parts = value.split(None, 1)
            if len(parts) == 2:
                lines.append(f"mx-host={fqdn},{parts[1]},{parts[0]}")
        elif rtype == "PTR":
            lines.append(f"ptr-record={fqdn},{value}")
        # NS/SOA not supported in dnsmasq address-only mode

    return "\n".join(lines) + "\n"


def _current_pid() -> Optional[int]:
    try:
        return int(_PID_PATH.read_text().strip())
    except (FileNotFoundError, ValueError):
        return None


def _is_running() -> bool:
    pid = _current_pid()
    if pid is None:
        return False
    try:
        os.kill(pid, 0)
        return True
    except (ProcessLookupError, PermissionError):
        return False


def start() -> None:
    """Write config and start dnsmasq. If already running, reload instead."""
    if _is_running():
        reload()
        return
    _CONF_PATH.write_text(_generate_config())
    try:
        # dnsmasq's argv parser rejects "--conf-file <path>" as two separate
        # tokens ("dnsmasq: junk found in command line") — it only accepts
        # the "=" form. Confirmed by direct reproduction: every start/reload
        # since this module was written has been silently failing, meaning
        # CloudCore's own DNS resolution (127.0.0.1:5353) has never actually
        # been up.
        subprocess.run(
            ["dnsmasq", f"--conf-file={_CONF_PATH}"],
            check=True, capture_output=True, text=True,
        )
        log.info("dns_server: dnsmasq started on 127.0.0.1:%d", PORT)
    except subprocess.CalledProcessError as e:
        log.error("dns_server: dnsmasq failed to start: %s", e.stderr or e.stdout)
    except FileNotFoundError:
        log.warning("dns_server: dnsmasq not found — DNS resolution disabled")


def reload() -> None:
    """Regenerate config and restart dnsmasq (stop + start, not SIGHUP).

    SIGHUP-based reload was tried first (cheaper — no resolution gap) but
    empirically, after enough reload cycles over a long-lived process's
    uptime, dnsmasq starts REFUSING previously-working queries outright
    (status REFUSED, EDNS extended error 14 "Not Ready") even though the
    config on disk and a fresh process started against the exact same
    config both work correctly — reproduced directly: the same conf file
    that a long-lived, many-times-SIGHUP'd process refused to answer was
    answered correctly instantly by a brand new process. Root cause inside
    dnsmasq itself wasn't pinned down (some SIGHUP-reload-only internal
    state, not config-related), but a full restart on every change avoids
    the whole class of failure — dnsmasq starts in well under a second, so
    the resolution gap this trades away is negligible against silently
    going permanently unresponsive.
    """
    _CONF_PATH.write_text(_generate_config())
    stop()
    start()


def stop() -> None:
    pid = _current_pid()
    if pid:
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
    _PID_PATH.unlink(missing_ok=True)
