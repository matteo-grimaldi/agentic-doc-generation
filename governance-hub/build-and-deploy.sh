#!/usr/bin/env bash
#
# Build and deploy the Governance Hub (compliance dashboard + REST API)
# directly with `oc` (no GitOps). The standalone Compliance MCP server is
# a separate component — see ../compliance-mcp-server/build-and-deploy.sh.
# Useful for local iteration; the GitOps path (gitops/bootstrap.sh) is the
# recommended way to deploy the full demo.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
NAMESPACE="${NAMESPACE:-doc-gen-demo}"
IMAGE_NAME="governance-hub"

echo "==> Ensuring namespace ${NAMESPACE} exists..."
oc get namespace "${NAMESPACE}" >/dev/null 2>&1 || oc create namespace "${NAMESPACE}"
oc label namespace "${NAMESPACE}" opendatahub.io/dashboard=true modelmesh-enabled=false --overwrite >/dev/null

echo "==> Applying RBAC and PersistentVolumeClaim..."
oc apply -f "${SCRIPT_DIR}/k8s/rbac.yaml" -n "${NAMESPACE}"
oc apply -f "${SCRIPT_DIR}/k8s/pvc.yaml" -n "${NAMESPACE}"

echo "==> Building image with OpenShift BuildConfig (binary build)..."
oc project "${NAMESPACE}" >/dev/null
if ! oc get imagestream "${IMAGE_NAME}" -n "${NAMESPACE}" >/dev/null 2>&1; then
  oc new-build --name="${IMAGE_NAME}" --binary --strategy=docker -n "${NAMESPACE}"
fi
oc start-build "${IMAGE_NAME}" --from-dir="${SCRIPT_DIR}" --follow -n "${NAMESPACE}"

# oc new-build leaves lookupPolicy.local=false, so kubelet always pulls from the
# internal registry (and needs a valid dockercfg). Local lookup uses the image
# already on the node from the build push and avoids registry auth failures.
echo "==> Enabling local image lookup on ImageStream ${IMAGE_NAME}..."
oc patch imagestream "${IMAGE_NAME}" -n "${NAMESPACE}" --type=merge \
  -p '{"spec":{"lookupPolicy":{"local":true}}}'

echo "==> Applying Deployment, Service, and Route..."
oc apply -f "${SCRIPT_DIR}/k8s/deployment.yaml" -n "${NAMESPACE}"
oc apply -f "${SCRIPT_DIR}/k8s/route.yaml" -n "${NAMESPACE}"

echo "==> Waiting for rollout..."
oc rollout status deployment/governance-hub -n "${NAMESPACE}" --timeout=300s

ROUTE_HOST=$(oc get route governance-hub -n "${NAMESPACE}" -o jsonpath='https://{.spec.host}' 2>/dev/null || true)
echo "==> Done. governance-hub is running in namespace ${NAMESPACE}."
if [[ -n "${ROUTE_HOST}" ]]; then
  echo "    Dashboard UI: ${ROUTE_HOST}"
  echo "    REST API: ${ROUTE_HOST}/api/repos"
fi
