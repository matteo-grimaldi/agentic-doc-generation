#!/usr/bin/env bash
# Act 3: Limitador budget on the model gateway (1 token / 1h → 429).
set -euo pipefail
# shellcheck source=lib.sh
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib.sh"

require_oc
demo_banner "3 — Model budget TIGHTEN (Limitador / MaaSSubscription)"

apply_manifest "${MANIFESTS_DIR}/maas-subscription-tight.yaml"
wait_for_policy 5

demo_step "RateLimitPolicy / Limitador objects (MaaS wires Limitador under the hood)"
oc get ratelimitpolicy -A 2>/dev/null || true
oc get limitador -A 2>/dev/null || true
echo
oc get maassubscription doc-gen-agents -n "${MAAS_NS}" -o yaml | sed -n '/tokenRateLimits:/,/^[[:space:]]*[^[:space:]]/p' | head -n 12

demo_step "Probe model up to 3 times — stop on first 429 (first may still be 200)"
for i in 1 2 3; do
  call_model "model-budget-attempt-${i}"
  if [[ "${DEMO_LAST_HTTP_STATUS}" == "429" ]]; then
    echo
    echo "Got 429 on attempt ${i} — budget exhausted."
    break
  fi
  sleep 1
done

if [[ "${DEMO_LAST_HTTP_STATUS}" != "429" ]]; then
  echo
  echo "warning: no 429 yet (quota counter may lag). Re-run this script once more, or wait and retry the curl."
fi

demo_banner "Read out 429 — next: ./04-model-budget-open.sh"
