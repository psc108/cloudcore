#!/usr/bin/env python3
"""Measure how fast this host runs a given LLM (llm-chat-placement-Phased-
Implementation.md, C3), so placement can choose the best machine by measured
speed rather than core counts.

Runs llama.cpp's own llama-bench, from the release in this host's package
repo, against the actual model file in the repo, using the cores this host
can spare (all but HOST_RESERVED_CORES, as placement does). Records prompt
and generation tokens/s, with the CPU, core count and load at the time, in
~/.local/share/cloudcore/llm-bench.json, which host_stats reports to the API
and its peers. Run once per host and model, and again when the hardware
changes. It reads the CPU hard for a minute or two: run it when the host is
quiet (the load is recorded either way).

Usage: python3 api/llm-bench.py --model Qwen2.5-Coder-14B-Instruct-Q4_K_M.gguf [--threads N] [--dry-run]
Exit: 0 measured; 1 the benchmark failed; 2 bad usage or a missing file.
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import shutil
import subprocess
import sys
import tarfile
import time
from pathlib import Path

API_DIR = Path(__file__).resolve().parent
ARTIFACTS = API_DIR / "package-repo" / "jammy" / "artifacts"
RESULTS = Path.home() / ".local" / "share" / "cloudcore" / "llm-bench.json"
CACHE = Path.home() / ".cache" / "cloudcore"
HOST_RESERVED_CORES = 2  # as api/capacity_gate.py


def log(msg: str) -> None:
    print(f"llm-bench: {msg}", file=sys.stderr, flush=True)


def cpu_model() -> str:
    try:
        for line in Path("/proc/cpuinfo").read_text().splitlines():
            if line.startswith("model name"):
                return line.split(":", 1)[1].strip()
    except OSError:
        pass
    return platform.processor() or "unknown"


def llama_bench() -> Path:
    """llama-bench from the newest llama.cpp release in the repo, unpacked once."""
    archives = sorted(ARTIFACTS.glob("llama-*-bin-ubuntu-x64.tar.gz"))
    if not archives:
        raise SystemExit(log(f"no llama.cpp release in {ARTIFACTS}") or 2)
    archive = archives[-1]
    dest = CACHE / archive.name.removesuffix(".tar.gz")
    binary = dest / "llama-bench"
    if not binary.exists():
        dest.mkdir(parents=True, exist_ok=True)
        with tarfile.open(archive) as tar:
            for m in tar.getmembers():
                # Flatten the release's top directory; refuse anything odd.
                name = m.name.split("/", 1)[-1]
                if not name or name.startswith(("/", "..")) or "/.." in name or not (m.isfile() or m.issym()):
                    continue
                m.name = name
                tar.extract(m, dest, filter="data" if hasattr(tarfile, "data_filter") else None)
    return binary


def run(model: Path, threads: int) -> dict:
    binary = llama_bench()
    env = {**os.environ, "LD_LIBRARY_PATH": str(binary.parent)}
    cmd = [str(binary), "-m", str(model), "-t", str(threads), "-p", "256", "-n", "64", "-r", "1", "-o", "json"]
    log(f"running {' '.join(cmd[1:])} (a minute or two at full CPU)")
    t0 = time.monotonic()
    r = subprocess.run(cmd, capture_output=True, text=True, env=env, timeout=1800)
    if r.returncode != 0:
        raise RuntimeError((r.stderr or r.stdout).strip()[-400:])
    rows = json.loads(r.stdout)
    pp = next((x["avg_ts"] for x in rows if x.get("n_prompt", 0) > 0 and x.get("n_gen", 0) == 0), None)
    tg = next((x["avg_ts"] for x in rows if x.get("n_gen", 0) > 0 and x.get("n_prompt", 0) == 0), None)
    return {"prompt_tps": round(pp, 2) if pp else None, "gen_tps": round(tg, 2) if tg else None,
            "bench_secs": round(time.monotonic() - t0)}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--model", required=True, help="a .gguf file name in the package repo's artifacts")
    ap.add_argument("--threads", type=int, default=0, help="default: this host's cores minus the reserved 2")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    if "/" in args.model or not args.model.endswith(".gguf"):
        ap.error("--model is a .gguf file name from the repo's artifacts")
    model = ARTIFACTS / args.model
    if not model.is_file():
        log(f"{model} not found: is this host's repo in step (two-host S3)?")
        return 2
    cores = os.cpu_count() or 1
    threads = args.threads or max(1, cores - HOST_RESERVED_CORES)
    load = os.getloadavg()[0]
    log(f"{args.model} on {cpu_model()}, {threads} of {cores} cores, load {load:.2f}")
    if args.dry_run:
        return 0
    try:
        measured = run(model, threads)
    except (RuntimeError, subprocess.TimeoutExpired, json.JSONDecodeError, OSError) as e:
        log(f"benchmark failed: {e}")
        return 1
    record = {**measured, "threads": threads, "cores": cores, "cpu": cpu_model(), "load_at_start": round(load, 2),
              "llama": llama_bench().parent.name, "measured_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
    RESULTS.parent.mkdir(parents=True, exist_ok=True)
    try:
        data = json.loads(RESULTS.read_text())
    except (OSError, ValueError):
        data = {}
    data.setdefault("models", {})[args.model] = record
    tmp = RESULTS.with_name(RESULTS.name + ".tmp")
    tmp.write_text(json.dumps(data, indent=1, sort_keys=True))
    os.replace(tmp, RESULTS)
    log(f"{args.model}: prompt {record['prompt_tps']} tok/s, generation {record['gen_tps']} tok/s "
        f"({threads} threads) -> {RESULTS}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
