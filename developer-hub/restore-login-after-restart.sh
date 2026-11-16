#!/usr/bin/env bash
# Restore Developer Hub OIDC login after this demo's ephemeral Keycloak restarts.
# Run from any directory after `oc login` (also safe to rerun when healthy).
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

oc whoami >/dev/null
echo "==> Waiting for Keycloak..."
oc wait --for=condition=Ready pod/keycloak-0 -n rhdh-keycloak --timeout=600s

echo "==> Rerunning Developer Hub provisioning..."
oc delete job job-configure-rhdh -n rhdh --ignore-not-found
oc apply -f "${SCRIPT_DIR}/k8s/jobs/configure-rhdh.yaml"
if ! oc wait --for=condition=complete job/job-configure-rhdh -n rhdh --timeout=1800s; then
  oc logs job/job-configure-rhdh -n rhdh --tail=80 || true
  echo "error: Developer Hub provisioning did not complete" >&2
  exit 1
fi

KC_HOST="$(oc get route keycloak -n rhdh-keycloak -o jsonpath='{.spec.host}')"
curl --fail --silent --show-error --insecure --max-time 15 \
  "https://${KC_HOST}/realms/platform/.well-known/openid-configuration" \
  -o /dev/null
echo "==> Developer Hub login restored; platform realm discovery is available."
