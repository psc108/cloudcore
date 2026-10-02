#!/usr/bin/env python3
"""Held-out evaluation of the Linux Help pipeline, through the same endpoint
the page uses: ask -> grounding/reuse -> answer -> lab run -> repairs ->
another way. Resumable: questions already recorded are skipped.

Usage: run_heldout.py [--base URL] [--sentinel-db PATH] [--only N,N]
Writes heldout-asks.jsonl (one line per question) and, once every lab run
has finished, heldout-labs.jsonl (the final lab status of each).
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import time
from pathlib import Path

import httpx

HERE = Path(__file__).resolve().parent
ASKS = HERE / "heldout-asks.jsonl"
LABS = HERE / "heldout-labs.jsonl"


def log(msg: str) -> None:
    print(time.strftime("%H:%M:%S"), msg, file=sys.stderr, flush=True)


def ask(base: str, question: str) -> dict:
    """One question, as the page asks it. Waits out 503 (busy) / 429."""
    while True:
        t0 = time.time()
        text, sources, notices, lab = [], [], [], None
        with httpx.Client(timeout=httpx.Timeout(30.0, read=2400.0)) as client:
            with client.stream("POST", f"{base}/sandbox/linux-ask",
                               json={"question": question, "history": []}) as resp:
                if resp.status_code in (429, 503):
                    resp.read()
                    log(f"  busy ({resp.status_code}); retrying in 60s")
                    time.sleep(60)
                    continue
                resp.raise_for_status()
                done = False
                for line in resp.iter_lines():
                    if not line.startswith("data: "):
                        continue
                    payload = line[6:]
                    if payload == "[DONE]":
                        done = True
                        break
                    try:
                        obj = json.loads(payload)
                    except ValueError:
                        continue
                    if "sources" in obj:
                        sources = obj["sources"]
                    elif "notices" in obj:
                        notices = obj["notices"]
                    elif "lab_run" in obj:
                        lab = obj["lab_run"]
                    elif obj.get("choices"):
                        text.append((obj["choices"][0].get("delta") or {}).get("content") or "")
        return {"answer": "".join(text), "sources": sources, "notices": notices, "lab_run": lab,
                "complete": done, "secs": round(time.time() - t0)}


def grounding_row(db: Path, question: str) -> dict:
    """What Sentinel logged for this ask: search terms, and the stored
    answer the matcher handed the model, if any."""
    try:
        conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        row = conn.execute(
            "SELECT search_terms, grounding_source, references_json FROM grounding_log "
            "WHERE question = ? AND endpoint = '/sandbox/linux-ask' ORDER BY id DESC LIMIT 1",
            (question,)).fetchone()
    except sqlite3.Error as e:
        return {"error": str(e)}
    if not row:
        return {}
    refs = json.loads(row[2] or "[]")
    return {"search_terms": row[0], "grounding_source": row[1],
            "references": [{"source": r.get("source"), "title": r.get("title")} for r in refs]}


def wait_for_labs(base: str, records: list[dict]) -> None:
    pending = {r["n"]: r for r in records if r.get("lab_run")}
    done = {json.loads(l)["n"] for l in LABS.read_text().splitlines()} if LABS.exists() else set()
    pending = {n: r for n, r in pending.items() if n not in done}
    while pending:
        for n, r in list(pending.items()):
            lab = r["lab_run"]
            try:
                data = httpx.get(f"{base}/sandbox/lab-run", params={"id": lab["id"], "token": lab["token"]},
                                 timeout=30).json()
            except (httpx.HTTPError, ValueError) as e:
                log(f"  #{n}: status check failed: {e}")
                continue
            if data.get("status") in ("done", "error", "unknown") and not data.get("retrying"):
                with LABS.open("a") as f:
                    f.write(json.dumps({"n": n, "lab": data}) + "\n")
                v = data.get("verdict")
                rep = (data.get("repaired") or {}).get("verdict")
                att = [a.get("repaired") or a.get("verdict") for a in data.get("attempts") or []]
                log(f"  #{n}: lab {v}" + (f" -> repaired {rep}" if rep else "") + (f" -> attempts {att}" if att else ""))
                del pending[n]
        if pending:
            time.sleep(60)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:8620")
    ap.add_argument("--sentinel-db", type=Path, default=Path.home() / ".local/share/sentinel/sentinel.db")
    ap.add_argument("--only", default="")
    args = ap.parse_args()
    questions = json.loads((HERE / "heldout-questions.json").read_text())["questions"]
    only = {int(x) for x in args.only.split(",") if x}
    have = {json.loads(l)["n"]: json.loads(l) for l in ASKS.read_text().splitlines()} if ASKS.exists() else {}
    for q in questions:
        if q["n"] in have or (only and q["n"] not in only):
            continue
        log(f"#{q['n']} [{q['kind']}] {q['q']}")
        try:
            out = ask(args.base, q["q"])
        except httpx.HTTPError as e:
            log(f"  failed: {e!r}; will retry on the next run")
            continue
        time.sleep(3)  # let the grounding push reach Sentinel
        rec = {**q, **out, "grounding": grounding_row(args.sentinel_db, q["q"])}
        with ASKS.open("a") as f:
            f.write(json.dumps(rec) + "\n")
        have[q["n"]] = rec
        g = rec["grounding"]
        log(f"  answered in {out['secs']}s ({len(out['answer'])} chars, complete={out['complete']}); "
            f"grounding={g.get('grounding_source')} refs={[r['title'][:40] for r in g.get('references', [])][:2]}; "
            f"lab={'yes' if out['lab_run'] else 'no'}")
    log("all questions asked; waiting for lab runs")
    wait_for_labs(args.base, list(have.values()))
    log("ALL DONE")
    return 0


if __name__ == "__main__":
    sys.exit(main())
