#!/usr/bin/env bash
# Run once with sudo, after setup-logging-service.sh has made Loki a
# host-level service, to ship THIS host's own logs there too.
#
# Every Lab example's own promtail (files/promtail-config.yml) only
# ever ships FROM a Lab VM -- it has no way to see anything running
# directly on the CloudCore host itself. HAProxy (api/lb.py) is the
# one real exception: it's always local (coordinators/LBs run on
# whichever host initiated the build, never peer-placed), so its own
# access log (/var/log/haproxy.log, F-124's own `log`/`option
# httplog` addition) sits on THIS host with nothing shipping it
# anywhere -- confirmed live the file existed, group-readable, but
# completely empty of any consumer. This installs a second, dedicated
# promtail instance (its own http_listen_port, 9081, so it can't
# collide with a Lab VM's own promtail if one is ever run against this
# same host directly) scoped to exactly that one file.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CODENAME="${1:-jammy}"
REPO_DIR="$SCRIPT_DIR/package-repo/$CODENAME/apt-repo"

if [ "$(id -u)" -ne 0 ]; then
  echo "Run with sudo: sudo bash api/setup-host-promtail.sh [codename]" >&2
  exit 1
fi

PROMTAIL_DEB=$(ls "$REPO_DIR"/promtail_*.deb 2>/dev/null | head -1 || true)
if [ -z "$PROMTAIL_DEB" ]; then
  echo "promtail .deb not found in $REPO_DIR — run" >&2
  echo "  bash api/build-package-repo.sh $CODENAME" >&2
  echo "first to populate the host-level package repo." >&2
  exit 1
fi

echo "==> Installing $(basename "$PROMTAIL_DEB")..."
dpkg -i "$PROMTAIL_DEB" || apt-get install -f -y

echo "==> Writing host-level promtail config (haproxy.log only)..."
cat > /etc/promtail/config.yml <<'EOF'
server:
  http_listen_port: 9081
  grpc_listen_port: 0

positions:
  filename: /var/lib/promtail/positions.yaml

clients:
  - url: http://localhost:3100/loki/api/v1/push

scrape_configs:
  # job/host labels match the convention every Lab VM's own
  # promtail-config.yml already uses, so a Grafana/Sentinel query
  # looks the same regardless of which side of the pipeline a log
  # actually came from.
  - job_name: haproxy
    static_configs:
      - targets: [localhost]
        labels:
          job: haproxy
          host: stourport
          __path__: /var/log/haproxy.log
EOF

# Same F-071 class of gap already fixed for loki's own data dir in
# setup-logging-service.sh -- the .deb doesn't create its own
# positions-file directory, and the dedicated "promtail" system user
# the .deb's own postinst creates has no adm group membership by
# default, so it can't read /var/log/haproxy.log (group adm) without
# this -- same fix the Lab VM's own cloud-init already applies for
# the identical reason (coordinator-cloud-init.yaml.tftpl's own
# `usermod -aG adm promtail`).
mkdir -p /var/lib/promtail
chown -R promtail:promtail /var/lib/promtail
usermod -aG adm promtail

systemctl daemon-reload
systemctl enable --now promtail
systemctl restart promtail

echo ""
echo "host-promtail is running, shipping /var/log/haproxy.log to Loki."
echo "Query it in Grafana/Sentinel with: {job=\"haproxy\", host=\"stourport\"}"
echo "Remove with: sudo systemctl disable --now promtail && sudo rm /etc/systemd/system/promtail.service"
