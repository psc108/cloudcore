"""Dynamic coordinator/worker layer split for llama.cpp RPC templates
(llm-chat, distributed-llm) — per direct request: "can we make it a
dynamic calculation and update the template accordingly for each run so
we constantly adjust resource allocation for wherever it might do the
best?" Recomputed fresh at every build submission from each
participant's real current CPU core count and load (the same
host_stats.py numbers the Peers/Capacity traffic-light already uses),
not a static "roughly even split" tuned once and left to go stale as
machines get busier or new peers join — found needed after tuning
rpc_offload_layers by hand three separate times this session (Mistral-
7B, Qwen2.5-Coder-7B, then -14B) and realizing stourport running busy
(80% CPU, 4 cores) alongside an idle Llywyn-Y-Groes (8 cores) makes a
50/50 split actively the wrong call, not just an approximation.

Called from both Build Managers' own submit routes (tofu_routes.py /
build_manager_routes.py), right after capacity_gate's own RAM check —
only ever fills in rpc_offload_layers when the caller didn't already
set it explicitly, same "auto-fill, never override an explicit choice"
convention as peers_routes.recommend_placement().

Memory-aware, not just CPU-aware — found live the hard way: an early,
CPU-only version of this pushed 47 of 48 layers (~8.2GB of an 8.37GB
Q4_K_M model) onto a worker's own standard.xlarge (8192MB) flavor,
leaving almost no room for KV cache, compute buffers, or the OS inside
that VM — its own ggml-rpc-server crashed under real memory pressure
mid-transfer, identically, on every single restart
(`ggml-rpc.cpp:569: Remote RPC server crashed or returned malformed
response`, confirmed via the real guest's own journalctl). CPU load
alone says nothing about whether a given split actually fits in either
side's own RAM ceiling, so this now clamps the CPU-derived split
against each participant's real flavor RAM before ever returning it.
"""
from __future__ import annotations

from pathlib import Path

import compute
import gguf_meta
import host_stats
import peers_routes

_ARTIFACTS_DIR = Path(__file__).parent / "package-repo" / "jammy" / "artifacts"

# A host pegged at 100% CPU still gets a small nonzero share rather than
# being treated as having none at all — a temporary spike (e.g. this
# same build's own coordinator warming up) shouldn't be read as "this
# host can never do any compute," just "weight it much lower right now."
_MIN_FREE_FRACTION = 0.05

# Fraction of a flavor's own RAM ceiling that model WEIGHTS are allowed
# to occupy — the remainder is reserved for KV cache, compute buffers,
# and the OS/systemd/python overhead already running inside that VM.
# 0.75 is a reasoned estimate (this template's own context_size default,
# 4096, keeps llama-server's own KV cache in the low hundreds of MB for
# a model this size, not multiple GB) rather than an empirically
# measured constant — err toward leaving more headroom, not less, if
# this ever needs revisiting after a real measurement.
_SAFE_WEIGHT_FRACTION = 0.75


def _effective_capacity(stats: dict) -> float:
    cores = stats["cpu"]["cores"]
    load_pct = stats["cpu"]["load_pct_1m"]
    free_fraction = max(_MIN_FREE_FRACTION, 1 - load_pct / 100)
    return cores * free_fraction


def _flavor_ram_mb(flavor_name: str | None) -> int | None:
    if not flavor_name:
        return None
    flavor = compute.FLAVORS.get(flavor_name)
    return flavor[1] if flavor else None


def compute_offload_layers(model_path: Path, coordinator_stats: dict,
                            worker_stats_list: list[dict],
                            coordinator_flavor_mb: int | None = None,
                            worker_flavor_mb: int | None = None) -> int | None:
    """Returns the number of the model's own real transformer layers
    (read from the GGUF file itself, not a hand-maintained table) to
    hand to the worker(s) combined via -ngl, weighted by each
    participant's current effective capacity (cores * headroom this
    exact moment), then clamped so neither side's own share of the
    model's real weight bytes exceeds a safe fraction of its flavor's
    RAM (when both flavor sizes are known) — or None if it can't be
    computed at all (unreadable GGUF, no worker stats) or no split
    fits safely on either side no matter how it's divided, in which
    case the caller should leave the template's own static default in
    place rather than assert a computed-looking value that's still
    dangerous."""
    total_layers = gguf_meta.block_count(model_path)
    if not total_layers or total_layers < 2:
        return None
    if not worker_stats_list:
        return None

    coordinator_cap = _effective_capacity(coordinator_stats)
    worker_cap = sum(_effective_capacity(s) for s in worker_stats_list)
    total_cap = coordinator_cap + worker_cap
    if total_cap <= 0:
        return None

    worker_share = worker_cap / total_cap
    layers = round(total_layers * worker_share)
    # Never 0 (a configured worker doing literally nothing defeats the
    # point of having it) and never the full model (the coordinator
    # must still hold at least one layer plus the embedding/output
    # tensors it always keeps regardless of -ngl).
    layers = max(1, min(total_layers - 1, layers))

    if coordinator_flavor_mb and worker_flavor_mb:
        model_bytes = model_path.stat().st_size
        bytes_per_layer = model_bytes / total_layers
        mb = 1024 * 1024
        max_worker_layers = int((worker_flavor_mb * _SAFE_WEIGHT_FRACTION * mb) / bytes_per_layer)
        max_coordinator_layers = int((coordinator_flavor_mb * _SAFE_WEIGHT_FRACTION * mb) / bytes_per_layer)
        min_worker_layers = total_layers - max_coordinator_layers
        if min_worker_layers > max_worker_layers:
            # The model's real weight bytes don't fit safely across
            # these two flavors no matter how the layers are divided —
            # don't hand back a value that merely looks deliberate.
            return None
        layers = max(min(layers, max_worker_layers), min_worker_layers)
        layers = max(1, min(total_layers - 1, layers))

    return layers


def maybe_apply(var_overrides: dict, schema: dict) -> None:
    """Mutates var_overrides in place with a freshly-computed
    rpc_offload_layers, if and only if: this template actually has
    both a worker_peers and rpc_offload_layers variable (detected
    generically from schema, same convention as capacity_gate.py — not
    hardcoded to one template by name), the caller didn't already set
    rpc_offload_layers explicitly, the chosen model_filename's GGUF is
    readable locally, and every named worker peer's stats are
    reachable. Silently leaves the template's own static default
    variable in place otherwise — this is a best-effort optimization,
    never something that should block or fail a build."""
    if "worker_peers" not in schema or "rpc_offload_layers" not in schema:
        return
    if str(var_overrides.get("rpc_offload_layers", "")).strip():
        return  # caller already chose one explicitly — never override that

    worker_peers = var_overrides.get("worker_peers")
    if not worker_peers or not isinstance(worker_peers, list):
        return

    model_filename = var_overrides.get("model_filename") \
        or (schema.get("model_filename", {}) or {}).get("default")
    if not model_filename:
        return
    model_path = _ARTIFACTS_DIR / model_filename
    if not model_path.is_file():
        return

    worker_stats_list = []
    for entry in worker_peers:
        peer_id = entry.get("peer_id") if isinstance(entry, dict) else None
        if not peer_id:
            return  # can't compute a partial split safely — leave the default
        stats = peers_routes.peer_stats(peer_id)
        if stats is None:
            return
        worker_stats_list.append(stats)

    try:
        coordinator_stats = host_stats.collect()
    except Exception:
        return

    coordinator_flavor = var_overrides.get("coordinator_flavor") \
        or (schema.get("coordinator_flavor", {}) or {}).get("default")
    worker_flavor = var_overrides.get("worker_flavor") \
        or (schema.get("worker_flavor", {}) or {}).get("default")

    layers = compute_offload_layers(
        model_path, coordinator_stats, worker_stats_list,
        coordinator_flavor_mb=_flavor_ram_mb(coordinator_flavor),
        worker_flavor_mb=_flavor_ram_mb(worker_flavor),
    )
    if layers is not None:
        var_overrides["rpc_offload_layers"] = layers
