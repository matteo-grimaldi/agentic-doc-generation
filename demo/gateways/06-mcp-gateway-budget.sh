#!/usr/bin/env bash
# Act 6 (screen): MCP Gateway cost-limit config.
set -euo pipefail
# shellcheck source=lib.sh
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib.sh"

require_oc
demo_banner "6 — MCP Gateway cost limiting (RateLimitPolicy)"

show_yaml "${MCP_GATEWAY_DIR}/rate-limit-policy.yaml"
show_live ratelimitpolicy mcp-gateway-budget "${MCP_NS}"

demo_banner "Next: demo/governance (or notes/06 for handoff text)"
