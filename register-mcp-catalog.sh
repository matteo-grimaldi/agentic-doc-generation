#!/usr/bin/env bash
#
# Registers every MCP server in this repo in OpenShift AI's Gen AI
# Playground tool list, by merging each component's own
# <component>/mcp-catalog-entry.yaml fragment into the platform-wide
# `gen-ai-aa-mcp-servers` ConfigMap (`redhat-ods-applications` namespace,
# RHOAI 3.0+). See mcp-catalog-source.yaml / generate-mcp-catalog-source.sh
# for the other, independent import path (the AI Hub MCP Catalog's own
# "Add a source" upload) — both can be used together.
#
# Run imperatively, like install-openshift-ai.sh and
# ci-cd-pipeline/install-operator.sh: it targets a shared,
# cluster-admin-owned ConfigMap outside this repo's doc-gen-demo
# namespace/ArgoCD App-of-Apps tree, so it's deliberately not wired into
# any component's k8s/ directory or build-and-deploy.sh. ArgoCD's
# prune: true on e.g. github-mcp-server's Application would delete the
# WHOLE shared ConfigMap — including every other component's and every
# other team's registered MCP servers — the moment a fragment was ever
# removed from git. Each fragment is instead applied as its own JSON
# Merge Patch (`oc patch --type=merge --patch-file=...`), which only
# ever adds/updates that one ConfigMap key.
#
# To register a NEW MCP server component: copy
# github-mcp-server/mcp-catalog-entry.yaml into the new component's
# directory as a template (give it a unique ConfigMap key — see that
# file's header for why), then re-run this script — no other files need
# to change.
#
# Usage:
#   ./register-mcp-catalog.sh
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
RHOAI_NAMESPACE="redhat-ods-applications"
CONFIGMAP_NAME="gen-ai-aa-mcp-servers"

echo "==> Checking cluster login..."
oc whoami >/dev/null || { echo "error: not logged into an OpenShift cluster (run 'oc login' first)"; exit 1; }

echo "==> Checking for ${RHOAI_NAMESPACE} namespace (OpenShift AI installed)..."
if ! oc get namespace "${RHOAI_NAMESPACE}" >/dev/null 2>&1; then
  echo "error: namespace ${RHOAI_NAMESPACE} not found — install OpenShift AI first (see ./install-openshift-ai.sh)."
  exit 1
fi

echo "==> Discovering MCP server catalog entry fragments (*/mcp-catalog-entry.yaml)..."
ENTRY_FILES=()
while IFS= read -r f; do
  ENTRY_FILES+=("${f}")
done < <(find "${SCRIPT_DIR}" -mindepth 2 -maxdepth 2 -name 'mcp-catalog-entry.yaml' | sort)
if [[ ${#ENTRY_FILES[@]} -eq 0 ]]; then
  echo "    No */mcp-catalog-entry.yaml fragments found — nothing to register."
  exit 0
fi
for f in "${ENTRY_FILES[@]}"; do
  echo "    - ${f#"${SCRIPT_DIR}"/}"
done

if ! oc get configmap "${CONFIGMAP_NAME}" -n "${RHOAI_NAMESPACE}" >/dev/null 2>&1; then
  echo "==> ${CONFIGMAP_NAME} does not exist yet — creating it (empty) first..."
  oc create configmap "${CONFIGMAP_NAME}" -n "${RHOAI_NAMESPACE}"
fi

echo "==> Merging each component's entry into ${CONFIGMAP_NAME} (other entries, from this repo or any other, are left untouched)..."
for f in "${ENTRY_FILES[@]}"; do
  component="$(basename "$(dirname "${f}")")"
  echo "    Patching entry from ${component}..."
  oc patch configmap "${CONFIGMAP_NAME}" -n "${RHOAI_NAMESPACE}" --type=merge --patch-file="${f}"
done

echo "==> Done. Verify with:"
echo "      oc get configmap ${CONFIGMAP_NAME} -n ${RHOAI_NAMESPACE} -o yaml"
echo "    It may take a few minutes to appear in the OpenShift AI dashboard's AI Hub / Gen AI Playground tool list."
