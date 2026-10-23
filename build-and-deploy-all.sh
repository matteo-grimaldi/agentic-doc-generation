#!/usr/bin/env bash
#
# Quick-start, non-GitOps deploy of the application components
# (github-mcp-server, doc-gen-agent, openapi-gen-agent,
# compliance-scan-agent, governance-hub, compliance-mcp-server) directly
# with `oc`.
#
# Inference is served by an external, configurable OpenAI-compatible
# endpoint (no in-cluster model serving) — set it via
# doc-gen-agent/k8s/cluster-config.yaml (shared by both agent components,
# and, if your endpoint requires auth, $EXTERNAL_MODEL_API_KEY — see
# doc-gen-agent/build-and-deploy.sh and openapi-gen-agent/build-and-deploy.sh).
# For a single command that provisions the whole stack via GitOps, use
# gitops/bootstrap.sh instead.
#
# Usage:
#   ./build-and-deploy-all.sh                  # deploy everything
#   ./build-and-deploy-all.sh github-mcp-server # deploy a single component
#   ./build-and-deploy-all.sh doc-gen-agent
#   ./build-and-deploy-all.sh openapi-gen-agent
#   ./build-and-deploy-all.sh compliance-scan-agent
#   ./build-and-deploy-all.sh governance-hub
#   ./build-and-deploy-all.sh compliance-mcp-server
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
NAMESPACE="${NAMESPACE:-doc-gen-demo}"

echo "==> Checking cluster login..."
oc whoami >/dev/null || { echo "error: not logged into an OpenShift cluster (run 'oc login' first)"; exit 1; }

MODEL_BASE_URL_SET=$(oc get configmap cluster-config -n "${NAMESPACE}" -o jsonpath='{.data.model_base_url}' 2>/dev/null || true)
if [[ -z "${MODEL_BASE_URL_SET}" || "${MODEL_BASE_URL_SET}" == REPLACE_WITH_* ]]; then
  echo ""
  echo "    NOTE: doc-gen-agent/k8s/cluster-config.yaml's model_base_url still looks"
  echo "    unset or is the placeholder value. Edit it to point at your external"
  echo "    OpenAI-compatible endpoint before (or right after) deploying doc-gen-agent,"
  echo "    and set \$EXTERNAL_MODEL_API_KEY if that endpoint requires auth."
  echo ""
fi

COMPONENTS=("github-mcp-server" "doc-gen-agent" "openapi-gen-agent" "compliance-scan-agent" "governance-hub" "compliance-mcp-server")
if [[ $# -gt 0 ]]; then
  COMPONENTS=("$@")
fi

for component in "${COMPONENTS[@]}"; do
  echo ""
  echo "==================================================================="
  echo "  Deploying: ${component}"
  echo "==================================================================="
  NAMESPACE="${NAMESPACE}" "${SCRIPT_DIR}/${component}/build-and-deploy.sh"
done

echo ""
echo "==> All requested components deployed."
DOC_ROUTE_HOST=$(oc get route doc-gen-agent -n "${NAMESPACE}" -o jsonpath='https://{.spec.host}' 2>/dev/null || true)
if [[ -n "${DOC_ROUTE_HOST}" ]]; then
  echo "    Doc-gen agent UI: ${DOC_ROUTE_HOST}"
fi
OPENAPI_ROUTE_HOST=$(oc get route openapi-gen-agent -n "${NAMESPACE}" -o jsonpath='https://{.spec.host}' 2>/dev/null || true)
if [[ -n "${OPENAPI_ROUTE_HOST}" ]]; then
  echo "    OpenAPI-gen agent UI: ${OPENAPI_ROUTE_HOST}"
fi
COMPLIANCE_SCAN_ROUTE_HOST=$(oc get route compliance-scan-agent -n "${NAMESPACE}" -o jsonpath='https://{.spec.host}' 2>/dev/null || true)
if [[ -n "${COMPLIANCE_SCAN_ROUTE_HOST}" ]]; then
  echo "    Compliance-scan agent UI: ${COMPLIANCE_SCAN_ROUTE_HOST}"
fi
GOVERNANCE_ROUTE_HOST=$(oc get route governance-hub -n "${NAMESPACE}" -o jsonpath='https://{.spec.host}' 2>/dev/null || true)
if [[ -n "${GOVERNANCE_ROUTE_HOST}" ]]; then
  echo "    Governance Hub dashboard: ${GOVERNANCE_ROUTE_HOST}"
fi
COMPLIANCE_MCP_ROUTE_HOST=$(oc get route compliance-mcp-server -n "${NAMESPACE}" -o jsonpath='https://{.spec.host}' 2>/dev/null || true)
if [[ -n "${COMPLIANCE_MCP_ROUTE_HOST}" ]]; then
  echo "    Compliance MCP endpoint: ${COMPLIANCE_MCP_ROUTE_HOST}/mcp"
fi
