#!/usr/bin/env bash
# Run once with sudo, after setup-network.sh, to make the host-level
# package repo + artifact cache always-available — a systemd service
# (not a manually re-launched background process) specifically so it
# survives a host reboot without anyone remembering to re-run anything.
#
# This only starts the *serving* side. The repo directory itself starts
# empty — run build-package-repo.sh (as the invoking user, not root) to
# actually populate a release's apt-repo/artifacts content before any
# guest tries to install from it.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SERVICE_USER="${SUDO_USER:-$(logname)}"

if [ "$(id -u)" -ne 0 ]; then
  echo "Run with sudo: sudo bash api/setup-package-repo.sh" >&2
  exit 1
fi

mkdir -p "$SCRIPT_DIR/package-repo"
chown "$SERVICE_USER":"$SERVICE_USER" "$SCRIPT_DIR/package-repo"

cat > /etc/systemd/system/cloudcore-package-repo.service <<EOF
[Unit]
Description=CloudCore host-level package repo + artifact cache (always-available, not per-project)
After=network.target

[Service]
User=$SERVICE_USER
ExecStart=/usr/bin/python3 $SCRIPT_DIR/serve-package-repo.py
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable --now cloudcore-package-repo

echo "cloudcore-package-repo.service running, serving http://192.168.100.1:8090/"
echo "Populate it with: bash api/build-package-repo.sh [codename]"
echo "Remove with: sudo systemctl disable --now cloudcore-package-repo && sudo rm /etc/systemd/system/cloudcore-package-repo.service"
