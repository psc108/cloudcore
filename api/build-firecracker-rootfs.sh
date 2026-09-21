#!/usr/bin/env bash
# Builds the golden Firecracker guest rootfs for examples/llm-chat's Stage 5
# sandbox terminal — run by hand, offline, same cadence as
# build-package-repo.sh (when the base image needs a security update, or the
# guest's own package list changes). Not part of any live cloud-init path —
# the coordinator only ever downloads the finished, checksummed ext4 image
# this script produces, the same way it downloads llama.cpp's own binaries.
#
# debootstrap + a chroot both need a real root environment matching (or
# close enough to) the target release, so this follows build-package-repo.sh's
# own architecture exactly: launch a throwaway CloudCore ubuntu-22.04
# instance, build the image there over SSH, pull the result back, tear the
# instance down again. Deliberately NOT Firecracker's own quickstart demo
# rootfs (a shared squashfs + a shared public demo SSH key, fine for a
# single-user tutorial, wrong for a multi-tenant lab where every session
# needs its own isolated, freshly-keyed guest) — this builds a minimal
# rootfs from scratch instead: sshd, a coreutils/shell base, and a one-shot
# boot unit that fetches this SESSION's own public key from Firecracker's
# MMDS and installs it — no key ever baked into the image itself.
#
# Usage: api/build-firecracker-rootfs.sh
set -euo pipefail

CLOUDCORE_API_URL="${CLOUDCORE_API_URL:-http://127.0.0.1:8080}"
CLOUDCORE_API_TOKEN="${CLOUDCORE_API_TOKEN:-dev-token}"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
KEY="$SCRIPT_DIR/keys/cloudcore_ed25519"
REPO_DIR="$SCRIPT_DIR/package-repo/jammy"
API="$CLOUDCORE_API_URL"
AUTH=(-H "Authorization: Bearer $CLOUDCORE_API_TOKEN")
SSH_OPTS=(-o StrictHostKeyChecking=no -o ConnectTimeout=5 -i "$KEY")
SUFFIX="cloudcore-fcrootfs-builder-$(date +%s)"

# Same output location build-package-repo.sh's own ARTIFACT_URLS-downloaded
# files land in, so the coordinator's cloud-init fetches this exactly the
# same way it fetches every other pinned artifact — no new serving path.
mkdir -p "$REPO_DIR/artifacts"
OUT_NAME="firecracker-rootfs-jammy.ext4.gz"

cleanup() {
  echo "Cleaning up throwaway build infrastructure..."
  [ -n "${INSTANCE_ID:-}" ] && curl -s -X DELETE "$API/v1/instances/$INSTANCE_ID" "${AUTH[@]}" >/dev/null || true
  sleep 5
  [ -n "${SG_ID:-}" ] && curl -s -X DELETE "$API/v1/security-groups/$SG_ID" "${AUTH[@]}" >/dev/null || true
  [ -n "${SUBNET_ID:-}" ] && curl -s -X DELETE "$API/v1/subnets/$SUBNET_ID" "${AUTH[@]}" >/dev/null || true
  [ -n "${VPC_ID:-}" ] && curl -s -X DELETE "$API/v1/vpcs/$VPC_ID" "${AUTH[@]}" >/dev/null || true
}
trap cleanup EXIT

echo "=== Standing up a throwaway jammy builder instance ==="
VPC_ID=$(curl -s -X POST "$API/v1/vpcs" "${AUTH[@]}" -H "Content-Type: application/json" \
  -d "{\"name\":\"$SUFFIX-vpc\",\"cidr_block\":\"10.251.0.0/16\"}" | python3 -c "import json,sys; print(json.load(sys.stdin)['id'])")
SUBNET_ID=$(curl -s -X POST "$API/v1/subnets" "${AUTH[@]}" -H "Content-Type: application/json" \
  -d "{\"name\":\"$SUFFIX-subnet\",\"vpc_id\":\"$VPC_ID\",\"cidr_block\":\"10.251.0.0/16\",\"zone\":\"a\",\"public\":true}" | python3 -c "import json,sys; print(json.load(sys.stdin)['id'])")
SG_ID=$(curl -s -X POST "$API/v1/security-groups" "${AUTH[@]}" -H "Content-Type: application/json" \
  -d "{\"name\":\"$SUFFIX-sg\",\"vpc_id\":\"$VPC_ID\",\"ingress_rules\":[{\"protocol\":\"tcp\",\"from_port\":22,\"to_port\":22,\"cidr\":\"0.0.0.0/0\"}],\"egress_rules\":[{\"protocol\":\"-1\",\"cidr\":\"0.0.0.0/0\"}]}" | python3 -c "import json,sys; print(json.load(sys.stdin)['id'])")
INSTANCE_ID=$(curl -s -X POST "$API/v1/instances" "${AUTH[@]}" -H "Content-Type: application/json" \
  -d "{\"name\":\"$SUFFIX\",\"image_id\":\"ubuntu-22.04\",\"flavor\":\"standard.medium\",\"vpc_id\":\"$VPC_ID\",\"subnet_id\":\"$SUBNET_ID\",\"security_group_ids\":[\"$SG_ID\"]}" \
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

echo "=== Building the golden rootfs on the throwaway instance ==="
# Kept deliberately minimal: sshd + a login shell + coreutils + curl/wget
# (the student's own network access is the whole point of this feature) +
# a handful of everyday CLI tools. No compiler toolchain, no package
# manager reachability assumed at runtime (there is no apt mirror inside
# the sandbox subnet) — anything a student wants beyond this base they
# fetch themselves, over their own real internet access, same as any
# ordinary machine.
ssh "${SSH_OPTS[@]}" "ubuntu@$INSTANCE_IP" "
  set -e
  sudo DEBIAN_FRONTEND=noninteractive apt-get update
  sudo DEBIAN_FRONTEND=noninteractive apt-get install -y debootstrap

  ROOTFS_DIR=/tmp/fc-rootfs
  sudo rm -rf \"\$ROOTFS_DIR\"
  sudo debootstrap --arch=amd64 --variant=minbase jammy \"\$ROOTFS_DIR\" http://archive.ubuntu.com/ubuntu

  sudo chroot \"\$ROOTFS_DIR\" /bin/bash -c '
    set -e
    export DEBIAN_FRONTEND=noninteractive
    echo \"nameserver 8.8.8.8\" > /etc/resolv.conf
    apt-get update
    # systemd/systemd-sysv explicitly -- debootstrap --variant=minbase
    # only pulls Priority:required packages, and systemd itself is only
    # Priority:important, so a minbase chroot has no /bin/systemctl at
    # all unless something else pulls it in as a dependency. Confirmed
    # live: openssh-server alone does not do this on jammy, and every
    # systemctl enable/is-enabled call below fails outright without it.
    apt-get install -y --no-install-recommends \
      systemd systemd-sysv \
      openssh-server curl wget ca-certificates iproute2 iputils-ping \
      python3 nano less procps
    # A real login shell (not the minbase default of dash-only bare
    # essentials) and a real, unprivileged-by-default student account —
    # sudo works passwordless inside the microVM because the isolation
    # boundary this feature actually relies on is the microVM itself
    # (KVM + the iptables egress policy), not the in-guest account; root
    # inside a fully disposable, single-session, network-fenced guest
    # gains nothing an ordinary account inside the same guest did not
    # already have. Never gets them anywhere the guest itself cannot go.
    apt-get install -y --no-install-recommends sudo bash-completion
    useradd -m -s /bin/bash student
    usermod -aG sudo student
    echo \"student ALL=(ALL) NOPASSWD:ALL\" > /etc/sudoers.d/90-student
    chmod 440 /etc/sudoers.d/90-student
    mkdir -p /home/student/.ssh
    chmod 700 /home/student/.ssh
    chown -R student:student /home/student

    # MMDS v1 (no session-token dance — kept deliberately simple, the host
    # side pins mmds-config to version=V1 to match) fetch, one-shot at
    # every boot: this session own ed25519 public key was PUT into MMDS by
    # sandbox_terminal.py (Stage 5B) before this microVM ever started, so
    # by the time sshd comes up the key is already in place. Nothing here
    # is baked in at image-build time.
    cat > /usr/local/bin/fetch-mmds-key.sh <<\"EOS\"
#!/bin/bash
set -e
for i in \$(seq 1 20); do
  # --connect-timeout/--max-time are load-bearing, not cosmetic: confirmed
  # live that a bare curl call here (no timeout at all) can hang on the
  # OS-level TCP connect timeout -- tens of seconds -- on EACH of these 20
  # attempts if MMDS is unreachable for any reason, turning a boot that
  # should fail over in ~5s into one that stalls for minutes.
  KEY=\$(curl -s -f --connect-timeout 1 --max-time 2 -H \"Accept: application/json\" \"http://169.254.169.254/latest/meta-data/public-key\" || true)
  [ -n \"\$KEY\" ] && break
  sleep 0.25
done
if [ -n \"\$KEY\" ]; then
  echo \"\$KEY\" > /home/student/.ssh/authorized_keys
  chmod 600 /home/student/.ssh/authorized_keys
  chown student:student /home/student/.ssh/authorized_keys
fi
# Explicit exit 0 -- confirmed live that without this, a false \"if\"
# condition (no key available) becomes this script own exit status,
# which systemd reports as a hard unit FAILURE even though \"no key was
# available\" is the correct, graceful outcome of a best-effort fetch,
# not a real error (ssh.service still starts regardless; a connection
# without a matching key just fails normally at the SSH layer).
exit 0
EOS
    chmod +x /usr/local/bin/fetch-mmds-key.sh

    cat > /etc/systemd/system/fetch-mmds-key.service <<EOS
[Unit]
Description=Fetch this session own SSH key from Firecracker MMDS
Before=ssh.service
Wants=network-online.target
After=network-online.target

[Service]
Type=oneshot
ExecStart=/usr/local/bin/fetch-mmds-key.sh
RemainAfterExit=yes

[Install]
WantedBy=multi-user.target
EOS
    systemctl enable fetch-mmds-key.service
    systemctl enable ssh.service

    # Password auth off — the MMDS-delivered key is the only way in.
    sed -i \"s/^#\\?PasswordAuthentication.*/PasswordAuthentication no/\" /etc/ssh/sshd_config
    echo \"PermitRootLogin no\" >> /etc/ssh/sshd_config

    # Regenerate host keys fresh at every boot (not baked into the golden
    # image) — same one-shot-unit idiom as the MMDS key fetch above.
    rm -f /etc/ssh/ssh_host_*key*
    cat > /etc/systemd/system/regen-host-keys.service <<EOS
[Unit]
Description=Regenerate SSH host keys fresh every boot
Before=ssh.service
ConditionFileNotEmpty=!/etc/ssh/ssh_host_rsa_key

[Service]
Type=oneshot
ExecStart=/usr/bin/ssh-keygen -A
RemainAfterExit=yes

[Install]
WantedBy=multi-user.target
EOS
    systemctl enable regen-host-keys.service

    apt-get clean
    rm -rf /var/lib/apt/lists/* /tmp/* /var/tmp/*
  '

  # A serial getty on ttyS0 too, as a fallback console independent of
  # networking/sshd (matches Firecracker's own conventional boot args,
  # console=ttyS0) — genuinely useful for diagnosing a session that never
  # comes up over SSH, not exposed to the student themselves.
  sudo chroot \"\$ROOTFS_DIR\" systemctl enable serial-getty@ttyS0.service 2>/dev/null || true

  # -d populates the filesystem straight from the directory in one shot —
  # the same technique Firecracker's own getting-started guide uses for
  # its demo image, avoids a separate mount/copy/unmount dance.
  ROOTFS_SIZE_MB=768
  sudo truncate -s \"\${ROOTFS_SIZE_MB}M\" /tmp/firecracker-rootfs-jammy.ext4
  sudo mkfs.ext4 -q -d \"\$ROOTFS_DIR\" -F /tmp/firecracker-rootfs-jammy.ext4
  sudo e2fsck -fy /tmp/firecracker-rootfs-jammy.ext4 || true
  gzip -c /tmp/firecracker-rootfs-jammy.ext4 > /tmp/$OUT_NAME
"

echo "=== Pulling the built rootfs back to the host ==="
scp "${SSH_OPTS[@]}" "ubuntu@$INSTANCE_IP:/tmp/$OUT_NAME" "$REPO_DIR/artifacts/$OUT_NAME"

SHA256=$(sha256sum "$REPO_DIR/artifacts/$OUT_NAME" | cut -d' ' -f1)
echo "=== Done ==="
echo "Rootfs: $REPO_DIR/artifacts/$OUT_NAME"
echo "SHA256: $SHA256"
echo "Set firecracker_rootfs_name = \"$OUT_NAME\" and firecracker_rootfs_sha256 = \"$SHA256\" in examples/llm-chat/variables.tf"
echo "Served at: http://192.168.100.1:8090/jammy/artifacts/$OUT_NAME"
