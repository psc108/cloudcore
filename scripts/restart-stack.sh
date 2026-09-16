#!/usr/bin/env bash
# Restarts the whole CloudCore + Sentinel stack together, in the
# correct dependency order, and makes sure Sentinel's knowledge base
# is up to date with this repo's own findings log before finishing.
#
# Order: CloudCore's own user services -> host-level Loki/Grafana
# (sudo) -> a sibling Sentinel checkout's own user services -> re-
# ingest the KB last, once everything's actually up. Sentinel has zero
# runtime dependency on CloudCore's own API (it only ever talks to
# Loki directly, see its own README) -- but restarting it after
# Loki/Grafana are already back up means its very first poll lands on
# a ready target instead of racing a still-starting one.
#
# Mirrored at restart-stack.sh in a sibling Sentinel checkout -- same
# script, usable from either repo, each anchored at its own directory
# and detecting the other by filename (never an assumed directory
# name), same convention install.sh already uses both directions.
#
# Safe to re-run any time -- every step here is already idempotent.
#
# Usage: bash scripts/restart-stack.sh [--help]
set -euo pipefail

if [[ "${1:-}" == "--help" ]]; then
    echo "Usage: bash scripts/restart-stack.sh"
    echo ""
    echo "Restarts cloudcore-api/cloudcore-terminal, the host-level"
    echo "loki/grafana-server services (needs sudo -- run this yourself in a"
    echo "real terminal, not piped through anything that can't answer a"
    echo "password prompt), then a sibling Sentinel checkout's own services,"
    echo "then re-ingests its knowledge base from this repo's findings log."
    exit 0
fi

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

# Kills whatever's still listening on $1 after the service that owns
# it has already been stopped -- an orphaned, un-managed process left
# over from an earlier manual run, not the service itself. Confirmed
# live as a real problem, not a hypothetical: exactly this left
# cloudcore-api crash-looping for hours (NRestarts in the hundreds),
# unable to rebind its own port because a stray `python3 server.py`
# from an earlier manual start was still squatting on it.
ensure_port_clear() {
    local port="$1" pid
    pid="$(ss -tlnp 2>/dev/null | grep ":${port} " | grep -oP 'pid=\K[0-9]+' | head -1 || true)"
    if [[ -n "$pid" ]]; then
        echo "    Port $port still held by PID $pid -- killing it (an orphaned process from an earlier manual run, not the service itself)."
        kill "$pid" 2>/dev/null || true
        sleep 1
    fi
}

echo "==> Restarting cloudcore-api / cloudcore-terminal..."
systemctl --user stop cloudcore-api cloudcore-terminal 2>/dev/null || true
ensure_port_clear 8080
ensure_port_clear 8081
systemctl --user start cloudcore-api cloudcore-terminal

echo "==> Restarting host-level logging (Loki + Grafana)..."
if systemctl list-unit-files loki.service &>/dev/null; then
    sudo systemctl restart loki grafana-server
    echo "    Waiting for Grafana to come back up..."
    for i in $(seq 1 60); do
        curl -sf http://192.168.100.1:3000/api/health &>/dev/null && break
        sleep 1
    done
elif ! compgen -G "$REPO_DIR/api/package-repo/*/apt-repo/grafana_*.deb" >/dev/null \
  || ! compgen -G "$REPO_DIR/api/package-repo/*/apt-repo/loki_*.deb" >/dev/null; then
    echo "    The host-level package repo hasn't cached grafana/loki yet:"
    echo "      CLOUDCORE_API_URL=http://127.0.0.1:8080 CLOUDCORE_API_TOKEN=dev-token \\"
    echo "        bash api/build-package-repo.sh jammy"
    echo "    Takes 15-20+ minutes (one-time, real downloads) -- not something to"
    echo "    fold into a routine restart, so this step is skipped for now."
else
    echo "    grafana/loki are cached but the service was never installed."
    # Fast (a few seconds) and low-risk -- worth offering to just do it,
    # unlike the slow build-package-repo.sh case above. Only offer with
    # a real terminal on the other end to answer it.
    if [ -t 0 ]; then
        read -r -p "    Set it up now? Needs sudo. [y/N] " REPLY
        if [[ "$REPLY" =~ ^[Yy]$ ]]; then
            sudo bash "$REPO_DIR/api/setup-logging-service.sh"
            echo "    Waiting for Grafana to come up..."
            for i in $(seq 1 60); do
                curl -sf http://192.168.100.1:3000/api/health &>/dev/null && break
                sleep 1
            done
        else
            echo "    Skipped. Run it yourself when ready:"
            echo "      sudo bash api/setup-logging-service.sh"
        fi
    else
        echo "      sudo bash api/setup-logging-service.sh"
    fi
fi

# Sibling Sentinel checkout, detected by filename rather than an
# assumed directory name.
SENTINEL_DIR=""
for CANDIDATE in "$REPO_DIR"/../*/systemd/sentinel-watch.service; do
    [[ -f "$CANDIDATE" ]] || continue
    SENTINEL_DIR="$(cd "$(dirname "$CANDIDATE")/.." && pwd)"
    break
done

if [[ -z "$SENTINEL_DIR" ]]; then
    echo ""
    echo "==> No sibling Sentinel checkout found -- done (CloudCore only)."
    exit 0
fi

echo "==> Restarting Sentinel ($SENTINEL_DIR)..."
systemctl --user stop sentinel-watch sentinel-ui 2>/dev/null || true
ensure_port_clear "${SENTINEL_UI_PORT:-8900}"
systemctl --user start sentinel-watch sentinel-ui

FINDINGS_LOG="$REPO_DIR/haFullStack-Findings-Log.md"
if [[ -x "$SENTINEL_DIR/.venv/bin/sentinel" && -f "$FINDINGS_LOG" ]]; then
    echo "==> Refreshing Sentinel's knowledge base from $FINDINGS_LOG..."
    "$SENTINEL_DIR/.venv/bin/sentinel" ingest-kb "$FINDINGS_LOG"
else
    echo "==> Sentinel isn't installed yet ($SENTINEL_DIR/install.sh) -- skipping KB refresh."
fi

echo ""
echo "==> Done."
echo "    CloudCore: http://127.0.0.1:8080"
echo "    Sentinel:  http://localhost:8900/"
