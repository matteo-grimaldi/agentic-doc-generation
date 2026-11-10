#!/usr/bin/env bash
#
# Quick-start, non-GitOps deploy of all demo components directly with `oc`.
#
# Components deployed (in order):
#   github-mcp-server     — MCP server for GitHub repo browsing
#   mcp-gateway           — gateway wiring, compatibility Job, and agent MCP URL
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
# For GitOps, install the operators, run gitops/prepare.sh, then
# gitops/bootstrap.sh (see README for the URL-finalization step).
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
#
# MLflow OGX sidecar: default pull from Quay (see cluster-config ogx_mlflow_sidecar_image).
# Set OGX_MLFLOW_SIDECAR_MODE=cluster to build ogx-mlflow-sidecar on the cluster instead.
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

REPO_ROOT="${SCRIPT_DIR}"
# shellcheck source=scripts/lib-openshift-binary-build.sh
source "${REPO_ROOT}/scripts/lib-openshift-binary-build.sh"
# shellcheck source=scripts/lib-ogx-sidecar-deploy.sh
source "${REPO_ROOT}/scripts/lib-ogx-sidecar-deploy.sh"

AGENT_COMPONENTS=(doc-gen-agent openapi-gen-agent catalog-gen-agent compliance-scan-agent)
export OGX_MLFLOW_SIDECAR_MODE="${OGX_MLFLOW_SIDECAR_MODE:-quay}"
export OGX_MLFLOW_SIDECAR_IMAGE="${OGX_MLFLOW_SIDECAR_IMAGE:-quay.io/mgrimald/ogx-mlflow-sidecar:latest}"

echo "==> Checking cluster login..."
oc whoami >/dev/null || { echo "error: not logged into an OpenShift cluster (run 'oc login' first)"; exit 1; }

MODEL_BASE_URL_SET=$(oc get configmap cluster-config -n "${NAMESPACE}" -o jsonpath='{.data.model_base_url}' 2>/dev/null || true)
if [[ -z "${MODEL_BASE_URL_SET}" || "${MODEL_BASE_URL_SET}" == REPLACE_WITH_* ]]; then
  echo ""
  echo "    NOTE: doc-gen-agent/k8s/cluster-config.yaml's model_base_url still looks"
  echo "    unset or is the placeholder value. It should be the in-cluster AI Gateway"
  echo "    /v1 Service URL (see install-openshift-ai.sh). Ensure"
  echo "    secret/external-model-credentials holds a MaaS sk-oai- key"
  echo "    (minted by the install script, or set \$EXTERNAL_MODEL_API_KEY)."
  echo ""
fi
if ! oc get gateway maas-default-gateway -n openshift-ingress >/dev/null 2>&1; then
  echo ""
  echo "    NOTE: Gateway/maas-default-gateway is missing — run"
  echo "    UPSTREAM_MODEL_API_KEY=... ./install-openshift-ai.sh before agents"
  echo "    can reach the model through the AI Gateway."
  echo ""
fi

COMPONENTS=("github-mcp-server" "mcp-gateway" "doc-gen-agent" "openapi-gen-agent" "catalog-gen-agent" "compliance-scan-agent" "governance-hub" "compliance-mcp-server" "ci-cd-pipeline" "keycloak" "developer-hub")
if [[ $# -gt 0 ]]; then
  COMPONENTS=("$@")
fi
# Validate the entire selection before deploying anything (including credentials).
for component in "${COMPONENTS[@]}"; do
  case "${component}" in
    github-mcp-server|mcp-gateway|doc-gen-agent|openapi-gen-agent|catalog-gen-agent|compliance-scan-agent|governance-hub|compliance-mcp-server|ci-cd-pipeline|keycloak|developer-hub) ;;
    *) echo "error: unknown component ${component}" >&2; exit 1 ;;
  esac
done
if [[ " ${COMPONENTS[*]} " == *" ci-cd-pipeline "* ]]; then
  oc get crd pipelines.tekton.dev >/dev/null || {
    echo "error: run ./ci-cd-pipeline/install-operator.sh before deploying the full stack" >&2
    exit 1
  }
fi

deploying_agent=false
for component in "${COMPONENTS[@]}"; do
  for agent in "${AGENT_COMPONENTS[@]}"; do
    if [[ "${component}" == "${agent}" ]]; then
      deploying_agent=true
      break 2
    fi
  done
done

if [[ "${deploying_agent}" == true ]]; then
  # Build scripts preserve installer URLs; refuse to deploy fail-closed agents
  # when a single-component invocation skipped initial gateway provisioning.
  # The default component order provisions it before reaching the agents.
  echo "==> Agent deployment requires a programmed MCP gateway (deployed first in the default order)."
fi

if [[ "${deploying_agent}" == true && "${OGX_MLFLOW_SIDECAR_MODE}" == cluster ]]; then
  echo ""
  echo "==> MLflow OGX sidecar: in-cluster build (ogx-mlflow-sidecar ImageStream)"
  ensure_ogx_mlflow_sidecar_image_cluster "${NAMESPACE}" "${REPO_ROOT}"
fi

for component in "${COMPONENTS[@]}"; do
  echo ""
  echo "==================================================================="
  case "${component}" in
    doc-gen-agent|openapi-gen-agent|catalog-gen-agent|compliance-scan-agent)
      MCP_URL=$(oc get configmap cluster-config -n "${NAMESPACE}" -o jsonpath='{.data.mcp_gateway_url}')
      if [[ -z "${MCP_URL}" ]]; then
        echo "error: deploy mcp-gateway first: ./build-and-deploy-all.sh mcp-gateway" >&2
        exit 1
      fi
      oc wait --for=condition=Programmed gateway/mcp-gateway -n mcp-gateway --timeout=300s
      ;;
  esac
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
KC_ROUTE_HOST=$(oc get route -n rhdh-keycloak --no-headers 2>/dev/null | grep keycloak | awk '{print $2}' | head -1 || true)
if [[ -n "${KC_ROUTE_HOST}" ]]; then
  echo "    Keycloak:              https://${KC_ROUTE_HOST}"
  echo "      Realm:               https://${KC_ROUTE_HOST}/realms/platform"
fi
RHDH_ROUTE_HOST=$(oc get route -n rhdh --no-headers 2>/dev/null | grep backstage | awk '{print $2}' | head -1 || true)
if [[ -n "${RHDH_ROUTE_HOST}" ]]; then
  echo "    Developer Hub:         https://${RHDH_ROUTE_HOST}"
  echo "      Users: admin / password   developer / password"
fi
