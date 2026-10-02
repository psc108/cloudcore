#!/usr/bin/env bash
# Isolated network for llm-chat's full lab VMs (llm-chat-full-vm-Phased-
# Implementation.md, F2). Run with sudo on each CloudCore host that may run
# lab VMs.
#
# Why a separate bridge: on ccbr0, CloudCore's security groups can't isolate
# a VM -- bridge-netfilter isn't loaded, so VM-to-VM traffic never reaches
# iptables; SGs only filter routed egress; traffic to the host's own services
# goes through INPUT, which SGs don't touch (F-201). Lab VMs run model-written
# commands, so they get their own bridge, cclab0, fenced with nftables tables
# that apply to that bridge only:
#   lab -> internet          allowed (NAT)
#   lab -> private/LAN/link-local/CGNAT/loopback ranges   dropped
#   lab -> this host         DHCP and DNS only
#   -> lab                   SSH (22, and the lab's control sshd on 1022) from
#                            --controllers only, plus replies
#   lab VM <-> lab VM        dropped at layer 2, except registered pairs
#                            (a run's target and prober), managed by
#                            /usr/local/sbin/cloudcore-labnet
#
# Usage:
#   sudo api/setup-lab-network.sh --controllers CIDR[,CIDR...] [--dry-run]
#   sudo api/setup-lab-network.sh --remove [--dry-run]
#   api/setup-lab-network.sh --help
#
# --controllers  who may SSH into lab VMs: the coordinators' bridge subnets
#                (this host's and its peers', e.g.
#                192.168.100.0/24,192.168.101.0/24). Lab VMs are key-only.
# The lab subnet is 10.250.<N>.0/24, N being this host's ccbr0 third octet
# (192.168.100.x -> 10.250.100.0/24), so hosts never overlap.
#
# Settings are kept in /etc/cloudcore/labnet.conf, and cloudcore-labnet.service
# re-applies them at boot.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BRIDGE=cclab0
CONF=/etc/cloudcore/labnet.conf
HELPER=/usr/local/sbin/cloudcore-labnet
UNIT=/etc/systemd/system/cloudcore-labnet.service
SUDOERS=/etc/sudoers.d/cloudcore-labnet
LEASE_FILE=/var/lib/misc/cloudcore-lab-dnsmasq.leases
PIDFILE=/var/run/cloudcore-lab-dnsmasq.pid
DRY_RUN=0

log() { echo "setup-lab-network: $*" >&2; }
die() { log "$1"; exit "${2:-1}"; }
usage() { sed -n '2,33p' "$0" | sed 's/^# \{0,1\}//'; }
run() { if [[ "${DRY_RUN}" -eq 1 ]]; then echo "+ $*"; else log "$*"; "$@"; fi; }

valid_cidr() {
  python3 -c "import ipaddress,sys; ipaddress.ip_network(sys.argv[1], strict=False)" "$1" 2>/dev/null
}

lab_octet() {
  local a
  a="$(ip -4 -o addr show ccbr0 2>/dev/null | awk '{print $4}' | cut -d. -f3)"
  [[ "${a}" =~ ^[0-9]+$ ]] || die "ccbr0 has no IPv4 address -- run setup-network.sh first"
  echo "${a}"
}

ruleset() {
  local subnet="$1" gw="$2" controllers="$3"
  cat <<EOF
table inet cclab {
  set controllers { type ipv4_addr; flags interval; elements = { ${controllers} } }
  set blocked {
    type ipv4_addr; flags interval
    elements = { 0.0.0.0/8, 10.0.0.0/8, 100.64.0.0/10, 127.0.0.0/8, 169.254.0.0/16,
                 172.16.0.0/12, 192.168.0.0/16, 224.0.0.0/4, 240.0.0.0/4 }
  }
  chain input {
    type filter hook input priority filter; policy accept;
    iifname "${BRIDGE}" ct state established,related accept
    iifname "${BRIDGE}" udp dport 67 accept
    iifname "${BRIDGE}" ip daddr ${gw} udp dport 53 accept
    iifname "${BRIDGE}" ip daddr ${gw} tcp dport 53 accept
    iifname "${BRIDGE}" drop
  }
  chain forward {
    type filter hook forward priority filter; policy accept;
    iifname "${BRIDGE}" meta nfproto ipv6 drop
    oifname "${BRIDGE}" meta nfproto ipv6 drop
    iifname "${BRIDGE}" ct state established,related accept
    oifname "${BRIDGE}" ct state established,related accept
    iifname "${BRIDGE}" ip daddr @blocked drop
    iifname "${BRIDGE}" accept
    oifname "${BRIDGE}" ip saddr @controllers tcp dport { 22, 1022 } ct state new accept
    oifname "${BRIDGE}" drop
  }
}
table bridge cclab {
  set pairs { type ipv4_addr . ipv4_addr; }
  chain forward {
    type filter hook forward priority filter; policy accept;
    meta ibrname "${BRIDGE}" ether type arp accept
    meta ibrname "${BRIDGE}" ip saddr . ip daddr @pairs accept
    meta ibrname "${BRIDGE}" drop
  }
}
table ip cclab_nat {
  chain postrouting {
    type nat hook postrouting priority srcnat; policy accept;
    ip saddr ${subnet} oifname != "${BRIDGE}" masquerade
  }
}
EOF
}

install_helper() {
  local tmp
  tmp="$(mktemp)"
  cat > "${tmp}" <<'EOS'
#!/usr/bin/env bash
# CloudCore lab-network pair helper (installed by api/setup-lab-network.sh).
# The only root action CloudCore's broker may take on the lab network: let a
# run's target and prober reach each other, and undo it.
#   cloudcore-labnet pair add|del IP_A IP_B
#   cloudcore-labnet unpair IP        (every pair involving IP)
#   cloudcore-labnet list
set -euo pipefail
. /etc/cloudcore/labnet.conf
in_lab() { [[ "$1" =~ ^10\.250\.${LAB_OCTET}\.([0-9]{1,3})$ ]] && (( BASH_REMATCH[1] >= 2 && BASH_REMATCH[1] <= 254 )); }
case "${1:-}" in
  pair)
    [[ "${2:-}" == add || "${2:-}" == del ]] || { echo "usage: pair add|del IP_A IP_B" >&2; exit 2; }
    in_lab "${3:-}" && in_lab "${4:-}" && [[ "$3" != "$4" ]] || { echo "both addresses must be distinct lab addresses" >&2; exit 2; }
    verb=add; [[ "$2" == del ]] && verb=delete
    nft "${verb}" element bridge cclab pairs "{ $3 . $4, $4 . $3 }" 2>/dev/null || [[ "$2" == del ]]
    ;;
  unpair)
    in_lab "${2:-}" || { echo "not a lab address" >&2; exit 2; }
    for el in $(nft -j list set bridge cclab pairs | python3 -c '
import json, sys
for o in json.load(sys.stdin)["nftables"]:
    for e in (o.get("set", {}).get("elem") or []):
        a, b = e["concat"]; print(f"{a}.{b}")'); do
      IFS=. read -r a1 a2 a3 a4 b1 b2 b3 b4 <<< "${el}"
      a="${a1}.${a2}.${a3}.${a4}"; b="${b1}.${b2}.${b3}.${b4}"
      if [[ "${a}" == "$2" || "${b}" == "$2" ]]; then nft delete element bridge cclab pairs "{ ${a} . ${b} }"; fi
    done
    ;;
  list) nft list set bridge cclab pairs ;;
  *) echo "usage: cloudcore-labnet pair add|del IP_A IP_B | unpair IP | list" >&2; exit 2 ;;
esac
EOS
  run install -o root -g root -m 0755 "${tmp}" "${HELPER}"
  rm -f "${tmp}"
}

install_sudoers() {
  local user="${CLOUDCORE_BRIDGE_USER:-${SUDO_USER:-}}" tmp line
  [[ -n "${user}" ]] || { log "WARNING: no user for the helper's sudoers grant (run via sudo, or set CLOUDCORE_BRIDGE_USER)"; return 0; }
  line="${user} ALL=(root) NOPASSWD: ${HELPER} pair *, ${HELPER} unpair *, ${HELPER} list"
  tmp="$(mktemp)"; echo "${line}" > "${tmp}"; chmod 440 "${tmp}"
  if visudo -c -f "${tmp}" >/dev/null 2>&1; then run install -o root -g root -m 0440 "${tmp}" "${SUDOERS}"
  else log "WARNING: sudoers line failed validation, not installed: ${line}"; fi
  rm -f "${tmp}"
}

install_unit() {
  local tmp
  tmp="$(mktemp)"
  cat > "${tmp}" <<EOF
[Unit]
Description=CloudCore isolated lab network (cclab0)
After=network.target cloudcore-bridge.service
Wants=cloudcore-bridge.service

[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart=/bin/bash ${SCRIPT_DIR}/setup-lab-network.sh --from-conf
ExecStop=/bin/bash ${SCRIPT_DIR}/setup-lab-network.sh --remove --keep-conf

[Install]
WantedBy=multi-user.target
EOF
  run install -o root -g root -m 0644 "${tmp}" "${UNIT}"
  rm -f "${tmp}"
  run systemctl daemon-reload
  run systemctl enable cloudcore-labnet.service
}

apply() {
  local controllers_csv="$1" octet subnet gw tmp
  octet="$(lab_octet)"; subnet="10.250.${octet}.0/24"; gw="10.250.${octet}.1"
  local c; IFS=, read -ra cs <<< "${controllers_csv}"
  for c in "${cs[@]}"; do valid_cidr "${c}" || die "--controllers: '${c}' is not a CIDR" 2; done

  run mkdir -p /etc/cloudcore
  if [[ "${DRY_RUN}" -eq 1 ]]; then echo "+ write ${CONF}: LAB_OCTET=${octet} CONTROLLERS=${controllers_csv}"
  else printf 'LAB_OCTET=%s\nCONTROLLERS=%s\n' "${octet}" "${controllers_csv}" > "${CONF}"; chmod 644 "${CONF}"; fi

  ip link show "${BRIDGE}" >/dev/null 2>&1 || run ip link add "${BRIDGE}" type bridge
  ip -4 addr show "${BRIDGE}" | grep -qF "${gw}/24" || run ip addr add "${gw}/24" dev "${BRIDGE}"
  run ip link set "${BRIDGE}" up
  run sysctl -qw net.ipv4.ip_forward=1

  # Hosts whose iptables FORWARD policy is DROP (Docker sets it) need the
  # bridge let through there; the real filtering is the nftables table.
  iptables -C FORWARD -i "${BRIDGE}" -j ACCEPT 2>/dev/null || run iptables -I FORWARD -i "${BRIDGE}" -j ACCEPT
  iptables -C FORWARD -o "${BRIDGE}" -j ACCEPT 2>/dev/null || run iptables -I FORWARD -o "${BRIDGE}" -j ACCEPT

  tmp="$(mktemp)"
  ruleset "${subnet}" "${gw}" "$(echo "${controllers_csv}" | sed 's/,/, /g')" > "${tmp}"
  if [[ "${DRY_RUN}" -eq 1 ]]; then echo "+ nft -f (ruleset below)"; cat "${tmp}"
  else
    nft -c -f "${tmp}" || { rm -f "${tmp}"; die "generated nftables ruleset failed its check"; }
    for t in "inet cclab" "bridge cclab" "ip cclab_nat"; do nft delete table ${t} 2>/dev/null || true; done
    run nft -f "${tmp}"
  fi
  rm -f "${tmp}"

  grep -qxF "allow ${BRIDGE}" /etc/qemu/bridge.conf 2>/dev/null || {
    run mkdir -p /etc/qemu
    if [[ "${DRY_RUN}" -eq 1 ]]; then echo "+ append 'allow ${BRIDGE}' to /etc/qemu/bridge.conf"
    else echo "allow ${BRIDGE}" >> /etc/qemu/bridge.conf; fi
  }

  if [[ -f "${PIDFILE}" ]]; then run kill "$(cat "${PIDFILE}")" 2>/dev/null || true; sleep 0.5; fi
  run touch "${LEASE_FILE}"
  # DNS goes straight to public resolvers: lab VMs never see this host's or
  # the LAN's own names.
  run dnsmasq --interface="${BRIDGE}" --bind-interfaces --except-interface=lo \
    --dhcp-range="10.250.${octet}.10,10.250.${octet}.250,12h" \
    --dhcp-option="option:dns-server,${gw}" --server=1.1.1.1 --server=8.8.8.8 --no-resolv \
    --dhcp-leasefile="${LEASE_FILE}" --pid-file="${PIDFILE}" --log-facility=/var/log/cloudcore-lab-dnsmasq.log

  install_helper
  install_sudoers
  [[ -f "${UNIT}" ]] || install_unit
  log "lab network up: ${BRIDGE} ${gw}/24, DHCP 10.250.${octet}.10-250, SSH allowed from ${controllers_csv}"
}

remove() {
  local keep_conf="$1"
  if [[ -f "${PIDFILE}" ]]; then run kill "$(cat "${PIDFILE}")" 2>/dev/null || true; fi
  for t in "inet cclab" "bridge cclab" "ip cclab_nat"; do run nft delete table ${t} 2>/dev/null || true; done
  iptables -C FORWARD -i "${BRIDGE}" -j ACCEPT 2>/dev/null && run iptables -D FORWARD -i "${BRIDGE}" -j ACCEPT
  iptables -C FORWARD -o "${BRIDGE}" -j ACCEPT 2>/dev/null && run iptables -D FORWARD -o "${BRIDGE}" -j ACCEPT
  ip link show "${BRIDGE}" >/dev/null 2>&1 && run ip link delete "${BRIDGE}"
  if [[ "${keep_conf}" -eq 0 ]]; then
    [[ -f "${UNIT}" ]] && { run systemctl disable cloudcore-labnet.service || true; run rm -f "${UNIT}"; }
    run rm -f "${SUDOERS}" "${HELPER}" "${CONF}"
  fi
  log "lab network removed"
}

main() {
  local mode=apply controllers="" keep_conf=0
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --controllers) controllers="${2:-}"; shift 2 ;;
      --from-conf) mode=conf; shift ;;
      --remove) mode=remove; shift ;;
      --keep-conf) keep_conf=1; shift ;;
      --dry-run) DRY_RUN=1; shift ;;
      -h|--help) usage; exit 0 ;;
      *) usage; die "unknown argument: $1" 2 ;;
    esac
  done
  [[ "${EUID}" -eq 0 || "${DRY_RUN}" -eq 1 ]] || die "must run as root (sudo)"
  case "${mode}" in
    remove) remove "${keep_conf}" ;;
    conf) [[ -f "${CONF}" ]] || die "${CONF} not found -- run with --controllers first"
          # shellcheck disable=SC1090
          . "${CONF}"; apply "${CONTROLLERS}" ;;
    apply) [[ -n "${controllers}" ]] || die "--controllers is required (who may SSH into lab VMs)" 2
           apply "${controllers}" ;;
  esac
}

main "$@"
