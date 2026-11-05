#!/usr/bin/env bash
# Act 7: Limitador budget on MCP (2 req / 1m → third call 429).
# Leave Gradio agent UIs idle so they do not consume the two requests.
set -euo pipefail
# shellcheck source=lib.sh
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib.sh"

require_oc
demo_banner "7 — MCP budget TIGHTEN (Limitador RateLimitPolicy 2/1m)"

echo "Leave agent Gradio UIs idle during this act."
apply_manifest "${MANIFESTS_DIR}/mcp-budget-tight.yaml"
wait_for_policy 5

demo_step "RateLimitPolicy / Limitador"
oc get ratelimitpolicy mcp-gateway-budget -n "${MCP_NS}" -o yaml | sed -n '/defaults:/,/status:/p' | head -n 30
oc get limitador -A 2>/dev/null || true

start_mcp_port_forward
TRUSTED_TOKEN="$(mint_mcp_token "${TRUSTED_SA}" "${DEMO_NS}")"

demo_step "Three MCP initialize calls — third should be 429"
for i in 1 2 3; do
  call_mcp "mcp-budget-attempt-${i}" "${TRUSTED_TOKEN}"
  if [[ "${DEMO_LAST_HTTP_STATUS}" == "429" ]]; then
    echo
    echo "Got 429 on attempt ${i} — MCP budget exhausted."
    break
  fi
  sleep 0.5
done

if [[ "${DEMO_LAST_HTTP_STATUS}" != "429" ]]; then
  echo
  echo "warning: no 429 yet (policy may still be syncing, or other traffic used the window). Retry once."
fi

stop_mcp_port_forward
demo_banner "Read out 429 — next: ./08-mcp-budget-open.sh"
