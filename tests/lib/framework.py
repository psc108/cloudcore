from __future__ import annotations

import json
import os
import time
import traceback
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

API_BASE    = "http://127.0.0.1:8080"
API_TOKEN   = os.environ.get("CLOUDCORE_API_TOKEN", "dev-token")
RESULTS_DIR = Path(__file__).parent.parent / "results"
RESULTS_DIR.mkdir(exist_ok=True)

# ---------------------------------------------------------------------------
# Colours
# ---------------------------------------------------------------------------
GREEN  = "\033[32m"
RED    = "\033[31m"
YELLOW = "\033[33m"
CYAN   = "\033[36m"
BOLD   = "\033[1m"
RESET  = "\033[0m"

def _c(colour, text): return f"{colour}{text}{RESET}"

# ---------------------------------------------------------------------------
# HTTP client
# ---------------------------------------------------------------------------
def req(method: str, path: str, body: dict | None = None,
        expected: int | tuple = 200) -> tuple[int, dict]:
    url  = API_BASE + path
    data = json.dumps(body).encode() if body is not None else None
    r    = urllib.request.Request(url, data=data, method=method, headers={
        "Authorization": f"Bearer {API_TOKEN}",
        "Content-Type":  "application/json",
    })
    try:
        with urllib.request.urlopen(r) as resp:
            status = resp.status
            raw    = resp.read()
            body   = json.loads(raw) if raw else {}
    except urllib.error.HTTPError as e:
        status = e.code
        raw    = e.read()
        body   = json.loads(raw) if raw else {}

    if isinstance(expected, int):
        expected = (expected,)
    if status not in expected:
        raise AssertionError(
            f"{method} {path} → {status} (expected {expected})\n  body: {body}"
        )
    return status, body

# ---------------------------------------------------------------------------
# Assertions
# ---------------------------------------------------------------------------
def assert_eq(actual, expected, label=""):
    if actual != expected:
        raise AssertionError(
            f"{label}: expected {expected!r}, got {actual!r}"
        )

def assert_in(key, container, label=""):
    if key not in container:
        raise AssertionError(f"{label}: {key!r} not found in {container!r}")

def assert_not_in(key, container, label=""):
    if key in container:
        raise AssertionError(f"{label}: {key!r} unexpectedly found in {container!r}")

# ---------------------------------------------------------------------------
# vm_test decorator
# ---------------------------------------------------------------------------
def vm_test(fn):
    """Mark a test as requiring a real KVM instance (slow)."""
    fn._vm_test = True
    return fn

# ---------------------------------------------------------------------------
# Result store
# ---------------------------------------------------------------------------
_results: list[dict] = []

def get_results() -> list[dict]:
    return _results

# ---------------------------------------------------------------------------
# Suite runner
# ---------------------------------------------------------------------------
def run_suite(cls, skip_vm: bool = False):
    suite_name = cls.__name__.replace("Test", "").replace("_", " ").strip()
    print(f"\n{_c(BOLD+CYAN, '━━ ' + suite_name + ' ━━')}")
    inst    = cls()
    methods = sorted(m for m in dir(inst) if m.startswith("test_"))

    for name in methods:
        method = getattr(inst, name)
        if skip_vm and getattr(method, "_vm_test", False):
            print(f"  {_c(YELLOW, 'SKIP')}  {name.replace('test_', '', 1)}")
            _results.append({"suite": suite_name, "test": name,
                             "status": "SKIP", "duration": 0, "error": ""})
            continue
        t0 = time.time()
        try:
            if hasattr(inst, "setUp"):
                inst.setUp()
            method()
            dur = time.time() - t0
            print(f"  {_c(GREEN, 'PASS')}  {name.replace('test_', '', 1)}"
                  f"  {_c(YELLOW, f'{dur:.2f}s')}")
            _results.append({"suite": suite_name, "test": name,
                             "status": "PASS", "duration": round(dur, 2), "error": ""})
        except Exception as e:
            dur = time.time() - t0
            print(f"  {_c(RED, 'FAIL')}  {name.replace('test_', '', 1)}"
                  f"  {_c(YELLOW, f'{dur:.2f}s')}")
            print(f"         {_c(RED, str(e))}")
            _results.append({"suite": suite_name, "test": name,
                             "status": "FAIL", "duration": round(dur, 2),
                             "error": str(e)})

# ---------------------------------------------------------------------------
# Results writer
# ---------------------------------------------------------------------------
def write_results(skip_vm: bool) -> tuple[int, int, int]:
    ts      = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    passed  = sum(1 for r in _results if r["status"] == "PASS")
    failed  = sum(1 for r in _results if r["status"] == "FAIL")
    skipped = sum(1 for r in _results if r["status"] == "SKIP")
    total   = len(_results)
    total_t = sum(r["duration"] for r in _results)

    col_suite = max(len(r["suite"]) for r in _results)
    col_test  = max(len(r["test"])  for r in _results)
    sep = ("+" + "-"*(col_suite+2) + "+" + "-"*(col_test+2)
           + "+" + "-"*8 + "+" + "-"*10 + "+")
    hdr = (f"| {'Suite':<{col_suite}} | {'Test':<{col_test}}"
           f" | {'Status':<6} | {'Time':>8} |")

    lines = [
        "CloudCore Test Results",
        f"Run at : {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        f"VM tests: {'skipped' if skip_vm else 'included'}",
        "", sep, hdr, sep,
    ]
    for r in _results:
        lines.append(
            f"| {r['suite']:<{col_suite}} | {r['test']:<{col_test}}"
            f" | {r['status']:<6} | {r['duration']:>7.2f}s |"
        )
        if r["error"]:
            snippet = r["error"][:col_suite + col_test + 10]
            lines.append(f"|   ERROR: {snippet}")
    lines += [
        sep, "",
        f"PASSED : {passed}",
        f"FAILED : {failed}",
        f"SKIPPED: {skipped}",
        f"TOTAL  : {total}  ({total_t:.2f}s)",
    ]
    text = "\n".join(lines) + "\n"

    (RESULTS_DIR / "latest.txt").write_text(text)
    (RESULTS_DIR / f"{ts}.txt").write_text(text)
    (RESULTS_DIR / "latest.json").write_text(json.dumps({
        "run_at": ts,
        "skip_vm": skip_vm,
        "summary": {
            "passed": passed, "failed": failed,
            "skipped": skipped, "total": total,
            "duration": round(total_t, 2),
        },
        "results": _results,
    }, indent=2))
    return passed, failed, skipped
