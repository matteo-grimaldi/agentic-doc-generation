#!/usr/bin/env bash
# Act 1: Authorino denies model access (MaaSAuthPolicy → empty group).
set -euo pipefail
# shellcheck source=lib.sh
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib.sh"

require_oc
demo_banner "1 — Model authorization DENY (Authorino / MaaSAuthPolicy)"

apply_manifest "${MANIFESTS_DIR}/maas-auth-nobody.yaml"
wait_for_policy 5

demo_step "Probe model gateway (expect 403)"
call_model "model-auth-deny"

demo_step "Authorino AuthPolicies on the cluster"
oc get authpolicy -A
echo
echo "MaaSAuthPolicy subjects (should be demo-no-access):"
oc get maasauthpolicy doc-gen-agents -n "${MAAS_NS}" -o yaml | sed -n '/subjects:/,/modelRefs:/p' | head -n 20

demo_banner "Read out 403 — next: ./02-model-authorization-allow.sh"
# Do not delete the subscription — auth alone without subscription → 429 noise.
