#!/usr/bin/env bash
# Act 5: Authorino allows only the four agent SAs on the MCP gateway.
set -euo pipefail
# shellcheck source=lib.sh
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib.sh"

require_oc
demo_banner "5 — MCP authorization DENY (Authorino agents-only allowlist)"

ensure_untrusted_sa
apply_manifest "${MANIFESTS_DIR}/mcp-auth-agents-only.yaml"
wait_for_policy 5

demo_step "AuthPolicy on screen"
oc get authpolicy mcp-gateway-tokenreview -n "${MCP_NS}" -o yaml | sed -n '/authorization:/,/response:/p' | head -n 40

start_mcp_port_forward
TRUSTED_TOKEN="$(mint_mcp_token "${TRUSTED_SA}" "${DEMO_NS}")"
UNTRUSTED_TOKEN="$(mint_mcp_token "${UNTRUSTED_SA}" "${UNTRUSTED_SA_NS}")"

demo_step "Untrusted SA → expect 403"
call_mcp "mcp-untrusted-deny" "${UNTRUSTED_TOKEN}"

demo_step "Trusted agent SA → expect 200"
call_mcp "mcp-trusted-allow" "${TRUSTED_TOKEN}"

stop_mcp_port_forward
demo_banner "Read out 403 then 200 — next: ./06-mcp-authorization-open.sh"
