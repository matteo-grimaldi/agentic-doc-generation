#!/usr/bin/env bash
#
# Pre-flight checks before applying the GitOps App-of-Apps. Warns on optional
# gaps; exits non-zero on missing CRDs, generated ConfigMaps or prepared images.
#
set -euo pipefail

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
  echo "    namespace ${NAMESPACE} is missing — bash gitops/prepare.sh creates it."
fi

if ! oc get secret external-model-credentials -n "${NAMESPACE}" >/dev/null 2>&1; then
  warn "secret/external-model-credentials missing in ${NAMESPACE} — run ./install-openshift-ai.sh with UPSTREAM_MODEL_API_KEY (mints a MaaS key) or set EXTERNAL_MODEL_API_KEY / create the Secret (see README Model configuration)."
fi

if ! oc get gateway maas-default-gateway -n openshift-ingress >/dev/null 2>&1; then
  warn "Gateway/maas-default-gateway missing in openshift-ingress — run ./install-openshift-ai.sh so agents can reach the AI Gateway."
fi

if ! oc get externalmodel qwen38-27b -n llm >/dev/null 2>&1; then
  warn "ExternalModel/qwen38-27b missing in llm — run ./install-openshift-ai.sh with UPSTREAM_MODEL_API_KEY to register the upstream model behind the gateway."
fi

require_prepared_resource() {
  local kind="$1" name="$2"
  if ! oc get "${kind}" "${name}" -n "${NAMESPACE}" >/dev/null 2>&1; then
    if [[ "${WARN_ONLY}" == 1 ]]; then
      warn "${kind}/${name} missing in ${NAMESPACE} — run bash gitops/prepare.sh before bootstrap."
    else
      fail "${kind}/${name} missing in ${NAMESPACE} — run bash gitops/prepare.sh before bootstrap."
    fi
  fi
}
for cm in cluster-config ogx-stack-config agent-knowledge openapi-agent-knowledge catalog-gen-knowledge compliance-scan-knowledge; do
  require_prepared_resource configmap "${cm}"
done
for component in github-mcp-server doc-gen-agent openapi-gen-agent catalog-gen-agent \
    compliance-scan-agent governance-hub compliance-mcp-server; do
  require_prepared_resource imagestreamtag "${component}:latest"
done

if ! oc get rolebinding openshift-gitops-argocd-application-controller-admin -n "${NAMESPACE}" >/dev/null 2>&1; then
  echo "    Argo CD namespace RoleBinding not yet applied (bootstrap.sh applies gitops/argocd-namespace-rolebindings.yaml next)."
fi

echo "==> Preflight checks passed."
