#!/usr/bin/env bash
#
# Quick-start, non-GitOps deploy of all demo components directly with `oc`.
#
# Components deployed (in order):
#   github-mcp-server     — MCP server for GitHub repo browsing
#   doc-gen-agent         — architecture documentation agent (Gradio UI + OGX)
#   openapi-gen-agent     — OpenAPI specification agent (Gradio UI + OGX)
#   catalog-gen-agent     — catalog component generation agent (Gradio UI + OGX)
#   compliance-scan-agent — architectural compliance scan agent (Gradio UI + OGX, report-only)
#   governance-hub        — compliance dashboard + REST API (observes/triggers the three agents)
#   compliance-mcp-server — standalone MCP server proxying governance-hub's REST API
#   ci-cd-pipeline        — Tekton CI/CD pipeline
#   keycloak              — Red Hat Build of Keycloak (RHBK) identity provider
#   developer-hub         — Red Hat Developer Hub (RHDH 1.10) internal dev portal
#
# Inference is served by an external, configurable OpenAI-compatible
# endpoint (no in-cluster model serving) — set it via
# doc-gen-agent/k8s/cluster-config.yaml (shared by all four agent
# components, and, if your endpoint requires auth, $EXTERNAL_MODEL_API_KEY
# — see doc-gen-agent/build-and-deploy.sh and the other agents' equivalents).
# github-mcp-server and compliance-mcp-server are each an MCPServer custom
# resource — if their `oc apply` errors with "no matches for kind
# MCPServer", run ./install-openshift-ai.sh first (see their own
# build-and-deploy.sh for details).
# For a single command that provisions the whole stack via GitOps, use
# gitops/bootstrap.sh instead.
#
# Usage:
#   ./build-and-deploy-all.sh                  # deploy everything
#   ./build-and-deploy-all.sh github-mcp-server # deploy a single component
#   ./build-and-deploy-all.sh doc-gen-agent
#   ./build-and-deploy-all.sh openapi-gen-agent
#   ./build-and-deploy-all.sh catalog-gen-agent
#   ./build-and-deploy-all.sh compliance-scan-agent
#   ./build-and-deploy-all.sh governance-hub
#   ./build-and-deploy-all.sh compliance-mcp-server
#   ./build-and-deploy-all.sh keycloak
#   ./build-and-deploy-all.sh developer-hub
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# Only force a namespace onto every component if the user explicitly set
# NAMESPACE themselves. ci-cd-pipeline, keycloak, and developer-hub each
# default to their own namespace (ci-cd-demo, keycloak, rhdh) in their own
# build-and-deploy.sh and hardcode it in their manifests — blindly
# overriding all of them with doc-gen-demo breaks `oc apply` with a
# "namespace from the provided object does not match" error.
NAMESPACE_OVERRIDDEN=false
if [[ -n "${NAMESPACE:-}" ]]; then
  NAMESPACE_OVERRIDDEN=true
fi
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

COMPONENTS=("github-mcp-server" "doc-gen-agent" "openapi-gen-agent" "catalog-gen-agent" "compliance-scan-agent" "governance-hub" "compliance-mcp-server" "ci-cd-pipeline" "keycloak" "developer-hub")
if [[ $# -gt 0 ]]; then
  COMPONENTS=("$@")
fi

for component in "${COMPONENTS[@]}"; do
  echo ""
  echo "==================================================================="
  echo "  Deploying: ${component}"
  echo "==================================================================="
  if [[ "${NAMESPACE_OVERRIDDEN}" == true ]]; then
    NAMESPACE="${NAMESPACE}" "${SCRIPT_DIR}/${component}/build-and-deploy.sh"
  else
    "${SCRIPT_DIR}/${component}/build-and-deploy.sh"
  fi
done

echo ""
echo "==> All requested components deployed."
DOC_ROUTE_HOST=$(oc get route doc-gen-agent -n "${NAMESPACE}" -o jsonpath='https://{.spec.host}' 2>/dev/null || true)
if [[ -n "${DOC_ROUTE_HOST}" ]]; then
  echo "    Doc-gen agent UI:      ${DOC_ROUTE_HOST}"
fi
OPENAPI_ROUTE_HOST=$(oc get route openapi-gen-agent -n "${NAMESPACE}" -o jsonpath='https://{.spec.host}' 2>/dev/null || true)
if [[ -n "${OPENAPI_ROUTE_HOST}" ]]; then
  echo "    OpenAPI-gen agent UI:  ${OPENAPI_ROUTE_HOST}"
fi
CATALOG_ROUTE_HOST=$(oc get route catalog-gen-agent -n "${NAMESPACE}" -o jsonpath='https://{.spec.host}' 2>/dev/null || true)
if [[ -n "${CATALOG_ROUTE_HOST}" ]]; then
  echo "    Catalog-gen agent UI:  ${CATALOG_ROUTE_HOST}"
fi
COMPLIANCE_SCAN_ROUTE_HOST=$(oc get route compliance-scan-agent -n "${NAMESPACE}" -o jsonpath='https://{.spec.host}' 2>/dev/null || true)
if [[ -n "${COMPLIANCE_SCAN_ROUTE_HOST}" ]]; then
  echo "    Compliance-scan agent UI: ${COMPLIANCE_SCAN_ROUTE_HOST}"
fi
GOVERNANCE_HUB_ROUTE_HOST=$(oc get route governance-hub -n "${NAMESPACE}" -o jsonpath='https://{.spec.host}' 2>/dev/null || true)
if [[ -n "${GOVERNANCE_HUB_ROUTE_HOST}" ]]; then
  echo "    Governance Hub dashboard: ${GOVERNANCE_HUB_ROUTE_HOST}"
fi
COMPLIANCE_MCP_ROUTE_HOST=$(oc get route compliance-mcp-server -n "${NAMESPACE}" -o jsonpath='https://{.spec.host}' 2>/dev/null || true)
if [[ -n "${COMPLIANCE_MCP_ROUTE_HOST}" ]]; then
  echo "    Compliance MCP endpoint: ${COMPLIANCE_MCP_ROUTE_HOST}/mcp"
fi
KC_ROUTE_HOST=$(oc get route -n keycloak --no-headers 2>/dev/null | grep keycloak | awk '{print $2}' | head -1 || true)
if [[ -n "${KC_ROUTE_HOST}" ]]; then
  echo "    Keycloak:              https://${KC_ROUTE_HOST}"
  echo "      Realm:               https://${KC_ROUTE_HOST}/realms/platform"
fi
RHDH_ROUTE_HOST=$(oc get route -n rhdh --no-headers 2>/dev/null | grep backstage | awk '{print $2}' | head -1 || true)
if [[ -n "${RHDH_ROUTE_HOST}" ]]; then
  echo "    Developer Hub:         https://${RHDH_ROUTE_HOST}"
  echo "      Users: admin / password   developer / password"
fi
