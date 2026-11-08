#!/usr/bin/env bash
#
# Bootstrap the Agentic Doc Generation demo on a freshly deployed
# OpenShift 4.22 cluster using GitOps.
#
# What this does:
#   1. Installs the OpenShift GitOps operator (if not already present)
#   2. Waits for the default ArgoCD instance to come up
#   3. Runs gitops/preflight.sh (CRD / optional Secret+ConfigMap checks)
#   4. Applies gitops/argocd-namespace-rolebindings.yaml so Argo CD can
#      reconcile workloads in doc-gen-demo, ci-cd-demo, and mcp-gateway
#   5. Merges gitops/argocd-instance-demo.yaml (UI RBAC for user admin +
#      rhdh-local-user API token for Developer Hub)
#   6. Optionally creates external-model-credentials / github-token Secrets
#      when EXTERNAL_MODEL_API_KEY / GITHUB_TOKEN are set in the environment
#   7. Points the root ArgoCD Application at THIS repo (rewriting the
#      committed repoURL if bootstrapping a different fork) and applies it
#   8. ArgoCD then takes over: child Applications under gitops/apps/, in
#      dependency order (sync-waves). Model inference is not provisioned by
#      this branch — see doc-gen-agent/k8s/cluster-config.yaml and README.
#
# Usage:
#   # First: install-openshift-ai.sh, ci-cd-pipeline/install-operator.sh,
#   # then bash gitops/prepare.sh (images and generated configuration).
#   ./gitops/bootstrap.sh [repo-url] [git-ref]
#
#   repo-url  Git URL of this repo as ArgoCD should clone it.
#             Defaults to the `origin` remote of the local checkout.
#   git-ref   Branch/tag/commit ArgoCD should track. Defaults to HEAD.
#
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
NAMESPACE="${NAMESPACE:-doc-gen-demo}"
# Committed manifests use this fork's URL. Also rewrite a legacy YOUR_ORG
# placeholder if any file still has one.
DEFAULT_REPO_URL="https://github.com/matteo-grimaldi/agentic-doc-generation.git"
PLACEHOLDER_URL="https://github.com/YOUR_ORG/agentic-doc-generation.git"

REPO_URL="${1:-$(git -C "${REPO_ROOT}" config --get remote.origin.url 2>/dev/null || true)}"
GIT_REF="${2:-HEAD}"

if [[ -z "${REPO_URL}" ]]; then
  echo "error: could not detect a git remote URL automatically." >&2
  echo "       usage: $0 <repo-url> [git-ref]" >&2
  exit 1
fi

echo "==> Using repo URL: ${REPO_URL} (ref: ${GIT_REF})"

echo "==> Checking cluster login..."
oc whoami >/dev/null || { echo "error: not logged into an OpenShift cluster (run 'oc login' first)"; exit 1; }

echo "==> Installing OpenShift GitOps operator (if needed)..."
if ! oc get csv -n openshift-operators 2>/dev/null | grep -q openshift-gitops-operator; then
  oc apply -f "${SCRIPT_DIR}/openshift-gitops-subscription.yaml"
  echo "==> Waiting for OpenShift GitOps operator to be installed..."
  until oc get csv -n openshift-operators 2>/dev/null | grep -q openshift-gitops-operator; do
    sleep 5
  done
else
  echo "    OpenShift GitOps operator already installed."
fi

echo "==> Waiting for ArgoCD instance (openshift-gitops) to be ready..."
until oc get deployment openshift-gitops-server -n openshift-gitops >/dev/null 2>&1; do
  sleep 5
done
oc rollout status deployment/openshift-gitops-server -n openshift-gitops --timeout=300s

echo "==> Running GitOps preflight checks..."
"${SCRIPT_DIR}/preflight.sh"

echo "==> Granting Argo CD application-controller access to demo namespaces..."
oc apply -f "${SCRIPT_DIR}/argocd-namespace-rolebindings.yaml"

echo "==> Patching Argo CD instance RBAC (demo UI + RHDH local user)..."
oc apply -f "${SCRIPT_DIR}/argocd-instance-demo.yaml"
echo "    Waiting for operator local-user Secret..."
for _ in $(seq 1 60); do
  if oc get secret rhdh-local-user-local-user -n openshift-gitops >/dev/null 2>&1; then
    "${SCRIPT_DIR}/sync-rhdh-argocd-secret.sh"
    if oc get secret rhdh-secrets -n rhdh >/dev/null 2>&1; then
      "${SCRIPT_DIR}/inject-rhdh-argocd-token.sh" || true
    fi
    break
  fi
  sleep 2
done
if ! oc get secret rhdh-local-user -n openshift-gitops >/dev/null 2>&1; then
  echo "    warning: rhdh-local-user Secret not ready — RHDH Argo CD plugin may stay empty until you run gitops/sync-rhdh-argocd-secret.sh"
fi

if [[ -n "${EXTERNAL_MODEL_API_KEY:-}" ]]; then
  echo "==> Creating/updating external-model-credentials secret from \$EXTERNAL_MODEL_API_KEY..."
  oc create secret generic external-model-credentials \
    --from-literal=api_key="${EXTERNAL_MODEL_API_KEY}" \
    -n "${NAMESPACE}" --dry-run=client -o yaml | oc apply -f -
else
  echo "    EXTERNAL_MODEL_API_KEY not set — skipping external-model-credentials (see README)."
fi

if [[ -n "${GITHUB_TOKEN:-}" ]]; then
  echo "==> Creating/updating github-token secret from \$GITHUB_TOKEN..."
  oc create secret generic github-token --from-literal=token="${GITHUB_TOKEN}" \
    -n "${NAMESPACE}" --dry-run=client -o yaml | oc apply -f -
else
  echo "    GITHUB_TOKEN not set — skipping github-token (search_code / PR steps need it)."
fi

rewrite_repo_urls() {
  # Rewrite committed default URL and any leftover YOUR_ORG placeholder.
  sed -e "s#${PLACEHOLDER_URL}#${REPO_URL}#g" \
      -e "s#${DEFAULT_REPO_URL}#${REPO_URL}#g" \
      -e "s#targetRevision: HEAD#targetRevision: ${GIT_REF}#g"
}

echo "==> Rendering root Application with repo URL..."
TMP_ROOT_APP="$(mktemp)"
trap 'rm -f "${TMP_ROOT_APP}"' EXIT
rewrite_repo_urls < "${SCRIPT_DIR}/root-app.yaml" > "${TMP_ROOT_APP}"

if [[ "${REPO_URL}" != "${DEFAULT_REPO_URL}" ]]; then
  echo "==> Rewriting child Application manifests for fork ${REPO_URL}..."
  for f in "${SCRIPT_DIR}"/apps/*.yaml; do
    rewrite_repo_urls < "$f" > "${f}.tmp" && mv "${f}.tmp" "$f"
  done
  echo "    Commit and push gitops/apps so Argo CD sees the fork URL:"
  echo "      git add gitops/apps && git commit -m 'gitops: point apps at fork' && git push"
else
  echo "    Child Application repoURLs already match ${DEFAULT_REPO_URL} — leaving apps/ unchanged."
fi

echo "==> Applying root Application (App-of-Apps)..."
oc apply -f "${TMP_ROOT_APP}"

echo "==> Done. ArgoCD will now reconcile the full stack."
echo "    Finish MCP URL configuration after wave 0 creates the Gateway:"
echo "      oc wait --for=create gateway/mcp-gateway -n mcp-gateway --timeout=300s"
echo "      ./install-mcp-gateway.sh"
echo ""
echo "    Watch progress with:"
echo "      oc get applications -n openshift-gitops -w"
echo ""
ARGOCD_ROUTE=$(oc get route openshift-gitops-server -n openshift-gitops -o jsonpath='https://{.spec.host}' 2>/dev/null || true)
if [[ -n "${ARGOCD_ROUTE}" ]]; then
  echo "    ArgoCD UI: ${ARGOCD_ROUTE}"
fi
