#!/usr/bin/env bash
# Move CloudCore's artifact cache (api/package-repo/jammy/artifacts: ZIMs,
# model files, VM images -- the storage-hungry, re-downloadable part) onto a
# dedicated data disk, keeping every path that uses it unchanged.
#
# Direct request: "move all storage hungry products of cloudcore to" a new
# disk, after the hub's root filesystem reached 90%.
#
# Layout afterwards:
#   <disk>                      ext4, label cloudcore-data, mounted by UUID
#   /srv/cloudcore-data         its mount point (fstab, nofail)
#   .../artifacts               the cache itself
#   api/package-repo/jammy/artifacts  bind mount of it (cloudcore-repo, the
#                               ZIM updater and every script keep this path)
#   /srv/cloudcore-artifacts    the existing read-only bind for NFS
#                               (setup-artifact-nfs.sh), now of the new copy
#
# Stages, run in order; each is safe to re-run and takes --dry-run:
#   prepare --device DEV --wipe   format DEV (a whole disk, by-id path) as ext4
#                                 and mount it permanently. DESTROYS DEV.
#   copy                          copy the cache across (resumable)
#   verify                        compare every file's contents, both sides
#   switch                        final sync with the repo stopped, then point
#                                 the paths at the new disk
#   rollback                      undo switch: the old copy on the root disk
#                                 is still there until cleanup
#   cleanup --yes                 delete the old copy from the root disk
#
# Usage: sudo api/move-artifacts-to-disk.sh [--dry-run] STAGE [options]
#        api/move-artifacts-to-disk.sh --help
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SRC="${SCRIPT_DIR}/package-repo/jammy/artifacts"
OLD="${SRC}.root-disk-old"
DATA_MNT="/srv/cloudcore-data"
DEST="${DATA_MNT}/artifacts"
NFS_MNT="/srv/cloudcore-artifacts"
LABEL="cloudcore-data"
FSTAB_TAG="# cloudcore-data (move-artifacts-to-disk.sh)"
NFS_FSTAB_TAG="# cloudcore-artifacts (setup-artifact-nfs.sh)"
REPO_UNIT="cloudcore-repo.service"
NFS_UNIT="nfs-server.service"
DRY_RUN=0

log() { echo "move-artifacts: $*" >&2; }
die() { log "$1"; exit "${2:-1}"; }
usage() { sed -n '2,33p' "$0" | sed 's/^# \{0,1\}//'; }
run() { if [[ "${DRY_RUN}" -eq 1 ]]; then echo "+ $*"; else log "$*"; "$@"; fi; }
need_root() { [[ "${EUID}" -eq 0 || "${DRY_RUN}" -eq 1 ]] || die "this stage must run as root (sudo)"; }
owner_of() { stat -c '%U:%G' "$1"; }

# The cache's real location: SRC itself before switch, OLD after it.
cache_src() { if [[ -d "${OLD}" ]]; then echo "${OLD}"; else echo "${SRC}"; fi; }

prepare() {
  local dev="" wipe=0
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --device) dev="${2:-}"; shift 2 ;;
      --wipe) wipe=1; shift ;;
      *) die "prepare: unknown argument: $1" 2 ;;
    esac
  done
  [[ -n "${dev}" ]] || die "prepare: --device is required (use /dev/disk/by-id/..., not /dev/sdX)" 2
  [[ -b "${dev}" ]] || die "prepare: ${dev} is not a block device"
  local real
  real="$(readlink -f "${dev}")"
  [[ "$(lsblk -dno TYPE "${real}")" == disk ]] || die "prepare: ${dev} is not a whole disk"
  # Never the disk holding / (or anything else mounted outside /media).
  local m
  while read -r m; do
    [[ -z "${m}" || "${m}" == /media/* ]] || die "prepare: ${real} has a filesystem mounted at ${m} -- refusing"
  done < <(lsblk -nro MOUNTPOINT "${real}")
  if lsblk -nro NAME,TYPE -s "$(findmnt -no SOURCE /)" | grep -q "^$(basename "${real}") disk$"; then
    die "prepare: ${real} holds the root filesystem -- refusing"
  fi
  if findmnt -rno SOURCE "${DATA_MNT}" >/dev/null 2>&1; then
    log "prepare: ${DATA_MNT} is already mounted ($(findmnt -no SOURCE "${DATA_MNT}")); nothing to do"
    return 0
  fi
  log "prepare: will ERASE ${dev} -> ${real} ($(lsblk -dno SIZE,MODEL "${real}" | xargs))"
  lsblk -o NAME,SIZE,FSTYPE,LABEL,MOUNTPOINT "${real}" >&2
  [[ "${wipe}" -eq 1 || "${DRY_RUN}" -eq 1 ]] || die "prepare: add --wipe to confirm erasing ${real}" 2
  need_root

  while read -r m; do
    [[ -n "${m}" ]] && run umount "${m}"
  done < <(lsblk -nro MOUNTPOINT "${real}")
  run wipefs -a "${real}"
  run parted -s "${real}" mklabel gpt mkpart "${LABEL}" ext4 1MiB 100%
  run udevadm settle
  local part
  part="$(lsblk -nrpo NAME,TYPE "${real}" | awk '$2 == "part" {print $1; exit}')"
  [[ -n "${part}" || "${DRY_RUN}" -eq 1 ]] || die "prepare: no partition appeared on ${real}"
  part="${part:-${real}1}"
  # -m 1: the cache is data, not a system disk; 1% reserve is plenty.
  run mkfs.ext4 -F -q -L "${LABEL}" -m 1 "${part}"
  run udevadm settle
  # A desktop session may auto-mount the new filesystem under /media.
  while read -r m; do
    [[ -n "${m}" ]] && run umount "${m}"
  done < <(lsblk -nro MOUNTPOINT "${part}" 2>/dev/null)
  local uuid="UUID-AFTER-MKFS"
  [[ "${DRY_RUN}" -eq 1 ]] || uuid="$(blkid -s UUID -o value "${part}")"
  run mkdir -p "${DATA_MNT}"
  # nofail: a USB disk that isn't there must never stop the host booting.
  local line="UUID=${uuid} ${DATA_MNT} ext4 defaults,noatime,nofail,x-systemd.device-timeout=15s 0 2"
  if grep -qF "${FSTAB_TAG}" /etc/fstab; then
    run sed -i "\|${FSTAB_TAG}|,+1d" /etc/fstab
  fi
  if [[ "${DRY_RUN}" -eq 1 ]]; then
    echo "+ append to /etc/fstab: ${FSTAB_TAG} / ${line}"
  else
    printf '%s\n%s\n' "${FSTAB_TAG}" "${line}" >> /etc/fstab
  fi
  run systemctl daemon-reload
  run mount "${DATA_MNT}"
  run mkdir -p "${DEST}"
  run chown "$(owner_of "$(cache_src)")" "${DATA_MNT}" "${DEST}"
  log "prepare: done -- ${DATA_MNT} ($(df -h --output=size "${DATA_MNT}" 2>/dev/null | tail -1 | xargs))"
}

check_data_mounted() {
  mountpoint -q "${DATA_MNT}" || die "${DATA_MNT} isn't mounted -- run prepare first"
}

copy() {
  need_root
  check_data_mounted
  local from
  from="$(cache_src)"
  local need have
  need="$(du -sb "${from}" | cut -f1)"
  have="$(df -B1 --output=avail "${DATA_MNT}" | tail -1)"
  (( have > need )) || die "copy: ${DATA_MNT} has $((have / 2**30))GB free, the cache is $((need / 2**30))GB"
  log "copy: ${from}/ -> ${DEST}/ ($((need / 2**30))GB; resumable -- re-run if interrupted)"
  run rsync -aHAX --partial --info=progress2 "${from}/" "${DEST}/"
}

verify() {
  check_data_mounted
  local from
  from="$(cache_src)"
  log "verify: reading every file on both sides (slow: the whole cache, twice)"
  local diffs
  diffs="$(rsync -aHAX --checksum --dry-run --itemize-changes --delete "${from}/" "${DEST}/")"
  if [[ -n "${diffs}" ]]; then
    printf '%s\n' "${diffs}" >&2
    die "verify: the copies differ (above) -- run copy again"
  fi
  log "verify: identical ($(find "${DEST}" -type f | wc -l) files)"
}

fstab_has() { grep -qF "$1" /etc/fstab; }

# On a failed switch: if nothing was moved yet, put the services back;
# otherwise say how to undo.
switch_failed() {
  local rc=$?
  [[ "${rc}" -ne 0 && "${DRY_RUN}" -eq 0 ]] || return 0
  if [[ ! -d "${OLD}" ]]; then
    log "switch failed before anything moved -- restarting what it stopped"
    mountpoint -q "${NFS_MNT}" || mount "${NFS_MNT}" || true
    systemctl start "${NFS_UNIT}" "${REPO_UNIT}" || true
  else
    log "switch failed part-way -- run: sudo $0 rollback"
  fi
}

switch() {
  need_root
  check_data_mounted
  [[ ! -d "${OLD}" ]] || die "switch: already switched (${OLD} exists) -- see rollback/cleanup"
  [[ -n "$(ls -A "${DEST}")" ]] || die "switch: ${DEST} is empty -- run copy and verify first"
  trap switch_failed EXIT

  # Final sync with nothing writing: the repo stopped; the ZIM updater only
  # runs weekly, and a run caught mid-switch would just retry next week.
  run systemctl stop "${REPO_UNIT}"
  run rsync -aHAX --delete "${SRC}/" "${DEST}/"

  # An exported bind that a client has mounted can't be unmounted (found on
  # the first real switch: "target is busy"); NFS clients' hard mounts wait
  # out the minute this takes.
  run systemctl stop "${NFS_UNIT}"
  if mountpoint -q "${NFS_MNT}"; then run umount "${NFS_MNT}"; fi
  local owner
  owner="$(owner_of "${SRC}")"
  run mv "${SRC}" "${OLD}"
  run mkdir "${SRC}"
  run chown "${owner}" "${SRC}"

  local bind="${DEST} ${SRC} none bind,nofail,x-systemd.requires-mounts-for=${DATA_MNT} 0 0"
  if ! fstab_has "${bind}"; then
    if [[ "${DRY_RUN}" -eq 1 ]]; then
      echo "+ append to /etc/fstab (after ${FSTAB_TAG}'s mount): ${bind}"
    else
      sed -i "\|${FSTAB_TAG}|{n;a\\
${bind}
}" /etc/fstab
    fi
  fi
  # The NFS bind must wait for the one above.
  if fstab_has "${NFS_FSTAB_TAG}" && ! grep -A1 -F "${NFS_FSTAB_TAG}" /etc/fstab | grep -q "requires-mounts-for=${SRC}"; then
    run sed -i "\|${NFS_FSTAB_TAG}|{n;s|bind,ro|bind,ro,nofail,x-systemd.requires-mounts-for=${SRC}|}" /etc/fstab
  fi
  run systemctl daemon-reload
  run mount "${SRC}"
  run mount "${NFS_MNT}"
  run systemctl start "${NFS_UNIT}"
  run exportfs -ra
  run systemctl start "${REPO_UNIT}"
  trap - EXIT
  if [[ "${DRY_RUN}" -eq 0 ]]; then
    [[ "$(stat -c %d "${SRC}")" == "$(stat -c %d "${DATA_MNT}")" ]] || die "switch: ${SRC} isn't on ${DATA_MNT}"
    log "switch: done -- ${SRC} and ${NFS_MNT} now on ${DATA_MNT}; old copy kept at ${OLD}"
    log "switch: NFS clients (the kiwix VM) need a remount: their open files were on the old disk"
  fi
}

rollback() {
  need_root
  [[ -d "${OLD}" ]] || die "rollback: nothing to roll back (${OLD} doesn't exist)"
  run systemctl stop "${REPO_UNIT}"
  run systemctl stop "${NFS_UNIT}"
  if mountpoint -q "${NFS_MNT}"; then run umount "${NFS_MNT}"; fi
  if mountpoint -q "${SRC}"; then run umount "${SRC}"; fi
  run rmdir "${SRC}"
  run mv "${OLD}" "${SRC}"
  run sed -i "\|^${DEST} ${SRC} none bind|d" /etc/fstab
  run systemctl daemon-reload
  run mount "${NFS_MNT}"
  run systemctl start "${NFS_UNIT}"
  run exportfs -ra
  run systemctl start "${REPO_UNIT}"
  log "rollback: done -- the cache is back on the root disk; ${DATA_MNT} is still mounted and untouched"
}

cleanup() {
  [[ "${1:-}" == --yes ]] || die "cleanup: add --yes to delete ${OLD}" 2
  need_root
  [[ -d "${OLD}" ]] || { log "cleanup: ${OLD} already gone"; return 0; }
  [[ "$(stat -c %d "${SRC}")" == "$(stat -c %d "${DATA_MNT}")" ]] \
    || die "cleanup: ${SRC} isn't on ${DATA_MNT} -- refusing to delete the only copy"
  run rm -rf --one-file-system "${OLD}"
  log "cleanup: done -- $(df -h --output=avail / | tail -1 | xargs) free on /"
}

main() {
  local stage=""
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --dry-run) DRY_RUN=1; shift ;;
      -h|--help) usage; exit 0 ;;
      prepare|copy|verify|switch|rollback|cleanup) stage="$1"; shift; break ;;
      *) usage; die "unknown argument: $1" 2 ;;
    esac
  done
  [[ -n "${stage}" ]] || { usage; exit 2; }
  "${stage}" "$@"
}

main "$@"
