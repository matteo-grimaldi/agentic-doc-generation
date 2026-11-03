# shellcheck shell=bash
# Create/update mlflow-credentials in the agent namespace when MLflow tracing is enabled.
#
# Usage (after sourcing):
#   ensure_mlflow_credentials_secret "${NAMESPACE}" "${REPO_ROOT}"
#
# Token source (first match wins):
#   1. MLFLOW_TRACKING_TOKEN env var
#   2. oc whoami -t (current OpenShift session)
#
# Skips quietly when mlflow_tracking_uri is unset/empty in cluster-config.

_ensure_mlflow_credentials_read_uri() {
  local namespace="$1"
  local repo_root="$2"
  local uri=""

  uri="$(oc get configmap cluster-config -n "${namespace}" \
    -o jsonpath='{.data.mlflow_tracking_uri}' 2>/dev/null || true)"
  if [[ -n "${uri}" ]]; then
    printf '%s' "${uri}"
    return 0
  fi

  local cluster_config_file="${repo_root}/doc-gen-agent/k8s/cluster-config.yaml"
  if [[ -f "${cluster_config_file}" ]]; then
    uri="$(python3 -c "
import yaml, sys
with open(sys.argv[1]) as f:
    data = yaml.safe_load(f).get('data') or {}
print((data.get('mlflow_tracking_uri') or '').strip())
" "${cluster_config_file}" 2>/dev/null || true)"
  fi
  printf '%s' "${uri}"
}

ensure_mlflow_credentials_secret() {
  local namespace="$1"
  local repo_root="$2"
  local mlflow_uri token

  mlflow_uri="$(_ensure_mlflow_credentials_read_uri "${namespace}" "${repo_root}")"
  if [[ -z "${mlflow_uri}" ]]; then
    echo "==> MLflow tracing disabled (mlflow_tracking_uri empty) — skipping mlflow-credentials Secret."
    return 0
  fi

  if [[ -n "${MLFLOW_TRACKING_TOKEN:-}" ]]; then
    token="${MLFLOW_TRACKING_TOKEN}"
  else
    token="$(oc whoami -t 2>/dev/null || true)"
  fi

  if [[ -z "${token}" ]]; then
    echo "==> WARNING: mlflow_tracking_uri is set but no token available —"
    echo "    set MLFLOW_TRACKING_TOKEN or run 'oc login' before deploy."
    echo "    Skipping mlflow-credentials Secret (tracing may fail until created)."
    return 0
  fi

  echo "==> Creating/updating mlflow-credentials Secret (MLflow tracking auth)..."
  oc create secret generic mlflow-credentials \
    --from-literal=token="${token}" \
    -n "${namespace}" --dry-run=client -o yaml | oc apply -f -
}
