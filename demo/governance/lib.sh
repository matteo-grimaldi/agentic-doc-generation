#!/usr/bin/env bash
# Shared helpers for the governance screen-share scripts.
# Tokens and API keys are read into variables and never printed.

set -euo pipefail

DEMO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${DEMO_DIR}/../.." && pwd)"
MANIFESTS_DIR="${DEMO_DIR}/manifests"

AI_GATEWAY_DIR="${REPO_ROOT}/ai-gateway/k8s"
MCP_GATEWAY_DIR="${REPO_ROOT}/mcp-gateway/k8s"

DEMO_NS="${DEMO_NS:-doc-gen-demo}"
MCP_NS="${MCP_NS:-mcp-gateway}"
MAAS_NS="${MAAS_NS:-models-as-a-service}"
GITOPS_NS="${GITOPS_NS:-openshift-gitops}"

MCP_LOCAL_PORT="${MCP_LOCAL_PORT:-18080}"
MCP_PF_PID_FILE="${DEMO_DIR}/.mcp-port-forward.pid"
MCP_VIRTUAL_SERVER="${MCP_VIRTUAL_SERVER:-mcp-gateway/github-tools}"
MODEL_ID="${MODEL_ID:-qwen38-27b}"

TRUSTED_SA="${TRUSTED_SA:-doc-gen-agent-sa}"
UNTRUSTED_SA="${UNTRUSTED_SA:-demo-untrusted}"
UNTRUSTED_SA_NS="${UNTRUSTED_SA_NS:-${MCP_NS}}"

# ---------------------------------------------------------------------------
# Output helpers
# ---------------------------------------------------------------------------

demo_banner() {
  echo
  echo "================================================================"
  echo " $*"
  echo "================================================================"
}

demo_step() {
  echo
  echo "--- $* ---"
}

show_yaml() {
  local path="$1"
  demo_step "YAML: ${path#"${REPO_ROOT}/"}"
  cat "${path}"
  echo
}

apply_manifest() {
  local path="$1"
  show_yaml "${path}"
  echo "Applying..."
  oc apply -f "${path}"
}

# ---------------------------------------------------------------------------
# GitOps pause / resume (mcp-gateway Application has selfHeal)
# ---------------------------------------------------------------------------

pause_mcp_gateway_autosync() {
  demo_step "Pause Argo CD auto-sync on Application/mcp-gateway"
  if oc get application mcp-gateway -n "${GITOPS_NS}" -o jsonpath='{.spec.syncPolicy.automated}' 2>/dev/null | grep -q .; then
    oc patch application mcp-gateway -n "${GITOPS_NS}" --type=json \
      -p '[{"op":"remove","path":"/spec/syncPolicy/automated"}]'
    echo "Removed /spec/syncPolicy/automated"
  else
    echo "Auto-sync already paused (or absent)"
  fi
}

resume_mcp_gateway_autosync() {
  demo_step "Resume Argo CD auto-sync on Application/mcp-gateway"
  oc patch application mcp-gateway -n "${GITOPS_NS}" --type=merge -p '{
    "spec": {
      "syncPolicy": {
        "automated": {
          "prune": true,
          "selfHeal": true
        }
      }
    }
  }'
  echo "Restored syncPolicy.automated (prune + selfHeal)"
}

# ---------------------------------------------------------------------------
# Credentials (never echoed)
# ---------------------------------------------------------------------------

require_oc() {
  command -v oc >/dev/null 2>&1 || {
    echo "error: oc not found in PATH" >&2
    exit 1
  }
  oc whoami >/dev/null 2>&1 || {
    echo "error: oc is not logged in" >&2
    exit 1
  }
}

maas_hostname() {
  local host
  host="$(oc get gateway maas-default-gateway -n openshift-ingress \
    -o jsonpath='{.spec.listeners[?(@.name=="https")].hostname}' 2>/dev/null || true)"
  if [[ -z "${host}" ]]; then
    echo "error: could not read MaaS Gateway https hostname" >&2
    exit 1
  fi
  printf '%s' "${host}"
}

maas_api_key() {
  local key
  key="$(oc get secret external-model-credentials -n "${DEMO_NS}" \
    -o jsonpath='{.data.api_key}' 2>/dev/null | base64 -d 2>/dev/null || true)"
  if [[ -z "${key}" ]]; then
    echo "error: secret/external-model-credentials api_key missing in ${DEMO_NS}" >&2
    exit 1
  fi
  printf '%s' "${key}"
}

ensure_untrusted_sa() {
  if ! oc get sa "${UNTRUSTED_SA}" -n "${UNTRUSTED_SA_NS}" >/dev/null 2>&1; then
    oc create sa "${UNTRUSTED_SA}" -n "${UNTRUSTED_SA_NS}"
  fi
}

mint_mcp_token() {
  local sa="$1"
  local ns="$2"
  oc create token "${sa}" -n "${ns}" --audience=mcp-gateway --duration=1h
}

# ---------------------------------------------------------------------------
# MCP port-forward
# ---------------------------------------------------------------------------

mcp_svc_name() {
  local svc
  svc="$(oc get svc -n "${MCP_NS}" -l gateway.networking.k8s.io/gateway-name=mcp-gateway \
    -o jsonpath='{.items[0].metadata.name}' 2>/dev/null || true)"
  if [[ -z "${svc}" ]]; then
    svc="mcp-gateway-openshift-default"
  fi
  printf '%s' "${svc}"
}

mcp_listener_host() {
  local host
  host="$(oc get gateway mcp-gateway -n "${MCP_NS}" \
    -o jsonpath='{.spec.listeners[?(@.name=="mcp")].hostname}' 2>/dev/null || true)"
  if [[ -z "${host}" ]]; then
    host="mcp-gateway-openshift-default.mcp-gateway.svc.cluster.local"
  fi
  printf '%s' "${host}"
}

start_mcp_port_forward() {
  local svc
  svc="$(mcp_svc_name)"
  stop_mcp_port_forward
  demo_step "Port-forward ${MCP_NS}/svc/${svc} → localhost:${MCP_LOCAL_PORT}"
  oc port-forward -n "${MCP_NS}" "svc/${svc}" "${MCP_LOCAL_PORT}:8080" \
    >/dev/null 2>&1 &
  echo $! >"${MCP_PF_PID_FILE}"
  # Wait until the local port accepts connections.
  local i
  for i in 1 2 3 4 5 6 7 8 9 10; do
    if curl -sS -o /dev/null --connect-timeout 1 "http://127.0.0.1:${MCP_LOCAL_PORT}/" 2>/dev/null \
      || true; then
      # Even a refused-with-connection-reset after accept means PF is up;
      # prefer a short sleep then continue.
      :
    fi
    if kill -0 "$(cat "${MCP_PF_PID_FILE}")" 2>/dev/null; then
      sleep 1
      return 0
    fi
    sleep 0.5
  done
  echo "error: MCP port-forward failed to start" >&2
  exit 1
}

stop_mcp_port_forward() {
  if [[ -f "${MCP_PF_PID_FILE}" ]]; then
    local pid
    pid="$(cat "${MCP_PF_PID_FILE}" 2>/dev/null || true)"
    if [[ -n "${pid}" ]] && kill -0 "${pid}" 2>/dev/null; then
      kill "${pid}" 2>/dev/null || true
      wait "${pid}" 2>/dev/null || true
    fi
    rm -f "${MCP_PF_PID_FILE}"
  fi
}

# ---------------------------------------------------------------------------
# Probe helpers — print status + short body; never print tokens
# ---------------------------------------------------------------------------

# Last probe status is written here so callers can branch without capturing
# stdout (stdout is reserved for the human-readable status/body lines).
DEMO_LAST_HTTP_STATUS=""

_print_http_result() {
  local label="$1"
  local status="$2"
  local body="$3"
  local snippet
  snippet="$(printf '%s' "${body}" | tr '\n' ' ' | cut -c1-200)"
  DEMO_LAST_HTTP_STATUS="${status}"
  echo
  echo "[${label}] HTTP ${status}"
  if [[ -n "${snippet}" ]]; then
    echo "[${label}] body: ${snippet}"
  fi
}

call_model() {
  local label="${1:-model}"
  local host key url
  host="$(maas_hostname)"
  key="$(maas_api_key)"
  url="https://${host}/v1/chat/completions"

  local tmp_body tmp_code
  tmp_body="$(mktemp)"
  tmp_code="$(mktemp)"

  curl -sS -k -o "${tmp_body}" -w '%{http_code}' \
    -X POST "${url}" \
    -H "Authorization: Bearer ${key}" \
    -H "Content-Type: application/json" \
    -d "{\"model\":\"${MODEL_ID}\",\"max_tokens\":16,\"messages\":[{\"role\":\"user\",\"content\":\"Say hi in three words.\"}]}" \
    >"${tmp_code}" || true

  local status body
  status="$(cat "${tmp_code}")"
  body="$(cat "${tmp_body}")"
  rm -f "${tmp_body}" "${tmp_code}"
  _print_http_result "${label}" "${status}" "${body}"
}

# MCP JSON-RPC initialize (one request = one Limitador counter tick).
call_mcp() {
  local label="$1"
  local token="$2"
  local host
  host="$(mcp_listener_host)"

  local tmp_body tmp_code
  tmp_body="$(mktemp)"
  tmp_code="$(mktemp)"

  curl -sS -o "${tmp_body}" -w '%{http_code}' \
    -X POST "http://127.0.0.1:${MCP_LOCAL_PORT}/mcp" \
    -H "Host: ${host}" \
    -H "Authorization: Bearer ${token}" \
    -H "Content-Type: application/json" \
    -H "Accept: application/json, text/event-stream" \
    -H "X-Mcp-Virtualserver: ${MCP_VIRTUAL_SERVER}" \
    -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"governance-demo","version":"1.0"}}}' \
    >"${tmp_code}" || true

  local status body
  status="$(cat "${tmp_code}")"
  body="$(cat "${tmp_body}")"
  rm -f "${tmp_body}" "${tmp_code}"
  _print_http_result "${label}" "${status}" "${body}"
}

# tools/list after initialize when the open path should show tool names.
# Counts as two requests against Limitador (initialize + tools/list).
call_mcp_tools_list() {
  local label="$1"
  local token="$2"
  local host
  host="$(mcp_listener_host)"

  local tmp_init tmp_hdrs tmp_body tmp_code session
  tmp_init="$(mktemp)"
  tmp_hdrs="$(mktemp)"
  tmp_body="$(mktemp)"
  tmp_code="$(mktemp)"

  curl -sS -D "${tmp_hdrs}" -o "${tmp_init}" \
    -X POST "http://127.0.0.1:${MCP_LOCAL_PORT}/mcp" \
    -H "Host: ${host}" \
    -H "Authorization: Bearer ${token}" \
    -H "Content-Type: application/json" \
    -H "Accept: application/json, text/event-stream" \
    -H "X-Mcp-Virtualserver: ${MCP_VIRTUAL_SERVER}" \
    -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"governance-demo","version":"1.0"}}}' \
    >/dev/null || true

  session="$(grep -i '^mcp-session-id:' "${tmp_hdrs}" | awk '{print $2}' | tr -d '\r' || true)"

  local -a curl_args=(
    -sS -o "${tmp_body}" -w '%{http_code}'
    -X POST "http://127.0.0.1:${MCP_LOCAL_PORT}/mcp"
    -H "Host: ${host}"
    -H "Authorization: Bearer ${token}"
    -H "Content-Type: application/json"
    -H "Accept: application/json, text/event-stream"
    -H "X-Mcp-Virtualserver: ${MCP_VIRTUAL_SERVER}"
  )
  if [[ -n "${session}" ]]; then
    curl_args+=(-H "Mcp-Session-Id: ${session}")
  fi

  curl "${curl_args[@]}" \
    -d '{"jsonrpc":"2.0","id":2,"method":"tools/list","params":{}}' \
    >"${tmp_code}" || true

  local status body
  status="$(cat "${tmp_code}")"
  body="$(cat "${tmp_body}")"
  rm -f "${tmp_init}" "${tmp_hdrs}" "${tmp_body}" "${tmp_code}"
  _print_http_result "${label}" "${status}" "${body}"
}

print_policy_summary() {
  demo_step "Live policies (Authorino / Limitador)"
  echo "MaaSAuthPolicy:"
  oc get maasauthpolicy -n "${MAAS_NS}" -o wide 2>/dev/null || true
  echo
  echo "MaaSSubscription:"
  oc get maassubscription -n "${MAAS_NS}" -o wide 2>/dev/null || true
  echo
  echo "AuthPolicy (cluster):"
  oc get authpolicy -A 2>/dev/null || true
  echo
  echo "RateLimitPolicy (cluster):"
  oc get ratelimitpolicy -A 2>/dev/null || true
  echo
  echo "Limitador (if present):"
  oc get limitador -A 2>/dev/null || true
}

wait_for_policy() {
  # Give Kuadrant / MaaS controllers a moment after apply.
  sleep "${1:-3}"
}
