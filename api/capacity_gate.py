"""Pre-flight capacity check for peer-placed worker instances (today:
examples/llm-chat, examples/distributed-llm — any template whose
var_overrides carry a worker_peers list plus a worker_flavor key) —
per direct request: "...allow the use of a large model with a larger
vm (if the peer can afford the resources)." Runs BEFORE a build is
even submitted (tofu_routes.py/build_manager_routes.py's own POST
/builds), so an under-resourced peer is rejected with a clear message
instead of either (a) silently OOM-killing that peer's own worker
process partway through model load, or (b) tying up several minutes
building a cluster that was never going to come up healthy.

Reuses the exact peers_routes.peer_stats() machinery the Peers page's
own traffic-light/recommend-placement system already relies on — no
new concept, just a real RAM-vs-flavor threshold check ahead of time
instead of after.
"""
from __future__ import annotations

import compute
import peers_routes

# Real weights alone aren't the whole story — llama.cpp/ggml also needs
# room for the KV cache, compute buffers, and the RPC layer's own
# serialization buffers for whatever share of the model that peer ends
# up holding. 20% headroom over the flavor's own advertised RAM is a
# conservative-but-not-paranoid margin — a too-tight allocation fails
# outright rather than degrading gracefully (see rpc_offload_layers'
# own comment in examples/llm-chat/variables.tf for a real example of
# that failure mode).
HEADROOM_FACTOR = 1.2


def check_worker_peers(var_overrides: dict, schema: dict) -> str | None:
    """Returns None if every worker peer referenced in var_overrides can
    afford the requested worker_flavor, or a human-readable error string
    naming the first peer that can't. `schema` is this template's own
    extract_template_vars() output, used only to resolve worker_flavor's
    configured default when the caller didn't override it — mirrors how
    tofu_routes.py/build_manager_routes.py already resolve required-var
    defaults from that same schema."""
    worker_peers = var_overrides.get("worker_peers")
    if not worker_peers or not isinstance(worker_peers, list):
        return None

    flavor_name = var_overrides.get("worker_flavor") \
        or (schema.get("worker_flavor", {}) or {}).get("default")
    if not flavor_name:
        return None
    flavor = compute.FLAVORS.get(flavor_name)
    if flavor is None:
        return None  # an unknown flavor name is caught later, at apply time

    _, flavor_mb, _ = flavor
    required_mb = flavor_mb * HEADROOM_FACTOR

    for entry in worker_peers:
        peer_id = entry.get("peer_id") if isinstance(entry, dict) else None
        if not peer_id:
            continue
        stats = peers_routes.peer_stats(peer_id)
        if stats is None:
            return (f"Peer '{peer_id}' is unreachable right now — can't confirm it has "
                     f"room for {flavor_name} (~{required_mb:.0f}MB required incl. headroom).")
        available_mb = stats.get("memory", {}).get("available_mb", 0)
        if available_mb < required_mb:
            return (f"Peer '{peer_id}' only has {available_mb}MB RAM available — "
                     f"{flavor_name} needs ~{required_mb:.0f}MB (incl. "
                     f"{int((HEADROOM_FACTOR - 1) * 100)}% headroom). Choose a smaller "
                     f"flavor or a less-loaded peer.")
    return None


def check_coordinator_peer(var_overrides: dict, schema: dict) -> str | None:
    """Same check as check_worker_peers(), applied to a single peer-placed
    coordinator instead of a list of workers — added alongside
    examples/llm-chat's coordinator_peer_id/coordinator_flavor variables.
    Returns None if coordinator_peer_id is unset (today's default: stays
    local, no peer RAM to check) or if the named peer can afford
    coordinator_flavor; otherwise a human-readable rejection string."""
    peer_id = var_overrides.get("coordinator_peer_id")
    if not peer_id:
        return None

    flavor_name = var_overrides.get("coordinator_flavor") \
        or (schema.get("coordinator_flavor", {}) or {}).get("default")
    if not flavor_name:
        return None
    flavor = compute.FLAVORS.get(flavor_name)
    if flavor is None:
        return None  # an unknown flavor name is caught later, at apply time

    _, flavor_mb, _ = flavor
    required_mb = flavor_mb * HEADROOM_FACTOR

    stats = peers_routes.peer_stats(peer_id)
    if stats is None:
        return (f"Peer '{peer_id}' is unreachable right now — can't confirm it has "
                 f"room for the coordinator's {flavor_name} (~{required_mb:.0f}MB "
                 f"required incl. headroom).")
    available_mb = stats.get("memory", {}).get("available_mb", 0)
    if available_mb < required_mb:
        return (f"Peer '{peer_id}' only has {available_mb}MB RAM available — the "
                 f"coordinator's {flavor_name} needs ~{required_mb:.0f}MB (incl. "
                 f"{int((HEADROOM_FACTOR - 1) * 100)}% headroom). Choose a smaller "
                 f"flavor or a less-loaded peer.")
    return None


def check_no_coordinator_worker_overlap(var_overrides: dict) -> str | None:
    """Rejects with a clear message if coordinator_peer_id names the same
    peer as any worker_peers[].peer_id — landing both roles on the same
    machine silently defeats the entire reason examples/llm-chat splits
    across hosts via RPC in the first place. Cheap, so checked
    unconditionally alongside the RAM checks above, before a build is
    ever submitted."""
    coordinator_peer_id = var_overrides.get("coordinator_peer_id")
    if not coordinator_peer_id:
        return None
    worker_peers = var_overrides.get("worker_peers")
    if not worker_peers or not isinstance(worker_peers, list):
        return None
    for entry in worker_peers:
        peer_id = entry.get("peer_id") if isinstance(entry, dict) else None
        if peer_id and peer_id == coordinator_peer_id:
            return (f"coordinator_peer_id can't match a worker_peers entry "
                     f"(peer '{peer_id}') — the coordinator and its workers must be "
                     f"on different machines for RPC splitting to do anything useful.")
    return None
