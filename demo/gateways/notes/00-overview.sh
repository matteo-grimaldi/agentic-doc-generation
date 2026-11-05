#!/usr/bin/env bash
# Act 0 (notes): what to say while the screen runs ../00-overview.sh
set -euo pipefail
# shellcheck source=../lib.sh
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/../lib.sh"

demo_banner "0 — NOTES (screen: ../00-overview.sh)"

demo_readout "Agents never call the upstream model or MCP Services directly for inference or tools."
demo_readout "Gradio talks to MCP Gateway; the OGX sidecar talks to AI Gateway (MaaS)."
demo_readout "Confirm both Gateways show Programmed=True and cluster-config has model_base_url + mcp_gateway_url set."

demo_banner "Next notes: ./01-agent-ai-gateway.sh"
