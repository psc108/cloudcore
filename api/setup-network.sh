#!/usr/bin/env bash
# Run once with sudo to create the ccbr0 bridge and DHCP server.
# After this, instances get real routable IPs instead of SLIRP 10.0.2.15.
set -euo pipefail

BRIDGE=ccbr0
SUBNET=192.168.100
GW=${SUBNET}.1
DHCP_START=${SUBNET}.10
DHCP_END=${SUBNET}.254
LEASE_FILE=/var/lib/misc/cloudcore-dnsmasq.leases
PIDFILE=/var/run/cloudcore-dnsmasq.pid

# Create bridge
ip link add "$BRIDGE" type bridge 2>/dev/null || true
ip addr add "${GW}/24" dev "$BRIDGE" 2>/dev/null || true
ip link set "$BRIDGE" up

# Enable IP forwarding
sysctl -qw net.ipv4.ip_forward=1

# NAT outbound traffic from bridge subnet
iptables -t nat -C POSTROUTING -s "${SUBNET}.0/24" ! -d "${SUBNET}.0/24" -j MASQUERADE 2>/dev/null \
  || iptables -t nat -A POSTROUTING -s "${SUBNET}.0/24" ! -d "${SUBNET}.0/24" -j MASQUERADE

# Stop any existing cloudcore dnsmasq before starting
[ -f "$PIDFILE" ] && kill "$(cat $PIDFILE)" 2>/dev/null || true
sleep 0.5

# Start dnsmasq for DHCP on the bridge
touch "$LEASE_FILE"
dnsmasq \
  --interface="$BRIDGE" \
  --bind-interfaces \
  --dhcp-range="${DHCP_START},${DHCP_END},12h" \
  --dhcp-leasefile="$LEASE_FILE" \
  --pid-file="$PIDFILE" \
  --log-facility=/var/log/cloudcore-dnsmasq.log \
  --no-resolv \
  --except-interface=lo

echo "Bridge $BRIDGE up at ${GW}/24, DHCP ${DHCP_START}-${DHCP_END}"
echo "Run 'sudo bash api/teardown-network.sh' to remove."
