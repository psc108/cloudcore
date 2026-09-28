#!/usr/bin/env bash
# setup-capture-firewall.sh — lets machines on the lab LAN reach the
# llm-chat capture listener (examples_listener.py, port 8083) through ufw,
# so students can use scripts/llm-capture-client/ from their own laptops
# (llm-chat Stage 13).
#
# Scoped to one CIDR on purpose: the listener only serves the capture and
# self-registration routes (server.py's port gate) and every one of them is
# token-authenticated, but it is plain HTTP, so it should never be open
# beyond the LAN. Guests and paired peers already reach it over ccbr0 and
# WireGuard; this script doesn't touch those paths.
#
# Usage: sudo bash api/setup-capture-firewall.sh --lan-cidr 192.168.1.0/24 [--dry-run]
#        sudo bash api/setup-capture-firewall.sh --lan-cidr 192.168.1.0/24 --remove
#        bash api/setup-capture-firewall.sh --help
set -euo pipefail

PORT=8083
COMMENT="CloudCore llm-chat capture client (Stage 13)"

usage() {
  sed -n '2,15p' "$0" | sed 's/^# \{0,1\}//'
}

log() { echo "setup-capture-firewall: $*" >&2; }

main() {
  local cidr="" dry_run=0 remove=0
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --lan-cidr) cidr="${2:-}"; shift 2 ;;
      --dry-run)  dry_run=1; shift ;;
      --remove)   remove=1; shift ;;
      -h|--help)  usage; exit 0 ;;
      *) log "unknown argument: $1"; usage; exit 2 ;;
    esac
  done

  if [[ -z "${cidr}" ]]; then
    log "--lan-cidr is required (e.g. 192.168.1.0/24)"; exit 2
  fi
  if ! python3 -c "import ipaddress,sys; n=ipaddress.ip_network(sys.argv[1], strict=True); sys.exit(0 if n.is_private else 1)" "${cidr}" 2>/dev/null; then
    log "${cidr} is not a valid private network CIDR -- refusing to open plain-HTTP port ${PORT} to it"; exit 2
  fi
  if ! command -v ufw >/dev/null; then
    log "ufw is not installed; open tcp/${PORT} from ${cidr} in your own firewall instead"; exit 1
  fi
  if [[ "${EUID}" -ne 0 && "${dry_run}" -eq 0 ]]; then
    log "must run as root (sudo)"; exit 1
  fi

  # `systemctl is-active ufw` only says the unit ran at boot; whether rules
  # are enforced is `ufw status` (found live: unit active, firewall
  # inactive, so an added rule changed nothing).
  if [[ "${dry_run}" -eq 0 ]] && ufw status | grep -q '^Status: inactive'; then
    log "WARNING: ufw is INACTIVE -- this rule will be stored but not enforced, and port ${PORT}"
    log "is currently reachable from every network this host is on. Enabling ufw applies a"
    log "default-deny to ALL incoming traffic; review what else this host serves before doing so."
  fi

  local rule=(from "${cidr}" to any port "${PORT}" proto tcp)
  if [[ "${remove}" -eq 1 ]]; then
    log "removing: allow tcp/${PORT} from ${cidr}"
    [[ "${dry_run}" -eq 1 ]] && { echo "ufw delete allow ${rule[*]}"; exit 0; }
    ufw delete allow "${rule[@]}"
  else
    log "adding (idempotent -- ufw skips an existing identical rule): allow tcp/${PORT} from ${cidr}"
    [[ "${dry_run}" -eq 1 ]] && { echo "ufw allow ${rule[*]} comment '${COMMENT}'"; exit 0; }
    ufw allow "${rule[@]}" comment "${COMMENT}"
  fi
  ufw status numbered | grep -E "(^Status|${PORT})" >&2 || true
}

main "$@"
