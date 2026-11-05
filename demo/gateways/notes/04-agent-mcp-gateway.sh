#!/usr/bin/env bash
# Act 4 (notes): what to say while the screen runs ../04-agent-mcp-gateway.sh
set -euo pipefail
# shellcheck source=../lib.sh
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/../lib.sh"

demo_banner "4 — NOTES (screen: ../04-agent-mcp-gateway.sh)"

demo_readout "Tools go through MCP Gateway with a TokenReview bearer (projected SA token) plus X-Mcp-Virtualserver."
demo_readout "Projected token audience is mcp-gateway — must match AuthPolicy audiences."
demo_readout "Empty MCP_GATEWAY_URL fails closed — no fallback to the direct github-mcp-server Service for tools."
demo_readout "GITHUB_MCP_SERVER_URL is REST-only (/propose-doc); MCP tool traffic stays on the gateway."

demo_banner "Next notes: ./05-mcp-gateway-access.sh"
