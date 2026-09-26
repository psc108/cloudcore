#!/usr/bin/env bash
# Run once with sudo to let the running (unprivileged) API process read
# per-DIMM memory detail (size/speed/manufacturer/part number per
# physical stick) via `sudo -n dmidecode -t memory` — see
# api/hw_info.py's own module docstring. Everything else on the new
# Hardware tab (BIOS/board identity, CPU, disks, GPU, network) already
# works with zero privilege, read straight from world-readable sysfs
# and standard read-only commands; this script only ever unlocks that
# one additional, genuinely root-gated detail. Not run automatically
# by anything — the Hardware tab works fully without it, just without
# per-DIMM specifics, and says so plainly rather than failing.
#
# Usage: sudo bash setup-hwinfo.sh
set -euo pipefail

DMIDECODE_BIN=$(command -v dmidecode || true)
if [ -z "$DMIDECODE_BIN" ]; then
    echo "dmidecode not found — install it first: sudo apt-get install -y --no-install-recommends dmidecode" >&2
    exit 1
fi

# Same CLOUDCORE_BRIDGE_USER-first pattern as setup-network.sh's own
# grants — see that script's own comment for why SUDO_USER/USER alone
# aren't reliable when this is ever invoked non-interactively.
HWINFO_SUDOERS_USER=${CLOUDCORE_BRIDGE_USER:-${SUDO_USER:-$USER}}
HWINFO_SUDOERS_FILE=/etc/sudoers.d/cloudcore-hwinfo

if [ -z "$HWINFO_SUDOERS_USER" ]; then
    echo "WARNING: could not determine a user for the hwinfo sudoers grant" \
         "(CLOUDCORE_BRIDGE_USER/SUDO_USER/USER all empty) — skipping. The" \
         "Hardware tab will keep working without per-DIMM memory detail." >&2
    exit 1
fi

# Bare binary, no arguments in the rule -- dmidecode is called with
# varying -t <type> arguments (only "memory" today, but any future
# addition to hw_info.py needs no sudoers change), and a rule with no
# arguments at all is the one shape sudo genuinely treats as "any
# arguments allowed" (confirmed the hard way fixing F-148's own
# cloudcore-netrebuild grant, which specified one argument and so
# needed an explicit wildcard instead).
HWINFO_SUDOERS_LINE="${HWINFO_SUDOERS_USER} ALL=(root) NOPASSWD: ${DMIDECODE_BIN}"
if [ -f "$HWINFO_SUDOERS_FILE" ] && grep -qxF "$HWINFO_SUDOERS_LINE" "$HWINFO_SUDOERS_FILE" 2>/dev/null; then
    echo "hwinfo sudoers grant already present for $HWINFO_SUDOERS_USER, skipping."
    exit 0
fi

TMP_SUDOERS=$(mktemp)
echo "$HWINFO_SUDOERS_LINE" > "$TMP_SUDOERS"
chmod 440 "$TMP_SUDOERS"
if visudo -c -f "$TMP_SUDOERS" >/dev/null 2>&1; then
    mv "$TMP_SUDOERS" "$HWINFO_SUDOERS_FILE"
    echo "Granted $HWINFO_SUDOERS_USER passwordless sudo for dmidecode (Hardware tab per-DIMM memory detail)."
else
    echo "WARNING: generated sudoers rule failed validation, not installed: $HWINFO_SUDOERS_LINE" >&2
    rm -f "$TMP_SUDOERS"
    exit 1
fi
