#!/usr/bin/env bash
# Run once with sudo to grant this user passwordless root access to
# `wg`/`wg-quick` specifically — same scoped-binary pattern
# setup-network.sh already uses for iptables/ip6tables (see its own
# cloudcore-sg sudoers grant), not a blanket sudo grant — so
# api/wireguard.py's own "sudo -n wg-quick ..."/"sudo -n wg ..." calls
# succeed non-interactively once a peer is actually approved. The
# existing cloudcore-sg grant already covers the iptables FORWARD rule
# cc0 needs (same two binaries setup-network.sh already grants for
# ccbr0 — no new iptables grant needed here).
#
# This script does NOT itself bring up cc0 — there's no config to
# bring up until a peer is actually approved; api/wireguard.py handles
# that once this grant exists.
set -euo pipefail

WG_BIN=$(command -v wg || true)
WG_QUICK_BIN=$(command -v wg-quick || true)
if [ -z "$WG_BIN" ] || [ -z "$WG_QUICK_BIN" ]; then
    echo "wg/wg-quick not found — install wireguard-tools first: sudo apt-get install -y wireguard-tools" >&2
    exit 1
fi

WG_SUDOERS_USER=${CLOUDCORE_BRIDGE_USER:-${SUDO_USER:-$USER}}
WG_SUDOERS_FILE=/etc/sudoers.d/cloudcore-wg

if [ -z "$WG_SUDOERS_USER" ]; then
    echo "WARNING: could not determine a user for the WireGuard sudoers grant" \
         "(CLOUDCORE_BRIDGE_USER/SUDO_USER/USER all empty) — skipping. Cross-host" \
         "peering's WireGuard tunnels will fail to come up until this is fixed; run" \
         "'sudo bash $0' interactively, or set CLOUDCORE_BRIDGE_USER, to resolve." >&2
    exit 1
fi

WG_SUDOERS_LINE="${WG_SUDOERS_USER} ALL=(root) NOPASSWD: ${WG_BIN}, ${WG_QUICK_BIN}"
if [ -f "$WG_SUDOERS_FILE" ] && grep -qxF "$WG_SUDOERS_LINE" "$WG_SUDOERS_FILE" 2>/dev/null; then
    echo "WireGuard sudoers grant already present for $WG_SUDOERS_USER, skipping."
    exit 0
fi

TMP_SUDOERS=$(mktemp)
echo "$WG_SUDOERS_LINE" > "$TMP_SUDOERS"
chmod 440 "$TMP_SUDOERS"
if visudo -c -f "$TMP_SUDOERS" >/dev/null 2>&1; then
    mv "$TMP_SUDOERS" "$WG_SUDOERS_FILE"
    echo "Granted $WG_SUDOERS_USER passwordless sudo for wg/wg-quick (cross-host peering tunnels)."
else
    echo "WARNING: generated sudoers rule failed validation, not installed: $WG_SUDOERS_LINE" >&2
    rm -f "$TMP_SUDOERS"
    exit 1
fi
