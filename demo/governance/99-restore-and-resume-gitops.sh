#!/usr/bin/env bash
# Cleanup: re-apply every permissive policy and resume Argo CD selfHeal.
# Safe to run even if an earlier demo step was skipped.
set -euo pipefail
# shellcheck source=lib.sh
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib.sh"

require_oc
demo_banner "99 — Restore permissive policies + resume GitOps"

stop_mcp_port_forward
stop_maas_port_forward

demo_step "Re-apply permissive MaaS + MCP baselines"
apply_manifest "${AI_GATEWAY_DIR}/maas-auth-policy.yaml"
apply_manifest "${AI_GATEWAY_DIR}/maas-subscription.yaml"
apply_manifest "${MCP_GATEWAY_DIR}/auth-policy.yaml"
apply_manifest "${MCP_GATEWAY_DIR}/rate-limit-policy.yaml"
wait_for_policy 3

resume_mcp_gateway_autosync

print_policy_summary

demo_banner "Demo restored — Application/mcp-gateway selfHeal is back on"
