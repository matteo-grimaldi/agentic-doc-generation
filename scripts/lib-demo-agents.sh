# shellcheck shell=bash
# Shared helpers for the four demo agents (doc-gen, openapi-gen, compliance-scan,
# catalog-gen). Sourced by install-openshift-ai.sh and install-mcp-gateway.sh.
DEMO_NS="${DEMO_NS:-doc-gen-demo}"
DEMO_AGENT_DEPLOYMENTS=(
  doc-gen-agent
  openapi-gen-agent
  catalog-gen-agent
  compliance-scan-agent
)

restart_demo_agent_deployments() {
  local ns="${1:-${DEMO_NS}}"
  for dep in "${DEMO_AGENT_DEPLOYMENTS[@]}"; do
    if oc get deployment "${dep}" -n "${ns}" >/dev/null 2>&1; then
      oc rollout restart "deployment/${dep}" -n "${ns}"
      echo "    Restarted deployment/${dep} in ${ns}."
    fi
  done
}

patch_cluster_config_data() {
  local ns="${1:-${DEMO_NS}}"
  local patch_json="${2:?patch JSON required}"
  local existing_name repo_root
  if ! existing_name=$(oc get configmap cluster-config -n "${ns}" --ignore-not-found -o name); then
    echo "error: could not read cluster-config in ${ns}" >&2
    return 1
  fi
  if [[ -z "${existing_name}" ]]; then
    # On a fresh install, model URL discovery precedes GitOps preparation.
    # Seed the committed shared ConfigMap so the discovered URL is retained.
    # Existing ConfigMaps are never reapplied here.
    repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
    oc apply -f "${repo_root}/doc-gen-agent/k8s/cluster-config.yaml" -n "${ns}" || return 1
  fi
  oc patch configmap cluster-config -n "${ns}" --type=merge -p "${patch_json}"
  echo "    Patched configmap/cluster-config in ${ns}."
  return 0
}
