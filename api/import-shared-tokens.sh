#!/usr/bin/env bash
# import-shared-tokens.sh -- make this host accept the guest-facing tokens
# another host issues (two-host S5 and S7, cloudcore-two-host-Phased-Implementation.md).
#
# A guest carries the tokens of whichever host built it, but asks its own
# host's broker (broker.cloudcore.internal) and capture's one home host
# (capture.cloudcore.internal). So every host must accept the same:
#   CLOUDCORE_LABVM_TOKEN     lab-VM broker: lab VMs on the caller's host, within quotas
#   CLOUDCORE_EXAMPLES_TOKEN  capture: examples and LLM deployment registration
# Never the master token.
#
# Reads those lines on stdin and writes them into ~/.config/cloudcore/api.env
# (mode 0600), replacing existing values. Tokens are never printed. Restart
# cloudcore-api afterwards.
#
# Usage, from the host whose tokens are shared (run as the API's user):
#   grep -E '^CLOUDCORE_(LABVM|EXAMPLES)_TOKEN=' ~/.config/cloudcore/api.env \
#     | ssh <user>@<other host> 'bash <repo>/api/import-shared-tokens.sh'
set -euo pipefail

ENV_FILE="${HOME}/.config/cloudcore/api.env"

log() { echo "import-shared-tokens: $*" >&2; }

if [[ "${1:-}" == "-h" || "${1:-}" == "--help" ]]; then
  sed -n '2,20p' "$0" | sed 's/^# \{0,1\}//'
  exit 0
fi
[[ -t 0 ]] && { log "pipe the token line in on stdin (see --help)"; exit 2; }

lines=()
while IFS= read -r line; do
  [[ -z "${line}" ]] && continue
  if [[ "${line}" =~ ^CLOUDCORE_LABVM_TOKEN=ccl_[A-Za-z0-9_-]{20,}$ || "${line}" =~ ^CLOUDCORE_EXAMPLES_TOKEN=ccx_[A-Za-z0-9_-]{20,}$ ]]; then
    lines+=("${line}")
  else
    log "stdin has a line that isn't CLOUDCORE_LABVM_TOKEN=ccl_... or CLOUDCORE_EXAMPLES_TOKEN=ccx_...; nothing changed"
    exit 2
  fi
done
(( ${#lines[@]} > 0 )) || { log "no token lines on stdin; nothing changed"; exit 2; }
[[ -f "${ENV_FILE}" ]] || { log "${ENV_FILE} not found: set this host up first (peer-update-checklist.md)"; exit 1; }

tmp="$(mktemp "${ENV_FILE}.XXXXXX")"
trap 'rm -f "${tmp}"' EXIT
chmod 600 "${tmp}"
names=()
for l in "${lines[@]}"; do names+=("${l%%=*}"); done
grep -vE "^($(IFS='|'; echo "${names[*]}"))=" "${ENV_FILE}" > "${tmp}" || true
printf '%s\n' "${lines[@]}" >> "${tmp}"
mv "${tmp}" "${ENV_FILE}"
trap - EXIT
log "installed ${names[*]} in ${ENV_FILE}; now: systemctl --user restart cloudcore-api"
