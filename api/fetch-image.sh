#!/usr/bin/env bash
set -euo pipefail

IMAGES_DIR="$(cd "$(dirname "$0")" && pwd)/images"
IMAGE_NAME="ubuntu-22.04"
IMAGE_FILE="${IMAGES_DIR}/${IMAGE_NAME}.qcow2"
IMAGE_URL="https://cloud-images.ubuntu.com/jammy/current/jammy-server-cloudimg-amd64.img"

mkdir -p "${IMAGES_DIR}"

if [[ -f "${IMAGE_FILE}" ]]; then
    echo "Image already exists: ${IMAGE_FILE}"
    qemu-img info "${IMAGE_FILE}" | grep -E "virtual size|disk size"
    exit 0
fi

echo "Downloading Ubuntu 22.04 cloud image..."
TMPFILE="${IMAGES_DIR}/${IMAGE_NAME}.tmp"
curl -L --progress-bar -o "${TMPFILE}" "${IMAGE_URL}"

echo "Converting to qcow2..."
qemu-img convert -f qcow2 -O qcow2 "${TMPFILE}" "${IMAGE_FILE}"
rm -f "${TMPFILE}"

echo "Done: ${IMAGE_FILE}"
qemu-img info "${IMAGE_FILE}" | grep -E "virtual size|disk size"
