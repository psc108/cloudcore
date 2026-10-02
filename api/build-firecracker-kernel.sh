#!/usr/bin/env bash
# build-firecracker-kernel.sh — build the llm-chat lab's Firecracker guest
# kernel: Firecracker's own CI config plus api/firecracker-kernel-lab.config
# (llm-chat-lab-sandbox-Phased-Implementation.md, L1).
#
# Why: Firecracker's stock CI kernel has no nf_tables, none of the xtables
# matches ufw/fail2ban use, no TUN/WireGuard/FUSE/NFSD/AppArmor and no module
# support, so common admin advice fails in the lab even once installed.
#
# How: the base config is read out of the currently pinned kernel's own
# embedded /proc/config.gz, so the result is exactly that config plus the
# fragment. The build runs on a throwaway CloudCore ubuntu-22.04 instance
# (jammy's gcc 11.4, the compiler the base kernel was built with), the same
# pattern as build-firecracker-rootfs.sh. Run by hand, offline; the output is
# a pinned artifact the coordinator downloads like any other.
#
# The build fails if any option in the fragment is not enabled after
# `make olddefconfig`: Kconfig silently drops options whose dependencies
# aren't met, and a lab kernel quietly missing nf_tables is the bug this
# exists to fix.
#
# Usage: api/build-firecracker-kernel.sh [--version 6.1.155] [--flavor standard.medium]
#                                        [--base-kernel PATH] [--dry-run]
#        api/build-firecracker-kernel.sh --help
#
# Output: api/package-repo/jammy/artifacts/firecracker-vmlinux-<version>-lab
#         and its sha256 on stdout -- pin it in examples/llm-chat/variables.tf
#         (firecracker_kernel_name/_sha256) and the Ansible playbook.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CLOUDCORE_API_URL="${CLOUDCORE_API_URL:-http://127.0.0.1:8080}"
# F-201: no default token -- from the environment or ~/.config/cloudcore/api.env.
CLOUDCORE_API_TOKEN="${CLOUDCORE_API_TOKEN:-$(sed -n 's/^CLOUDCORE_API_TOKEN=//p' "${HOME}/.config/cloudcore/api.env" 2>/dev/null)}"
[[ -n "${CLOUDCORE_API_TOKEN}" ]] || { echo "CLOUDCORE_API_TOKEN not set and ${HOME}/.config/cloudcore/api.env not found" >&2; exit 1; }
KEY="${SCRIPT_DIR}/keys/cloudcore_ed25519"
ARTIFACTS="${SCRIPT_DIR}/package-repo/jammy/artifacts"
FRAGMENT="${SCRIPT_DIR}/firecracker-kernel-lab.config"
KERNEL_CDN="https://cdn.kernel.org/pub/linux/kernel"

log() { echo "build-firecracker-kernel: $*" >&2; }
die() { log "$1"; exit "${2:-1}"; }
usage() { sed -n '2,29p' "$0" | sed 's/^# \{0,1\}//'; }

api() {
  local method="$1" path="$2" body="${3:-}"
  local -a args=(-sf -X "${method}" "${CLOUDCORE_API_URL}${path}"
                 -H "Authorization: Bearer ${CLOUDCORE_API_TOKEN}")
  [[ -n "${body}" ]] && args+=(-H "Content-Type: application/json" -d "${body}")
  curl "${args[@]}"
}

json_field() { python3 -c "import json,sys; print(json.load(sys.stdin).get('$1') or '')"; }

# The base config, straight from the pinned kernel's IKCONFIG blob.
extract_config() {
  python3 - "$1" "$2" <<'PY'
import gzip, sys
b = open(sys.argv[1], "rb").read()
start = b.find(b"IKCFG_ST")
end = b.find(b"IKCFG_ED", start)
if start < 0 or end < 0:
    sys.exit(f"{sys.argv[1]}: no embedded config (CONFIG_IKCONFIG off)")
open(sys.argv[2], "wb").write(gzip.decompress(b[start + 8:end]))
PY
}

cleanup() {
  local rc=$?
  if [[ -n "${INSTANCE_ID:-}" ]]; then
    log "tearing down builder ${INSTANCE_ID}"
    api DELETE "/v1/instances/${INSTANCE_ID}" >/dev/null || true
    sleep 5
  fi
  [[ -n "${SG_ID:-}" ]] && { api DELETE "/v1/security-groups/${SG_ID}" >/dev/null || true; }
  [[ -n "${SUBNET_ID:-}" ]] && { api DELETE "/v1/subnets/${SUBNET_ID}" >/dev/null || true; }
  [[ -n "${VPC_ID:-}" ]] && { api DELETE "/v1/vpcs/${VPC_ID}" >/dev/null || true; }
  [[ -n "${WORK:-}" ]] && rm -rf "${WORK}"
  exit "${rc}"
}

main() {
  local version="6.1.155" flavor="standard.medium" base="" dry_run=0
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --version) version="${2:?--version needs a value}"; shift 2 ;;
      --flavor) flavor="${2:?--flavor needs a value}"; shift 2 ;;
      --base-kernel) base="${2:?--base-kernel needs a value}"; shift 2 ;;
      --dry-run) dry_run=1; shift ;;
      -h|--help) usage; exit 0 ;;
      *) log "unknown argument: $1"; usage; exit 2 ;;
    esac
  done
  [[ "${version}" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] || die "--version must look like 6.1.155" 2
  base="${base:-${ARTIFACTS}/firecracker-vmlinux-${version}}"
  [[ -f "${base}" ]] || die "base kernel not found: ${base} (pass --base-kernel)"
  [[ -f "${FRAGMENT}" ]] || die "fragment not found: ${FRAGMENT}"
  [[ -f "${KEY}" ]] || die "builder SSH key not found: ${KEY}"

  local out="${ARTIFACTS}/firecracker-vmlinux-${version}-lab"
  local major="${version%%.*}"
  local tarball="linux-${version}.tar.xz"

  trap cleanup EXIT
  WORK="$(mktemp -d)"
  extract_config "${base}" "${WORK}/base.config"
  log "base config: $(wc -l < "${WORK}/base.config") lines from ${base}"

  local expected
  expected="$(curl -sf "${KERNEL_CDN}/v${major}.x/sha256sums.asc" | awk -v f="${tarball}" '$2 == f {print $1}')"
  [[ "${expected}" =~ ^[0-9a-f]{64}$ ]] || die "no published sha256 for ${tarball}"
  log "upstream ${tarball} sha256 ${expected}"

  if [[ "${dry_run}" -eq 1 ]]; then
    log "dry run: would build ${tarball} on a ${flavor} builder with $(grep -c '^CONFIG_' "${FRAGMENT}") fragment options -> ${out}"
    exit 0
  fi

  local suffix="cloudcore-fckernel-builder-$(date +%s)"
  log "standing up a throwaway ${flavor} builder (${suffix})"
  VPC_ID="$(api POST /v1/vpcs "{\"name\":\"${suffix}-vpc\",\"cidr_block\":\"10.252.0.0/16\"}" | json_field id)"
  SUBNET_ID="$(api POST /v1/subnets "{\"name\":\"${suffix}-subnet\",\"vpc_id\":\"${VPC_ID}\",\"cidr_block\":\"10.252.0.0/16\",\"zone\":\"a\",\"public\":true}" | json_field id)"
  SG_ID="$(api POST /v1/security-groups "{\"name\":\"${suffix}-sg\",\"vpc_id\":\"${VPC_ID}\",\"ingress_rules\":[{\"protocol\":\"tcp\",\"from_port\":22,\"to_port\":22,\"cidr\":\"0.0.0.0/0\"}],\"egress_rules\":[{\"protocol\":\"-1\",\"cidr\":\"0.0.0.0/0\"}]}" | json_field id)"
  INSTANCE_ID="$(api POST /v1/instances "{\"name\":\"${suffix}\",\"image_id\":\"ubuntu-22.04\",\"flavor\":\"${flavor}\",\"vpc_id\":\"${VPC_ID}\",\"subnet_id\":\"${SUBNET_ID}\",\"security_group_ids\":[\"${SG_ID}\"]}" | json_field id)"
  [[ -n "${INSTANCE_ID}" ]] || die "builder instance was not created"

  local ip="" status="" resp i
  for i in $(seq 1 60); do
    resp="$(api GET "/v1/instances/${INSTANCE_ID}")"
    status="$(json_field status <<< "${resp}")"
    ip="$(json_field private_ip <<< "${resp}")"
    [[ "${status}" == running && -n "${ip}" ]] && break
    sleep 5
  done
  [[ -n "${ip}" ]] || die "builder never reached running with an IP"
  # Throwaway hosts: never persist their host keys (see build-firecracker-rootfs.sh).
  local -a ssh_opts=(-o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null
                     -o ConnectTimeout=5 -o LogLevel=ERROR -o ServerAliveInterval=30 -i "${KEY}")
  for i in $(seq 1 30); do
    ssh "${ssh_opts[@]}" "ubuntu@${ip}" true 2>/dev/null && break
    sleep 5
  done
  log "builder at ${ip}"

  scp "${ssh_opts[@]}" "${WORK}/base.config" "${FRAGMENT}" "ubuntu@${ip}:/tmp/"
  log "building (this takes a while on a small builder)"
  # shellcheck disable=SC2029 -- the variables are meant to expand locally.
  ssh "${ssh_opts[@]}" "ubuntu@${ip}" "set -euo pipefail
    sudo DEBIAN_FRONTEND=noninteractive apt-get update -q
    sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -q --no-install-recommends \
      build-essential flex bison bc libelf-dev libssl-dev xz-utils curl ca-certificates
    cd /tmp
    curl -sfLO '${KERNEL_CDN}/v${major}.x/${tarball}'
    echo '${expected}  ${tarball}' | sha256sum -c -
    tar xf '${tarball}'
    cd 'linux-${version}'
    cp /tmp/base.config .config
    scripts/kconfig/merge_config.sh -m .config /tmp/firecracker-kernel-lab.config >/dev/null
    make olddefconfig >/dev/null
    missing=\$(grep -E '^CONFIG_[A-Z0-9_]+=' /tmp/firecracker-kernel-lab.config | while read -r want; do
      grep -qxF \"\${want}\" .config || echo \"\${want}\"; done)
    if [[ -n \"\${missing}\" ]]; then
      echo 'options not enabled after olddefconfig (unmet dependencies?):' >&2
      echo \"\${missing}\" >&2
      exit 3
    fi
    make -j\"\$(nproc)\" vmlinux >/tmp/kbuild.log 2>&1 || { tail -40 /tmp/kbuild.log >&2; exit 4; }
    ls -la vmlinux"

  scp "${ssh_opts[@]}" "ubuntu@${ip}:/tmp/linux-${version}/vmlinux" "${out}.partial"
  mv "${out}.partial" "${out}"
  local sha
  sha="$(sha256sum "${out}" | awk '{print $1}')"
  log "built ${out}"
  echo "${sha}  $(basename "${out}")"
}

main "$@"
