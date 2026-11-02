#!/usr/bin/env bash
# Copy the OpenShift GitOps operator's local-user Secret to the name RHDH
# expects (openshift-gitops/rhdh-local-user, key apiToken). The operator
# creates rhdh-local-user-local-user when spec.localUsers includes
# rhdh-local-user.
set -euo pipefail

NAMESPACE="${NAMESPACE:-openshift-gitops}"
SOURCE="${1:-rhdh-local-user-local-user}"
TARGET="${2:-rhdh-local-user}"

if ! oc get secret "${SOURCE}" -n "${NAMESPACE}" >/dev/null 2>&1; then
  echo "error: secret ${SOURCE} not found in ${NAMESPACE} (apply gitops/argocd-instance-demo.yaml first)" >&2
  exit 1
fi

TOKEN_B64="$(oc get secret "${SOURCE}" -n "${NAMESPACE}" -o jsonpath='{.data.apiToken}')"
if [[ -z "${TOKEN_B64}" ]]; then
  echo "error: ${SOURCE} has no apiToken key" >&2
  exit 1
fi

oc create secret generic "${TARGET}" \
  --from-literal=apiToken="$(printf '%s' "${TOKEN_B64}" | base64 -d)" \
  -n "${NAMESPACE}" --dry-run=client -o yaml | oc apply -f -
echo "==> Synced ${TARGET} from operator Secret ${SOURCE} in ${NAMESPACE}."
