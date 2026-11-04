#!/usr/bin/env bash
# Act 4: restore the 10M/hour model subscription budget.
set -euo pipefail
# shellcheck source=lib.sh
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib.sh"

require_oc
demo_banner "4 — Model budget OPEN (restore MaaSSubscription 10M/1h)"

apply_manifest "${AI_GATEWAY_DIR}/maas-subscription.yaml"
wait_for_policy 5

demo_step "Probe model gateway (expect 200)"
call_model "model-budget-open"

demo_banner "Read out 200 — next: ./05-mcp-authorization-deny.sh"
