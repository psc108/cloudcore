#!/usr/bin/env bash
# Install the LFS build's tutor timer (lfs-os-Phased-Implementation.md, C5
# rung 3) as user units, on a host where Claude Code is installed and signed in.
#
# Usage: lfs/install-lfs-tutor.sh --api-ssh USER@HOST [--cap N] [--model M] [--dry-run]
#   --api-ssh  the CloudCore API host that holds the LFS build, reached by
#              agent-free SSH with ~/.ssh/id_ed25519 (its token stays there)
#   --cap      tutor sessions per day (default 6)
#   --model    the Claude model alias for sessions (default opus)
set -euo pipefail

REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
UNIT_DIR="${HOME}/.config/systemd/user"
ENV_FILE="${HOME}/.config/cloudcore/lfs-tutor.env"
API_SSH="" CAP=6 MODEL=opus DRY=0

usage() { sed -n '2,10p' "$0" | sed 's/^# \{0,1\}//'; }
while [[ $# -gt 0 ]]; do
    case "$1" in
        --api-ssh) API_SSH="${2:?}"; shift 2 ;;
        --cap) CAP="${2:?}"; shift 2 ;;
        --model) MODEL="${2:?}"; shift 2 ;;
        --dry-run) DRY=1; shift ;;
        -h|--help) usage; exit 0 ;;
        *) echo "unknown argument: $1" >&2; usage >&2; exit 2 ;;
    esac
done
[[ -n "${API_SSH}" ]] || { usage >&2; exit 2; }

CLAUDE_BIN="$(command -v claude || true)"
[[ -n "${CLAUDE_BIN}" ]] || { echo "claude (Claude Code) is not on PATH here; install and sign in first" >&2; exit 1; }
[[ -f "${HOME}/.claude/.credentials.json" ]] || echo "warning: no ~/.claude/.credentials.json; run 'claude' once and sign in" >&2

echo "==> repo ${REPO_DIR}; claude ${CLAUDE_BIN}; API host ${API_SSH}; cap ${CAP}/day; model ${MODEL}" >&2
ssh -o IdentityAgent=none -o IdentitiesOnly=yes -i "${HOME}/.ssh/id_ed25519" -o BatchMode=yes -o ConnectTimeout=10 \
    "${API_SSH}" true || { echo "can't reach ${API_SSH} by agent-free SSH" >&2; exit 1; }
if [[ "${DRY}" -eq 1 ]]; then
    echo "dry run: would write ${ENV_FILE} and ${UNIT_DIR}/lfs-tutor.{service,timer}, then enable the timer" >&2
    exit 0
fi

mkdir -p "${UNIT_DIR}" "$(dirname "${ENV_FILE}")"
umask 077
cat > "${ENV_FILE}" <<ENV
LFS_API_SSH=${API_SSH}
LFS_TUTOR_CLAUDE=${CLAUDE_BIN}
LFS_TUTOR_DAILY_CAP=${CAP}
LFS_TUTOR_MODEL=${MODEL}
ENV
umask 022
for u in lfs-tutor.service lfs-tutor.timer; do
    sed "s|/home/scottp/IdeaProjects/CloudProject|${REPO_DIR}|g" "${REPO_DIR}/lfs/${u}" > "${UNIT_DIR}/${u}"
done
systemctl --user daemon-reload
systemctl --user enable --now lfs-tutor.timer
echo "==> installed; the timer checks every 2 min. Sessions are kept in ~/.local/state/lfs-tutor/sessions" >&2
