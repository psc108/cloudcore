#!/usr/bin/env python3
"""Walk every CloudCore route through the central authorization gate (A6,
cloudcore-auth-Phased-Implementation.md).

For each route: no token, then each identity's token. A refused request
never reaches the route's code, so mutating routes are only ever sent
refused requests; valid admin requests go only to read-only routes without
path parameters. Runs in-process against a *copy* of the API database.

Usage: python3 tests/authz_walk.py [--db path/to/cloudcore.db]
Exit 1 if any route lets through an identity it doesn't declare.
"""

from __future__ import annotations

import argparse
import re
import shutil
import sys
import tempfile
from pathlib import Path

API = Path(__file__).resolve().parent.parent / "api"
sys.path.insert(0, str(API))


def _fill(rule: str) -> str:
    """A URL that matches the rule: typed parameters need a value of their
    type (an <int:...> given "x" doesn't match at all, so the route answers
    404 and a refusal can't be seen)."""
    return re.sub(r"<(?:(\w+)(?:\([^)]*\))?:)?\w+>", lambda m: "1" if m.group(1) in ("int", "float") else "x", rule)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=str(API / "cloudcore.db"))
    args = ap.parse_args()
    tmp = Path(tempfile.mkdtemp(prefix="authz-walk-"))
    copy = tmp / "cloudcore.db"
    shutil.copy(args.db, copy)

    import db
    db.init(copy)
    import authz
    import cc_token
    import server

    app = server.app
    client = app.test_client()
    tokens = {"none": "", "admin": cc_token.master_token(), "capture": cc_token.examples_token(),
              "labvm": cc_token.labvm_token(), "bogus": "not-a-token"}
    failures, counted = [], 0
    for rule in app.url_map.iter_rules():
        need = authz.allowed(rule.endpoint)
        url = rule.rule
        url = _fill(url)
        url = url.replace("<path:filename>", "x")
        for method in sorted(rule.methods - {"HEAD", "OPTIONS"}):
            for who, tok in tokens.items():
                if who == "capture" and not tok or who == "labvm" and not tok:
                    continue
                passes = need is None or (who in ("admin", "capture", "labvm") and who in need)
                if passes and method != "GET":
                    continue  # never execute a mutating route
                if passes and rule.arguments:
                    continue  # nor guess ids for read routes
                headers = {"Authorization": f"Bearer {tok}"} if tok else {}
                status = client.open(url, method=method, headers=headers, json={}).status_code
                counted += 1
                refused = status in (401, 403)
                if passes and refused and need is not None:
                    failures.append(f"{method} {rule.rule} [{rule.endpoint}] refused {who} ({status}) but allows it")
                if not passes and not refused:
                    failures.append(f"{method} {rule.rule} [{rule.endpoint}] LET THROUGH {who} ({status})")
    # F-209: a CORS preflight carries no token and must still get a 2xx, or
    # the browser never sends the real request ("Failed to fetch").
    for rule in app.url_map.iter_rules():
        if rule.websocket:
            continue  # browsers never preflight a WebSocket handshake; it isn't a CORS request
        url = rule.rule
        url = _fill(url)
        status = client.open(url, method="OPTIONS", headers={"Origin": "http://localhost:8080",
                             "Access-Control-Request-Method": "GET"}).status_code
        counted += 1
        if not 200 <= status < 300:
            failures.append(f"OPTIONS {rule.rule} [{rule.endpoint}] preflight got {status}")
    public = sorted(r.endpoint for r in app.url_map.iter_rules() if authz.allowed(r.endpoint) is None)
    shutil.rmtree(tmp, ignore_errors=True)
    print(f"{counted} requests over {len(list(app.url_map.iter_rules()))} routes; public routes: {public}")
    for f in failures:
        print("FAIL", f)
    print("OK" if not failures else f"{len(failures)} failures")
    return 0 if not failures else 1


if __name__ == "__main__":
    sys.exit(main())
