#!/usr/bin/env bash
#
# Bootstrap the Agentic Doc Generation demo on a freshly deployed
# OpenShift 4.22 cluster using GitOps.
#
# What this does:
#   1. Installs the OpenShift GitOps operator (if not already present)
#   2. Waits for the default ArgoCD instance to come up
#   3. Points the root ArgoCD Application at THIS repo (substituting the
#      repo URL placeholder) and applies it
#   4. ArgoCD then takes over: it installs the GitHub MCP server and the
#      doc-gen agent, in dependency order (via sync-waves). Model
#      inference is not provisioned by this branch — the agent calls a
#      configurable external OpenAI-compatible endpoint (see
#      doc-gen-agent/k8s/cluster-config.yaml and the
#      external-model-credentials Secret documented in the README).
#
# Usage:
#   ./gitops/bootstrap.sh [repo-url] [git-ref]
#
#   repo-url  Git URL of this repo as ArgoCD should clone it.
#             Defaults to the `origin` remote of the local checkout.
#   git-ref   Branch/tag/commit ArgoCD should track. Defaults to HEAD.
#
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
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

echo "==> Rendering root Application with repo URL substituted..."
TMP_ROOT_APP="$(mktemp)"
trap 'rm -f "${TMP_ROOT_APP}"' EXIT
sed "s#${PLACEHOLDER_URL}#${REPO_URL}#g; s#targetRevision: HEAD#targetRevision: ${GIT_REF}#g" \
  "${SCRIPT_DIR}/root-app.yaml" > "${TMP_ROOT_APP}"

echo "==> Also substituting repo URL in child Application manifests..."
for f in "${SCRIPT_DIR}"/apps/*.yaml; do
  sed -i.bak "s#${PLACEHOLDER_URL}#${REPO_URL}#g; s#targetRevision: HEAD#targetRevision: ${GIT_REF}#g" "$f"
  rm -f "${f}.bak"
done

echo "==> Applying root Application (App-of-Apps)..."
oc apply -f "${TMP_ROOT_APP}"

echo "==> Done. ArgoCD will now reconcile the full stack."
echo ""
echo "    Watch progress with:"
echo "      oc get applications -n openshift-gitops -w"
echo ""
ARGOCD_ROUTE=$(oc get route openshift-gitops-server -n openshift-gitops -o jsonpath='https://{.spec.host}' 2>/dev/null || true)
if [[ -n "${ARGOCD_ROUTE}" ]]; then
  echo "    ArgoCD UI: ${ARGOCD_ROUTE}"
fi
echo ""
echo "    IMPORTANT: this script edited gitops/apps/*.yaml in place to point"
echo "    at your repo URL. Commit and push that change so ArgoCD (which"
echo "    reads from git) sees the same content it was bootstrapped with:"
echo ""
echo "      git add gitops/apps && git commit -m 'gitops: point apps at repo' && git push"
