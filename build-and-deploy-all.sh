#!/usr/bin/env bash
#
# Quick-start, non-GitOps deploy of the two application components
# (github-mcp-server, doc-gen-agent) directly with `oc`.
#
# This assumes the RHOAI operator and model-serving stack are already
# present on the cluster (either via `gitops/bootstrap.sh`, or applied
# manually from gitops/operators/rhoai and gitops/model-serving). For a
# single command that provisions EVERYTHING, including the RHOAI
# operator and model serving, use gitops/bootstrap.sh instead.
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

echo "==> Checking for model-serving InferenceService in ${NAMESPACE}..."
if ! oc get inferenceservice doc-gen-llm -n "${NAMESPACE}" >/dev/null 2>&1; then
  echo ""
  echo "    WARNING: InferenceService 'doc-gen-llm' not found in namespace '${NAMESPACE}'."
  echo "    The RHOAI operator and model-serving stack do not appear to be deployed yet."
  echo "    Deploy them first with one of:"
  echo "      ./gitops/bootstrap.sh                     (recommended: full GitOps deploy)"
  echo "      oc apply -k gitops/operators/rhoai && oc apply -k gitops/model-serving"
  echo ""
  read -r -p "    Continue deploying app components anyway? [y/N] " REPLY
  [[ "${REPLY}" =~ ^[Yy]$ ]] || exit 1
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
