#!/usr/bin/env python3
"""F5 (llm-chat-full-vm-Phased-Implementation.md): what does the lab backend
alone change? Re-runs the held-out answers already recorded in
heldout-asks.jsonl -- the same answer text, no new model answer -- through
advice_runner on the microVM backend and on full VMs from the lab-VM broker.
Repairs (model_fix) run as they would live.

Runs ON the coordinator, from /opt/llama.cpp, with verify-proxy's environment
(LABVM_BROKER_URL / LABVM_BROKER_TOKEN for the full backend):
  sudo env $(systemctl show verify-proxy -p Environment --value) \\
      python3 run_backend_compare.py heldout-asks.jsonl /tmp/f5-compare.jsonl [--only 1,2]
Resumable: (n, backend) pairs already in the output are skipped.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time

sys.path.insert(0, "/opt/llama.cpp")

import advice_runner  # noqa: E402
import verify_proxy  # noqa: E402
from fullvm import FullVMLabs  # noqa: E402
from microvm import PAIR_SUBNET, IpPool, MicroVM, VmSizing, create_pair_bridge, delete_pair_bridge  # noqa: E402


def microvm_backend():
    pool = IpPool("10.200.0.0/24", part="advice")

    def make_target(**kw):
        return MicroVM("advc", pool, "fcbr0", sizing=VmSizing(2048, 1024, 2, 16384, 4096), boot_timeout_s=90,
                       root="snapshot", spare_disks_mib=(1024, 1024), **kw)

    def make_prober(bridge):
        return MicroVM("advp", IpPool(PAIR_SUBNET), bridge, sizing=VmSizing(512, 384, 1, 2048, 1024), isolate=False,
                       boot_timeout_s=90, extra_boot_args="systemd.mask=refresh-apt-index.service")

    return make_target, make_prober, (create_pair_bridge, delete_pair_bridge)


def full_backend():
    labs = FullVMLabs(os.environ["LABVM_BROKER_URL"], os.environ["LABVM_BROKER_TOKEN"])
    return labs.make_target, labs.make_prober, (labs.new_run, labs.end_run)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("asks")
    ap.add_argument("out")
    ap.add_argument("--only", default="")
    ap.add_argument("--backends", default="full,microvm")
    ap.add_argument("--presume", action="store_true", help="L16/L17: with the setup stage (model reading)")
    ap.add_argument("--tag", default="", help="suffix for the backend label, e.g. +setup")
    args = ap.parse_args()
    only = {int(x) for x in args.only.split(",") if x}
    asks = [json.loads(line) for line in open(args.asks)]
    done = set()
    if os.path.exists(args.out):
        for line in open(args.out):
            r = json.loads(line)
            done.add((r["n"], r["backend"]))
    makers = {"microvm": microvm_backend, "full": full_backend}
    with open(args.out, "a") as out:
        for backend in args.backends.split(","):
            make_target, make_prober, bridges = makers[backend]()
            label = backend + args.tag
            for a in asks:
                n = int(a["n"])
                if (only and n not in only) or (n, label) in done:
                    continue
                t0 = time.time()
                try:
                    res = advice_runner.run_advice(a["answer"], make_target, question=a["q"], make_prober=make_prober,
                                                   pair_bridges=bridges, model_fix=verify_proxy._model_fix,
                                                   presume=verify_proxy._model_presumptions if args.presume else None)
                    rec = {"n": n, "backend": label, "kind": a.get("kind", ""), "question": a["q"],
                           "verdict": res.verdict, "summary": res.summary, "setup": res.setup, "error": res.error,
                           "repaired": (res.repaired or {}).get("verdict", ""),
                           "goals": [[c["subject"], c["ok"]] for c in res.checks if c["kind"] == "goal"],
                           "secs": round(time.time() - t0)}
                except Exception as e:  # one broken run must not end the measurement
                    rec = {"n": n, "backend": label, "kind": a.get("kind", ""), "question": a["q"],
                           "verdict": "error", "summary": repr(e)[:300], "secs": round(time.time() - t0)}
                out.write(json.dumps(rec) + "\n")
                out.flush()
                print(f"#{n} {label} {rec['verdict']}"
                      + (f" -> {rec.get('repaired')}" if rec.get("repaired") else "")
                      + f" ({rec['secs']}s) {rec['question'][:60]}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
