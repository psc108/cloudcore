"""Signing/verification for the cross-host pairing handshake
(api/peers_routes.py), built entirely on `ssh-keygen -Y sign`/`-Y
verify` — OpenSSH's own signature format — rather than adding a Python
crypto dependency (cryptography/pynacl). Matches this codebase's
existing convention of shelling out to ssh-keygen for every other
cryptographic operation (api/identity.py, the original SSH keypair).

Verification here proves "this signature could only have been produced
by the private key matching the given public key" — a self-consistency
proof, not identity-authority verification (there is no CA / trust
store; nothing here establishes that the claimed hostname is who they
say they are). That's intentional: it's exactly the TOFU-with-a-click
model this feature was built around — the human clicking Approve on
the *target* machine is the actual trust decision, not this check.
This check only rules out a request whose signature doesn't match its
own claimed key, i.e. cheap, un-signed spam.
"""
from __future__ import annotations

import json
import subprocess
import tempfile
from pathlib import Path

NAMESPACE = "cloudcore-pairing"


def canonical_json(payload: dict) -> bytes:
    return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()


def sign(payload: dict, privkey_path: Path) -> str:
    """Sign payload with the given private key, returning the ASCII
    "-----BEGIN SSH SIGNATURE-----" block as a string."""
    with tempfile.TemporaryDirectory() as td:
        data_file = Path(td) / "payload"
        data_file.write_bytes(canonical_json(payload))
        subprocess.run(
            ["ssh-keygen", "-Y", "sign", "-f", str(privkey_path), "-n", NAMESPACE, str(data_file)],
            check=True, capture_output=True,
        )
        return (data_file.with_suffix(".sig")).read_text()


def verify(payload: dict, signature: str, pubkey_text: str) -> bool:
    """True iff `signature` is a valid cloudcore-pairing-namespace
    signature over payload's canonical form, made by the private key
    matching pubkey_text. Never raises — a malformed/forged signature
    is just "not valid", not an exceptional condition worth crashing a
    request over."""
    try:
        with tempfile.TemporaryDirectory() as td:
            data_file = Path(td) / "payload"
            data_file.write_bytes(canonical_json(payload))
            sig_file = Path(td) / "payload.sig"
            sig_file.write_text(signature)
            allowed_signers = Path(td) / "allowed_signers"
            allowed_signers.write_text(f"pairing-peer {pubkey_text}\n")
            result = subprocess.run(
                ["ssh-keygen", "-Y", "verify", "-f", str(allowed_signers),
                 "-I", "pairing-peer", "-n", NAMESPACE, "-s", str(sig_file)],
                input=data_file.read_bytes(), capture_output=True,
            )
            return result.returncode == 0
    except Exception:
        return False


def fingerprint_of(pubkey_text: str) -> str:
    """SHA256 fingerprint of an arbitrary (not necessarily this host's
    own) OpenSSH public key — used to compute the fingerprint of a
    pairing counterparty's claimed key, same format as
    identity.peer_pubkey_fingerprint()."""
    with tempfile.TemporaryDirectory() as td:
        pub_file = Path(td) / "key.pub"
        pub_file.write_text(pubkey_text.strip() + "\n")
        out = subprocess.run(
            ["ssh-keygen", "-lf", str(pub_file)],
            check=True, capture_output=True, text=True,
        ).stdout
        return out.split()[1]
