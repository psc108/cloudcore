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

# Allow forwarding to/from the bridge. Needed even with ip_forward=1 and the
# MASQUERADE rule above: NAT only rewrites source IP in POSTROUTING, which
# runs *after* the FORWARD chain's own accept/drop decision — on a host
# where something else (commonly Docker) has already set FORWARD's default
# policy to DROP and only allowlists its own bridges, ccbr0 traffic is
# silently dropped before it ever reaches the NAT rule. Insert (-I, not -A)
# so this wins over any earlier DROP-oriented rules already in the chain.
iptables -C FORWARD -i "$BRIDGE" -j ACCEPT 2>/dev/null || iptables -I FORWARD -i "$BRIDGE" -j ACCEPT
iptables -C FORWARD -o "$BRIDGE" -j ACCEPT 2>/dev/null || iptables -I FORWARD -o "$BRIDGE" -j ACCEPT

# libvirt's QEMU driver needs explicit permission to attach a guest NIC to
# this bridge via the (unprivileged, session-mode) qemu-bridge-helper —
# without this the helper refuses with "failed to create tun device:
# Operation not permitted" and every bridged instance create fails.
mkdir -p /etc/qemu
grep -qxF "allow $BRIDGE" /etc/qemu/bridge.conf 2>/dev/null \
  || echo "allow $BRIDGE" >> /etc/qemu/bridge.conf

# The bridge helper itself also needs CAP_NET_ADMIN to create that tun
# device in the first place (session-mode libvirt runs qemu as the
# invoking user, not root, so there's no ambient capability for this
# otherwise) — same failure mode as the bridge.conf gap above.
BRIDGE_HELPER=$(command -v qemu-bridge-helper 2>/dev/null || echo /usr/lib/qemu/qemu-bridge-helper)
if [ -x "$BRIDGE_HELPER" ] && ! getcap "$BRIDGE_HELPER" 2>/dev/null | grep -q cap_net_admin; then
  setcap cap_net_admin+ep "$BRIDGE_HELPER"
fi

# The API server (api/server.py) runs unprivileged, but bridge-mode
# security-group enforcement (api/sg.py) needs to run iptables/ip6tables as
# root to filter the FORWARD chain per-instance by MAC address — without
# this, every security group ever attached to a bridged instance silently
# fails to apply (exit 4, "Permission denied (you must be root)"), logged
# but never surfaced, leaving the instance fully open instead of enforced.
# Grant a tightly-scoped NOPASSWD sudo rule for exactly these two binaries
# (never blanket root access) to the user who ran this script, so
# api/sg.py's "sudo -n iptables ..." calls succeed non-interactively from
# the API's background instance-launch thread.
IPTABLES_BIN=$(command -v iptables)
IP6TABLES_BIN=$(command -v ip6tables)
SG_SUDOERS_USER=${SUDO_USER:-$USER}
SG_SUDOERS_FILE=/etc/sudoers.d/cloudcore-sg
SG_SUDOERS_LINE="${SG_SUDOERS_USER} ALL=(root) NOPASSWD: ${IPTABLES_BIN}, ${IP6TABLES_BIN}"
if [ ! -f "$SG_SUDOERS_FILE" ] || ! grep -qxF "$SG_SUDOERS_LINE" "$SG_SUDOERS_FILE" 2>/dev/null; then
  TMP_SUDOERS=$(mktemp)
  echo "$SG_SUDOERS_LINE" > "$TMP_SUDOERS"
  chmod 440 "$TMP_SUDOERS"
  if visudo -c -f "$TMP_SUDOERS" >/dev/null 2>&1; then
    mv "$TMP_SUDOERS" "$SG_SUDOERS_FILE"
    echo "Granted $SG_SUDOERS_USER passwordless sudo for iptables/ip6tables (security-group enforcement)."
  else
    echo "WARNING: generated sudoers rule failed validation, not installed: $SG_SUDOERS_LINE" >&2
    rm -f "$TMP_SUDOERS"
  fi
fi

# Stop any existing cloudcore dnsmasq before starting
[ -f "$PIDFILE" ] && kill "$(cat $PIDFILE)" 2>/dev/null || true
sleep 0.5

# Start dnsmasq for DHCP+DNS on the bridge. Guests are handed the GATEWAY
# (this dnsmasq itself) as their DNS server, not public DNS directly —
# --server=/cloudcore.internal/127.0.0.1#5353 forwards queries for CloudCore's
# own zones (instances.cloudcore.internal, lb.cloudcore.internal — see
# api/dns.py's BUILTIN_ZONES) to the API's own dns_server.py, which already
# maintains that data but binds host-loopback-only and was previously
# unreachable from any guest. Plain --server=IP entries (no domain) are the
# default/catch-all for everything else, so normal internet resolution
# (apt installs, curl downloads, ...) keeps working unchanged. --no-resolv
# is kept so this never depends on the host's own /etc/resolv.conf state.
touch "$LEASE_FILE"
dnsmasq \
  --interface="$BRIDGE" \
  --bind-interfaces \
  --dhcp-range="${DHCP_START},${DHCP_END},12h" \
  --dhcp-option="option:dns-server,${GW}" \
  --server="/cloudcore.internal/127.0.0.1#5353" \
  --server=8.8.8.8 \
  --server=1.1.1.1 \
  --dhcp-leasefile="$LEASE_FILE" \
  --pid-file="$PIDFILE" \
  --log-facility=/var/log/cloudcore-dnsmasq.log \
  --no-resolv \
  --except-interface=lo

echo "Bridge $BRIDGE up at ${GW}/24, DHCP ${DHCP_START}-${DHCP_END}, DNS ${GW} (cloudcore.internal -> API DNS, everything else -> 8.8.8.8/1.1.1.1)"
echo "Run 'sudo bash api/teardown-network.sh' to remove."
