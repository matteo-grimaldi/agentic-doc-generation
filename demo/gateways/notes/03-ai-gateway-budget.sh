#!/usr/bin/env bash
# Act 3 (notes): what to say while the screen runs ../03-ai-gateway-budget.sh
set -euo pipefail
# shellcheck source=../lib.sh
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/../lib.sh"

demo_banner "3 — NOTES (screen: ../03-ai-gateway-budget.sh)"

demo_readout "Cost limit is MaaSSubscription tokenRateLimits — open budget 10M tokens / 1h."
demo_readout "Both MaaSAuthPolicy and MaaSSubscription are required (subscription alone → 403; auth alone → 429)."
demo_readout "demo/governance temporarily tightens the subscription to show a live 429."

demo_banner "Next notes: ./04-agent-mcp-gateway.sh"
