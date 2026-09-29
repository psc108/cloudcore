#!/usr/bin/env bash
# setup-artifact-nfs.sh — export the host artifact cache over NFS, read-only,
# so guests can read large artifacts in place instead of copying them.
#
# Why: llm-chat's kiwix VM used to download every ZIM from the HTTP package
# repo onto its own disk -- 73GB per build, ~20 minutes of boot -- and adding
# Stack Overflow (107GB) cannot fit on stourport's disk at all as a second
# copy. Exporting the same directory the repo already serves means no copies
# (llm-chat-kiwix-expansion-Phased-Implementation.md, K6).
#
# What it does (idempotent):
#   1. installs nfs-kernel-server
#   2. bind-mounts <repo>/api/package-repo/jammy/artifacts READ-ONLY at
#      /srv/cloudcore-artifacts (fixed path, so templates don't depend on
#      where the repo checkout lives -- it differs on Llwyn-y-Groes)
#   3. exports /srv/cloudcore-artifacts read-only to the lab bridge (plus any
#      --allow CIDR, e.g. a peer's bridge reached over WireGuard), with every
#      client mapped to the directory's owner: the repo lives under a 0750
#      home directory that the usual 'nobody' mapping could not traverse,
#      and the export is read-only so the mapping grants nothing new -- these
#      files are already served to the same guests over HTTP.
#
# Usage: sudo bash api/setup-artifact-nfs.sh [--allow CIDR ...] [--dry-run]
#        sudo bash api/setup-artifact-nfs.sh --remove
#        bash api/setup-artifact-nfs.sh --help
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SRC="${SCRIPT_DIR}/package-repo/jammy/artifacts"
MNT="/srv/cloudcore-artifacts"
EXPORTS_FILE="/etc/exports.d/cloudcore-artifacts.exports"
FSTAB_TAG="# cloudcore-artifacts (setup-artifact-nfs.sh)"
# Fixed fsid: bind mounts should carry an explicit one so the export's
# identity doesn't depend on the underlying filesystem's.
FSID=7001

log() { echo "setup-artifact-nfs: $*" >&2; }
die() { log "$1"; exit "${2:-1}"; }
usage() { sed -n '2,26p' "$0" | sed 's/^# \{0,1\}//'; }
run() { if [[ "${DRY_RUN}" -eq 1 ]]; then echo "+ $*"; else "$@"; fi; }

bridge_cidr() {
  local octet=100
  if [[ -r "${SCRIPT_DIR}/cloudcore.db" ]]; then
    octet="$(python3 - "${SCRIPT_DIR}/cloudcore.db" <<'PY'
import json, sqlite3, sys
try:
    row = sqlite3.connect(f"file:{sys.argv[1]}?mode=ro", uri=True).execute(
        "SELECT value FROM settings WHERE key='network.bridge_subnet_octet'").fetchone()
    print(json.loads(row[0]) if row else 100)
except sqlite3.Error:
    print(100)
PY
)"
  fi
  echo "192.168.${octet}.0/24"
}

valid_private_cidr() {
  python3 -c "import ipaddress,sys; sys.exit(0 if ipaddress.ip_network(sys.argv[1]).is_private else 1)" "$1" 2>/dev/null
}

remove() {
  log "removing export, bind mount and fstab entry (nfs-kernel-server stays installed)"
  run rm -f "${EXPORTS_FILE}"
  run exportfs -ra
  if mountpoint -q "${MNT}"; then run umount "${MNT}"; fi
  run sed -i "\|${FSTAB_TAG}|,+1d" /etc/fstab
}

main() {
  DRY_RUN=0
  local mode=setup
  local -a allow=()
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --allow) allow+=("${2:-}"); shift 2 ;;
      --dry-run) DRY_RUN=1; shift ;;
      --remove) mode=remove; shift ;;
      -h|--help) usage; exit 0 ;;
      *) log "unknown argument: $1"; usage; exit 2 ;;
    esac
  done
  [[ "${EUID}" -eq 0 || "${DRY_RUN}" -eq 1 ]] || die "must run as root (sudo)"
  if [[ "${mode}" == remove ]]; then remove; exit 0; fi

  [[ -d "${SRC}" ]] || die "artifact cache not found at ${SRC}"
  local uid gid
  uid="$(stat -c %u "${SRC}")"
  gid="$(stat -c %g "${SRC}")"

  local -a cidrs=("$(bridge_cidr)")
  local c
  for c in "${allow[@]}"; do
    valid_private_cidr "${c}" || die "--allow ${c}: not a private CIDR -- refusing to export to it" 2
    cidrs+=("${c}")
  done

  log "installing nfs-kernel-server"
  run env DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends nfs-kernel-server

  log "read-only bind mount ${SRC} -> ${MNT}"
  run mkdir -p "${MNT}"
  if ! grep -qF "${FSTAB_TAG}" /etc/fstab; then
    if [[ "${DRY_RUN}" -eq 1 ]]; then
      echo "+ append to /etc/fstab: ${SRC} ${MNT} none bind,ro 0 0"
    else
      printf '%s\n%s %s none bind,ro 0 0\n' "${FSTAB_TAG}" "${SRC}" "${MNT}" >> /etc/fstab
    fi
    # Otherwise systemd keeps generating mount units from the old fstab.
    run systemctl daemon-reload
  fi
  if ! mountpoint -q "${MNT}"; then
    run mount "${MNT}"
  fi

  local opts="ro,all_squash,anonuid=${uid},anongid=${gid},no_subtree_check,fsid=${FSID}"
  local line="${MNT}"
  for c in "${cidrs[@]}"; do line+=" ${c}(${opts})"; done
  log "export: ${line}"
  run mkdir -p "$(dirname "${EXPORTS_FILE}")"
  if [[ "${DRY_RUN}" -eq 1 ]]; then
    echo "+ write ${EXPORTS_FILE}: ${line}"
  else
    printf '# Managed by api/setup-artifact-nfs.sh -- read-only artifact cache\n%s\n' "${line}" > "${EXPORTS_FILE}"
  fi
  run systemctl enable --now nfs-server
  run exportfs -ra
  if [[ "${DRY_RUN}" -eq 0 ]]; then
    exportfs -v | grep -F "${MNT}" >&2 || die "export not active after exportfs -ra"
    # Prove the mount really is read-only.
    if touch "${MNT}/.rw-probe" 2>/dev/null; then
      rm -f "${MNT}/.rw-probe"
      die "${MNT} is writable -- the bind mount must be read-only"
    fi
    log "done: guests on ${cidrs[*]} can mount 192.168.$(bridge_cidr | cut -d. -f3).1:${MNT} (nfs4, ro)"
  fi
}

main "$@"
