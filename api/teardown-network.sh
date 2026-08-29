#!/usr/bin/env bash
set -euo pipefail
BRIDGE=ccbr0
PIDFILE=/var/run/cloudcore-dnsmasq.pid

[ -f "$PIDFILE" ] && kill "$(cat $PIDFILE)" 2>/dev/null || true
ip link set "$BRIDGE" down 2>/dev/null || true
ip link del "$BRIDGE" 2>/dev/null || true
iptables -t nat -D POSTROUTING -s 192.168.100.0/24 ! -d 192.168.100.0/24 -j MASQUERADE 2>/dev/null || true
echo "Bridge $BRIDGE removed."
