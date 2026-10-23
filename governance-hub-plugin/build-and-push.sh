#!/usr/bin/env bash
#
# Build the governance-hub dynamic plugin OCI image and push it to quay.io.
#
# RHDH 1.10 requires the OCI image to carry the `io.backstage.dynamic-packages`
# OCI manifest annotation so the install-dynamic-plugins init container can
# discover the plugin inside the image.
#
# This script uses `janus-cli package export-dynamic-plugin` (via the Dockerfile)
# to generate the dist-dynamic/ scalprum bundle, then injects the annotation via
# `podman build --annotation` (or docker buildx --annotation).
#
# Prerequisites:
#   - Podman or Docker (with buildx) installed
#   - Logged in to quay.io: `podman login quay.io` or `docker login quay.io`
#   - quay.io/mcombi/rhdh-gov-hub repository exists and you have push access
#
# Usage:
#   bash governance-hub-plugin/build-and-push.sh            # push :latest
#   bash governance-hub-plugin/build-and-push.sh v0.1.0     # push :v0.1.0
#
# After pushing, enable the plugin in RHDH:
#   1. Set disabled: false in developer-hub/k8s/instance/dynamic-plugins-configmap.yaml
#   2. oc apply -f developer-hub/k8s/instance/dynamic-plugins-configmap.yaml
#   3. oc rollout restart deployment/backstage-developer-hub -n rhdh
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
IMAGE="quay.io/mcombi/rhdh-gov-hub"
TAG="${1:-latest}"

# OCI manifest annotation required by RHDH 1.10's install-dynamic-plugins.
# The init container BASE64-DECODES this value then parses it as JSON.
# Format: array of objects where each KEY is the plugin path.
# e.g. [{"@mcombi/backstage-plugin-governance-hub": {}}]
# This is base64-encoded here because passing plain JSON via --annotation
# results in "Incorrect padding" when RHDH tries to base64-decode it.
ANNOTATION_JSON='[{"backstage-plugin-governance-hub": {}}]'
ANNOTATION_B64=$(printf '%s' "${ANNOTATION_JSON}" | base64 | tr -d '\n')
ANNOTATION="io.backstage.dynamic-packages=${ANNOTATION_B64}"

echo "==> Building ${IMAGE}:${TAG} from ${SCRIPT_DIR}..."

if command -v podman >/dev/null 2>&1; then
  podman build \
    --platform linux/amd64 \
    --annotation "${ANNOTATION}" \
    --tag "${IMAGE}:${TAG}" \
    "${SCRIPT_DIR}"
  echo "==> Pushing ${IMAGE}:${TAG}..."
  podman push "${IMAGE}:${TAG}"
elif docker buildx version >/dev/null 2>&1; then
  docker buildx build \
    --platform linux/amd64 \
    --annotation "${ANNOTATION}" \
    --tag "${IMAGE}:${TAG}" \
    --push \
    "${SCRIPT_DIR}"
else
  docker build \
    --tag "${IMAGE}:${TAG}" \
    "${SCRIPT_DIR}"
  docker push "${IMAGE}:${TAG}"
  echo "WARNING: docker build does not support --annotation; the OCI annotation"
  echo "  may be missing. Consider using podman or docker buildx instead."
fi

echo ""
echo "==> Pushed ${IMAGE}:${TAG}"
echo "    OCI annotation 'io.backstage.dynamic-packages' injected via --annotation."
echo ""
echo "    To verify: skopeo inspect --raw docker://${IMAGE}:${TAG} | python3 -c \\"
echo "      \"import sys,json; d=json.load(sys.stdin); print(d.get('annotations',{}).get('io.backstage.dynamic-packages','NOT FOUND'))\""
echo ""
echo "    To pin to a digest:"
echo "      skopeo inspect docker://${IMAGE}:${TAG} | python3 -c \\"
echo "        \"import sys,json; d=json.load(sys.stdin); print(d['Digest'])\""
echo "    Then in dynamic-plugins-configmap.yaml:"
echo "      package: oci://${IMAGE}@sha256:<digest>"
