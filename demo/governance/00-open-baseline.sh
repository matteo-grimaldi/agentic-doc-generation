#!/usr/bin/env bash
# Act 0: permissive baseline — identities allowed, budgets too high to trip.
set -euo pipefail
# shellcheck source=lib.sh
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib.sh"

require_oc
demo_banner "0 — Open baseline (Authorino allows, Limitador budgets open)"

pause_mcp_gateway_autosync

demo_step "Ensure untrusted ServiceAccount for MCP deny act later"
ensure_untrusted_sa

demo_step "Apply permissive MaaS + MCP policies"
# Do not re-apply group-doc-gen-agents.yaml — it has users: [] and would
# wipe members that install-openshift-ai.sh already added.
apply_manifest "${AI_GATEWAY_DIR}/maas-subscription.yaml"
apply_manifest "${AI_GATEWAY_DIR}/maas-auth-policy.yaml"
apply_manifest "${MCP_GATEWAY_DIR}/auth-policy.yaml"
apply_manifest "${MCP_GATEWAY_DIR}/rate-limit-policy.yaml"
wait_for_policy 5

print_policy_summary

demo_step "Probe model gateway (expect 200)"
call_model "model-open"

start_mcp_port_forward
TRUSTED_TOKEN="$(mint_mcp_token "${TRUSTED_SA}" "${DEMO_NS}")"
UNTRUSTED_TOKEN="$(mint_mcp_token "${UNTRUSTED_SA}" "${UNTRUSTED_SA_NS}")"

demo_step "Probe MCP with untrusted SA (expect 200 — open auth)"
call_mcp_tools_list "mcp-untrusted-open" "${UNTRUSTED_TOKEN}"

demo_step "Probe MCP with trusted agent SA (expect 200)"
call_mcp_tools_list "mcp-trusted-open" "${TRUSTED_TOKEN}"

stop_mcp_port_forward
demo_banner "Baseline ready — next: ./01-model-authorization-deny.sh"
