#!/usr/bin/env bash
# setup-host-firewall.sh — turn on ufw on a CloudCore host from an editable
# inventory of what the host serves, without cutting off the lab, the peer
# link, or the other projects running on it.
#
# Workflow (same on every host, e.g. stourport and Llwyn-y-Groes):
#   1. sudo bash api/setup-host-firewall.sh --lan-cidr 192.168.1.0/24 --scan
#      Writes the inventory: one line per port open right now, kept at
#      scope "any" so nothing that works today stops working, plus the
#      ports CloudCore needs even when idle.
#   2. Edit the inventory: delete a line to close that port, or narrow
#      "any" to "lan", "bridge" or a CIDR. Annotations say what each is.
#   3. sudo bash api/setup-host-firewall.sh --lan-cidr 192.168.1.0/24 --apply
#      Makes ufw match the file exactly (this script's own earlier rules
#      are replaced) and arms a 5-minute auto-rollback.
#   4. Check the lab, peer link and your other projects still work, then:
#      sudo bash api/setup-host-firewall.sh --confirm
#   Escape hatch at any time: sudo bash api/setup-host-firewall.sh --disable
#
# Without --scan/--apply/--confirm/--disable it prints the plan and changes
# nothing.
#
# Options:
#   --lan-cidr CIDR       the lab LAN (private range)
#   --inventory PATH      default /etc/cloudcore/host-firewall.inventory
#   --scan                (re)write the inventory from the host's listeners
#   --force               let --scan overwrite an existing inventory
#   --apply               apply the inventory (root)
#   --rollback-seconds N  auto-disable after N seconds unless --confirm (default 300; 0 = off)
#   --confirm             keep the firewall on (cancel the rollback)
#   --disable             ufw disable (rules kept)
#
# Inventory line:  PORT/PROTO  SCOPE  # note
#   SCOPE: any | lan (the --lan-cidr, on the interfaces that carry it)
#        | bridge (lab guests: ccbr0 and the cc0 peer tunnel) | ccbr0 | a CIDR
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DB="${SCRIPT_DIR}/cloudcore.db"
BRIDGE="ccbr0"          # api/setup-network.sh
WG_IFACE="cc0"          # api/wireguard.py
ROLLBACK_UNIT="cloudcore-fw-rollback"
COMMENT="cloudcore-host-fw"

LAN_CIDR=""
INVENTORY="/etc/cloudcore/host-firewall.inventory"
ROLLBACK_S=300
FORCE=0

log() { echo "setup-host-firewall: $*" >&2; }
die() { log "$1"; exit "${2:-1}"; }
usage() { sed -n '2,38p' "$0" | sed 's/^# \{0,1\}//'; }
need_root() { [[ "${EUID}" -eq 0 ]] || die "$1 must run as root (sudo)"; }

setting() {
  local key="$1" default="$2"
  if [[ -r "${DB}" ]]; then
    python3 - "${DB}" "${key}" "${default}" <<'PY'
import json, sqlite3, sys
db, key, default = sys.argv[1:]
try:
    row = sqlite3.connect(f"file:{db}?mode=ro", uri=True).execute(
        "SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    print(json.loads(row[0]) if row else default)
except sqlite3.Error:
    print(default)
PY
  else
    echo "${default}"
  fi
}

# What CloudCore itself needs: "port/proto|designed scope|role".
cloudcore_ports() {
  local peer_port wg_port
  peer_port="$(setting network.peer_listener_port 8082)"
  wg_port="$(setting network.wg_listen_port 51820)"
  cat <<EOF
53/udp|bridge|CloudCore DNS for guests (api/dns_server.py)
53/tcp|bridge|CloudCore DNS for guests
67/udp|ccbr0|CloudCore guest DHCP on ccbr0
8090/tcp|bridge|CloudCore package repo + artifact cache
3100/tcp|bridge|Loki (guest promtail pushes here)
3000/tcp|bridge|Grafana (setup-logging-service.sh)
8083/tcp|bridge|CloudCore capture listener (guests; LAN clients need lan too)
8900/tcp|bridge|Sentinel (guest verify-proxy pushes here)
${peer_port}/tcp|lan|CloudCore peer listener (network.peer_listener_port)
${wg_port}/udp|lan|WireGuard to paired peers (network.wg_listen_port)
5353/udp|lan|mDNS peer discovery (zeroconf)
22/tcp|lan|SSH
EOF
}

lan_ifaces() {
  python3 - "${LAN_CIDR}" <<'PY'
import ipaddress, json, subprocess, sys
net = ipaddress.ip_network(sys.argv[1])
for link in json.loads(subprocess.run(["ip", "-j", "-4", "addr"], capture_output=True, text=True).stdout):
    if any(ipaddress.ip_address(a["local"]) in net for a in link.get("addr_info", [])):
        print(link["ifname"])
PY
}

docker_published() {
  command -v docker >/dev/null || return 0
  docker ps --format '{{.Names}} {{.Ports}}' 2>/dev/null | while read -r name ports; do
    grep -oE ':[0-9]+->[0-9]+/(tcp|udp)' <<<"${ports}" | sed -E "s#^:([0-9]+)->[0-9]+/(tcp|udp)\$#\1/\2 ${name}#"
  done | sort -u || true
}

# Current non-loopback listeners as "port/proto addr process", one per socket.
listeners() {
  { ss -Hltnp 2>/dev/null | awk '{print "tcp", $4, $6}'; ss -Hlunp 2>/dev/null | awk '{print "udp", $4, $6}'; } \
    | sed -E 's/users:\(\("([^"]+)".*/\1/' \
    | while read -r proto addr proc; do
        case "${addr}" in 127.*|\[::1\]*|\[::ffff:127.*|*%lo:*) continue ;; esac
        port="${addr##*:}"
        [[ "${proto}" == udp && "${port}" == 546 ]] && continue   # DHCPv6 client, not a service
        echo "${port}/${proto} ${addr} ${proc:--}"
      done | sort -u
}

# UDP sockets in the kernel's ephemeral range are usually client sockets
# (a resolver, a browser), not services, and get a new port every time.
# But real services live there too -- WireGuard's default 51820 does -- so
# only ports CloudCore doesn't know about are treated this way, and they
# are written commented-out rather than dropped.
is_ephemeral_udp() {
  local port="$1" lo hi
  read -r lo hi < /proc/sys/net/ipv4/ip_local_port_range
  [[ "${port}" -ge "${lo}" && "${port}" -le "${hi}" ]]
}

# --- scan: write the inventory -----------------------------------------------------
scan() {
  need_root "--scan"
  if [[ -e "${INVENTORY}" && "${FORCE}" -eq 0 ]]; then
    die "${INVENTORY} already exists (it may hold your edits); pass --force to overwrite" 1
  fi
  local tmp; tmp="$(mktemp)"
  trap 'rm -f "${tmp}"' RETURN
  local docker_ports; docker_ports="$(docker_published)"
  declare -A seen_addr=() seen_proc=() role=() designed=()
  local line key scope r
  while IFS='|' read -r key scope r; do role["${key}"]="${r}"; designed["${key}"]="${scope}"; done < <(cloudcore_ports)

  local port_proto addr proc
  declare -A libvirt=()
  while read -r port_proto addr proc; do
    if [[ "${addr}" == *"%virbr"* || "${addr}" == 192.168.123.* ]]; then
      libvirt["${port_proto}"]=1; continue
    fi
    seen_addr["${port_proto}"]+="${seen_addr[${port_proto}]:+, }${addr}"
    [[ "${proc}" != "-" && ",${seen_proc[${port_proto}]:-}," != *",${proc},"* ]] \
      && seen_proc["${port_proto}"]+="${seen_proc[${port_proto}]:+,}${proc}"
  done < <(listeners)

  {
    echo "# CloudCore host firewall inventory for $(hostname), scanned $(date -Is)"
    echo "# LAN: ${LAN_CIDR}. Edit, then: sudo bash ${SCRIPT_DIR}/setup-host-firewall.sh --lan-cidr ${LAN_CIDR} --apply"
    echo "# Delete a line to close that port; narrow 'any' to lan | bridge | ccbr0 | a CIDR."
    echo "# Loopback is always allowed. Outgoing traffic is not affected."
    echo "# Careful narrowing a port that a Docker container on this host uses: container -> host"
    echo "# traffic arrives on Docker's own bridges, so 'lan'/'bridge' would cut it off; 'any' keeps it."
    echo "#"
    echo "# PORT/PROTO  SCOPE   # what it is"
    echo
    echo "# --- open on this host right now (kept at 'any' so nothing changes until you edit) ---"
    for key in $(printf '%s\n' "${!seen_addr[@]}" | sort -t/ -k1,1n -k2); do
      local who="${seen_proc[${key}]:-owner unknown}" note="" dock
      dock="$(awk -v k="${key}" '$1==k {print $2}' <<<"${docker_ports}" | paste -sd, -)"
      if [[ -n "${dock}" ]]; then
        echo "# ${key}  -- Docker-published by container(s) ${dock}: Docker's own iptables rules bypass ufw, so no ufw rule can restrict it"
        continue
      fi
      if [[ -z "${role[${key}]:-}" && "${key}" == */udp ]] && is_ephemeral_udp "${key%/*}"; then
        echo "# ${key}  -- probably an ephemeral client socket (${who} on ${seen_addr[${key}]}); uncomment as '${key} any' if it is a real service"
        continue
      fi
      [[ -n "${role[${key}]:-}" ]] && note=" | CloudCore: ${role[${key}]}; could be narrowed to '${designed[${key}]}'"
      local where="${seen_addr[${key}]}"
      [[ "$(tr -cd ',' <<<"${where}" | wc -c)" -ge 4 ]] && where="$(cut -d, -f1-4 <<<"${where}"), ..."
      printf '%-12s %-7s # %s on %s%s\n' "${key}" any "${who}" "${where}" "${note}"
    done
    echo
    echo "# --- needed by CloudCore but not listening right now (e.g. no guests or no peer tunnel yet) ---"
    while IFS='|' read -r key scope r; do
      [[ -n "${seen_addr[${key}]:-}" ]] && continue
      printf '%-12s %-7s # %s\n' "${key}" "${scope}" "${r}"
    done < <(cloudcore_ports)
    if [[ ${#libvirt[@]} -gt 0 ]]; then
      echo
      echo "# --- libvirt's own network (virbr0): libvirt inserts its own rules, nothing needed here ---"
      for key in "${!libvirt[@]}"; do echo "# ${key}  -- libvirt"; done
    fi
  } > "${tmp}"

  mkdir -p "$(dirname "${INVENTORY}")"
  install -m 0644 "${tmp}" "${INVENTORY}"
  log "wrote ${INVENTORY} -- review and edit it, then run --apply"
  cat "${INVENTORY}" >&2
}

# --- read the inventory ----------------------------------------------------------------
# Emits "port proto scope" for every rule line; dies on a malformed one.
read_inventory() {
  [[ -r "${INVENTORY}" ]] || die "no inventory at ${INVENTORY}; run --scan first" 1
  local n=0 line spec scope port proto
  while IFS= read -r line || [[ -n "${line}" ]]; do
    n=$((n + 1))
    line="${line%%#*}"
    read -r spec scope _rest <<<"${line}" || true
    [[ -z "${spec:-}" ]] && continue
    port="${spec%/*}"; proto="${spec#*/}"
    [[ "${port}" =~ ^[0-9]+$ && "${proto}" =~ ^(tcp|udp)$ ]] || die "${INVENTORY}:${n}: bad port/proto '${spec}'" 2
    if ! [[ "${scope:-}" =~ ^(any|lan|bridge|ccbr0)$ ]]; then
      python3 -c "import ipaddress,sys; ipaddress.ip_network(sys.argv[1])" "${scope:-x}" 2>/dev/null \
        || die "${INVENTORY}:${n}: bad scope '${scope:-}' (any | lan | bridge | ccbr0 | CIDR)" 2
    fi
    echo "${port} ${proto} ${scope}"
  done < "${INVENTORY}"
}

ufw_commands() {
  local -a lan_if; mapfile -t lan_if < <(lan_ifaces)
  local port proto scope i c
  while read -r port proto scope; do
    c="comment '${COMMENT}: ${port}/${proto} ${scope}'"
    case "${scope}" in
      any)    echo "ufw allow ${port}/${proto} ${c}" ;;
      bridge) echo "ufw allow in on ${BRIDGE} to any port ${port} proto ${proto} ${c}"
              echo "ufw allow in on ${WG_IFACE} to any port ${port} proto ${proto} ${c}" ;;
      ccbr0)  echo "ufw allow in on ${BRIDGE} to any port ${port} proto ${proto} ${c}" ;;
      lan)
        [[ ${#lan_if[@]} -gt 0 ]] || die "no interface on this host has an address in ${LAN_CIDR}" 2
        for i in "${lan_if[@]}"; do
          if [[ "${port}/${proto}" == 5353/udp ]]; then   # link-local multicast, IPv4 and IPv6
            echo "ufw allow in on ${i} to any port ${port} proto ${proto} ${c}"
          else
            echo "ufw allow in on ${i} from ${LAN_CIDR} to any port ${port} proto ${proto} ${c}"
          fi
        done ;;
      *)      echo "ufw allow from ${scope} to any port ${port} proto ${proto} ${c}" ;;
    esac
  done < <(read_inventory)
  # Traffic routed THROUGH this host for guests and the peer tunnel.
  # setup-network.sh / wireguard.py already insert FORWARD ACCEPTs for
  # these; ufw's routed default is deny, so these make it explicit and
  # hold even if ccbr0/cc0 come up after ufw. Not optional.
  for i in "${BRIDGE}" "${WG_IFACE}"; do
    echo "ufw route allow in on ${i} comment '${COMMENT}: route ${i}'"
    echo "ufw route allow out on ${i} comment '${COMMENT}: route ${i}'"
  done
}

# Listeners open now that no inventory line allows (by port/proto only).
closing_ports() {
  local allowed; allowed="$(read_inventory | awk '{print $1"/"$2}' | sort -u)"
  local docker_ports; docker_ports="$(docker_published | awk '{print $1}')"
  local known; known="$(cloudcore_ports | cut -d'|' -f1)"
  listeners | while read -r pp addr proc; do
    [[ "${addr}" == *"%virbr"* || "${addr}" == 192.168.123.* ]] && continue
    if [[ "${pp}" == */udp ]] && ! grep -qx "${pp}" <<<"${known}" && is_ephemeral_udp "${pp%/*}"; then continue; fi
    grep -qx "${pp}" <<<"${allowed}" && continue
    grep -qx "${pp}" <<<"${docker_ports}" && continue
    echo "${pp} ${addr} ${proc}"
  done
}

ssh_session_check() {
  [[ -n "${SSH_CONNECTION:-}" ]] || return 0
  local src="${SSH_CONNECTION%% *}" port
  port="$(awk '{print $4}' <<<"${SSH_CONNECTION}")"
  # Read first, then feed: piping straight in let python's early exit
  # SIGPIPE read_inventory, which pipefail turned into a false lock-out.
  local parsed; parsed="$(read_inventory)"
  python3 -c "
import ipaddress, sys
src, port, lan = ipaddress.ip_address(sys.argv[1]), sys.argv[2], ipaddress.ip_network(sys.argv[3])
for line in sys.stdin:
    p, proto, scope = line.split()
    if p != port or proto != 'tcp':
        continue
    if scope == 'any' or (scope == 'lan' and src in lan) or \
       (scope not in ('lan', 'bridge', 'ccbr0') and src in ipaddress.ip_network(scope)):
        sys.exit(0)
sys.exit(1)" "${src}" "${port}" "${LAN_CIDR}" <<<"${parsed}" \
    || die "your SSH session (${src} -> port ${port}) is not allowed by ${INVENTORY}: applying would lock you out" 1
  log "your SSH session from ${src} stays allowed"
}

# Deletes every ufw rule this script added before, so the inventory is the
# single source of truth (a line removed from the file really closes it).
remove_own_rules() {
  local nums
  nums="$(ufw status numbered | grep -F "# ${COMMENT}:" | sed -nE 's/^\[ *([0-9]+)\].*/\1/p' | sort -rn)"
  local n
  for n in ${nums}; do ufw --force delete "${n}" >/dev/null; done
  [[ -n "${nums}" ]] && log "removed $(wc -w <<<"${nums}") rule(s) from a previous --apply" || true
}

post_checks() {
  local ok=1 i
  local status; status="$(ufw status)"   # not piped into grep -q: SIGPIPE + pipefail
  grep -q '^Status: active' <<<"${status}" || { log "CHECK FAILED: ufw is not active"; ok=0; }
  for i in "${BRIDGE}" "${WG_IFACE}"; do
    ip link show "${i}" >/dev/null 2>&1 || { log "check: ${i} not present (fine if no guests / no peer tunnel yet)"; continue; }
    if iptables -C FORWARD -i "${i}" -j ACCEPT 2>/dev/null; then
      log "check: FORWARD ACCEPT for ${i} still in place"
    else
      log "CHECK FAILED: FORWARD ACCEPT for ${i} is missing -- guest/peer routing may be broken"; ok=0
    fi
  done
  [[ "${ok}" -eq 1 ]]
}

show_plan() {
  log "host $(hostname), LAN ${LAN_CIDR} on: $(lan_ifaces | tr '\n' ' '), inventory ${INVENTORY}"
  log "ufw rules the inventory produces (defaults: deny incoming, allow outgoing, deny routed):"
  ufw_commands | sed 's/^/  /' >&2
  local closing; closing="$(closing_ports)"
  if [[ -n "${closing}" ]]; then
    log "open now but NOT in the inventory -- these will be closed:"
    sed 's/^/  /' <<<"${closing}" >&2
  else
    log "every port open now is in the inventory"
  fi
}

apply() {
  need_root "--apply"
  ssh_session_check
  show_plan
  # Build the whole command list first so a malformed inventory fails
  # before anything has been changed.
  local cmds; cmds="$(ufw_commands)"
  remove_own_rules
  log "setting defaults: deny incoming, allow outgoing, deny routed"
  ufw default deny incoming >/dev/null
  ufw default allow outgoing >/dev/null
  ufw default deny routed >/dev/null
  local cmd
  while IFS= read -r cmd; do
    eval "${cmd}" >/dev/null
  done <<<"${cmds}"
  log "added $(wc -l <<<"${cmds}") rule(s)"

  if [[ "${ROLLBACK_S}" -gt 0 ]]; then
    systemctl stop "${ROLLBACK_UNIT}.timer" 2>/dev/null || true
    systemctl reset-failed "${ROLLBACK_UNIT}.service" "${ROLLBACK_UNIT}.timer" 2>/dev/null || true
    systemd-run --quiet --unit "${ROLLBACK_UNIT}" --on-active="${ROLLBACK_S}" "$(command -v ufw)" disable
    log "auto-rollback armed: ufw will be DISABLED in ${ROLLBACK_S}s unless you run: sudo bash $0 --confirm"
  fi
  log "enabling ufw"
  ufw --force enable >/dev/null
  if post_checks; then
    log "post-apply checks passed. Now check the lab (guest boot, peer link) and your other projects, then --confirm."
  else
    log "post-apply checks FAILED -- disabling ufw now"
    systemctl stop "${ROLLBACK_UNIT}.timer" 2>/dev/null || true
    ufw disable
    exit 1
  fi
  ufw status verbose >&2
}

main() {
  local mode=plan
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --lan-cidr) LAN_CIDR="${2:-}"; shift 2 ;;
      --inventory) INVENTORY="${2:-}"; shift 2 ;;
      --scan) mode=scan; shift ;;
      --force) FORCE=1; shift ;;
      --apply) mode=apply; shift ;;
      --rollback-seconds) ROLLBACK_S="${2:-}"; shift 2 ;;
      --confirm) mode=confirm; shift ;;
      --disable) mode=disable; shift ;;
      -h|--help) usage; exit 0 ;;
      *) log "unknown argument: $1"; usage; exit 2 ;;
    esac
  done
  command -v ufw >/dev/null || die "ufw is not installed" 1

  case "${mode}" in
    confirm)
      need_root "--confirm"
      if systemctl stop "${ROLLBACK_UNIT}.timer" 2>/dev/null; then
        log "auto-rollback cancelled; the firewall stays on"
      else
        log "no pending auto-rollback (already confirmed, expired, or never armed)"
      fi
      exit 0 ;;
    disable)
      need_root "--disable"
      systemctl stop "${ROLLBACK_UNIT}.timer" 2>/dev/null || true
      log "disabling ufw (rules are kept for the next --apply)"
      ufw disable
      exit 0 ;;
  esac

  [[ -n "${LAN_CIDR}" ]] || die "--lan-cidr is required (e.g. 192.168.1.0/24)" 2
  python3 -c "import ipaddress,sys; sys.exit(0 if ipaddress.ip_network(sys.argv[1]).is_private else 1)" "${LAN_CIDR}" 2>/dev/null \
    || die "${LAN_CIDR} is not a valid private network CIDR" 2
  [[ "${ROLLBACK_S}" =~ ^[0-9]+$ ]] || die "--rollback-seconds must be a whole number" 2

  # Validate here, in the main shell: the same parsing also runs inside
  # process substitutions later, where a die() would only end a subshell
  # and a bad line would be silently skipped (found testing this script).
  if [[ "${mode}" != scan && -r "${INVENTORY}" ]]; then
    read_inventory >/dev/null
    local parsed; parsed="$(read_inventory)"   # not piped into grep -q: SIGPIPE + pipefail
    if grep -q ' lan$' <<<"${parsed}" && [[ -z "$(lan_ifaces)" ]]; then
      die "the inventory uses 'lan' but no interface on this host has an address in ${LAN_CIDR}" 2
    fi
  fi

  case "${mode}" in
    scan)  scan ;;
    apply) apply ;;
    plan)
      if [[ -r "${INVENTORY}" ]]; then
        show_plan
      else
        log "no inventory at ${INVENTORY} yet -- run with --scan (as root) to create it"
      fi
      log "plan only -- nothing changed" ;;
  esac
}

main "$@"
