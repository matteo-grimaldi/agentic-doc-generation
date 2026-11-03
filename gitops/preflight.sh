#!/usr/bin/env bash
#
# Pre-flight checks before applying the GitOps App-of-Apps. Warns on optional
# gaps; exits non-zero when required CRDs are missing (see README prerequisites).
#
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
NAMESPACE="${NAMESPACE:-doc-gen-demo}"
WARN_ONLY="${GITOPS_PREFLIGHT_WARN_ONLY:-0}"

fail() {
  echo "error: $*" >&2
  exit 1
}

warn() {
  echo "warning: $*" >&2
}

require_crd() {
  local crd="$1"
  local hint="$2"
  if ! oc get crd "${crd}" >/dev/null 2>&1; then
    if [[ "${WARN_ONLY}" == "1" ]]; then
      warn "${hint}"
    else
      fail "${hint}"
    fi
  fi
}

echo "==> GitOps preflight: checking cluster prerequisites..."

oc whoami >/dev/null || fail "not logged into an OpenShift cluster (run 'oc login' first)"

require_crd "mcpservers.mcp.x-k8s.io" \
  "MCPServer CRD not found — run ./install-openshift-ai.sh before GitOps bootstrap."

require_crd "authpolicies.kuadrant.io" \
  "AuthPolicy CRD not found — run ./install-openshift-ai.sh (or ./install-mcp-gateway.sh) before GitOps bootstrap (Red Hat Connectivity Link)."

require_crd "mcpgatewayextensions.mcp.kuadrant.io" \
  "MCPGatewayExtension CRD not found — run ./install-openshift-ai.sh (or ./install-mcp-gateway.sh) before GitOps bootstrap (MCP Gateway Operator)."

require_crd "pipelines.tekton.dev" \
  "Tekton Pipeline CRD not found — run ./ci-cd-pipeline/install-operator.sh before GitOps bootstrap (or set GITOPS_PREFLIGHT_WARN_ONLY=1 to continue)."

if ! oc get namespace "${NAMESPACE}" >/dev/null 2>&1; then
  echo "    namespace ${NAMESPACE} will be created by ArgoCD (CreateNamespace sync option)."
fi

if ! oc get secret external-model-credentials -n "${NAMESPACE}" >/dev/null 2>&1; then
  warn "secret/external-model-credentials missing in ${NAMESPACE} — set EXTERNAL_MODEL_API_KEY before bootstrap or create the Secret (see README Model configuration)."
fi

for cm in ogx-stack-config agent-knowledge; do
  if ! oc get configmap "${cm}" -n "${NAMESPACE}" >/dev/null 2>&1; then
    warn "configmap/${cm} missing in ${NAMESPACE} — run component build-and-deploy.sh or build-and-deploy-all.sh so agents have OGX stack config."
    break
  fi
done

if ! oc get rolebinding openshift-gitops-argocd-application-controller-admin -n "${NAMESPACE}" >/dev/null 2>&1; then
  echo "    Argo CD namespace RoleBinding not yet applied (bootstrap.sh applies gitops/argocd-namespace-rolebindings.yaml next)."
fi

echo "==> Preflight checks passed."
