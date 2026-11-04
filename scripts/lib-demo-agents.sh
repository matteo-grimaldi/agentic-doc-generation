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
  if ! oc get configmap cluster-config -n "${ns}" >/dev/null 2>&1; then
    echo "    configmap/cluster-config not in ${ns} yet — re-run after GitOps syncs doc-gen-agent."
    return 1
  fi
  oc patch configmap cluster-config -n "${ns}" --type=merge -p "${patch_json}"
  echo "    Patched configmap/cluster-config in ${ns}."
  return 0
}
