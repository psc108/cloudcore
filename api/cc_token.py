"""CloudCore's API tokens, from the environment or ~/.config/cloudcore/api.env.

F-201: every module used to default to the well-known "dev-token" -- the
master token for the whole API -- and the llm-chat coordinator guest held it
too. Now there is no default: a missing token, or that old value, stops the
API from starting (fail closed) rather than quietly accepting a known
password. An empty token would be worse still: a header of just "Bearer "
would match it.

Two tokens:
- CLOUDCORE_API_TOKEN: the master token, for this host's own clients on
  127.0.0.1:8080 (the dashboard, tofu builds, scripts). Refused on the
  network-facing peer and examples listeners.
- CLOUDCORE_EXAMPLES_TOKEN: what llm-chat coordinator guests use for their
  two capture routes (submit an example, register a deployment), nothing
  else.

Set them in ~/.config/cloudcore/api.env (mode 0600) -- scripts/install.sh
creates it -- or in the environment.
"""

from __future__ import annotations

import os
from pathlib import Path

ENV_FILE = Path(os.environ.get("CLOUDCORE_ENV_FILE", str(Path.home() / ".config" / "cloudcore" / "api.env")))
_RETIRED = {"dev-token"}


def _from_file(name: str) -> str:
    try:
        for line in ENV_FILE.read_text().splitlines():
            key, sep, value = line.strip().partition("=")
            if sep and key == name:
                return value.strip().strip("'\"")
    except OSError:
        pass
    return ""


def _get(name: str) -> str:
    return (os.environ.get(name) or _from_file(name)).strip()


def master_token() -> str:
    token = _get("CLOUDCORE_API_TOKEN")
    if not token or (token in _RETIRED and os.environ.get("CLOUDCORE_ALLOW_DEV_TOKEN") != "1"):
        raise RuntimeError(
            "CLOUDCORE_API_TOKEN is not set (or is the retired default 'dev-token'). "
            f"Set it in {ENV_FILE} (scripts/install.sh creates one) or the environment.")
    return token


def examples_token() -> str:
    """The coordinator guests' capture token; "" if not configured (then
    only the master token is accepted on those routes, i.e. none from a
    guest -- capture is off rather than open)."""
    return _get("CLOUDCORE_EXAMPLES_TOKEN")
