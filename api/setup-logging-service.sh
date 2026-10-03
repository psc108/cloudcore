#!/usr/bin/env bash
# Run once with sudo, after build-package-repo.sh has populated the
# host-level package repo, to make Loki + Grafana a host-level,
# always-available logging service — every example's own promtail
# agent ships to this one fixed address (192.168.100.1:3100) instead
# of needing a dedicated per-example logging node. Mirrors
# setup-package-repo.sh's own pattern exactly: a systemd service, not
# a manually re-launched background process, so it survives a host
# reboot without anyone remembering to re-run anything.
#
# Installs grafana/loki directly from the already-cached .debs in
# api/package-repo/<codename>/apt-repo/ (dpkg -i, no network call) —
# both have been in build-package-repo.sh's own THIRDPARTY_PACKAGES
# array since ha-frontend-lb's own centralized-logging tier was built,
# so nothing new needs caching. This is why it can't run automatically
# during scripts/install.sh the way cloudcore-repo's own *serving*
# side does: unlike that service (which starts genuinely empty and is
# populated separately), this one needs those .debs to already exist —
# run build-package-repo.sh first on a truly fresh clone.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CODENAME="${1:-jammy}"
REPO_DIR="$SCRIPT_DIR/package-repo/$CODENAME/apt-repo"

if [ "$(id -u)" -ne 0 ]; then
  echo "Run with sudo: sudo bash api/setup-logging-service.sh [codename]" >&2
  exit 1
fi

# The newest of each: a repo copied from another host can also hold older
# .debs, and `ls | head -1` picked grafana 13.2.1 over 13.2.2 (two-host S4).
newest_deb() { find "$REPO_DIR" -maxdepth 1 -name "$1_*.deb" -printf '%f\n' 2>/dev/null | sort -V | tail -1; }
GRAFANA_DEB=$(newest_deb grafana); GRAFANA_DEB=${GRAFANA_DEB:+$REPO_DIR/$GRAFANA_DEB}
LOKI_DEB=$(newest_deb loki); LOKI_DEB=${LOKI_DEB:+$REPO_DIR/$LOKI_DEB}
if [ -z "$GRAFANA_DEB" ] || [ -z "$LOKI_DEB" ]; then
  echo "grafana/loki .debs not found in $REPO_DIR — run" >&2
  echo "  bash api/build-package-repo.sh $CODENAME" >&2
  echo "first to populate the host-level package repo." >&2
  exit 1
fi

echo "==> Installing $(basename "$GRAFANA_DEB") and $(basename "$LOKI_DEB")..."
dpkg -i "$GRAFANA_DEB" "$LOKI_DEB" || apt-get install -f -y

echo "==> Writing Loki config..."
mkdir -p /etc/loki
cat > /etc/loki/config.yml <<'EOF'
auth_enabled: false

server:
  http_listen_port: 3100
  grpc_listen_port: 9096

common:
  # Single-binary Loki routes every query through its own ring entry, which
  # by default holds the LAN address it saw at startup. When that address
  # changed (192.168.0.x -> 192.168.1.x, F-197), every query hung until the
  # timeout while ingestion carried on. It only ever talks to itself here.
  instance_addr: 127.0.0.1
  path_prefix: /var/lib/loki
  storage:
    filesystem:
      chunks_directory: /var/lib/loki/chunks
      rules_directory: /var/lib/loki/rules
  replication_factor: 1
  ring:
    kvstore:
      store: inmemory

schema_config:
  configs:
    - from: 2024-01-01
      store: tsdb
      object_store: filesystem
      schema: v13
      index:
        prefix: index_
        period: 24h

limits_config:
  retention_period: 168h
EOF

# Same F-071 fix already proven for the guest-VM version of this exact
# config (haFullStack-Findings-Log.md): the loki .deb doesn't create
# or own its own data directory. loki's own primary group is
# "nogroup" (no dedicated "loki" group exists either).
mkdir -p /var/lib/loki
chown -R loki:nogroup /var/lib/loki

echo "==> Writing Grafana's Loki datasource provisioning..."
mkdir -p /etc/grafana/provisioning/datasources
# uid: loki is a fixed value, not Grafana's own auto-generated default —
# without it, every install gets a different random uid, which breaks
# any deep link built against a known uid (e.g. Sentinel's own
# "View in Grafana" links, sentinel/ui/index.html).
cat > /etc/grafana/provisioning/datasources/loki.yaml <<'EOF'
apiVersion: 1
datasources:
  - name: Loki
    uid: loki
    type: loki
    access: proxy
    url: http://localhost:3100
    isDefault: true
    editable: true
EOF

# Same Lab-only shared-credential convention as var.admin_password
# everywhere else in this project — not a production secret. Override
# via CLOUDCORE_LOGGING_ADMIN_PASSWORD rather than an interactive
# prompt, since this script (like build-package-repo.sh) needs to stay
# safe to run unattended.
GRAFANA_PASSWORD="${CLOUDCORE_LOGGING_ADMIN_PASSWORD:-changeme-admin}"

echo "==> Setting Grafana's admin password..."
# Same first-boot-race handling already proven for the guest-VM
# version: Grafana only applies grafana.ini's admin_password when it
# creates the admin user on its OWN first-ever start — the packages:
# install above may already have auto-started grafana-server with the
# package's own default config (admin/admin) before this gets a
# chance to edit anything. Stop it and remove any already-created
# state first, so the real first boot happens only once, against the
# correct config.
systemctl stop grafana-server || true
rm -f /var/lib/grafana/grafana.db

python3 - "$GRAFANA_PASSWORD" <<'PYEOF'
import configparser
import sys

c = configparser.ConfigParser(strict=False)
c.read('/etc/grafana/grafana.ini')
if 'security' not in c:
    c['security'] = {}
c['security']['admin_password'] = sys.argv[1]
# Anonymous access — this is a Lab debugging aid, per direct
# instruction, not a multi-tenant install anyone needs to actually log
# into: the login screen is pure friction for someone just clicking a
# "View in Grafana" link (Sentinel's own UI, ui/index.html) to look at
# logs. org_role = Editor, not Viewer — confirmed live (F-081,
# haFullStack-Findings-Log.md) that Grafana's own built-in "Viewer"
# fixed role does not include the `datasources:explore` RBAC action in
# this version, so anonymous Viewer sessions get a 302 redirect back
# to a login wall the instant they open Explore — the entire point of
# this. OSS Grafana has no supported way to grant an anonymous session
# a custom, narrower permission set (that needs Enterprise); Editor is
# the least-privileged fixed role that actually includes Explore
# access. Real, accepted tradeoff, not an oversight: anonymous
# visitors can now create/edit/save dashboards too, not just view —
# still short of Admin (no user/datasource management) — judged
# acceptable given this instance never leaves the Lab's own internal
# bridge network (192.168.100.1, same trust boundary already used for
# skipping TLS here). The admin login above still works independently
# for anyone who actually needs to administer this Grafana instance.
if 'auth.anonymous' not in c:
    c['auth.anonymous'] = {}
c['auth.anonymous']['enabled'] = 'true'
c['auth.anonymous']['org_name'] = 'Main Org.'
c['auth.anonymous']['org_role'] = 'Editor'
with open('/etc/grafana/grafana.ini', 'w') as f:
    c.write(f)
PYEOF

systemctl daemon-reload
# restart, not `enable --now`: on a re-run Loki is already running, and
# `--now` leaves the old binary on the old config (F-210).
systemctl enable loki
systemctl restart loki
systemctl enable --now grafana-server
systemctl restart grafana-server

# Every approved peer's Loki as a further datasource (two-host S4).
bash "$SCRIPT_DIR/setup-loki-datasources.sh"

echo ""
echo "cloudcore-logging is running:"
echo "  Loki:    port 3100 on this host's bridge gateway; guests ship to it as"
echo "           logs.cloudcore.internal when services.conf says logs=self"
echo "  Grafana: port 3000 (grafana.cloudcore.internal) — opens straight to Explore/dashboards,"
echo "           no login screen (anonymous Editor access — required for Explore"
echo "           to work at all in this version, see setup-logging-service.sh's"
echo "           own comment). Log in as admin / \$CLOUDCORE_LOGGING_ADMIN_PASSWORD"
echo "           (default changeme-admin) only for actual instance administration"
echo "           (user/datasource management) — anonymous access can't do that."
echo "Remove with: sudo systemctl disable --now loki grafana-server"
