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
# UserKnownHostsFile=/dev/null, not just StrictHostKeyChecking=no --
# found live: this lab's own small throwaway-instance IP pool recycles
# addresses, so a later builder run can land on an IP a previous run
# already has a *different* host key recorded for in the real
# known_hosts file. StrictHostKeyChecking=no alone only skips the
# prompt for a genuinely new host; a *changed* key on an existing
# known_hosts entry is still refused outright (ssh's own real
# man-in-the-middle protection), which silently corrupted this script's
# own big multi-line remote command mid-connection rather than failing
# cleanly -- confirmed by reproducing the exact same run against a
# fresh IP with no known_hosts history at all, and separately by
# hitting ssh's own explicit "Offending key" refusal once one of these
# builder IPs happened to collide with a stale entry. Never treat these
# throwaway builder hosts as long-lived enough to want persisted host
# key state at all.
SSH_OPTS=(-o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o ConnectTimeout=5 -i "$KEY")
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
# a handful of everyday CLI tools, plus (Stage 12) the Node, C/C++ and Go
# toolchains the per-run microVMs compile and run model-generated code
# with. No package manager reachability assumed at runtime (there is no
# apt mirror inside the sandbox subnet) — anything a student wants beyond
# this base they fetch themselves, over their own real internet access,
# same as any ordinary machine.
ssh "${SSH_OPTS[@]}" "ubuntu@$INSTANCE_IP" "
  set -e
  sudo DEBIAN_FRONTEND=noninteractive apt-get update
  sudo DEBIAN_FRONTEND=noninteractive apt-get install -y debootstrap

  ROOTFS_DIR=/tmp/fc-rootfs
  sudo rm -rf \"\$ROOTFS_DIR\"
  sudo debootstrap --arch=amd64 --variant=minbase jammy \"\$ROOTFS_DIR\" http://archive.ubuntu.com/ubuntu

  # Written to a real file and run as a real script (chroot ... bash
  # /build-inner.sh), NOT the classic chroot ... bash -c '...' inline
  # form -- found live that the latter, nested three quote-levels deep
  # (ssh's own double-quoted argument, around a single-quoted -c
  # argument, around this script's own heredocs) reliably corrupted
  # partway through, bash reporting a phantom \"here-document ...
  # delimited by end-of-file\" failure that turned out to have nothing
  # to do with this script's own content at all: the exact same text,
  # written to a real file and executed with bash <file> instead, ran
  # start to finish with no error, every single time, across several
  # independent real rebuilds. The outer heredoc below uses a quoted
  # delimiter ('BUILDEOF') so nothing in the script body -- including
  # fetch-mmds-key.sh's own \$-references, meant to survive untouched
  # into that separate boot-time script -- gets expanded while this
  # step merely writes it to disk.
  cat > /tmp/build-inner.sh <<'BUILDEOF'
#!/bin/bash
set -e
export DEBIAN_FRONTEND=noninteractive
echo \"nameserver 8.8.8.8\" > /etc/resolv.conf
apt-get update

# Found live: /etc/hostname debootstrap leaves in a fresh minbase chroot
# defaulted to whatever the BUILDER instance's own real hostname was at
# build time (this script's own \$SUFFIX, cloudcore-fcrootfs-builder-<ts>)
# -- baked into the golden image and shipped identically to every future
# session, since every session boots from the same frozen artifact.
# Harmless on its own, but /etc/hosts doesn't exist in this chroot AT
# ALL (also a minbase-default gap, not something anything here ever
# created), so every single sudo invocation prints \"unable to resolve
# host <that leftover builder name>\" -- confirmed live, a real student-
# visible wart on every sudo command, though never fatal (sudo still
# runs the command regardless). A static, student-meaningful hostname
# plus a real /etc/hosts fixes both -- no per-session uniqueness needed,
# since this guest is already fully isolated and single-tenant.
echo \"sandbox\" > /etc/hostname
cat > /etc/hosts <<\"HOSTSEOF\"
127.0.0.1 localhost
127.0.1.1 sandbox
HOSTSEOF

# systemd/systemd-sysv explicitly -- debootstrap --variant=minbase
# only pulls Priority:required packages, and systemd itself is only
# Priority:important, so a minbase chroot has no /bin/systemctl at
# all unless something else pulls it in as a dependency. Confirmed
# live: openssh-server alone does not do this on jammy, and every
# systemctl enable/is-enabled call below fails outright without it.
# fdisk -- found live (direct report, tried partitioning a disk from a
# Terminal session) that it's genuinely not pulled in by anything else
# here: it's its own package on jammy (util-linux itself only carries
# lsblk/mount/blkid etc.), same class of gap as vim's own earlier fix.
apt-get install -y --no-install-recommends \
  systemd systemd-sysv \
  openssh-server curl wget ca-certificates iproute2 iputils-ping \
  python3 nano vim less procps fdisk
# A real login shell (not the minbase default of dash-only bare
# essentials) and a real, unprivileged-by-default student account --
# sudo works passwordless inside the microVM because the isolation
# boundary this feature actually relies on is the microVM itself
# (KVM + the iptables egress policy), not the in-guest account; root
# inside a fully disposable, single-session, network-fenced guest
# gains nothing an ordinary account inside the same guest did not
# already have. Never gets them anywhere the guest itself cannot go.
apt-get install -y --no-install-recommends sudo bash-completion

# Stage 12 (llm-chat-sandbox-extensions-Phased-Implementation.md): the
# toolchains verify_proxy.py's per-run microVMs use for Bash, Node, C/C++
# and Go. nodejs is in jammy's universe component, so universe is enabled
# for this install only and removed again afterwards: the Terminal's own
# runtime package index stays main-only, which is exactly what the Linux
# Help system prompt tells the model.
cp /etc/apt/sources.list /etc/apt/sources.list.main-only
sed -i \"s/ main\$/ main universe/\" /etc/apt/sources.list
apt-get update
apt-get install -y --no-install-recommends nodejs gcc g++ libc6-dev golang-go
mv /etc/apt/sources.list.main-only /etc/apt/sources.list

# Stage 11/12: the guest-side counterpart of verify_proxy.py's own
# _stdin_wait_state(). Run as root (via sudo) over SSH by the per-run
# executor with the path of a file holding the program's root PID;
# prints waiting / busy / unknown / gone. Same rules as the host-side
# version: a task blocked in read(0) on the program's own stdin means
# waiting (unless bytes we already sent are still unread), a running or
# timer-sleeping task means busy, anything else is unknown. x86_64
# syscall numbers. Written with no dollar signs, double quotes or
# backslashes so it survives this script's nested quoting untouched.
cat > /usr/local/bin/stdin-wait-check <<\"EOS\"
#!/usr/bin/python3
import array, fcntl, os, sys, termios

READ, POLL, SELECT, NANOSLEEP, CLOCK_NANOSLEEP, PSELECT6, PPOLL = 0, 7, 23, 35, 230, 270, 271


def descendants(root):
    children = {}
    for entry in os.listdir('/proc'):
        if not entry.isdigit():
            continue
        try:
            stat = open('/proc/' + entry + '/stat').read()
        except OSError:
            continue
        ppid = int(stat[stat.rfind(')') + 2:].split()[1])
        children.setdefault(ppid, []).append(int(entry))
    found, stack = [], [root]
    while stack:
        pid = stack.pop()
        found.append(pid)
        stack.extend(children.get(pid, []))
    return found


def unread(pid):
    try:
        fd = os.open('/proc/' + str(pid) + '/fd/0', os.O_RDONLY | os.O_NONBLOCK)
    except OSError:
        return 0
    try:
        buf = array.array('i', [0])
        fcntl.ioctl(fd, termios.FIONREAD, buf, True)
        return buf[0]
    except OSError:
        return 0
    finally:
        os.close(fd)


def state(root):
    try:
        target = os.readlink('/proc/' + str(root) + '/fd/0')
    except OSError:
        return 'gone'
    busy = False
    for pid in descendants(root):
        try:
            tids = os.listdir('/proc/' + str(pid) + '/task')
        except OSError:
            continue
        for tid in tids:
            base = '/proc/' + str(pid) + '/task/' + tid
            try:
                stat = open(base + '/stat').read()
                sc = open(base + '/syscall').read().split()
            except OSError:
                continue
            st = stat[stat.rfind(')') + 2:].split()[0]
            if st == 'R' or not sc or sc[0] == 'running':
                busy = True
                continue
            try:
                nr = int(sc[0])
            except ValueError:
                continue
            if nr == READ and len(sc) > 1 and int(sc[1], 16) == 0:
                try:
                    same = os.readlink('/proc/' + str(pid) + '/fd/0') == target
                except OSError:
                    same = False
                if same:
                    if unread(pid) > 0:
                        busy = True
                    else:
                        return 'waiting'
            elif nr in (NANOSLEEP, CLOCK_NANOSLEEP):
                busy = True
            elif nr in (SELECT, PSELECT6) and len(sc) > 1 and int(sc[1], 16) == 0:
                busy = True
            elif nr in (POLL, PPOLL) and len(sc) > 2 and int(sc[2], 16) == 0:
                busy = True
    return 'busy' if busy else 'unknown'


try:
    root_pid = int(open(sys.argv[1]).read().split()[0])
except (OSError, IndexError, ValueError):
    print('gone')
else:
    print(state(root_pid))
EOS
chmod 755 /usr/local/bin/stdin-wait-check
useradd -m -s /bin/bash student
usermod -aG sudo student
echo \"student ALL=(ALL) NOPASSWD:ALL\" > /etc/sudoers.d/90-student
chmod 440 /etc/sudoers.d/90-student
mkdir -p /home/student/.ssh
chmod 700 /home/student/.ssh
chown -R student:student /home/student

# MMDS v1 (no session-token dance -- kept deliberately simple, the host
# side pins mmds-config to version=V1 to match) fetch, one-shot at
# every boot: this session own ed25519 public key was PUT into MMDS by
# sandbox_terminal.py (Stage 5B) before this microVM ever started, so
# by the time sshd comes up the key is already in place. Nothing here
# is baked in at image-build time.
cat > /usr/local/bin/fetch-mmds-key.sh <<\"EOS\"
#!/bin/bash
set -e
# MMDS's link-local address needs an explicit host-scope route -- it is
# NOT reachable via the normal default route, confirmed directly against
# Firecracker's own docs (mmds-user-guide.md: \"guest applications must
# insert a new rule into the routing table... ip route add \$MMDS_IPV4_ADDR
# dev \$MMDS_NET_IF\"). Without this line every curl below fails with a
# real, immediate \"Network is unreachable\", not a slow timeout.
ip route add 169.254.169.254 dev eth0 2>/dev/null || true
for i in \$(seq 1 20); do
  # --connect-timeout/--max-time are load-bearing, not cosmetic: confirmed
  # live that a bare curl call here (no timeout at all) can hang on the
  # OS-level TCP connect timeout -- tens of seconds -- on EACH of these 20
  # attempts if MMDS is unreachable for any reason, turning a boot that
  # should fail over in ~5s into one that stalls for minutes.
  # Deliberately NO Accept: application/json here -- confirmed live that
  # MMDS returns this leaf string value JSON-quoted (literal double
  # quotes wrapped around it) when that header is sent, which then
  # lands verbatim in authorized_keys and breaks every login. Omitting
  # the header gets MMDS's own IMDS-compatible plain-text format
  # instead, which for a plain string value like this one is exactly
  # the raw key line SSH expects, no quotes.
  KEY=\$(curl -s -f --connect-timeout 1 --max-time 2 \"http://169.254.169.254/latest/meta-data/public-key\" || true)
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

# Password auth off -- the MMDS-delivered key is the only way in.
sed -i \"s/^#\\?PasswordAuthentication.*/PasswordAuthentication no/\" /etc/ssh/sshd_config
echo \"PermitRootLogin no\" >> /etc/ssh/sshd_config

# Regenerate host keys fresh at every boot (not baked into the golden
# image) -- same one-shot-unit idiom as the MMDS key fetch above.
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

# The image-build cleanup below strips /var/lib/apt/lists/* to keep
# the shipped golden image small -- correct for a frozen artifact
# reused across many future sessions, but it means a student trying
# apt install anything at runtime hits a bare, empty index and gets
# an Unable-to-locate-package error regardless of what they ask
# for, indistinguishable from the package genuinely not existing
# (found live: reported as vim specifically not installing, but the
# same failure applies to every package, not just that one). Real
# internet access is the whole point of this feature (see the
# top-of-file design note above, and sandbox_system_message own
# pip install / curl / clone-a-repo wording) so apt should work
# too. Fixed with a one-shot boot unit that refreshes the index
# fresh every session, over that session own real internet access,
# deliberately NOT gating ssh.service the way fetch-mmds-key
# service does -- a slower-than-usual apt mirror must never delay
# the terminal actually becoming usable, only apt own readiness a
# few seconds later.
#
# A bare `apt-get update` as ExecStart is not enough on its own -- two
# real, compounding bugs found live testing this exact unit:
#
# 1. network-online.target is satisfied trivially on this minimal
#    debootstrap image (no NetworkManager/systemd-networkd wait-online
#    unit installed to give that target real meaning), so it can fire
#    before the interface/routing is genuinely usable yet.
# 2. Far more persistent: /etc/resolv.conf as set at image-build time
#    (a plain, working \"nameserver 8.8.8.8\") gets silently replaced
#    at boot -- systemd (pulled in as a dependency of installing
#    systemd-sysv/openssh-server) manages /etc/resolv.conf itself and
#    points it at 127.0.0.53, systemd-resolved's own stub listener,
#    which is never actually running on this minimal image (that
#    package/service was never installed or enabled here). Every DNS
#    query failed permanently as a result -- \"Temporary failure
#    resolving archive.ubuntu.com\" -- regardless of how many times
#    apt-get update was retried, confirmed live with getent hosts
#    returning nothing at all, indefinitely, not just at boot. Forcing
#    a real, static resolv.conf on every attempt (not just once at
#    image-build time) fixes it for good, whatever keeps re-managing
#    the file.
cat > /usr/local/bin/refresh-apt-index.sh <<\"EOS\"
#!/bin/bash
for i in \$(seq 1 10); do
  rm -f /etc/resolv.conf
  echo \"nameserver 8.8.8.8\" > /etc/resolv.conf
  apt-get update -qq && exit 0
  sleep 2
done
# Best-effort, same reasoning as fetch-mmds-key.sh's own explicit
# exit 0 -- a still-empty index after 10 real tries just means apt
# install keeps failing same as before this fix, not a regression;
# it must never be reported as a hard unit failure.
exit 0
EOS
chmod +x /usr/local/bin/refresh-apt-index.sh

cat > /etc/systemd/system/refresh-apt-index.service <<EOS
[Unit]
Description=Refresh the apt package index for a fresh session, over this session own real internet access
Wants=network-online.target
After=network-online.target

[Service]
Type=oneshot
ExecStart=/usr/local/bin/refresh-apt-index.sh
RemainAfterExit=yes

[Install]
WantedBy=multi-user.target
EOS
systemctl enable refresh-apt-index.service

# F-165: the refresh above deliberately does not gate ssh, so a student who
# ran apt install in the first few seconds of a session got Unable to
# locate package for packages that exist. These wrappers sit in
# /usr/local/bin, ahead of /usr/bin on both the login PATH and sudo's
# secure_path, and wait (bounded) for the refresh to finish before running
# the real apt. Per-run microVMs mask the unit, so there it is never
# activating and the wrapper passes straight through. (No double quotes or
# backticks in this comment: it sits inside the outer ssh string.)
for tool in apt apt-get; do
cat > /usr/local/bin/\$tool <<\"EOS\"
#!/bin/sh
real=/usr/bin/\$(basename \"\$0\")
if [ \"\$(systemctl is-active refresh-apt-index.service 2>/dev/null)\" = activating ]; then
  echo \"Waiting for this new session's package index refresh to finish (first ~30s after start)...\" >&2
  i=0
  while [ \"\$(systemctl is-active refresh-apt-index.service 2>/dev/null)\" = activating ] && [ \$i -lt 90 ]; do
    sleep 1; i=\$((i + 1))
  done
fi
exec \"\$real\" \"\$@\"
EOS
chmod 755 /usr/local/bin/\$tool
done

# Stage 10 (llm-chat-sandbox-extensions-Phased-Implementation.md): this
# image is now attached READ-ONLY and shared by every session (hard-
# linked into each jailer chroot by examples/llm-chat/files/microvm.py),
# with a fresh per-session scratch drive as /dev/vdb. overlay-init runs
# as PID 1 (init=/sbin/overlay-init on the kernel command line), lays a
# writable overlayfs over the read-only root using the scratch drive,
# pivots into it and hands off to systemd. Every write a session makes
# lands on its own scratch drive; the golden image is never modified.
# /overlay and /rom must exist in the image itself -- the root is
# read-only by the time this runs, so they can't be created at boot.
mkdir -p /overlay /rom
cat > /sbin/overlay-init <<\"EOS\"
#!/bin/sh
set -e
# devtmpfs may or may not already be mounted by the kernel
# (CONFIG_DEVTMPFS_MOUNT) -- /dev/vdb is needed either way.
mount -t devtmpfs devtmpfs /dev 2>/dev/null || true
mount -t ext4 -o noatime /dev/vdb /overlay
mkdir -p /overlay/upper /overlay/work
mount -t overlay overlay \
  -o noatime,lowerdir=/,upperdir=/overlay/upper,workdir=/overlay/work /mnt
pivot_root /mnt /mnt/rom
exec /sbin/init \"\$@\"
EOS
chmod 755 /sbin/overlay-init

apt-get clean
rm -rf /var/lib/apt/lists/* /tmp/* /var/tmp/*
BUILDEOF
  sudo cp /tmp/build-inner.sh \"\$ROOTFS_DIR/build-inner.sh\"
  sudo chroot \"\$ROOTFS_DIR\" /bin/bash /build-inner.sh
  sudo rm -f \"\$ROOTFS_DIR/build-inner.sh\" /tmp/build-inner.sh

  # A serial getty on ttyS0 too, as a fallback console independent of
  # networking/sshd (matches Firecracker's own conventional boot args,
  # console=ttyS0) — genuinely useful for diagnosing a session that never
  # comes up over SSH, not exposed to the student themselves.
  sudo chroot \"\$ROOTFS_DIR\" systemctl enable serial-getty@ttyS0.service 2>/dev/null || true

  # -d populates the filesystem straight from the directory in one shot —
  # the same technique Firecracker's own getting-started guide uses for
  # its demo image, avoids a separate mount/copy/unmount dance.
  # 2048, up from 768 for Stage 12's toolchains (Go alone is ~400MB).
  # Costs disk once: the image is shared read-only by every session.
  ROOTFS_SIZE_MB=2048
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
