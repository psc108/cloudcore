#!/usr/bin/env bash
# Build the CloudCore Terraform provider and install it into the dev_overrides directory.
# Both the versioned and unversioned binary names are written so OpenTofu picks the
# correct binary regardless of which naming convention it prefers.
set -euo pipefail

PROVIDER_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../provider" && pwd)"
PLUGIN_DIR="${HOME}/.local/share/opentofu/plugins/registry.opentofu.org/cloudcore/cloudcore/0.1.0/linux_amd64"

mkdir -p "${PLUGIN_DIR}"

echo "Building provider..."
(cd "${PROVIDER_DIR}" && go build -o "${PLUGIN_DIR}/terraform-provider-cloudcore_v0.1.0" .)

# Keep the unversioned name in sync — OpenTofu may use either.
cp "${PLUGIN_DIR}/terraform-provider-cloudcore_v0.1.0" \
   "${PLUGIN_DIR}/terraform-provider-cloudcore"

echo "Installed:"
ls -lh "${PLUGIN_DIR}/"
