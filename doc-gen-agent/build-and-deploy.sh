#!/usr/bin/env bash
#
# Build and deploy the doc-gen agent (Gradio UI + OGX sidecar) directly
# with `oc` (no GitOps). Useful for local iteration; the GitOps path
# (gitops/bootstrap.sh) is the recommended way to deploy the full demo.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
NAMESPACE="${NAMESPACE:-doc-gen-demo}"
IMAGE_NAME="doc-gen-agent"

echo "==> Ensuring namespace ${NAMESPACE} exists..."
oc get namespace "${NAMESPACE}" >/dev/null 2>&1 || oc create namespace "${NAMESPACE}"
oc label namespace "${NAMESPACE}" opendatahub.io/dashboard=true modelmesh-enabled=false --overwrite >/dev/null

echo "==> Applying cluster-config ConfigMap and RBAC..."
oc apply -f "${SCRIPT_DIR}/k8s/cluster-config.yaml" -n "${NAMESPACE}"
oc apply -f "${SCRIPT_DIR}/k8s/rbac.yaml" -n "${NAMESPACE}"

echo "==> Creating/updating ogx-stack-config ConfigMap from ogx/stack_run_config.yaml..."
oc create configmap ogx-stack-config \
  --from-file=stack_run_config.yaml="${SCRIPT_DIR}/ogx/stack_run_config.yaml" \
  -n "${NAMESPACE}" --dry-run=client -o yaml | oc apply -f -

echo "==> Creating/updating agent-knowledge ConfigMap from knowledge.md..."
oc create configmap agent-knowledge \
  --from-file=knowledge.md="${SCRIPT_DIR}/knowledge.md" \
  -n "${NAMESPACE}" --dry-run=client -o yaml | oc apply -f -

if [[ -n "${EXTERNAL_MODEL_API_KEY:-}" ]]; then
  echo "==> Creating/updating external-model-credentials secret from \$EXTERNAL_MODEL_API_KEY..."
  oc create secret generic external-model-credentials --from-literal=api_key="${EXTERNAL_MODEL_API_KEY}" \
    -n "${NAMESPACE}" --dry-run=client -o yaml | oc apply -f -
else
  echo "==> \$EXTERNAL_MODEL_API_KEY not set — skipping secret creation (fine for endpoints that don't require auth)."
fi

echo "==> NOTE: edit k8s/cluster-config.yaml (model_base_url, model_name) and"
echo "    ogx/stack_run_config.yaml (model_id/provider_model_id) to point at"
echo "    your external OpenAI-compatible endpoint before/after this deploy."

echo "==> Building image with OpenShift BuildConfig (binary build)..."
oc project "${NAMESPACE}" >/dev/null
if ! oc get imagestream "${IMAGE_NAME}" -n "${NAMESPACE}" >/dev/null 2>&1; then
  oc new-build --name="${IMAGE_NAME}" --binary --strategy=docker -n "${NAMESPACE}"
fi
oc start-build "${IMAGE_NAME}" --from-dir="${SCRIPT_DIR}" --follow -n "${NAMESPACE}"

echo "==> Applying Deployment, Service, and Route..."
oc apply -f "${SCRIPT_DIR}/k8s/deployment.yaml" -n "${NAMESPACE}"
oc apply -f "${SCRIPT_DIR}/k8s/route.yaml" -n "${NAMESPACE}"

echo "==> Waiting for rollout..."
oc rollout status deployment/doc-gen-agent -n "${NAMESPACE}" --timeout=300s

ROUTE_HOST=$(oc get route doc-gen-agent -n "${NAMESPACE}" -o jsonpath='https://{.spec.host}' 2>/dev/null || true)
echo "==> Done. doc-gen-agent is running in namespace ${NAMESPACE}."
if [[ -n "${ROUTE_HOST}" ]]; then
  echo "    UI: ${ROUTE_HOST}"
fi
