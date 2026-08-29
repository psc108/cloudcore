#!/usr/bin/env bash
# CloudCore install script
# Run once after cloning: bash scripts/install.sh
# Requires: Ubuntu 22.04+, sudo access, KVM-capable host
set -euo pipefail

REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
CURRENT_USER="$(whoami)"

echo "==> CloudCore install"
echo "    repo : $REPO_DIR"
echo "    user : $CURRENT_USER"
echo ""

# ---------------------------------------------------------------------------
# 1. System packages
# ---------------------------------------------------------------------------
echo "==> Installing system packages..."
sudo apt-get update -qq
sudo apt-get install -y \
    python3 python3-pip python3-venv \
    qemu-kvm libvirt-daemon-system libvirt-clients virtinst \
    cloud-image-utils \
    haproxy \
    dnsmasq \
    lvm2 \
    nfs-common \
    git curl

# Add current user to libvirt group (takes effect on next login / newgrp)
if ! groups "$CURRENT_USER" | grep -q libvirt; then
    echo "==> Adding $CURRENT_USER to libvirt group..."
    sudo usermod -aG libvirt "$CURRENT_USER"
    echo "    NOTE: log out and back in (or run 'newgrp libvirt') for group to take effect"
fi

# ---------------------------------------------------------------------------
# 2. Python dependencies
# ---------------------------------------------------------------------------
echo "==> Installing Python dependencies..."
pip3 install --user -r "$REPO_DIR/requirements.txt"

# ---------------------------------------------------------------------------
# 3. Ansible collection
# ---------------------------------------------------------------------------
echo "==> Installing Ansible collection..."
(
    cd "$REPO_DIR/ansible/collections/cloudcore"
    ansible-galaxy collection build --force -q
    ansible-galaxy collection install cloudcore-cloudcore-*.tar.gz --force -q
    rm -f cloudcore-cloudcore-*.tar.gz
)

# ---------------------------------------------------------------------------
# 4. Ubuntu cloud image
# ---------------------------------------------------------------------------
echo "==> Fetching Ubuntu 22.04 cloud image (skipped if already present)..."
bash "$REPO_DIR/api/fetch-image.sh"

# ---------------------------------------------------------------------------
# 5. SSH keypair
# ---------------------------------------------------------------------------
KEYS_DIR="$REPO_DIR/api/keys"
mkdir -p "$KEYS_DIR"
if [[ ! -f "$KEYS_DIR/cloudcore_ed25519" ]]; then
    echo "==> Generating CloudCore SSH keypair..."
    ssh-keygen -t ed25519 -f "$KEYS_DIR/cloudcore_ed25519" -N "" -C "cloudcore"
else
    echo "==> SSH keypair already exists, skipping."
fi

# ---------------------------------------------------------------------------
# 6. Bridge network (system-level service)
# ---------------------------------------------------------------------------
echo "==> Installing bridge service (cloudcore-bridge)..."
sudo cp "$REPO_DIR/api/cloudcore-bridge.service" /etc/systemd/system/cloudcore-bridge.service
sudo systemctl daemon-reload
sudo systemctl enable --now cloudcore-bridge.service

# ---------------------------------------------------------------------------
# 7. Systemd user services (API + terminal)
# ---------------------------------------------------------------------------
echo "==> Installing user services..."
SERVICE_DIR="$HOME/.config/systemd/user"
mkdir -p "$SERVICE_DIR"

# Substitute the hardcoded path in service files with the actual clone location
for SVC in cloudcore-api cloudcore-terminal; do
    sed "s|/home/scottp/IdeaProjects/CloudProject|$REPO_DIR|g" \
        "$REPO_DIR/api/${SVC}.service" > "$SERVICE_DIR/${SVC}.service"
done

systemctl --user daemon-reload
systemctl --user enable --now cloudcore-api.service
systemctl --user enable --now cloudcore-terminal.service

# ---------------------------------------------------------------------------
# 8. Verify
# ---------------------------------------------------------------------------
echo ""
echo "==> Waiting for API to start..."
for i in $(seq 1 10); do
    if curl -sf -H "Authorization: Bearer dev-token" http://127.0.0.1:8080/v1/dashboard > /dev/null 2>&1; then
        echo "==> API is up."
        break
    fi
    sleep 1
done

echo ""
echo "==> Done. CloudCore is running."
echo ""
echo "    UI:  http://127.0.0.1:8080"
echo "    API: http://127.0.0.1:8080/v1/"
echo "    Token: dev-token  (set CLOUDCORE_API_TOKEN in the service to change)"
echo ""
echo "    To change the API token:"
echo "      systemctl --user edit cloudcore-api.service"
echo "      # Add: [Service]"
echo "      #      Environment=CLOUDCORE_API_TOKEN=your-token"
echo "      systemctl --user restart cloudcore-api.service"
echo ""
echo "    Service logs:"
echo "      journalctl --user -u cloudcore-api -f"
echo "      journalctl --user -u cloudcore-terminal -f"
