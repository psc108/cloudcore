"""Cross-host peering identity: this host's own keypairs.

Two separate keypairs, deliberately not reused from anything else:
  - cloudcore_peer_ed25519{,.pub}: this host's pairing identity. Signs
    outbound pairing requests (ssh-keygen -Y sign) so the approving
    human on the *target* machine can verify a request really came
    from the holder of the advertised fingerprint (api/peers_routes.py).
    Kept separate from api/keys/cloudcore_ed25519, which is for SSHing
    *into* provisioned guest VMs — a different trust domain; reusing it
    here would let compromise of one leak into the other.
  - wg_private.key / wg_public.key: this host's WireGuard identity
    (X25519, a different curve/purpose from the ed25519 pairing key),
    reused for every peer this host ever pairs with (api/wireguard.py).
    Only generated if wireguard-tools (`wg`) is actually installed —
    not fatal to the API's own startup if it isn't yet, since a tunnel
    is only needed once a pairing is actually approved.

Both are idempotent, skip-if-exists — same convention as the existing
SSH keypair step in scripts/install.sh, which generates the pairing
identity key too (belt and suspenders); ensure_peer_keypair() is the
lazy fallback that also covers an *upgraded* (not freshly installed)
checkout, called once at API startup (server.py).
"""
from __future__ import annotations

import shutil
import socket
import subprocess
from pathlib import Path

KEYS_DIR = Path(__file__).parent / "keys"
PEER_PRIVKEY = KEYS_DIR / "cloudcore_peer_ed25519"
PEER_PUBKEY = KEYS_DIR / "cloudcore_peer_ed25519.pub"
WG_PRIVKEY = KEYS_DIR / "wg_private.key"
WG_PUBKEY = KEYS_DIR / "wg_public.key"


def ensure_peer_keypair() -> None:
    """Generate this host's pairing identity + WireGuard keypair if
    they don't already exist yet. Safe to call on every API startup."""
    KEYS_DIR.mkdir(exist_ok=True)
    _ensure_ed25519_identity()
    _ensure_wireguard_keypair()


def _ensure_ed25519_identity() -> None:
    if PEER_PRIVKEY.exists():
        return
    subprocess.run(
        ["ssh-keygen", "-t", "ed25519", "-f", str(PEER_PRIVKEY),
         "-N", "", "-C", f"cloudcore-peer@{socket.gethostname()}"],
        check=True, capture_output=True,
    )


def _ensure_wireguard_keypair() -> None:
    if WG_PRIVKEY.exists():
        return
    if shutil.which("wg") is None:
        return
    priv = subprocess.run(["wg", "genkey"], check=True, capture_output=True, text=True).stdout.strip()
    pub = subprocess.run(["wg", "pubkey"], input=priv, check=True, capture_output=True, text=True).stdout.strip()
    WG_PRIVKEY.write_text(priv + "\n")
    WG_PRIVKEY.chmod(0o600)
    WG_PUBKEY.write_text(pub + "\n")


def has_wireguard_keypair() -> bool:
    return WG_PRIVKEY.exists() and WG_PUBKEY.exists()


def peer_pubkey_fingerprint() -> str:
    """SHA256 fingerprint of this host's own pairing public key — the
    only thing ever advertised over mDNS (api/discovery.py), or shown
    to a human approving a pairing request. Identifies this host
    without exposing key material. Delegates to ssh-keygen's own
    fingerprint format/computation rather than reimplementing it."""
    ensure_peer_keypair()
    out = subprocess.run(
        ["ssh-keygen", "-lf", str(PEER_PUBKEY)],
        check=True, capture_output=True, text=True,
    ).stdout
    # "256 SHA256:<b64> <comment> (ED25519)" — the fingerprint token itself.
    return out.split()[1]


def peer_pubkey_text() -> str:
    """This host's own pairing public key, in OpenSSH authorized_keys
    format — the full key material sent in a pairing request/response
    (api/peers_routes.py), not just its fingerprint."""
    ensure_peer_keypair()
    return PEER_PUBKEY.read_text().strip()
