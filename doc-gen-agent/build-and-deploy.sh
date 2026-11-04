#!/usr/bin/env bash
#
# Build and deploy the doc-gen agent (Gradio UI + OGX sidecar) directly
# with `oc` (no GitOps). Useful for local iteration; the GitOps path
# (gitops/bootstrap.sh) is the recommended way to deploy the full demo.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
NAMESPACE="${NAMESPACE:-doc-gen-demo}"
IMAGE_NAME="doc-gen-agent"

# shellcheck source=../scripts/lib-openshift-binary-build.sh
source "${REPO_ROOT}/scripts/lib-openshift-binary-build.sh"
# shellcheck source=../scripts/lib-mlflow-credentials.sh
source "${REPO_ROOT}/scripts/lib-mlflow-credentials.sh"
# shellcheck source=../scripts/lib-ogx-sidecar-deploy.sh
source "${REPO_ROOT}/scripts/lib-ogx-sidecar-deploy.sh"

echo "==> Ensuring namespace ${NAMESPACE} exists..."
oc get namespace "${NAMESPACE}" >/dev/null 2>&1 || oc create namespace "${NAMESPACE}"
oc label namespace "${NAMESPACE}" opendatahub.io/dashboard=true modelmesh-enabled=false --overwrite >/dev/null

echo "==> Applying cluster-config ConfigMap and RBAC..."
oc apply -f "${SCRIPT_DIR}/k8s/cluster-config.yaml" -n "${NAMESPACE}"
oc apply -f "${SCRIPT_DIR}/k8s/rbac.yaml" -n "${NAMESPACE}"

ensure_mlflow_credentials_secret "${NAMESPACE}" "${REPO_ROOT}"
ensure_ogx_mlflow_sidecar "${NAMESPACE}" "${REPO_ROOT}"

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
  echo "==> \$EXTERNAL_MODEL_API_KEY not set — skipping secret creation (install-openshift-ai.sh should have minted a MaaS key into external-model-credentials)."
fi

echo "==> NOTE: model_base_url in k8s/cluster-config.yaml should be the in-cluster"
echo "    AI Gateway /v1 Service URL; EXTERNAL_MODEL_API_KEY is a MaaS sk-oai- key."
echo "    Run UPSTREAM_MODEL_API_KEY=... ./install-openshift-ai.sh first."
echo "==> MLflow OGX sidecar: default Quay (${OGX_MLFLOW_SIDECAR_IMAGE:-quay.io/mgrimald/ogx-mlflow-sidecar:latest})"
echo "    Use OGX_MLFLOW_SIDECAR_MODE=cluster for in-cluster ImageStream build."

echo "==> Building image with OpenShift BuildConfig (binary build)..."
oc project "${NAMESPACE}" >/dev/null
run_openshift_binary_build "${IMAGE_NAME}" "${SCRIPT_DIR}" "${NAMESPACE}"

# oc new-build leaves lookupPolicy.local=false, so kubelet always pulls from the
# internal registry (and needs a valid dockercfg). Local lookup uses the image
# already on the node from the build push and avoids registry auth failures.
echo "==> Enabling local image lookup on ImageStream ${IMAGE_NAME}..."
oc patch imagestream "${IMAGE_NAME}" -n "${NAMESPACE}" --type=merge \
  -p '{"spec":{"lookupPolicy":{"local":true}}}'

echo "==> Applying Deployment, Service, and Route..."
oc apply -f "${SCRIPT_DIR}/k8s/deployment.yaml" -n "${NAMESPACE}"
oc apply -f "${SCRIPT_DIR}/k8s/route.yaml" -n "${NAMESPACE}"

maybe_apply_ogx_mlflow_sidecar_image doc-gen-agent "${NAMESPACE}"

echo "==> Waiting for rollout..."
oc rollout status deployment/doc-gen-agent -n "${NAMESPACE}" --timeout=300s

ROUTE_HOST=$(oc get route doc-gen-agent -n "${NAMESPACE}" -o jsonpath='https://{.spec.host}' 2>/dev/null || true)
echo "==> Done. doc-gen-agent is running in namespace ${NAMESPACE}."
if [[ -n "${ROUTE_HOST}" ]]; then
  echo "    UI: ${ROUTE_HOST}"
fi
