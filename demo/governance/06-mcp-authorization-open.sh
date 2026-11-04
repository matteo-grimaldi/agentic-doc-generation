#!/usr/bin/env bash
# Act 6: restore TokenReview-only AuthPolicy (any audience-matched SA).
set -euo pipefail
# shellcheck source=lib.sh
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib.sh"

require_oc
demo_banner "6 — MCP authorization OPEN (restore TokenReview-only AuthPolicy)"

ensure_untrusted_sa
apply_manifest "${MCP_GATEWAY_DIR}/auth-policy.yaml"
wait_for_policy 5

start_mcp_port_forward
TRUSTED_TOKEN="$(mint_mcp_token "${TRUSTED_SA}" "${DEMO_NS}")"
UNTRUSTED_TOKEN="$(mint_mcp_token "${UNTRUSTED_SA}" "${UNTRUSTED_SA_NS}")"

demo_step "Untrusted SA → expect 200"
call_mcp "mcp-untrusted-open" "${UNTRUSTED_TOKEN}"

demo_step "Trusted agent SA → expect 200"
call_mcp "mcp-trusted-open" "${TRUSTED_TOKEN}"

stop_mcp_port_forward
demo_banner "Read out both 200 — next: ./07-mcp-budget-tighten.sh"
