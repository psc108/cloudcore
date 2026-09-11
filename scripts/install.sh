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
# golang-go on Ubuntu 22.04 (1.22) is older than provider/go.mod's declared
# "go 1.25.8" — that's fine, Go's own toolchain auto-switch (GOTOOLCHAIN=auto,
# the default since 1.21) transparently downloads and uses 1.25.8 the first
# time `go build` runs in provider/, as long as this bootstrap Go and
# internet access are both present.
sudo apt-get update -qq
sudo apt-get install -y \
    python3 python3-pip python3-venv \
    qemu-kvm libvirt-daemon-system libvirt-clients virtinst \
    cloud-image-utils \
    haproxy \
    dnsmasq \
    lvm2 \
    nfs-common \
    golang-go \
    git curl

# Add current user to libvirt group (takes effect on next login / newgrp)
if ! groups "$CURRENT_USER" | grep -q libvirt; then
    echo "==> Adding $CURRENT_USER to libvirt group..."
    sudo usermod -aG libvirt "$CURRENT_USER"
    echo "    NOTE: log out and back in (or run 'newgrp libvirt') for group to take effect"
fi

# ---------------------------------------------------------------------------
# 2. OpenTofu CLI
# ---------------------------------------------------------------------------
if command -v tofu > /dev/null 2>&1; then
    echo "==> OpenTofu already installed ($(tofu version | head -1)), skipping."
else
    echo "==> Installing OpenTofu..."
    TOFU_INSTALLER="$(mktemp)"
    curl --proto '=https' --tlsv1.2 -fsSL https://get.opentofu.org/install-opentofu.sh -o "$TOFU_INSTALLER"
    chmod +x "$TOFU_INSTALLER"
    sudo "$TOFU_INSTALLER" --install-method deb
    rm -f "$TOFU_INSTALLER"
fi

# ---------------------------------------------------------------------------
# 3. CloudCore OpenTofu provider (built from source, not published to any
#    registry — dev_overrides is the only way OpenTofu can find it)
# ---------------------------------------------------------------------------
echo "==> Building the CloudCore provider..."
bash "$REPO_DIR/scripts/build-provider.sh"

TOFURC="$HOME/.tofurc"
DEV_OVERRIDE_LINE="\"registry.opentofu.org/cloudcore/cloudcore\" = \"$HOME/.local/share/opentofu/plugins/registry.opentofu.org/cloudcore/cloudcore/0.1.0/linux_amd64\""
if [[ -f "$TOFURC" ]] && grep -qF "registry.opentofu.org/cloudcore/cloudcore" "$TOFURC"; then
    echo "==> $TOFURC already has a cloudcore/cloudcore dev_override, leaving it alone."
elif [[ -f "$TOFURC" ]]; then
    echo "    WARNING: $TOFURC already exists and doesn't have a cloudcore/cloudcore"
    echo "    dev_override — not touching it automatically to avoid corrupting whatever"
    echo "    else is in there. Add this to its provider_installation block by hand:"
    echo "      dev_overrides {"
    echo "        $DEV_OVERRIDE_LINE"
    echo "      }"
else
    echo "==> Writing $TOFURC (provider dev_overrides)..."
    cat > "$TOFURC" <<EOF
provider_installation {
  dev_overrides {
    $DEV_OVERRIDE_LINE
  }
  direct {}
}
EOF
fi

# ---------------------------------------------------------------------------
# 4. Python dependencies
# ---------------------------------------------------------------------------
echo "==> Installing Python dependencies..."
pip3 install --user -r "$REPO_DIR/requirements.txt"

# ---------------------------------------------------------------------------
# 5. Ansible collection
# ---------------------------------------------------------------------------
echo "==> Installing Ansible collection..."
(
    cd "$REPO_DIR/ansible/collections/cloudcore"
    ansible-galaxy collection build --force -q
    ansible-galaxy collection install cloudcore-cloudcore-*.tar.gz --force -q
    rm -f cloudcore-cloudcore-*.tar.gz
)

# ---------------------------------------------------------------------------
# 6. Ubuntu cloud image
# ---------------------------------------------------------------------------
echo "==> Fetching Ubuntu 22.04 cloud image (skipped if already present)..."
bash "$REPO_DIR/api/fetch-image.sh"

# ---------------------------------------------------------------------------
# 7. SSH keypair
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
# 8. Bridge network (system-level service)
# ---------------------------------------------------------------------------
echo "==> Installing bridge service (cloudcore-bridge)..."
# cloudcore-bridge.service (unlike the two user services below) is a static
# committed file with a hardcoded example path baked in — substitute it the
# same way, rather than a plain copy, so this works from any clone location.
sed "s|/home/scottp/IdeaProjects/CloudProject|$REPO_DIR|g" \
    "$REPO_DIR/api/cloudcore-bridge.service" | sudo tee /etc/systemd/system/cloudcore-bridge.service > /dev/null
sudo systemctl daemon-reload
sudo systemctl enable --now cloudcore-bridge.service

# ---------------------------------------------------------------------------
# 9. Host-level package repo (system-level service, always-available)
# ---------------------------------------------------------------------------
# Only installs and starts the *serving* side (cloudcore-repo.service) —
# generates its own unit file with the correct path baked in at install
# time, no substitution needed here. Deliberately does NOT populate it
# (api/build-package-repo.sh) — that needs a throwaway builder VM and
# real bandwidth (several GB), and has to be re-run by every user who
# clones this repo; not something a one-time install script should do
# on someone's behalf.
echo "==> Installing package-repo service (cloudcore-repo)..."
sudo bash "$REPO_DIR/api/setup-package-repo.sh"

# ---------------------------------------------------------------------------
# 10. Systemd user services (API + terminal)
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
# 11. Verify
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
echo ""
echo "    Package repo (cloudcore-repo.service) is running but empty —"
echo "    populate it before building anything that needs packages:"
echo "      CLOUDCORE_API_URL=http://127.0.0.1:8080 CLOUDCORE_API_TOKEN=dev-token \\"
echo "        bash api/build-package-repo.sh jammy"
echo "    Takes 15-20+ minutes and several GB of real downloads — one-time"
echo "    per clone, not run automatically by this script."
