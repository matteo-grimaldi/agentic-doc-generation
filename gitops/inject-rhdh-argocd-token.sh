#!/usr/bin/env bash
# Push the OpenShift GitOps API token into RHDH's rhdh-secrets so the Argo CD
# plugin can list Applications. Run after gitops/sync-rhdh-argocd-secret.sh
# (or any time RHDH_ARGOCD_TOKEN is empty in rhdh/rhdh-secrets).
set -euo pipefail

GITOPS_NS="${GITOPS_NS:-openshift-gitops}"
RHDH_NS="${RHDH_NS:-rhdh}"
TOKEN_SECRET="${TOKEN_SECRET:-rhdh-local-user}"
RHDH_SECRET="${RHDH_SECRET:-rhdh-secrets}"

if ! oc get secret "${TOKEN_SECRET}" -n "${GITOPS_NS}" >/dev/null 2>&1; then
  echo "error: ${GITOPS_NS}/${TOKEN_SECRET} not found — run gitops/sync-rhdh-argocd-secret.sh first" >&2
  exit 1
fi

TOKEN="$(oc get secret "${TOKEN_SECRET}" -n "${GITOPS_NS}" \
  -o go-template='{{index .data "apiToken" | base64decode}}')"
if [[ -z "${TOKEN}" ]]; then
  echo "error: ${TOKEN_SECRET} has no apiToken" >&2
  exit 1
fi

ARGOCD_URL="$(oc get route openshift-gitops-server -n "${GITOPS_NS}" \
  -o jsonpath='https://{.spec.host}' 2>/dev/null || true)"

PATCH=$(python3 -c "import json,sys; print(json.dumps({'stringData':{'RHDH_ARGOCD_TOKEN':sys.argv[1], **({'RHDH_ARGOCD_URL':sys.argv[2]} if sys.argv[2] else {})}}))" \
  "${TOKEN}" "${ARGOCD_URL}")

oc patch secret "${RHDH_SECRET}" -n "${RHDH_NS}" --type merge -p "${PATCH}"

if oc get deploy backstage-developer-hub -n "${RHDH_NS}" >/dev/null 2>&1; then
  echo "==> Restarting RHDH so the backend reloads Argo CD credentials..."
  oc rollout restart deployment/backstage-developer-hub -n "${RHDH_NS}"
  oc rollout status deployment/backstage-developer-hub -n "${RHDH_NS}" --timeout=300s
fi

echo "==> Updated ${RHDH_NS}/${RHDH_SECRET} (RHDH_ARGOCD_TOKEN). Open RHDH → Argo CD after rollout completes."
