#!/usr/bin/env bash
# Act 2: restore MaaSAuthPolicy so the demo group may call the model again.
set -euo pipefail
# shellcheck source=lib.sh
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib.sh"

require_oc
demo_banner "2 — Model authorization ALLOW (restore MaaSAuthPolicy)"

apply_manifest "${AI_GATEWAY_DIR}/maas-auth-policy.yaml"
wait_for_policy 5

demo_step "Probe model gateway (expect 200)"
call_model "model-auth-allow"

demo_banner "Read out 200 — next: ./03-model-budget-tighten.sh"
