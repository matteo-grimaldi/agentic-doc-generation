#!/usr/bin/env bash
# Act 3 (screen): AI Gateway cost-limit config.
set -euo pipefail
# shellcheck source=lib.sh
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib.sh"

require_oc
demo_banner "3 — AI Gateway cost limiting (MaaSSubscription)"

show_yaml "${AI_GATEWAY_DIR}/maas-subscription.yaml"
show_live maassubscription doc-gen-agents "${MAAS_NS}"

demo_banner "Next: ./04-agent-mcp-gateway.sh"
