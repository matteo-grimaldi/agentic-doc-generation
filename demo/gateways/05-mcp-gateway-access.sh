#!/usr/bin/env bash
# Act 5 (screen): MCP Gateway access config.
set -euo pipefail
# shellcheck source=lib.sh
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib.sh"

require_oc
demo_banner "5 — MCP Gateway access (AuthPolicy + VirtualServer)"

show_yaml "${MCP_GATEWAY_DIR}/auth-policy.yaml"
show_yaml "${MCP_GATEWAY_DIR}/virtual-servers.yaml"

show_live authpolicy mcp-gateway-tokenreview "${MCP_NS}"
show_live mcpvirtualserver github-tools "${MCP_NS}"
show_live mcpvirtualserver compliance-tools "${MCP_NS}"

demo_banner "Next: ./06-mcp-gateway-budget.sh"
