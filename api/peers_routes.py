"""Cross-host peering routes (see the session's cross-host peering plan).

Two trust tiers in this one blueprint:
  - Local-only routes (POST/GET/DELETE /v1/peers, the pairing-requests
    list/approve/reject) — same shared-token `_auth()` every other
    blueprint uses, and only ever reachable via the dashboard-facing
    127.0.0.1:8080 bind in practice (nothing routes the peer-facing
    bind's traffic to them — see PEER_REACHABLE_ENDPOINTS below and
    server.py's before_request gate).
  - The two peer-reachable routes (the pairing-request bootstrap and
    the approval callback) — reachable from another host via
    api/peer_listener.py's second bind, authenticated by very
    different means (a self-signed proof of key possession for the
    bootstrap; a per-pairing bearer token for the callback), never the
    shared dev-token.
"""
from __future__ import annotations

import os
import socket
import secrets
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import quote

from flask import Blueprint, jsonify, request

import compute
import discovery
import host_stats
import identity
import peer_client
import peer_crypto
import peers_store
import settings_store
import wireguard
from models import now_iso

peers_bp = Blueprint("peers", __name__)

API_TOKEN = os.environ.get("CLOUDCORE_API_TOKEN", "dev-token")

# Endpoints reachable via the peer-facing bind (api/peer_listener.py).
# Everything else 403s there regardless of any token presented — see
# server.py's before_request gate. Named here, not in server.py, so
# this file stays the single source of truth for what peers can reach.
PEER_REACHABLE_ENDPOINTS = {
    "peers.pairing_request_bootstrap",
    "peers.complete_pairing",
    # Cross-blueprint exceptions, not this blueprint's own routes: same
    # reasoning as server.py's own list_vpcs/list_subnets addition to
    # _PEER_REACHABLE_LOCAL_ENDPOINTS — sg.list_sgs lets a peer's
    # dashboard populate its peer_security_group_id picker from this
    # host's real catalogue; the create/get/update/delete four let
    # security groups be peer-placed themselves, not just referenced
    # (per direct request — "do we have to limit resource placement to
    # just instances?"). Named here rather than adding a third tiny
    # allowlist set just for one file's own routes.
    "sg.list_sgs", "sg.create_sg", "sg.get_sg", "sg.update_sg", "sg.delete_sg",
    # stats.get_system_stats: read-only host performance stats (CPU/
    # memory/disk/instance count) — lets a peer's dashboard compare this
    # host's current load against its own before deciding where to
    # place a resource (per direct request — "collect performance
    # statistics from each peer in order to understand which is
    # overloaded... and which can tolerate it"). No secrets in the
    # response, no more sensitive than any other read already reachable
    # here.
    "stats.get_system_stats",
}

PAIRING_REQUEST_TTL_MINUTES = 15
MAX_PENDING_PAIRING_REQUESTS = 20
RATE_LIMIT_PER_HOUR = 5

# Source-IP -> recent bootstrap-request timestamps (in-memory, reset on
# restart — deliberately not persisted; this only needs to survive long
# enough to blunt a burst, not forever). Not a generic abuse-prevention
# framework, just this one intentionally-unauthenticated endpoint.
_rate_limit_hits: dict[str, list[float]] = {}


def _auth():
    token = request.headers.get("Authorization", "").removeprefix("Bearer ") \
            or request.args.get("token", "")
    if token != API_TOKEN:
        return jsonify({"status": 401, "title": "Unauthorized"}), 401
    return None


def _check_rate_limit(source_ip: str) -> bool:
    """True if `source_ip` is still under RATE_LIMIT_PER_HOUR bootstrap
    requests in the last hour; also prunes old entries as a side
    effect, so this dict doesn't grow unbounded."""
    now = time.monotonic()
    hits = [t for t in _rate_limit_hits.get(source_ip, []) if now - t < 3600]
    hits.append(now)
    _rate_limit_hits[source_ip] = hits
    return len(hits) <= RATE_LIMIT_PER_HOUR


def _my_wg_info() -> dict:
    """Best-effort WireGuard identity to advertise in a handshake —
    meaningful once Stage 4 actually stands up tunnels, harmless
    (empty pubkey) before then; wireguard-tools not being installed
    yet on this host shouldn't block pairing itself."""
    wg_pubkey = identity.WG_PUBKEY.read_text().strip() if identity.has_wireguard_keypair() else ""
    wg_port = settings_store.get("network.wg_listen_port", 51820)
    return {
        "wg_pubkey": wg_pubkey,
        "wg_endpoint": f"{discovery._local_ip()}:{wg_port}",
        "wg_bridge_subnet": compute.bridge_cidr(),
    }


@peers_bp.get("/v1/peers/discovered")
def list_discovered():
    """One-shot LAN scan for other CloudCore hosts currently
    advertising — feeds the dashboard's "Scan" button."""
    err = _auth()
    if err: return err
    timeout = request.args.get("timeout", default=3.0, type=float)
    timeout = max(0.5, min(timeout, 10.0))
    return jsonify({"items": discovery.browse(timeout=timeout)})


@peers_bp.post("/v1/peers")
def initiate_pairing():
    """Local-only: a human clicked "Pair" against a discovered (or
    manually entered) target. Mints a callback token, signs a pairing
    payload with this host's own identity key, and POSTs it
    unauthenticated to the target's own bootstrap endpoint."""
    err = _auth()
    if err: return err
    body = request.get_json(force=True) or {}
    hostname, address, port = body.get("hostname"), body.get("address"), body.get("port")
    if not (hostname and address and port):
        return jsonify({"status": 400, "title": "Bad Request",
                         "detail": "hostname, address, and port are required"}), 400

    # We don't know the target's own pubkey_fpr yet at this point (only
    # learned once it replies via /v1/peers/complete), so this dedup is
    # by api_url rather than identity — a reasonable proxy for "this is
    # the same physical host" since address:port is what was actually
    # used to reach it. The identity-based check (pubkey_fpr, stronger)
    # lives on the *receiving* side, pairing_request_bootstrap() below,
    # since that's the first point either side actually has it. Found
    # live as a real gap: nothing previously stopped re-pairing with an
    # already-paired host, leaving two fully-approved peers rows for
    # the same physical machine — confusing in the UI (which one do you
    # pick?) and, worse, both ended up in the rendered WireGuard config
    # with the identical PublicKey.
    existing = [p for p in peers_store.list_peers()
                if p["api_url"] == f"http://{address}:{port}" and p["status"] in ("approved", "pending_outbound")]
    if existing:
        return jsonify({"status": 409, "title": "Conflict",
                         "detail": f"Already {existing[0]['status']} with a peer at {address}:{port} "
                                   f"(id {existing[0]['id']}) — revoke it first if you want to re-pair."}), 409

    callback_token = secrets.token_urlsafe(32)
    payload = {
        "hostname": socket.gethostname(),
        "pubkey": identity.peer_pubkey_text(),
        "peer_port": discovery.peer_listener_port(),
        "callback_token": callback_token,
        **_my_wg_info(),
    }
    signature = peer_crypto.sign(payload, identity.PEER_PRIVKEY)
    target_url = f"http://{address}:{port}/v1/peers/pairing-requests"
    try:
        resp = peer_client.post(target_url, {"payload": payload, "signature": signature})
    except peer_client.PeerUnreachable as e:
        return jsonify({"status": 502, "title": "Bad Gateway",
                         "detail": f"Could not reach {target_url}: {e}"}), 502
    if resp.status not in (200, 202):
        return jsonify({"status": 502, "title": "Bad Gateway",
                         "detail": f"Target rejected pairing request: {resp.status} {resp.body}"}), 502

    row = peers_store.insert_peer(
        hostname=hostname, pubkey="", pubkey_fpr="",
        api_url=f"http://{address}:{port}",
        direction="outbound", status="pending_outbound",
        local_token=callback_token, remote_token=None,
        wg_pubkey=None, wg_endpoint=None, wg_bridge_subnet=None, wg_transit_ip=None,
    )
    return jsonify(peers_store.to_public_dict(row)), 201


@peers_bp.post("/v1/peers/pairing-requests")
def pairing_request_bootstrap():
    """Peer-reachable, intentionally unauthenticated bootstrap — this
    is the one route that has to accept a first contact from a host it
    has no prior relationship with. Protected by: a rolling per-source
    rate limit, a cap on total pending requests, and signature
    verification (rules out anything that isn't at least self-
    consistent with its own claimed key — see peer_crypto.py's own
    docstring for exactly what that does and doesn't prove). The real
    trust decision is the human clicking Approve below, not this
    check.
    """
    if not _check_rate_limit(request.remote_addr or "unknown"):
        return jsonify({"status": 429, "title": "Too Many Requests",
                         "detail": "Too many pairing requests from this source — try again later."}), 429

    peers_store.expire_stale_pairing_requests()
    if peers_store.count_pending_pairing_requests() >= MAX_PENDING_PAIRING_REQUESTS:
        return jsonify({"status": 429, "title": "Too Many Requests",
                         "detail": "Too many pending pairing requests on this host right now."}), 429

    body = request.get_json(force=True) or {}
    payload, signature = body.get("payload"), body.get("signature")
    if not isinstance(payload, dict) or not signature:
        return jsonify({"status": 400, "title": "Bad Request",
                         "detail": "payload and signature are required"}), 400
    required = ("hostname", "pubkey", "peer_port", "callback_token")
    if not all(k in payload for k in required):
        return jsonify({"status": 400, "title": "Bad Request",
                         "detail": f"payload missing required field(s): {required}"}), 400

    if not peer_crypto.verify(payload, signature, payload["pubkey"]):
        return jsonify({"status": 400, "title": "Bad Request",
                         "detail": "Signature does not match the claimed public key."}), 400

    pubkey_fpr = peer_crypto.fingerprint_of(payload["pubkey"])

    # Identity-based dedup (stronger than initiate_pairing()'s own
    # api_url-based check above — this is the first point either side
    # actually knows the requester's real fingerprint) — refuse a
    # second approved pairing with a host we already trust, rather
    # than silently ending up with two fully-approved peers rows for
    # the same physical machine.
    already_approved = [p for p in peers_store.list_peers()
                         if p["pubkey_fpr"] == pubkey_fpr and p["status"] == "approved"]
    if already_approved:
        return jsonify({"status": 409, "title": "Conflict",
                         "detail": f"Already paired with this host (peer id {already_approved[0]['id']}) "
                                   f"— revoke it first if you want to re-pair."}), 409

    callback_url = f"http://{request.remote_addr}:{payload['peer_port']}"
    expires_at = (datetime.now(timezone.utc) + timedelta(minutes=PAIRING_REQUEST_TTL_MINUTES)).isoformat()

    row = peers_store.insert_pairing_request(
        hostname=payload["hostname"], pubkey=payload["pubkey"], pubkey_fpr=pubkey_fpr,
        signature=signature, callback_token=payload["callback_token"], callback_url=callback_url,
        wg_pubkey=payload.get("wg_pubkey", ""), wg_endpoint=payload.get("wg_endpoint", ""),
        wg_bridge_subnet=payload.get("wg_bridge_subnet", ""), expires_at=expires_at,
    )
    return jsonify({"request_id": row["id"], "expires_at": row["expires_at"]}), 202


@peers_bp.get("/v1/peers/pairing-requests")
def list_pairing_requests():
    err = _auth()
    if err: return err
    peers_store.expire_stale_pairing_requests()
    status = request.args.get("status")
    rows = peers_store.list_pairing_requests(status=status)
    items = [{k: v for k, v in r.items() if k not in ("callback_token", "signature")} for r in rows]
    return jsonify({"items": items})


@peers_bp.put("/v1/peers/pairing-requests/<request_id>/approve")
def approve_pairing_request(request_id: str):
    """The one explicit human click this whole feature depends on."""
    err = _auth()
    if err: return err
    row = peers_store.get_pairing_request(request_id)
    if row is None:
        return jsonify({"status": 404, "title": "Not Found"}), 404
    if row["status"] != "pending":
        return jsonify({"status": 409, "title": "Conflict",
                         "detail": f"Request is '{row['status']}', not pending."}), 409
    if row["expires_at"] < now_iso():
        peers_store.update_pairing_request(request_id, status="expired")
        return jsonify({"status": 410, "title": "Gone", "detail": "Request expired."}), 410

    local_token = secrets.token_urlsafe(32)
    my_wg = _my_wg_info()
    peer_row = peers_store.insert_peer(
        hostname=row["hostname"], pubkey=row["pubkey"], pubkey_fpr=row["pubkey_fpr"],
        api_url=row["callback_url"], direction="inbound", status="approved",
        local_token=local_token, remote_token=row["callback_token"],
        wg_pubkey=row["wg_pubkey"], wg_endpoint=row["wg_endpoint"],
        wg_bridge_subnet=row["wg_bridge_subnet"], wg_transit_ip=None,
        approved_at=now_iso(),
    )
    wg_result = wireguard.on_peer_approved(peer_row, peers_store.list_peers(status="approved"))
    peer_row = peers_store.update_peer(peer_row["id"], **wg_result)

    callback_delivered = False
    callback_error = None
    try:
        resp = peer_client.post(
            row["callback_url"] + "/v1/peers/complete",
            {"pubkey": identity.peer_pubkey_text(), "token_for_you": local_token, **my_wg},
            token=row["callback_token"],
        )
        callback_delivered = resp.status == 200
        if not callback_delivered:
            callback_error = f"{resp.status} {resp.body}"
    except peer_client.PeerUnreachable as e:
        callback_error = str(e)

    peers_store.update_pairing_request(request_id, status="approved")
    result = peers_store.to_public_dict(peer_row)
    result["callback_delivered"] = callback_delivered
    if callback_error:
        # Not fatal to the approval itself — the human already made
        # the trust decision — but the UI needs to know the other side
        # may not yet know it's paired (e.g. it went offline between
        # the request and the approval).
        result["callback_error"] = callback_error
    return jsonify(result)


@peers_bp.put("/v1/peers/pairing-requests/<request_id>/reject")
def reject_pairing_request(request_id: str):
    err = _auth()
    if err: return err
    row = peers_store.get_pairing_request(request_id)
    if row is None:
        return jsonify({"status": 404, "title": "Not Found"}), 404
    if row["status"] != "pending":
        return jsonify({"status": 409, "title": "Conflict",
                         "detail": f"Request is '{row['status']}', not pending."}), 409
    peers_store.update_pairing_request(request_id, status="rejected")
    return jsonify({"status": "rejected"})


@peers_bp.post("/v1/peers/complete")
def complete_pairing():
    """Peer-reachable: the target host calling back to tell us they
    approved our earlier POST /v1/peers. Authenticated by the
    callback_token WE minted and sent them (not the shared dev-token,
    and not the normal _auth() check) — presenting it back is exactly
    what proves this really is a reply to our own request."""
    token = request.headers.get("Authorization", "").removeprefix("Bearer ")
    row = peers_store.find_pending_outbound_by_local_token(token) if token else None
    if row is None:
        return jsonify({"status": 401, "title": "Unauthorized"}), 401

    body = request.get_json(force=True) or {}
    if not (body.get("pubkey") and body.get("token_for_you")):
        return jsonify({"status": 400, "title": "Bad Request",
                         "detail": "pubkey and token_for_you are required"}), 400

    pubkey_fpr = peer_crypto.fingerprint_of(body["pubkey"])
    peer_row = peers_store.update_peer(
        row["id"], status="approved", pubkey=body["pubkey"], pubkey_fpr=pubkey_fpr,
        remote_token=body["token_for_you"],
        wg_pubkey=body.get("wg_pubkey", ""), wg_endpoint=body.get("wg_endpoint", ""),
        wg_bridge_subnet=body.get("wg_bridge_subnet", ""), approved_at=now_iso(),
    )
    wg_result = wireguard.on_peer_approved(peer_row, peers_store.list_peers(status="approved"))
    peers_store.update_peer(peer_row["id"], **wg_result)
    return jsonify({})


@peers_bp.get("/v1/peers")
def list_peers():
    err = _auth()
    if err: return err
    status = request.args.get("status")
    rows = peers_store.list_peers(status=status)
    return jsonify({"items": [peers_store.to_public_dict(r) for r in rows]})


@peers_bp.delete("/v1/peers/<peer_id>")
def revoke_peer(peer_id: str):
    err = _auth()
    if err: return err
    row = peers_store.get_peer(peer_id)
    if row is None:
        return jsonify({"status": 404, "title": "Not Found"}), 404
    peers_store.revoke_peer(peer_id)
    wireguard.on_peer_revoked(peers_store.list_peers(status="approved"))
    return jsonify({"status": "revoked"})


def _peer_proxy_get(peer_id: str, path: str):
    """GET `path` against `peer_id`'s own API using its stored remote_token.
    Returns (PeerResponse, None) on success or (None, (jsonify(...), status))
    on failure — lets each route below `return error` directly rather than
    duplicating the not-approved/unreachable handling three times over."""
    peer = peers_store.get_peer(peer_id)
    if not peer or peer["status"] != "approved":
        return None, (jsonify({"status": 400, "title": "Bad Request",
                                "detail": f"'{peer_id}' is not an approved peer"}), 400)
    try:
        resp = peer_client.get(peer["api_url"] + path, token=peer["remote_token"])
    except peer_client.PeerUnreachable as e:
        return None, (jsonify({"status": 502, "title": "Bad Gateway",
                                "detail": f"Could not reach peer '{peer['hostname']}': {e}"}), 502)
    return resp, None


# The three routes below back the Build Manager's cascading vpc/subnet/
# security-group pickers for a peer-placed resource (per direct request:
# "checking the current vpc id's on the localhost and connected peers"
# rather than expecting a user to already know an id that exists on a
# host they've never directly browsed) — plain reads proxied through to
# the peer's own catalogue, same trust boundary as every other peer-proxy
# call in this file (this host's own stored remote_token, never anything
# the browser supplies).

@peers_bp.get("/v1/peers/<peer_id>/vpcs")
def list_peer_vpcs(peer_id: str):
    err = _auth()
    if err: return err
    resp, error = _peer_proxy_get(peer_id, "/v1/vpcs")
    if error: return error
    return jsonify(resp.body), resp.status


@peers_bp.get("/v1/peers/<peer_id>/subnets")
def list_peer_subnets(peer_id: str):
    err = _auth()
    if err: return err
    vpc_id = request.args.get("vpc_id", "")
    path = "/v1/subnets" + (f"?vpc_id={quote(vpc_id)}" if vpc_id else "")
    resp, error = _peer_proxy_get(peer_id, path)
    if error: return error
    return jsonify(resp.body), resp.status


@peers_bp.get("/v1/peers/<peer_id>/security-groups")
def list_peer_security_groups(peer_id: str):
    err = _auth()
    if err: return err
    resp, error = _peer_proxy_get(peer_id, "/v1/security-groups")
    if error: return error
    return jsonify(resp.body), resp.status


@peers_bp.get("/v1/peers/<peer_id>/stats")
def get_peer_stats(peer_id: str):
    """This peer's own current CPU/memory/disk/instance-count stats —
    backs the Resource Placement page's capacity comparison (per direct
    request: "collect performance statistics from each peer in order to
    understand which is overloaded... and which can tolerate it")."""
    err = _auth()
    if err: return err
    resp, error = _peer_proxy_get(peer_id, "/v1/system/stats")
    if error: return error
    return jsonify(resp.body), resp.status


@peers_bp.get("/v1/peers/recommend-placement")
def recommend_placement():
    """Automatic placement recommendation across this host and every
    approved peer, using the same traffic-light verdict the dashboard's
    Capacity card already shows — per direct request: "we need...to be
    able to determine for ourselves (automatically), where to place
    resource based on our traffic light system... we ought to autofill
    the location for a resource based on our traffic lights." This only
    ever suggests a default; the Build Manager's peer_id picker stays a
    normal editable dropdown, so a user can always override it — "we
    still need to allow the user to override via the template."

    Best = lowest verdict severity (active < pending < error), tied
    broken by lowest CPU load_pct_1m. A peer that can't be reached right
    now is simply not a candidate — this host itself always is, so
    there's always at least one result unless something is badly wrong
    with this host's own stats collection."""
    err = _auth()
    if err: return err

    candidates = []
    try:
        local_stats = host_stats.collect()
        candidates.append({
            "peer_id": None, "hostname": "This host (local)",
            "verdict": host_stats.verdict(local_stats), "stats": local_stats,
        })
    except Exception:
        # Don't let a recommendation-only endpoint fail the whole picker
        # over this host's own stats being briefly unavailable — a peer
        # candidate (if any) can still stand in below.
        pass

    for peer in peers_store.list_peers(status="approved"):
        resp, error = _peer_proxy_get(peer["id"], "/v1/system/stats")
        if error or resp.status != 200:
            continue
        candidates.append({
            "peer_id": peer["id"], "hostname": peer["hostname"],
            "verdict": host_stats.verdict(resp.body), "stats": resp.body,
        })

    if not candidates:
        return jsonify({"recommended": None, "hosts": []})

    best = min(candidates, key=lambda c: (
        host_stats.TIER_SEVERITY[c["verdict"]], c["stats"]["cpu"]["load_pct_1m"],
    ))

    return jsonify({
        "recommended": {"peer_id": best["peer_id"], "hostname": best["hostname"], "verdict": best["verdict"]},
        "hosts": [{"peer_id": c["peer_id"], "hostname": c["hostname"], "verdict": c["verdict"]} for c in candidates],
    })
