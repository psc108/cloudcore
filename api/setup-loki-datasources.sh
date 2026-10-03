#!/usr/bin/env bash
# setup-loki-datasources.sh -- give this host's Grafana a datasource for
# every approved peer's Loki (two-host S4, cloudcore-two-host-Phased-Implementation.md).
#
# Each CloudCore host runs its own Loki, and its guests ship there. This
# host's Loki is the datasource `loki` (setup-logging-service.sh). Each
# approved peer's Loki is added as `loki-<hostname slug>`, at that peer's
# bridge gateway port 3100, read from this host's own peer list
# (api/cloudcore.db). Nothing names a host. Sentinel uses the same uids
# (sentinel/src/sentinel/loki_sources.py) for its "View in Grafana" links.
#
# Idempotent. Grafana is restarted only when the file changes. Re-run it
# after pairing with or revoking a peer.
#
# Usage: sudo bash api/setup-loki-datasources.sh [--dry-run]
#        bash api/setup-loki-datasources.sh --help
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DB="${SCRIPT_DIR}/cloudcore.db"
OUT=/etc/grafana/provisioning/datasources/cloudcore-peers.yaml

log() { echo "setup-loki-datasources: $*" >&2; }
usage() { sed -n '2,16p' "$0" | sed 's/^# \{0,1\}//'; }

DRY_RUN=0
case "${1:-}" in
  --dry-run) DRY_RUN=1 ;;
  -h|--help) usage; exit 0 ;;
  "") ;;
  *) log "unknown argument: $1"; usage; exit 2 ;;
esac
[[ "${EUID}" -eq 0 || "${DRY_RUN}" -eq 1 ]] || { log "must run as root (sudo)"; exit 1; }
[[ -r "${DB}" ]] || { log "can't read ${DB}: is CloudCore installed here?"; exit 1; }

# Python for the DB read and the subnet maths. The slug must match
# loki_sources.peer_uid on the Sentinel side.
yaml="$(python3 - "${DB}" <<'PY'
import ipaddress, re, sqlite3, sys
rows = sqlite3.connect(f"file:{sys.argv[1]}?mode=ro", uri=True).execute(
    "SELECT hostname, id, wg_bridge_subnet FROM peers WHERE status = 'approved' "
    "AND COALESCE(wg_bridge_subnet, '') != '' ORDER BY hostname").fetchall()
out, seen = ["apiVersion: 1", "datasources:"], set()
for hostname, pid, subnet in rows:
    try:
        gw = next(ipaddress.ip_network(subnet, strict=False).hosts())
    except (ValueError, StopIteration):
        print(f"skipping {hostname}: bad subnet {subnet!r}", file=sys.stderr)
        continue
    name = hostname or pid
    uid = ("loki-" + (re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-") or "peer"))[:40]
    if uid in seen:
        continue
    seen.add(uid)
    out += [f"  - name: Loki ({name})", f"    uid: {uid}", "    type: loki", "    access: proxy",
            f"    url: http://{gw}:3100", "    isDefault: false", "    editable: false"]
if len(out) == 2:
    out = ["apiVersion: 1", "datasources: []"]
print("\n".join(out))
PY
)"

if [[ "${DRY_RUN}" -eq 1 ]]; then
  echo "${yaml}"
  exit 0
fi

if [[ -f "${OUT}" ]] && [[ "$(cat "${OUT}")" == "${yaml}" ]]; then
  log "${OUT} already current; Grafana not restarted"
  exit 0
fi
install -d -m 755 "$(dirname "${OUT}")"
printf '%s\n' "${yaml}" | install -m 644 /dev/stdin "${OUT}"
log "wrote ${OUT}: $(grep -c '^  - name:' "${OUT}" || true) peer Loki datasource(s)"
systemctl restart grafana-server
log "grafana-server restarted"
