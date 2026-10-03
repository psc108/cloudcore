#!/usr/bin/env bash
# import-labvm-token.sh -- make this host's lab-VM broker accept another
# host's broker token (two-host S5, cloudcore-two-host-Phased-Implementation.md).
#
# A guest's LABVM_BROKER_TOKEN is issued by whichever host built it, but it
# asks its own host's broker (broker.cloudcore.internal). So every host's
# broker must accept the same token. It only reaches the broker endpoints,
# which give lab VMs on the caller's own host within quotas.
#
# Reads one line "CLOUDCORE_LABVM_TOKEN=..." on stdin and writes it into
# ~/.config/cloudcore/api.env (mode 0600), replacing any existing value.
# The token is never printed. Restart cloudcore-api afterwards.
#
# Usage, from the host whose token is shared (run as the API's user):
#   grep '^CLOUDCORE_LABVM_TOKEN=' ~/.config/cloudcore/api.env \
#     | ssh <user>@<other host> 'bash <repo>/api/import-labvm-token.sh'
set -euo pipefail

ENV_FILE="${HOME}/.config/cloudcore/api.env"

log() { echo "import-labvm-token: $*" >&2; }

if [[ "${1:-}" == "-h" || "${1:-}" == "--help" ]]; then
  sed -n '2,17p' "$0" | sed 's/^# \{0,1\}//'
  exit 0
fi
[[ -t 0 ]] && { log "pipe the token line in on stdin (see --help)"; exit 2; }

IFS= read -r line || true
if [[ ! "${line}" =~ ^CLOUDCORE_LABVM_TOKEN=ccl_[A-Za-z0-9_-]{20,}$ ]]; then
  log "stdin isn't a CLOUDCORE_LABVM_TOKEN=ccl_... line; nothing changed"
  exit 2
fi
[[ -f "${ENV_FILE}" ]] || { log "${ENV_FILE} not found: set this host up first (peer-update-checklist.md)"; exit 1; }

tmp="$(mktemp "${ENV_FILE}.XXXXXX")"
trap 'rm -f "${tmp}"' EXIT
chmod 600 "${tmp}"
grep -v '^CLOUDCORE_LABVM_TOKEN=' "${ENV_FILE}" > "${tmp}" || true
printf '%s\n' "${line}" >> "${tmp}"
mv "${tmp}" "${ENV_FILE}"
trap - EXIT
log "installed the shared lab-VM token in ${ENV_FILE}; now: systemctl --user restart cloudcore-api"
