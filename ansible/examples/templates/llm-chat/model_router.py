"""Send each model request to the endpoint expected to answer fastest now (F-236).

Placement (llm-chat-placement-Phased-Implementation.md) put the lab's own 14B on
the host a one-off benchmark said was good enough. Run time is what counts: a
host that is also someone's desktop can run several times slower than its
benchmark while it's in use, and a quiet one can be faster than expected. So
every request goes to the capable endpoint with the best expected time now:

  - capable: serves our own model (the quality floor, placement C2);
  - free: no slot busy right now (llama-server's /slots);
  - expected time: the request's size over that endpoint's live speeds --
    prompt and generation tokens/s, smoothed over its recent answers, from the
    timings llama-server returns with every completion.

An endpoint not measured yet is tried as soon as it's free, which measures it.
Speeds live in a small shared file, so verify-proxy and the LFS worker both
learn from every answer. Standard library only.
"""

from __future__ import annotations

import fcntl
import json
import os
import time
import urllib.error
import urllib.request
from pathlib import Path

STATE = Path(os.environ.get("MODEL_ROUTER_STATE", "/var/lib/model-router/speeds.json"))
ALPHA = 0.3            # weight of the newest answer in the smoothed speed
STALE_S = 3600         # older than this, a speed is replaced rather than blended
_capable: dict[str, tuple[float, bool]] = {}


def capable(url: str, model_file: str) -> bool:
    """Serves our model (checked via /props, remembered for 5 minutes)."""
    if not model_file:
        return True
    hit = _capable.get(url)
    if hit and time.monotonic() - hit[0] < 300:
        return hit[1]
    ok = False
    try:
        with urllib.request.urlopen(url + "/props", timeout=5) as r:
            p = json.loads(r.read())
        ok = os.path.basename(str(p.get("model_path") or "")) == model_file
    except (urllib.error.URLError, ValueError, TimeoutError, OSError):
        ok = False
    _capable[url] = (time.monotonic(), ok)
    return ok


def free(url: str) -> bool:
    try:
        with urllib.request.urlopen(url + "/slots", timeout=5) as r:
            slots = json.loads(r.read())
        return isinstance(slots, list) and any(not s.get("is_processing") for s in slots if isinstance(s, dict))
    except (urllib.error.URLError, ValueError, TimeoutError, OSError):
        return False


def _load() -> dict:
    try:
        return json.loads(STATE.read_text())
    except (OSError, ValueError):
        return {}


def record(url: str, timings: dict | None) -> None:
    """Blend one answer's measured speeds into the endpoint's."""
    if not timings:
        return
    pp, gen = timings.get("prompt_per_second"), timings.get("predicted_per_second")
    if not gen or gen <= 0:
        return
    STATE.parent.mkdir(parents=True, exist_ok=True)
    with open(STATE.with_suffix(".lock"), "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        data = _load()
        old = data.get(url)
        fresh = not old or time.time() - old.get("at", 0) > STALE_S
        def blend(key: str, new: float | None) -> float | None:
            if not new or new <= 0:
                return (old or {}).get(key)
            return new if fresh or not old.get(key) else (1 - ALPHA) * old[key] + ALPHA * new
        data[url] = {"prompt_tps": blend("prompt_tps", pp), "gen_tps": blend("gen_tps", gen),
                     "at": time.time(), "n": (0 if fresh else old.get("n", 0)) + 1}
        tmp = STATE.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=1))
        tmp.replace(STATE)


def expected_seconds(url: str, prompt_chars: int, max_tokens: int) -> float | None:
    """None if not measured yet. Output is assumed to be about half the cap."""
    s = _load().get(url)
    if not s or not s.get("gen_tps"):
        return None
    prompt_tokens = prompt_chars / 3.5
    return prompt_tokens / (s.get("prompt_tps") or s["gen_tps"] * 2) + (max_tokens / 2) / s["gen_tps"]


def order(urls: list[str], prompt_chars: int, max_tokens: int) -> list[str]:
    """Free endpoints first -- unmeasured ones (to measure them), then by expected
    time -- then the busy ones by expected time, as a last resort."""
    def key(u: str) -> tuple:
        e = expected_seconds(u, prompt_chars, max_tokens)
        return (e is not None, e or 0.0)
    free_now = [u for u in urls if free(u)]
    busy = [u for u in urls if u not in free_now]
    return sorted(free_now, key=key) + sorted(busy, key=key)


def speeds() -> dict:
    """What the router currently believes, for logs and the journal."""
    return _load()
