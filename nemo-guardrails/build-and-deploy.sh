#!/usr/bin/env bash
# Build and deploy the optional shared NeMo Guardrails policy service.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
NAMESPACE="${NAMESPACE:-doc-gen-demo}"
IMAGE_NAME="nemo-guardrails"

# shellcheck source=../scripts/lib-openshift-binary-build.sh
source "${REPO_ROOT}/scripts/lib-openshift-binary-build.sh"

oc get namespace "${NAMESPACE}" >/dev/null 2>&1 || oc create namespace "${NAMESPACE}"
oc project "${NAMESPACE}" >/dev/null
run_openshift_binary_build "${IMAGE_NAME}" "${SCRIPT_DIR}" "${NAMESPACE}"
oc patch imagestream "${IMAGE_NAME}" -n "${NAMESPACE}" --type=merge \
  -p '{"spec":{"lookupPolicy":{"local":true}}}'

if [[ "${PREPARE_ONLY:-false}" == true ]]; then
  echo "==> ${IMAGE_NAME}: image prepared; workload deployment skipped."
  exit 0
fi

oc apply -f "${SCRIPT_DIR}/k8s/deployment.yaml" -n "${NAMESPACE}"
# The Deployment references the ImageStream tag, so an apply alone does not
# change its Pod template after a new image is built. Restart to load it.
oc rollout restart deployment/${IMAGE_NAME} -n "${NAMESPACE}"
oc rollout status deployment/${IMAGE_NAME} -n "${NAMESPACE}" --timeout=300s
