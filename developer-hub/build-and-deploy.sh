#!/usr/bin/env bash
#
# Build and deploy Red Hat Developer Hub directly with `oc` (non-GitOps path).
# Useful for local iteration; the GitOps path (gitops/bootstrap.sh) is the
# recommended way to deploy the full demo stack.
#
# Prerequisites:
#   - Keycloak must already be running (keycloak/build-and-deploy.sh)
#   - The AI agent components should be deployed (build-and-deploy-all.sh)
#
# Usage:
#   ./developer-hub/build-and-deploy.sh
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
NAMESPACE="${NAMESPACE:-rhdh}"
OPERATOR_NS="rhdh-operator"

echo "==> Checking cluster login..."
oc whoami >/dev/null || { echo "error: not logged in (run 'oc login' first)"; exit 1; }

echo "==> Ensuring namespaces exist..."
oc get namespace "${NAMESPACE}" >/dev/null 2>&1 || oc create namespace "${NAMESPACE}"
oc get namespace "${OPERATOR_NS}" >/dev/null 2>&1 || oc create namespace "${OPERATOR_NS}"

echo "==> Applying namespace and network policy..."
oc apply -f "${SCRIPT_DIR}/k8s/namespace.yaml"
oc apply -f "${SCRIPT_DIR}/k8s/instance/network-policy.yaml"

echo "==> Applying RHDH operator resources (OperatorGroup, Subscription)..."
oc apply -f "${SCRIPT_DIR}/k8s/operator/operatorgroup.yaml"
oc apply -f "${SCRIPT_DIR}/k8s/operator/subscription.yaml"

echo "==> Applying approve-installplan Job..."
oc apply -f "${SCRIPT_DIR}/k8s/operator/approve-installplan.yaml"

echo "==> Waiting for RHDH operator to be installed..."
until oc get csv -n "${OPERATOR_NS}" 2>/dev/null | grep -q "rhdh-operator.v1.10.6"; do
  echo "  Waiting for CSV rhdh-operator.v1.10.6..."
  sleep 15
done
echo "  RHDH operator CSV found."

echo "==> Applying RHDH instance prerequisites (ConfigMaps, Secrets, RBAC)..."
oc apply -f "${SCRIPT_DIR}/k8s/instance/rhdh-secrets.yaml"
oc apply -f "${SCRIPT_DIR}/k8s/instance/rbac-policy.yaml"
oc apply -f "${SCRIPT_DIR}/k8s/instance/dynamic-plugins-configmap.yaml"
oc apply -f "${SCRIPT_DIR}/k8s/instance/kubernetes-reader.yaml"

echo "==> Creating/updating rhdh-catalog ConfigMap from catalog/all.yaml..."
oc create configmap rhdh-catalog \
  --from-file=all.yaml="${SCRIPT_DIR}/k8s/catalog/all.yaml" \
  -n "${NAMESPACE}" --dry-run=client -o yaml | oc apply -f -

echo "==> Applying app-config ConfigMap..."
oc apply -f "${SCRIPT_DIR}/k8s/instance/app-config-configmap.yaml"

echo "==> Applying ConsoleLink..."
oc apply -f "${SCRIPT_DIR}/k8s/instance/consolelink.yaml" || \
  echo "  WARNING: ConsoleLink CRD not available — skipping (not OpenShift?)"

echo "==> Applying Backstage CR..."
oc apply -f "${SCRIPT_DIR}/k8s/instance/backstage.yaml"

echo "==> Applying configure-rhdh Job..."
if oc get namespace openshift-gitops >/dev/null 2>&1; then
  oc apply -f "${SCRIPT_DIR}/k8s/jobs/configure-rhdh.yaml"
else
  echo "  openshift-gitops not found — skipping ArgoCD RBAC (Role/RoleBinding in that namespace)"
  oc apply -f "${SCRIPT_DIR}/k8s/jobs/configure-rhdh.yaml" || true
fi

echo "==> Waiting for configure-rhdh Job to complete..."
oc wait --for=condition=complete job/job-configure-rhdh -n "${NAMESPACE}" \
  --timeout=1800s || {
  echo "  WARNING: Job did not complete in time — check 'oc logs job/job-configure-rhdh -n ${NAMESPACE}'"
}

RHDH_ROUTE=$(oc get route -n "${NAMESPACE}" --no-headers 2>/dev/null \
  | grep backstage | awk '{print $2}' | head -1 || true)
echo ""
echo "==> Red Hat Developer Hub deployed in namespace ${NAMESPACE}."
if [[ -n "${RHDH_ROUTE}" ]]; then
  echo "  RHDH URL: https://${RHDH_ROUTE}"
fi
echo ""
echo "  Users: admin / password"
echo "         developer / password"
echo ""
echo "  NOTE: It may take a few minutes for the RHDH pod to become ready."
echo "  Watch with: oc get pods -n ${NAMESPACE} -w"
