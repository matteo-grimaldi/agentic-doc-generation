#!/usr/bin/env bash
# Act 6 (notes): what to say while the screen runs ../06-mcp-gateway-budget.sh
set -euo pipefail
# shellcheck source=../lib.sh
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/../lib.sh"

demo_banner "6 — NOTES (screen: ../06-mcp-gateway-budget.sh)"

demo_readout "MCP volume/cost cap is Limitador RateLimitPolicy — open budget 10000 requests / 1h."
demo_readout "Authentication stays in AuthPolicy; this policy only caps request volume."
demo_readout "demo/governance temporarily tightens this to show a live 429."

demo_banner "Wiring tour complete — handoff"
cat <<'EOF'

  Config tour done. For live Authorino deny/allow and Limitador 429 acts:

    cd ../../governance
    ./00-open-baseline.sh
    # … through 08, then always ./99-restore-and-resume-gitops.sh

EOF
