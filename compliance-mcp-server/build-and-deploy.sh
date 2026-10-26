#!/usr/bin/env bash
#
# Build and deploy the Compliance MCP server directly with `oc` (no
# GitOps). Useful for local iteration; the GitOps path
# (gitops/bootstrap.sh) is the recommended way to deploy the full demo.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
NAMESPACE="${NAMESPACE:-doc-gen-demo}"
IMAGE_NAME="compliance-mcp-server"

echo "==> Ensuring namespace ${NAMESPACE} exists..."
oc get namespace "${NAMESPACE}" >/dev/null 2>&1 || oc create namespace "${NAMESPACE}"
oc label namespace "${NAMESPACE}" opendatahub.io/dashboard=true modelmesh-enabled=false --overwrite >/dev/null

echo "==> Building image with OpenShift BuildConfig (binary build)..."
oc project "${NAMESPACE}" >/dev/null
if ! oc get imagestream "${IMAGE_NAME}" -n "${NAMESPACE}" >/dev/null 2>&1; then
  oc new-build --name="${IMAGE_NAME}" --binary --strategy=docker -n "${NAMESPACE}"
fi
oc start-build "${IMAGE_NAME}" --from-dir="${SCRIPT_DIR}" --follow -n "${NAMESPACE}"

echo "==> Enabling local image lookup on ImageStream ${IMAGE_NAME}..."
oc patch imagestream "${IMAGE_NAME}" -n "${NAMESPACE}" --type=merge \
  -p '{"spec":{"lookupPolicy":{"local":true}}}'

echo "==> Applying Kubernetes manifests (MCPServer CR — requires the MCPServer CRD;"
echo "    run ./install-openshift-ai.sh first if this errors with a 'no matches"
echo "    for kind MCPServer' message)..."
oc apply -f "${SCRIPT_DIR}/k8s/mcpserver.yaml" -n "${NAMESPACE}"
oc apply -f "${SCRIPT_DIR}/k8s/route.yaml" -n "${NAMESPACE}"

echo "==> Restarting to pick up the new image (no automatic ImageStream trigger"
echo "    is configured, so re-applying the MCPServer CR alone won't roll pods"
echo "    if this is a rebuild rather than first deploy)..."
oc rollout restart deployment/compliance-mcp-server -n "${NAMESPACE}" 2>/dev/null || true

echo "==> Waiting for rollout..."
oc rollout status deployment/compliance-mcp-server -n "${NAMESPACE}" --timeout=180s

ROUTE_HOST=$(oc get route compliance-mcp-server -n "${NAMESPACE}" -o jsonpath='https://{.spec.host}' 2>/dev/null || true)
echo "==> Done. compliance-mcp-server is running in namespace ${NAMESPACE}."
if [[ -n "${ROUTE_HOST}" ]]; then
  echo "    Compliance MCP endpoint: ${ROUTE_HOST}/mcp"
fi
