#!/usr/bin/env bash
# Populates the host-level package repo (api/package-repo/<codename>/) —
# run by hand, on a deliberate cadence: when the target Ubuntu release
# increments, or when a specific package needs a security patch. Not a
# continuously-reconciled mirror (haFullStack-LLD.md §6.1) — nothing
# calls this automatically.
#
# The apt-repo build needs a real matching-release environment (the
# CloudCore host itself may be on a different Ubuntu release than the
# guest images it serves), so it launches a throwaway CloudCore instance
# of the target release, builds the repo there, pulls the result back
# over SSH, and tears the instance down again — the repo *serving* is
# host-level and always-on (serve-package-repo.py), but *building* it
# still needs a same-release guest for correctness, same as every other
# CloudCore VM this platform creates.
#
# Usage: api/build-package-repo.sh [codename] [package...]
#   codename   Ubuntu release codename — must match a CloudCore image_id
#              of "ubuntu-<version>" (default: jammy -> ubuntu-22.04)
#   package... top-level packages to include (their own dependencies are
#              resolved automatically) — default: the set every current
#              example template installs
set -euo pipefail

CODENAME="${1:-jammy}"
shift || true
PACKAGES=("$@")
if [ ${#PACKAGES[@]} -eq 0 ]; then
  PACKAGES=(curl ca-certificates dpkg-dev mysql-server mysql-client nginx keepalived \
            keystone python3-pymysql python3-memcache python3 rabbitmq-server)
fi

case "$CODENAME" in
  jammy) IMAGE_ID="ubuntu-22.04" ;;
  noble) IMAGE_ID="ubuntu-24.04" ;;
  *) echo "Unknown codename '$CODENAME' — add it to the case statement in this script first" >&2; exit 1 ;;
esac

: "${CLOUDCORE_API_URL:?Set CLOUDCORE_API_URL (e.g. http://127.0.0.1:8080)}"
: "${CLOUDCORE_API_TOKEN:?Set CLOUDCORE_API_TOKEN}"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
KEY="$SCRIPT_DIR/keys/cloudcore_ed25519"
REPO_DIR="$SCRIPT_DIR/package-repo/$CODENAME"
API="$CLOUDCORE_API_URL"
AUTH=(-H "Authorization: Bearer $CLOUDCORE_API_TOKEN")
SSH_OPTS=(-o StrictHostKeyChecking=no -o ConnectTimeout=5 -i "$KEY")
SUFFIX="repobuild-$(date +%s)"

mkdir -p "$REPO_DIR/apt-repo" "$REPO_DIR/artifacts"

cleanup() {
  echo "Cleaning up throwaway build infrastructure..."
  # Deletion order matters — delete_vpc 409s if the VPC still has any
  # active instance/security-group/subnet attached (confirmed directly:
  # a first version of this script left orphaned VPCs behind because it
  # only deleted the instance and VPC, skipping the security group and
  # subnet in between).
  [ -n "${INSTANCE_ID:-}" ] && curl -s -X DELETE "$API/v1/instances/$INSTANCE_ID" "${AUTH[@]}" >/dev/null || true
  sleep 5
  [ -n "${SG_ID:-}" ] && curl -s -X DELETE "$API/v1/security-groups/$SG_ID" "${AUTH[@]}" >/dev/null || true
  [ -n "${SUBNET_ID:-}" ] && curl -s -X DELETE "$API/v1/subnets/$SUBNET_ID" "${AUTH[@]}" >/dev/null || true
  [ -n "${VPC_ID:-}" ] && curl -s -X DELETE "$API/v1/vpcs/$VPC_ID" "${AUTH[@]}" >/dev/null || true
}
trap cleanup EXIT

echo "=== Fetching pinned artifacts directly (no VM needed, OS-version-agnostic) ==="
declare -A ARTIFACT_URLS=(
  [step-ca.deb]="https://github.com/smallstep/certificates/releases/download/v0.30.2/step-ca_0.30.2-1_amd64.deb"
  [step-cli.deb]="https://github.com/smallstep/cli/releases/download/v0.30.6/step-cli_0.30.6-1_amd64.deb"
  [proxysql.deb]="https://github.com/sysown/proxysql/releases/download/v3.0.11/proxysql_3.0.11-ubuntu22_amd64.deb"
)
for name in "${!ARTIFACT_URLS[@]}"; do
  curl -fL --speed-limit 1024 --speed-time 30 -C - -o "$REPO_DIR/artifacts/$name" "${ARTIFACT_URLS[$name]}"
done
{
  echo "Built: $(date -u +%Y-%m-%dT%H:%M:%SZ)"
  for name in "${!ARTIFACT_URLS[@]}"; do
    echo "$name  sha256=$(sha256sum "$REPO_DIR/artifacts/$name" | cut -d' ' -f1)  source=${ARTIFACT_URLS[$name]}"
  done
} > "$REPO_DIR/artifacts/MANIFEST.txt"
touch "$REPO_DIR/artifacts/.build-complete"

echo "=== Standing up a throwaway $CODENAME builder instance ==="
VPC_ID=$(curl -s -X POST "$API/v1/vpcs" "${AUTH[@]}" -H "Content-Type: application/json" \
  -d "{\"name\":\"$SUFFIX-vpc\",\"cidr_block\":\"10.250.0.0/16\"}" | python3 -c "import json,sys; print(json.load(sys.stdin)['id'])")
SUBNET_ID=$(curl -s -X POST "$API/v1/subnets" "${AUTH[@]}" -H "Content-Type: application/json" \
  -d "{\"name\":\"$SUFFIX-subnet\",\"vpc_id\":\"$VPC_ID\",\"cidr_block\":\"10.250.0.0/16\",\"zone\":\"a\",\"public\":true}" | python3 -c "import json,sys; print(json.load(sys.stdin)['id'])")
SG_ID=$(curl -s -X POST "$API/v1/security-groups" "${AUTH[@]}" -H "Content-Type: application/json" \
  -d "{\"name\":\"$SUFFIX-sg\",\"vpc_id\":\"$VPC_ID\",\"ingress_rules\":[{\"protocol\":\"tcp\",\"from_port\":22,\"to_port\":22,\"cidr\":\"0.0.0.0/0\"}],\"egress_rules\":[{\"protocol\":\"-1\",\"cidr\":\"0.0.0.0/0\"}]}" | python3 -c "import json,sys; print(json.load(sys.stdin)['id'])")
INSTANCE_ID=$(curl -s -X POST "$API/v1/instances" "${AUTH[@]}" -H "Content-Type: application/json" \
  -d "{\"name\":\"$SUFFIX\",\"image_id\":\"$IMAGE_ID\",\"flavor\":\"standard.medium\",\"vpc_id\":\"$VPC_ID\",\"subnet_id\":\"$SUBNET_ID\",\"security_group_ids\":[\"$SG_ID\"]}" \
  | python3 -c "import json,sys; print(json.load(sys.stdin)['id'])")

echo "Waiting for $INSTANCE_ID to reach running with a real IP..."
INSTANCE_IP=""
for i in $(seq 1 60); do
  resp=$(curl -s "$API/v1/instances/$INSTANCE_ID" "${AUTH[@]}")
  status=$(echo "$resp" | python3 -c "import json,sys; print(json.load(sys.stdin).get('status',''))")
  INSTANCE_IP=$(echo "$resp" | python3 -c "import json,sys; print(json.load(sys.stdin).get('private_ip',''))")
  if [ "$status" = "running" ] && [ -n "$INSTANCE_IP" ]; then
    break
  fi
  sleep 5
done
if [ -z "$INSTANCE_IP" ]; then
  echo "Builder instance never reached running/IP-assigned — aborting" >&2
  exit 1
fi
echo "Builder at $INSTANCE_IP, waiting for SSH..."
for i in $(seq 1 30); do
  ssh "${SSH_OPTS[@]}" "ubuntu@$INSTANCE_IP" true 2>/dev/null && break
  sleep 5
done

echo "=== Building the repo on the throwaway instance ==="
ssh "${SSH_OPTS[@]}" "ubuntu@$INSTANCE_IP" "
  sudo apt-get update
  sudo apt-get install -y dpkg-dev
  sudo apt-get install --download-only --reinstall -y ${PACKAGES[*]}
  mkdir -p /tmp/repo-build
  cp /var/cache/apt/archives/*.deb /tmp/repo-build/
  cd /tmp/repo-build
  dpkg-scanpackages . /dev/null > Packages
  gzip -9fc Packages > Packages.gz
  cat > MANIFEST.txt <<MANIFEST
Built: \$(date -u +%Y-%m-%dT%H:%M:%SZ)
Ubuntu release: \$(lsb_release -cs)
Source packages requested: ${PACKAGES[*]}
Package count (including transitive deps): \$(ls /tmp/repo-build/*.deb | wc -l)
MANIFEST
"

echo "=== Pulling the built repo back to the host ==="
rm -f "$REPO_DIR/apt-repo"/*.deb "$REPO_DIR/apt-repo/.build-complete"
scp "${SSH_OPTS[@]}" "ubuntu@$INSTANCE_IP:/tmp/repo-build/*" "$REPO_DIR/apt-repo/"
touch "$REPO_DIR/apt-repo/.build-complete"

echo "=== Done ==="
echo "Repo: $REPO_DIR/apt-repo ($(ls "$REPO_DIR/apt-repo"/*.deb | wc -l) packages)"
echo "Artifacts: $REPO_DIR/artifacts"
echo "Served at: http://192.168.100.1:8090/$CODENAME/apt-repo/ and /$CODENAME/artifacts/"
