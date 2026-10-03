#!/usr/bin/env bash
# setup-backup-key.sh -- let this host send its backups to another host, and
# nothing more (two-host S6, cloudcore-two-host-Phased-Implementation.md).
#
# Creates ~/.ssh/cloudcore-backup (ed25519, no passphrase: the nightly job
# runs unattended) if it doesn't exist, then prints a small script for the
# OTHER host. Pipe it there over SSH; it creates
# ~/cloudcore-backups/<this host> and adds the key to authorized_keys,
# confined by rrsync to that one directory (no shell, no port forwarding,
# nothing outside it). Idempotent: re-running replaces the key's line.
#
# Usage, on the host being backed up:
#   bash api/setup-backup-key.sh | ssh <user>@<other host> bash
#   bash api/setup-backup-key.sh --help
set -euo pipefail

KEY="${HOME}/.ssh/cloudcore-backup"

log() { echo "setup-backup-key: $*" >&2; }

if [[ "${1:-}" == "-h" || "${1:-}" == "--help" ]]; then
  sed -n '2,15p' "$0" | sed 's/^# \{0,1\}//'
  exit 0
fi
[[ $# -eq 0 ]] || { log "unknown argument: $1 (see --help)"; exit 2; }

if [[ ! -f "${KEY}" ]]; then
  install -d -m 700 "${HOME}/.ssh"
  ssh-keygen -q -t ed25519 -N "" -C "cloudcore-backup@$(hostname -s)" -f "${KEY}"
  log "created ${KEY}"
fi

# Lower-case short hostname: the directory this host's backups land in.
src="$(hostname -s | tr '[:upper:]' '[:lower:]' | tr -c 'a-z0-9-\n' '-')"
pub="$(cat "${KEY}.pub")"
comment="${pub##* }"

# The script the other host runs. Values are fixed here; nothing is taken
# from that host's environment except $HOME.
cat <<EOF
set -euo pipefail
umask 077  # authorized_keys must not become group-writable: sshd would ignore it
command -v rrsync >/dev/null || { echo "rrsync not found: install rsync 3.2.4 or later" >&2; exit 1; }
install -d -m 700 "\$HOME/cloudcore-backups" "\$HOME/cloudcore-backups/${src}" "\$HOME/.ssh"
touch "\$HOME/.ssh/authorized_keys"; chmod 600 "\$HOME/.ssh/authorized_keys"
grep -vF " ${comment}" "\$HOME/.ssh/authorized_keys" > "\$HOME/.ssh/authorized_keys.tmp" || true
echo "command=\"rrsync \$HOME/cloudcore-backups/${src}\",restrict ${pub}" >> "\$HOME/.ssh/authorized_keys.tmp"
chmod 600 "\$HOME/.ssh/authorized_keys.tmp"
mv "\$HOME/.ssh/authorized_keys.tmp" "\$HOME/.ssh/authorized_keys"
echo "setup-backup-key: \$(hostname -s) now accepts backups from ${src} into \$HOME/cloudcore-backups/${src} (rrsync-confined)" >&2
EOF
