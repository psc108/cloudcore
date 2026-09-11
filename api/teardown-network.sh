#!/usr/bin/env bash
set -euo pipefail
BRIDGE=ccbr0
PIDFILE=/var/run/cloudcore-dnsmasq.pid

# cloudcore-package-repo.service binds 192.168.100.1:8090 — $BRIDGE's own
# gateway address. Tearing the bridge down while it's active doesn't stop
# the service, it just silently cuts every guest off from it. Require
# --force so that's a deliberate choice, not a surprise.
if [ "${1:-}" != "--force" ] && systemctl is-active --quiet cloudcore-package-repo 2>/dev/null; then
  echo "cloudcore-package-repo.service is active on $BRIDGE (192.168.100.1:8090)." >&2
  echo "Tearing down $BRIDGE now would cut every guest off from the package repo." >&2
  echo "Re-run with --force to proceed anyway, or stop the service first:" >&2
  echo "  sudo systemctl stop cloudcore-package-repo" >&2
  exit 1
fi

[ -f "$PIDFILE" ] && kill "$(cat $PIDFILE)" 2>/dev/null || true
ip link set "$BRIDGE" down 2>/dev/null || true
ip link del "$BRIDGE" 2>/dev/null || true
iptables -t nat -D POSTROUTING -s 192.168.100.0/24 ! -d 192.168.100.0/24 -j MASQUERADE 2>/dev/null || true
echo "Bridge $BRIDGE removed."
