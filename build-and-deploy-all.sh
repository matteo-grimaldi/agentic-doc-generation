#!/usr/bin/env bash
#
# Quick-start, non-GitOps deploy of the two application components
# (github-mcp-server, doc-gen-agent) directly with `oc`.
#
# Inference is served by an external, configurable OpenAI-compatible
# endpoint (no in-cluster model serving) — set it via
# doc-gen-agent/k8s/cluster-config.yaml (and, if your endpoint requires
# auth, $EXTERNAL_MODEL_API_KEY — see doc-gen-agent/build-and-deploy.sh).
# For a single command that provisions the whole stack via GitOps, use
# gitops/bootstrap.sh instead.
#
# Usage:
#   ./build-and-deploy-all.sh                  # deploy everything
#   ./build-and-deploy-all.sh github-mcp-server # deploy a single component
#   ./build-and-deploy-all.sh doc-gen-agent
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

COMPONENTS=("github-mcp-server" "doc-gen-agent")
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
ROUTE_HOST=$(oc get route doc-gen-agent -n "${NAMESPACE}" -o jsonpath='https://{.spec.host}' 2>/dev/null || true)
if [[ -n "${ROUTE_HOST}" ]]; then
  echo "    Open the demo UI at: ${ROUTE_HOST}"
fi
