#!/usr/bin/env bash
# Non-GitOps deployment of committed gateway wiring, including the compatibility
# Job (ArgoCD hook annotations have no execution semantics under oc apply).
# Prerequisites: install-openshift-ai.sh, gateway operators, backend components.
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
if [[ "${NAMESPACE:-doc-gen-demo}" != doc-gen-demo || "${MCP_SYSTEM_NS:-mcp-gateway}" != mcp-gateway ]]; then
  echo "error: gateway manifests require doc-gen-demo and mcp-gateway namespaces" >&2
  exit 1
fi
oc whoami >/dev/null
for crd in mcpgatewayextensions.mcp.kuadrant.io authpolicies.kuadrant.io; do
  oc get crd "${crd}" >/dev/null || {
    echo "error: run install-openshift-ai.sh before deploying MCP gateway wiring" >&2
    exit 1
  }
done

# Generate cluster-config before the installer patches the gateway URL.
bash "${REPO_ROOT}/scripts/configure-agents.sh"
oc apply -f "${SCRIPT_DIR}/k8s/namespace.yaml"
# Recreate only this managed compatibility Job so updated virtual servers are
# synchronized even when a previous invocation has already completed.
oc delete job mcp-virtualserver-config-sync -n mcp-gateway --ignore-not-found --wait=true
oc apply -f "${SCRIPT_DIR}/k8s/"
oc wait --for=condition=Programmed gateway/mcp-gateway -n mcp-gateway --timeout=300s
if ! oc wait --for=condition=Complete job/mcp-virtualserver-config-sync -n mcp-gateway --timeout=600s; then
  oc logs job/mcp-virtualserver-config-sync -n mcp-gateway || true
  exit 1
fi
oc rollout status deployment/mcp-gateway -n mcp-gateway --timeout=300s

# Performs Authorino compatibility setup, discovers the programmed address,
# patches mcp_gateway_url, and restarts any already-existing agent Deployments.
bash "${REPO_ROOT}/install-mcp-gateway.sh"
url="$(oc get configmap cluster-config -n doc-gen-demo -o jsonpath='{.data.mcp_gateway_url}')"
[[ -n "${url}" ]] || { echo "error: MCP gateway URL is still empty" >&2; exit 1; }
echo "==> MCP gateway ready at ${url}"
