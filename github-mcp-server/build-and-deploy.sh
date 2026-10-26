#!/usr/bin/env bash
#
# Build and deploy the GitHub MCP server directly with `oc` (no GitOps).
# Useful for local iteration; the GitOps path (gitops/bootstrap.sh) is the
# recommended way to deploy the full demo.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
NAMESPACE="${NAMESPACE:-doc-gen-demo}"
IMAGE_NAME="github-mcp-server"

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

if [[ -n "${GITHUB_TOKEN:-}" ]]; then
  echo "==> Creating/updating github-token secret from \$GITHUB_TOKEN..."
  oc create secret generic github-token --from-literal=token="${GITHUB_TOKEN}" \
    -n "${NAMESPACE}" --dry-run=client -o yaml | oc apply -f -
else
  echo "==> \$GITHUB_TOKEN not set — skipping secret creation (search_code tool will be unavailable, GitHub API rate limits will be lower)."
fi

echo "==> Applying Kubernetes manifests (MCPServer CR — requires the MCPServer CRD;"
echo "    run ./install-openshift-ai.sh first if this errors with a 'no matches"
echo "    for kind MCPServer' message)..."
oc apply -f "${SCRIPT_DIR}/k8s/mcpserver.yaml" -n "${NAMESPACE}"

echo "==> Restarting to pick up the new image (no automatic ImageStream trigger"
echo "    is configured, so re-applying the MCPServer CR alone won't roll pods"
echo "    if this is a rebuild rather than first deploy)..."
oc rollout restart deployment/github-mcp-server -n "${NAMESPACE}" 2>/dev/null || true

echo "==> Waiting for rollout..."
oc rollout status deployment/github-mcp-server -n "${NAMESPACE}" --timeout=180s

echo "==> Done. github-mcp-server is running in namespace ${NAMESPACE}."
echo "    To register it (and every other MCP server in this repo) in OpenShift AI's"
echo "    MCP catalog, run from the repo root: ./register-mcp-catalog.sh"
