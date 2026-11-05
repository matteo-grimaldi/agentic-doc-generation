#!/usr/bin/env bash
# Act 5 (notes): what to say while the screen runs ../05-mcp-gateway-access.sh
set -euo pipefail
# shellcheck source=../lib.sh
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/../lib.sh"

demo_banner "5 — NOTES (screen: ../05-mcp-gateway-access.sh)"

demo_readout "Auth = Kubernetes TokenReview with audience mcp-gateway (matches the projected SA token)."
demo_readout "Tool allowlist = MCPVirtualServer; agents send X-Mcp-Virtualserver: mcp-gateway/github-tools."
demo_readout "compliance-tools is a second virtual server on the same gateway for governance-hub tools."
demo_readout "Later, demo/governance will restrict AuthPolicy to agent SAs only to show untrusted → 403."

demo_banner "Next notes: ./06-mcp-gateway-budget.sh"
