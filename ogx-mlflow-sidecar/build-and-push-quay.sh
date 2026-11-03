#!/usr/bin/env bash
#
# Build ogx-mlflow-sidecar on your workstation and push to Quay.
# Deploy scripts do not run this automatically — use it (or a Quay build trigger)
# to publish the image, then deploy with OGX_MLFLOW_SIDECAR_MODE=quay.
#
# Requires: podman or docker, `podman login quay.io`, pull of ogxai/distribution-starter
#
# Usage:
#   ./ogx-mlflow-sidecar/build-and-push-quay.sh
#   OGX_MLFLOW_SIDECAR_IMAGE=quay.io/mgrimald/ogx-mlflow-sidecar:v1 ./ogx-mlflow-sidecar/build-and-push-quay.sh
#
# Repository: https://quay.io/repository/mgrimald/ogx-mlflow-sidecar
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TAG="${TAG:-latest}"
QUAY_IMAGE="${OGX_MLFLOW_SIDECAR_IMAGE:-quay.io/mgrimald/ogx-mlflow-sidecar:${TAG}}"
CONTAINER_PLATFORM="${CONTAINER_PLATFORM:-linux/amd64}"

if command -v podman >/dev/null 2>&1; then
  ENGINE=podman
elif command -v docker >/dev/null 2>&1; then
  ENGINE=docker
else
  echo "error: install podman or docker to build locally."
  exit 1
fi

echo "==> Building ${QUAY_IMAGE} (platform=${CONTAINER_PLATFORM})..."
if [[ "${ENGINE}" == podman ]]; then
  podman build --platform "${CONTAINER_PLATFORM}" -t "${QUAY_IMAGE}" "${SCRIPT_DIR}"
  podman push "${QUAY_IMAGE}"
else
  docker build --platform "${CONTAINER_PLATFORM}" -t "${QUAY_IMAGE}" "${SCRIPT_DIR}"
  docker push "${QUAY_IMAGE}"
fi

echo "==> Pushed ${QUAY_IMAGE}"
echo "    Deploy: OGX_MLFLOW_SIDECAR_MODE=quay ./doc-gen-agent/build-and-deploy.sh"
