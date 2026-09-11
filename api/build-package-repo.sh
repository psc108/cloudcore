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
# Covers every current example template's package/artifact needs, not
# just ha-frontend-lb (haFullStack-Findings-Log.md F-039-041 cleanup led
# to this repo going host-level/always-on; this pass extends its
# coverage to ghidra-workstation, kiwix-library, wifi-sniffer, full-stack
# and load-balanced-web too, since none of those have any bandwidth-
# saving mechanism of their own).
#
# Usage: api/build-package-repo.sh [codename] [package...]
#   codename   Ubuntu release codename — must match a CloudCore image_id
#              of "ubuntu-<version>" (default: jammy -> ubuntu-22.04)
#   package... top-level packages to include (their own dependencies are
#              resolved automatically) — default: the union of every
#              current example template's package list
set -euo pipefail

CODENAME="${1:-jammy}"
shift || true
PACKAGES=("$@")
if [ ${#PACKAGES[@]} -eq 0 ]; then
  PACKAGES=(
    # ha-frontend-lb
    curl ca-certificates dpkg-dev mysql-server mysql-client nginx keepalived \
    keystone python3-pymysql python3-memcache python3 rabbitmq-server \
    # full-stack, load-balanced-web (nginx already listed above)
    # ghidra-workstation
    xfce4 xfce4-terminal tigervnc-standalone-server tigervnc-common novnc websockify unzip gnupg \
    # kiwix-library (curl/ca-certificates already listed above)
    # wifi-sniffer
    build-essential dkms bc libelf-dev git aircrack-ng hcxtools hcxdumptool tcpdump tshark
  )
fi

# Packages that only exist in a third-party apt repo, not Ubuntu's own
# archive — the builder adds both repos unconditionally before the
# install step below (harmless on a throwaway instance even for a build
# that doesn't strictly need them) rather than threading a per-template
# opt-in through this script.
THIRDPARTY_PACKAGES=(temurin-21-jdk kismet)

case "$CODENAME" in
  jammy) IMAGE_ID="ubuntu-22.04" ;;
  noble) IMAGE_ID="ubuntu-24.04" ;;
  *) echo "Unknown codename '$CODENAME' — add it to the case statement in this script first" >&2; exit 1 ;;
esac

# Defaults match every other doc/script in this repo (README, HELP.md,
# setup-package-repo.sh's own printed instructions) — override either by
# exporting the env var first if your CloudCore instance uses a different
# API URL or token.
CLOUDCORE_API_URL="${CLOUDCORE_API_URL:-http://127.0.0.1:8080}"
CLOUDCORE_API_TOKEN="${CLOUDCORE_API_TOKEN:-dev-token}"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
KEY="$SCRIPT_DIR/keys/cloudcore_ed25519"
REPO_DIR="$SCRIPT_DIR/package-repo/$CODENAME"
API="$CLOUDCORE_API_URL"
AUTH=(-H "Authorization: Bearer $CLOUDCORE_API_TOKEN")
SSH_OPTS=(-o StrictHostKeyChecking=no -o ConnectTimeout=5 -i "$KEY")
SUFFIX="cloudcore-repo-builder-$(date +%s)"

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
# Exact values transcribed from each template's own variables.tf — keep
# these in sync by hand when a template bumps its pinned version; each
# guest's own cloud-init still verifies its checksum independently at
# boot, this cache is purely a bandwidth shortcut, not a trust boundary.
declare -A ARTIFACT_URLS=(
  [step-ca.deb]="https://github.com/smallstep/certificates/releases/download/v0.30.2/step-ca_0.30.2-1_amd64.deb"
  [step-cli.deb]="https://github.com/smallstep/cli/releases/download/v0.30.6/step-cli_0.30.6-1_amd64.deb"
  [proxysql.deb]="https://github.com/sysown/proxysql/releases/download/v3.0.11/proxysql_3.0.11-ubuntu22_amd64.deb"
  # ghidra-workstation
  [ghidra.zip]="https://github.com/NationalSecurityAgency/ghidra/releases/download/Ghidra_12.1.3_build/ghidra_12.1.3_PUBLIC_20260817.zip"
  # kiwix-library
  [kiwix-tools.tar.gz]="https://download.kiwix.org/release/kiwix-tools/kiwix-tools_linux-x86_64-3.8.2.tar.gz"
  # kiwix-library — the 2.2GB "top articles, no pictures" ZIM (variables.tf
  # notes the full-image variant is 8+GB and deliberately not used here).
  # This one dwarfs every other artifact in this list; skip it with
  # SKIP_ZIM=1 if disk space is a concern — everything else still builds.
  [wikipedia_en_top_nopic_2026-06.zim]="https://download.kiwix.org/zim/wikipedia/wikipedia_en_top_nopic_2026-06.zim"
)
if [ "${SKIP_ZIM:-0}" = "1" ]; then
  unset "ARTIFACT_URLS[wikipedia_en_top_nopic_2026-06.zim]"
fi
for name in "${!ARTIFACT_URLS[@]}"; do
  curl -fL --speed-limit 1024 --speed-time 30 -C - -o "$REPO_DIR/artifacts/$name" "${ARTIFACT_URLS[$name]}"
done

# wifi-sniffer's RTL8812AU driver is built from source via DKMS on the
# guest itself (kernel-specific, not pre-buildable as a binary), but the
# GitHub source clone it starts from can still be pre-cached — avoids a
# live git fetch from GitHub at every guest boot.
RTL8812AU_REF="v5.6.4.2"
if [ ! -f "$REPO_DIR/artifacts/rtl8812au-$RTL8812AU_REF.tar.gz" ]; then
  RTL_TMP="$(mktemp -d)"
  git clone -b "$RTL8812AU_REF" --depth 1 https://github.com/aircrack-ng/rtl8812au.git "$RTL_TMP/rtl8812au"
  tar -C "$RTL_TMP" -czf "$REPO_DIR/artifacts/rtl8812au-$RTL8812AU_REF.tar.gz" rtl8812au
  rm -rf "$RTL_TMP"
fi

{
  echo "Built: $(date -u +%Y-%m-%dT%H:%M:%SZ)"
  for name in "${!ARTIFACT_URLS[@]}"; do
    echo "$name  sha256=$(sha256sum "$REPO_DIR/artifacts/$name" | cut -d' ' -f1)  source=${ARTIFACT_URLS[$name]}"
  done
  echo "rtl8812au-$RTL8812AU_REF.tar.gz  sha256=$(sha256sum "$REPO_DIR/artifacts/rtl8812au-$RTL8812AU_REF.tar.gz" | cut -d' ' -f1)  source=https://github.com/aircrack-ng/rtl8812au.git@$RTL8812AU_REF"
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

# linux-headers-$(uname -r)/linux-modules-extra-$(uname -r) are cached
# under whatever kernel version this builder boots with — valid as long
# as guests boot the same $IMAGE_ID at roughly the same time as this
# build ran. A guest that picks up a kernel bump via unattended-upgrades
# before this repo is next rebuilt will fall back to wifi-sniffer's own
# live apt-get for just those two packages (same as it always has).
echo "=== Building the repo on the throwaway instance ==="
ssh "${SSH_OPTS[@]}" "ubuntu@$INSTANCE_IP" "
  set -e
  # Third-party apt repos — ghidra-workstation needs temurin-21-jdk
  # (Adoptium), wifi-sniffer needs kismet (kismetwireless.net). Neither
  # package exists in Ubuntu's own archive, so these repos have to be
  # trusted before the download step below can see them at all.
  sudo mkdir -p /etc/apt/keyrings
  curl -fsSL https://packages.adoptium.net/artifactory/api/gpg/key/public | sudo gpg --dearmor -o /etc/apt/keyrings/adoptium.gpg
  echo \"deb [signed-by=/etc/apt/keyrings/adoptium.gpg] https://packages.adoptium.net/artifactory/deb \$(lsb_release -cs) main\" | sudo tee /etc/apt/sources.list.d/adoptium.list
  wget -O - https://www.kismetwireless.net/repos/kismet-release.gpg.key --quiet | sudo gpg --dearmor | sudo tee /usr/share/keyrings/kismet-archive-keyring.gpg >/dev/null
  echo \"deb [signed-by=/usr/share/keyrings/kismet-archive-keyring.gpg] https://www.kismetwireless.net/repos/apt/release/\$(lsb_release -cs) \$(lsb_release -cs) main\" | sudo tee /etc/apt/sources.list.d/kismet.list >/dev/null

  sudo apt-get update
  sudo apt-get install -y dpkg-dev
  sudo DEBIAN_FRONTEND=noninteractive apt-get install --download-only --reinstall -y \
    ${PACKAGES[*]} ${THIRDPARTY_PACKAGES[*]} \
    linux-headers-\$(uname -r) linux-modules-extra-\$(uname -r)
  mkdir -p /tmp/repo-build
  cp /var/cache/apt/archives/*.deb /tmp/repo-build/
  cd /tmp/repo-build
  dpkg-scanpackages . /dev/null > Packages
  gzip -9fc Packages > Packages.gz
  cat > MANIFEST.txt <<MANIFEST
Built: \$(date -u +%Y-%m-%dT%H:%M:%SZ)
Ubuntu release: \$(lsb_release -cs)
Source packages requested: ${PACKAGES[*]} ${THIRDPARTY_PACKAGES[*]} linux-headers-\$(uname -r) linux-modules-extra-\$(uname -r)
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
