#!/usr/bin/env bash
# Generate file-backed agent ConfigMaps without building or deploying workloads.
# Usage: bash scripts/configure-agents.sh [agent ...] (default: all four).
# Does not restart pods; see README configuration update instructions.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
NAMESPACE="${NAMESPACE:-doc-gen-demo}"
AGENTS=(doc-gen-agent openapi-gen-agent catalog-gen-agent compliance-scan-agent)
if [[ $# -gt 0 ]]; then
  AGENTS=("$@")
fi
for agent in "${AGENTS[@]}"; do
  case "${agent}" in
    doc-gen-agent|openapi-gen-agent|catalog-gen-agent|compliance-scan-agent) ;;
    *) echo "error: unknown agent ${agent}" >&2; exit 1 ;;
  esac
done
if [[ "${NAMESPACE}" != doc-gen-demo ]]; then
  echo "error: manifests and Service URLs currently require NAMESPACE=doc-gen-demo" >&2
  exit 1
fi
oc whoami >/dev/null
oc get namespace "${NAMESPACE}" >/dev/null 2>&1 || oc create namespace "${NAMESPACE}"
oc label namespace "${NAMESPACE}" opendatahub.io/dashboard=true modelmesh-enabled=false --overwrite >/dev/null

# Apply committed configuration while preserving the two installer-managed URLs.
# Merge before applying, so an existing working gateway URL is never blanked.
# Fail on API/auth errors rather than mistaking them for an absent ConfigMap.
desired="$(oc create --dry-run=client -f "${REPO_ROOT}/doc-gen-agent/k8s/cluster-config.yaml" -o json)"
existing="$(oc get configmap cluster-config -n "${NAMESPACE}" --ignore-not-found -o json)"
python3 -c '
import json, sys
desired = json.loads(sys.argv[1])
existing = json.loads(sys.argv[2]) if sys.argv[2].strip() else {}
for key in ("model_base_url", "mcp_gateway_url"):
    if key in existing.get("data", {}):
        desired["data"][key] = existing["data"][key]
print(json.dumps(desired))
' "${desired}" "${existing}" | oc apply -f -

# One shared stack: every caller deliberately uses this canonical source.
echo "==> Generating shared ogx-stack-config from doc-gen-agent/ogx/stack_run_config.yaml"
oc create configmap ogx-stack-config \
  --from-file=stack_run_config.yaml="${REPO_ROOT}/doc-gen-agent/ogx/stack_run_config.yaml" \
  -n "${NAMESPACE}" --dry-run=client -o yaml | oc apply -f -

for agent in "${AGENTS[@]}"; do
  case "${agent}" in
    doc-gen-agent)
      name="agent-knowledge"
      source_file="knowledge.md=${REPO_ROOT}/${agent}/knowledge.md"
      ;;
    openapi-gen-agent)
      name="openapi-agent-knowledge"
      source_file="openapi-knowledge.md=${REPO_ROOT}/${agent}/openapi-knowledge.md"
      ;;
    catalog-gen-agent)
      name="catalog-gen-knowledge"
      source_file="${REPO_ROOT}/${agent}/guidelines/"
      ;;
    compliance-scan-agent)
      name="compliance-scan-knowledge"
      source_file="${REPO_ROOT}/${agent}/guidelines/"
      ;;
  esac
  oc create configmap "${name}" --from-file="${source_file}" \
    -n "${NAMESPACE}" --dry-run=client -o yaml | oc apply -f -
done

if [[ -n "${EXTERNAL_MODEL_API_KEY:-}" ]]; then
  oc create secret generic external-model-credentials \
    --from-literal=api_key="${EXTERNAL_MODEL_API_KEY}" \
    -n "${NAMESPACE}" --dry-run=client -o yaml | oc apply -f -
fi
# shellcheck source=lib-mlflow-credentials.sh
source "${SCRIPT_DIR}/lib-mlflow-credentials.sh"
ensure_mlflow_credentials_secret "${NAMESPACE}" "${REPO_ROOT}"
echo "==> Configuration generated. Restart affected agents after configuration updates."
