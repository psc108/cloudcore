from __future__ import annotations
import os
import subprocess
import sys
from flask import Blueprint, jsonify

about_bp = Blueprint("about", __name__)

_GALAXY = os.path.join(os.path.dirname(__file__), "..", "ansible", "collections", "cloudcore", "galaxy.yml")


def _cmd(*args) -> str:
    try:
        return subprocess.check_output(list(args), stderr=subprocess.STDOUT, text=True).strip()
    except Exception:
        return "unknown"


def _collection_version() -> str:
    try:
        import yaml
        with open(_GALAXY) as f:
            return yaml.safe_load(f).get("version", "unknown")
    except Exception:
        return "unknown"


def _libvirt_version() -> str:
    try:
        import libvirt
        v = libvirt.getVersion()
        major, minor, patch = v // 1_000_000, (v % 1_000_000) // 1_000, v % 1_000
        return f"{major}.{minor}.{patch}"
    except Exception:
        return "unknown"


def _parse_first_line(raw: str) -> str:
    return raw.splitlines()[0] if raw and raw != "unknown" else "unknown"


@about_bp.get("/v1/about")
def about():
    flask_ver = "unknown"
    try:
        import flask
        flask_ver = flask.__version__
    except Exception:
        pass

    paramiko_ver = "unknown"
    try:
        import paramiko
        paramiko_ver = paramiko.__version__
    except Exception:
        pass

    websockets_ver = "unknown"
    try:
        import websockets
        websockets_ver = websockets.__version__
    except Exception:
        pass

    pyyaml_ver = "unknown"
    try:
        import yaml
        pyyaml_ver = yaml.__version__
    except Exception:
        pass

    ansible_ver = _parse_first_line(_cmd("ansible", "--version"))
    qemu_ver    = _parse_first_line(_cmd("qemu-img", "--version"))
    haproxy_ver = _parse_first_line(_cmd("haproxy", "-v"))
    dnsmasq_ver = _parse_first_line(_cmd("dnsmasq", "--version"))

    return jsonify({
        "cloudcore": {
            "api":        "1.0.0",
            "collection": _collection_version(),
        },
        "runtime": {
            "python":    sys.version.split()[0],
            "flask":     flask_ver,
            "libvirt":   _libvirt_version(),
            "paramiko":  paramiko_ver,
            "websockets": websockets_ver,
            "pyyaml":    pyyaml_ver,
        },
        "system": {
            "ansible":  ansible_ver,
            "qemu_img": qemu_ver,
            "haproxy":  haproxy_ver,
            "dnsmasq":  dnsmasq_ver,
        },
    })
