#!/usr/bin/env bash
set -euo pipefail
BRIDGE=ccbr0
PIDFILE=/var/run/cloudcore-dnsmasq.pid

# cloudcore-repo.service (192.168.100.1:8090) and, since
# api/setup-logging-service.sh, loki/grafana-server (192.168.100.1:3100/
# :3000) all bind $BRIDGE's own gateway address. Tearing the bridge down
# while any of them is active doesn't stop the service, it just silently
# cuts every guest off from it. Require --force so that's a deliberate
# choice, not a surprise.
ACTIVE_HOST_SERVICES=""
for SVC in cloudcore-repo loki grafana-server; do
  systemctl is-active --quiet "$SVC" 2>/dev/null && ACTIVE_HOST_SERVICES="$ACTIVE_HOST_SERVICES $SVC"
done
if [ "${1:-}" != "--force" ] && [ -n "$ACTIVE_HOST_SERVICES" ]; then
  echo "The following host-level service(s) are active on $BRIDGE (192.168.100.1):$ACTIVE_HOST_SERVICES" >&2
  echo "Tearing down $BRIDGE now would cut every guest off from them." >&2
  echo "Re-run with --force to proceed anyway, or stop them first:" >&2
  echo "  sudo systemctl stop$ACTIVE_HOST_SERVICES" >&2
  exit 1
fi

[ -f "$PIDFILE" ] && kill "$(cat $PIDFILE)" 2>/dev/null || true
ip link set "$BRIDGE" down 2>/dev/null || true
ip link del "$BRIDGE" 2>/dev/null || true
iptables -t nat -D POSTROUTING -s 192.168.100.0/24 ! -d 192.168.100.0/24 -j MASQUERADE 2>/dev/null || true
echo "Bridge $BRIDGE removed."
