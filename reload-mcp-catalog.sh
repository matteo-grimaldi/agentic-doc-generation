#!/usr/bin/env bash
#
# Forces OpenShift AI's AI Hub MCP Catalog backend (the `model-catalog`
# pod in `rhoai-model-registries`) to reload its catalog sources.
#
# Root cause this works around: model-catalog only parses the
# mcp-catalog-sources ConfigMap's content (and any referenced catalog
# YAML, e.g. what you import via Settings > MCP resources > MCP catalog
# sources > Add a source) ONCE, at pod startup. The "MCP catalog
# sources" settings page reads source metadata (name/enabled/status)
# live, so a newly added or edited source shows up there immediately —
# but the actual browsable AI Hub MCP Catalog is a server index built
# only at startup, so new or changed `mcp_servers` entries silently
# don't appear until the pod restarts. There is no in-place
# "reload"/"sync" endpoint; a restart is required (same documented
# behavior as the plain Model Catalog: "Force a Reload (Recommended
# After Changes): oc delete pod -l component=model-catalog").
#
# Run this after every time you add or edit a component's
# mcp-catalog.yaml fragment, regenerate mcp-catalog-source.yaml, and
# (re-)import it via the dashboard's "Add a source" / edit flow.
#
# Usage:
#   ./reload-mcp-catalog.sh
set -euo pipefail

RHOAI_MODEL_REGISTRIES_NAMESPACE="rhoai-model-registries"

echo "==> Checking cluster login..."
oc whoami >/dev/null || { echo "error: not logged into an OpenShift cluster (run 'oc login' first)"; exit 1; }

echo "==> Checking for ${RHOAI_MODEL_REGISTRIES_NAMESPACE} namespace..."
if ! oc get namespace "${RHOAI_MODEL_REGISTRIES_NAMESPACE}" >/dev/null 2>&1; then
  echo "error: namespace ${RHOAI_MODEL_REGISTRIES_NAMESPACE} not found — is the Model Registry / AI Hub component enabled?"
  exit 1
fi

if ! oc get pods -n "${RHOAI_MODEL_REGISTRIES_NAMESPACE}" -l app=model-catalog --no-headers 2>/dev/null | grep -q .; then
  echo "error: no model-catalog pod found in ${RHOAI_MODEL_REGISTRIES_NAMESPACE} — is the AI Hub MCP Catalog enabled?"
  exit 1
fi

echo "==> Restarting model-catalog pod to force a reload of its catalog sources..."
oc delete pod -n "${RHOAI_MODEL_REGISTRIES_NAMESPACE}" -l app=model-catalog

echo "==> Waiting for the new pod to be ready..."
oc wait --for=condition=Ready pod -l app=model-catalog -n "${RHOAI_MODEL_REGISTRIES_NAMESPACE}" --timeout=180s

echo "==> Done. Newly added/edited MCP servers should now appear in the AI Hub MCP Catalog."
echo "    It may take a few more seconds for the dashboard page to reflect it (hard refresh if needed)."
