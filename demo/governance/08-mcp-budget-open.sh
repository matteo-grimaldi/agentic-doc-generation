#!/usr/bin/env bash
# Act 8: restore the 10000/hour MCP RateLimitPolicy.
set -euo pipefail
# shellcheck source=lib.sh
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib.sh"

require_oc
demo_banner "8 — MCP budget OPEN (restore RateLimitPolicy 10000/1h)"

apply_manifest "${MCP_GATEWAY_DIR}/rate-limit-policy.yaml"
wait_for_policy 5

start_mcp_port_forward
TRUSTED_TOKEN="$(mint_mcp_token "${TRUSTED_SA}" "${DEMO_NS}")"

demo_step "Probe MCP (expect 200)"
call_mcp "mcp-budget-open" "${TRUSTED_TOKEN}"

stop_mcp_port_forward
demo_banner "Read out 200 — finish with ./99-restore-and-resume-gitops.sh"
