#!/usr/bin/env bash
#
# Build the MLflow-instrumented OGX sidecar image into the demo namespace.
# Agent Deployments reference this ImageStream as their ogx-server container.
# Does not apply a Deployment of its own — only the image build.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
NAMESPACE="${NAMESPACE:-doc-gen-demo}"
IMAGE_NAME="ogx-mlflow-sidecar"

# Large base image + pip install; default 10Gi limit was too small for docker build.
export BUILD_EPHEMERAL_STORAGE_REQUEST="${BUILD_EPHEMERAL_STORAGE_REQUEST:-8Gi}"
export BUILD_EPHEMERAL_STORAGE_LIMIT="${BUILD_EPHEMERAL_STORAGE_LIMIT:-30Gi}"

# shellcheck source=../scripts/lib-openshift-binary-build.sh
source "${REPO_ROOT}/scripts/lib-openshift-binary-build.sh"

echo "==> Ensuring namespace ${NAMESPACE} exists..."
oc get namespace "${NAMESPACE}" >/dev/null 2>&1 || oc create namespace "${NAMESPACE}"

echo "==> Building image with OpenShift BuildConfig (binary build)..."
oc project "${NAMESPACE}" >/dev/null
run_openshift_binary_build "${IMAGE_NAME}" "${SCRIPT_DIR}" "${NAMESPACE}"

# oc new-build leaves lookupPolicy.local=false, so kubelet always pulls from the
# external registry hostname; flip to local so same-namespace image pulls work
# via the internal registry short name used in agent Deployments.
oc patch imagestream "${IMAGE_NAME}" -n "${NAMESPACE}" \
  --type=merge -p '{"spec":{"lookupPolicy":{"local":true}}}' >/dev/null

echo "==> ${IMAGE_NAME} image ready in namespace ${NAMESPACE}."
echo "    Agents should set ogx-server image to:"
echo "      image-registry.openshift-image-registry.svc:5000/${NAMESPACE}/${IMAGE_NAME}:latest"
