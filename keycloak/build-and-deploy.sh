#!/usr/bin/env bash
#
# Build and deploy Keycloak (RHBK) directly with `oc` (non-GitOps path).
# Useful for local iteration; the GitOps path (gitops/bootstrap.sh) is
# the recommended way to deploy the full demo stack.
#
# Idempotent: if the 'keycloak' namespace already exists AND a Running pod
# with "keycloak" in its name is found, the operator + instance deployment
# is skipped entirely — only the 'platform' realm KeycloakRealmImport CR
# is (re-)applied to ensure the realm exists.
#
# Usage:
#   ./keycloak/build-and-deploy.sh
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
NAMESPACE="${NAMESPACE:-keycloak}"

echo "==> Checking cluster login..."
oc whoami >/dev/null || { echo "error: not logged in (run 'oc login' first)"; exit 1; }

# ---------------------------------------------------------------------------
# Detect whether Keycloak is already installed:
#   - the namespace exists  AND
#   - at least one Running pod whose name contains "keycloak" is present
# ---------------------------------------------------------------------------
KEYCLOAK_INSTALLED=false
if oc get namespace "${NAMESPACE}" >/dev/null 2>&1; then
  if oc get pods -n "${NAMESPACE}" \
       --field-selector=status.phase=Running \
       --no-headers 2>/dev/null \
     | grep -q "keycloak"; then
    KEYCLOAK_INSTALLED=true
  fi
fi

if [[ "${KEYCLOAK_INSTALLED}" == "true" ]]; then
  # -----------------------------------------------------------------
  # Fast path: Keycloak already running — only ensure realm CR exists.
  # KeycloakRealmImport is a create-only resource at the Keycloak level,
  # so applying it when the realm already exists is a safe no-op.
  # -----------------------------------------------------------------
  echo "==> Keycloak already running in namespace ${NAMESPACE}."
  echo "    Skipping operator and instance deployment."
  echo "==> Ensuring 'platform' realm CR exists..."
  oc apply -f "${SCRIPT_DIR}/k8s/instance/realm-import.yaml"
else
  # -----------------------------------------------------------------
  # Full install path: namespace missing or no running Keycloak pod.
  # -----------------------------------------------------------------
  echo "==> Keycloak not detected — running full installation."

  echo "==> Ensuring namespace ${NAMESPACE} exists..."
  oc get namespace "${NAMESPACE}" >/dev/null 2>&1 || oc create namespace "${NAMESPACE}"

  echo "==> Applying OperatorGroup..."
  oc apply -f "${SCRIPT_DIR}/k8s/operator/operatorgroup.yaml"

  echo "==> Applying RHBK Subscription..."
  oc apply -f "${SCRIPT_DIR}/k8s/operator/subscription.yaml"

  echo "==> Waiting for RHBK operator to be installed (Automatic install plan)..."
  until oc get csv -n "${NAMESPACE}" 2>/dev/null | grep -q "rhbk-operator"; do
    echo "  Waiting for CSV..."
    sleep 10
  done
  echo "  RHBK operator CSV found."

  echo "==> Applying Keycloak CR..."
  oc apply -f "${SCRIPT_DIR}/k8s/instance/keycloak.yaml"

  echo "==> Applying realm import..."
  oc apply -f "${SCRIPT_DIR}/k8s/instance/realm-import.yaml"

  echo "==> Waiting for Keycloak to be ready..."
  for i in {1..60}; do
    READY=$(oc get keycloak keycloak -n "${NAMESPACE}" \
      -o jsonpath='{.status.conditions[?(@.type=="Ready")].status}' 2>/dev/null || true)
    if [[ "${READY}" == "True" ]]; then break; fi
    echo "  Waiting for Keycloak ready... (${i}/60)"
    sleep 10
  done
fi

# ---------------------------------------------------------------------------
# Always print the route URL and realm URL regardless of which path was taken.
# ---------------------------------------------------------------------------
KC_ROUTE=$(oc get route -n "${NAMESPACE}" --no-headers 2>/dev/null \
  | grep keycloak | awk '{print $2}' | head -1 || true)
echo ""
echo "==> Keycloak in namespace ${NAMESPACE}."
if [[ -n "${KC_ROUTE}" ]]; then
  echo "  Keycloak URL: https://${KC_ROUTE}"
  echo "  Realm URL:    https://${KC_ROUTE}/realms/platform"
fi
echo ""
echo "  NOTE: Users (admin/developer) and the RHDH OIDC client are provisioned"
echo "  by the configure-rhdh Job in developer-hub/ — run that component next."
