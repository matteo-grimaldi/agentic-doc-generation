#!/usr/bin/env bash
#
# Build and deploy the openapi-gen agent (Gradio UI + OGX sidecar) directly
# with `oc` (no GitOps). Useful for local iteration; the GitOps path
# (gitops/bootstrap.sh) is the recommended way to deploy the full demo.
#
# This component shares the cluster-config ConfigMap and
# external-model-credentials Secret with the other agents (same in-cluster
# AI Gateway). configure-agents.sh applies the shared committed configuration,
# preserving installer-managed URLs, and generates canonical stack/knowledge maps.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
NAMESPACE="${NAMESPACE:-doc-gen-demo}"
IMAGE_NAME="openapi-gen-agent"

# shellcheck source=../scripts/lib-openshift-binary-build.sh
source "${REPO_ROOT}/scripts/lib-openshift-binary-build.sh"
# shellcheck source=../scripts/lib-ogx-sidecar-deploy.sh
source "${REPO_ROOT}/scripts/lib-ogx-sidecar-deploy.sh"

echo "==> Ensuring namespace ${NAMESPACE} exists..."
oc get namespace "${NAMESPACE}" >/dev/null 2>&1 || oc create namespace "${NAMESPACE}"
oc label namespace "${NAMESPACE}" opendatahub.io/dashboard=true modelmesh-enabled=false --overwrite >/dev/null

echo "==> Generating shared stack and agent knowledge configuration..."
bash "${REPO_ROOT}/scripts/configure-agents.sh" "${IMAGE_NAME}"
oc apply -f "${SCRIPT_DIR}/k8s/rbac.yaml" -n "${NAMESPACE}"

if [[ "${PREPARE_ONLY:-false}" != true ]]; then
  mcp_url=$(oc get configmap cluster-config -n "${NAMESPACE}" -o jsonpath='{.data.mcp_gateway_url}')
  if [[ -z "${mcp_url}" ]]; then
    echo "error: provision MCP gateway first (bash mcp-gateway/build-and-deploy.sh), or use PREPARE_ONLY=true" >&2
    exit 1
  fi
fi

ensure_ogx_mlflow_sidecar "${NAMESPACE}" "${REPO_ROOT}"

echo "==> NOTE: model_base_url in doc-gen-agent/k8s/cluster-config.yaml should be the"
echo "    in-cluster AI Gateway /v1 Service URL; EXTERNAL_MODEL_API_KEY is a MaaS sk-oai- key."
echo "    Run UPSTREAM_MODEL_API_KEY=... ./install-openshift-ai.sh first."

echo "==> Building image with OpenShift BuildConfig (binary build)..."
oc project "${NAMESPACE}" >/dev/null
run_openshift_binary_build "${IMAGE_NAME}" "${SCRIPT_DIR}" "${NAMESPACE}"

# oc new-build leaves lookupPolicy.local=false, so kubelet always pulls from the
# internal registry (and needs a valid dockercfg). Local lookup uses the image
# already on the node from the build push and avoids registry auth failures.
echo "==> Enabling local image lookup on ImageStream ${IMAGE_NAME}..."
oc patch imagestream "${IMAGE_NAME}" -n "${NAMESPACE}" --type=merge \
  -p '{"spec":{"lookupPolicy":{"local":true}}}'

if [[ "${PREPARE_ONLY:-false}" == true ]]; then
  echo "==> ${IMAGE_NAME}: image/configuration prepared; workload deployment skipped."
  exit 0
fi

echo "==> Applying Deployment, Service, and Route..."
oc apply -f "${SCRIPT_DIR}/k8s/deployment.yaml" -n "${NAMESPACE}"
oc apply -f "${SCRIPT_DIR}/k8s/route.yaml" -n "${NAMESPACE}"

maybe_apply_ogx_mlflow_sidecar_image openapi-gen-agent "${NAMESPACE}"

# A rebuilt :latest tag or changed ConfigMap does not change the pod template.
# Normal deployment explicitly replaces pods; PREPARE_ONLY has already exited.
oc rollout restart deployment/"${IMAGE_NAME}" -n "${NAMESPACE}"

echo "==> Waiting for rollout..."
oc rollout status deployment/openapi-gen-agent -n "${NAMESPACE}" --timeout=300s

ROUTE_HOST=$(oc get route openapi-gen-agent -n "${NAMESPACE}" -o jsonpath='https://{.spec.host}' 2>/dev/null || true)
echo "==> Done. openapi-gen-agent is running in namespace ${NAMESPACE}."
if [[ -n "${ROUTE_HOST}" ]]; then
  echo "    UI: ${ROUTE_HOST}"
fi
