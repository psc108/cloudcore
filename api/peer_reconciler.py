"""Auto-reconcile an approved peer's stored network address after it
changes (e.g. a DHCP/WiFi-driven IP change) — direct follow-up to a
real incident: a network outage changed a paired host's IP, and both
sides' stored api_url/wg_endpoint went stale, breaking the
relationship until a human manually revoked and re-paired on both
machines (each side's own pairing_request_bootstrap() identity-based
dedup rejected a fresh pairing attempt, since each still had an
"approved" row for the other at the old address).

mDNS discovery (discovery.browse()) already finds a peer's current
address correctly, by identity, even after an IP change — that
machinery already works. This module is what actually *uses* it to
self-heal an already-approved relationship, instead of requiring a
human to notice and intervene.

Background-loop shape mirrors scheduler.py's own precedent exactly
(the closest "wake up periodically and do work" shape in this
codebase) — a separate module, not folded into scheduler.py itself,
since this is a distinct concern on its own interval, gated by a
different setting (discovery.enabled, not tied to any schedule a human
configured).

Security note, read before touching _reconcile_peer(): mDNS's own
advertised fpr is unauthenticated wire data — anyone on the LAN could
broadcast a fake record claiming to be a peer's identity. It is used
here ONLY as a cheap pre-filter for which address to even try calling,
never as the trust decision. The actual trust decision is verifying a
*signed* response from GET /v1/peers/self-info against the STORED
peer row's own pubkey_fpr — never against whatever fpr mDNS claimed.
Getting this order wrong (trusting mDNS's fpr first) would mean
sending this host's own credentials toward an address nobody has
proven is really the peer -- self_info deliberately needs no bearer
token precisely so there is nothing to leak here even before that
verification happens.
"""
from __future__ import annotations

import threading
import time

import discovery
import peer_client
import peer_crypto
import peers_store
import settings_store
import wireguard

TICK_INTERVAL_S = 60

# peer_id -> monotonic time before which reconciliation attempts for
# that peer are skipped. In-memory, reset on restart -- same
# "doesn't need to survive forever, just blunt repeated failures"
# precedent as peers_routes.py's own _rate_limit_hits. Without this, a
# peer with a flapping DHCP lease would trigger a full WireGuard
# re-render+reload (wireguard.apply()) on every single tick.
_backoff: dict[str, float] = {}
_BACKOFF_BASE_S = 30
_BACKOFF_MAX_S = 30 * 60


def _should_attempt(peer_id: str) -> bool:
    return time.monotonic() >= _backoff.get(peer_id, 0)


def _record_failure(peer_id: str, attempt: int) -> None:
    delay = min(_BACKOFF_MAX_S, _BACKOFF_BASE_S * (2 ** attempt))
    _backoff[peer_id] = time.monotonic() + delay


def _record_success(peer_id: str) -> None:
    _backoff.pop(peer_id, None)


def start() -> None:
    threading.Thread(target=_loop, daemon=True).start()


def _loop() -> None:
    while True:
        try:
            # Same opt-in gate discovery.advertise()/browse() already
            # live behind -- a host that never turned on peering stays
            # completely inert here too, not just silent on the wire.
            if settings_store.get("discovery.enabled", False):
                _tick()
        except Exception as e:
            print(f"[peer_reconciler] tick failed: {e}", flush=True)
        time.sleep(TICK_INTERVAL_S)


def _tick() -> None:
    found = discovery.browse()
    by_fpr = {f["pubkey_fpr"]: f for f in found if f["pubkey_fpr"]}
    for peer in peers_store.list_peers(status="approved"):
        hit = by_fpr.get(peer["pubkey_fpr"])
        if not hit:
            continue
        new_api_url = f"http://{hit['address']}:{hit['port']}"
        if new_api_url == peer["api_url"]:
            continue
        if not _should_attempt(peer["id"]):
            continue
        threading.Thread(target=_reconcile_peer, args=(peer["id"], hit), daemon=True).start()


def _reconcile_peer(peer_id: str, hit: dict) -> None:
    """`hit` is only ever a lead ("try this address") -- every fact
    this function actually acts on comes from the signed response
    verified below, checked against the STORED peer row fetched fresh
    here (not whatever was true when _tick() built `hit`)."""
    peer = peers_store.get_peer(peer_id)
    if not peer or peer["status"] != "approved":
        return  # revoked/changed under us since _tick() scanned it

    new_api_url = f"http://{hit['address']}:{hit['port']}"
    attempt = 0
    try:
        try:
            resp = peer_client.get(f"{new_api_url}/v1/peers/self-info")
        except peer_client.PeerUnreachable:
            attempt = 1
            _record_failure(peer_id, attempt)
            return
        if resp.status != 200:
            attempt = 1
            _record_failure(peer_id, attempt)
            return

        body = resp.body or {}
        payload, signature = body.get("payload"), body.get("signature")
        if not isinstance(payload, dict) or not signature:
            print(f"[peer_reconciler] {peer['hostname']}: malformed self-info response, skipping", flush=True)
            _record_failure(peer_id, 2)
            return

        if not peer_crypto.verify(payload, signature, payload.get("pubkey", "")):
            print(f"[peer_reconciler] {peer['hostname']}: self-info signature did not verify "
                  f"at {new_api_url} -- NOT updating, NOT trusting this address", flush=True)
            _record_failure(peer_id, 3)
            return

        claimed_fpr = peer_crypto.fingerprint_of(payload["pubkey"])
        if claimed_fpr != peer["pubkey_fpr"]:
            # The mDNS fpr got us to try this address, but the
            # cryptographically-proven identity disagrees with what we
            # actually have on file for this peer_id -- exactly the
            # spoofing case this whole design exists to catch. Refuse,
            # loudly, and don't keep retrying this address.
            print(f"[peer_reconciler] {peer['hostname']}: signed identity at {new_api_url} "
                  f"({claimed_fpr}) does not match the stored peer record ({peer['pubkey_fpr']}) "
                  f"-- refusing to update, this may be a spoofed address", flush=True)
            _record_failure(peer_id, 6)  # long backoff -- this isn't going to resolve itself
            return

        updated = peers_store.update_peer(
            peer_id,
            api_url=new_api_url,
            wg_pubkey=payload.get("wg_pubkey") or peer["wg_pubkey"],
            wg_endpoint=payload.get("wg_endpoint") or peer["wg_endpoint"],
            wg_bridge_subnet=payload.get("wg_bridge_subnet") or peer["wg_bridge_subnet"],
        )
        print(f"[peer_reconciler] {peer['hostname']}: address confirmed and updated "
              f"{peer['api_url']} -> {new_api_url}", flush=True)

        # Re-render + hot-reload the tunnel with the new endpoint, then
        # the same bounded handshake poll approval already does --
        # nothing else in this codebase ever refreshes wg_tunnel_status
        # outside that path, so skipping it here would leave the
        # dashboard showing whatever it said before the address changed
        # right after the self-heal that was supposed to fix it.
        wg_result = wireguard.on_peer_approved(updated, peers_store.list_peers(status="approved"))
        peers_store.update_peer(peer_id, **wg_result)
        _record_success(peer_id)
    except Exception as e:
        print(f"[peer_reconciler] {peer['hostname']}: reconciliation failed: {e}", flush=True)
        _record_failure(peer_id, attempt + 1)
