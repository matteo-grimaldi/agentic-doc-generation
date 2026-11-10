#!/usr/bin/env bash
#
# Deploy the ci-cd-pipeline demo (OpenShift Pipelines resources) directly
# with `oc` (no GitOps). Useful for local iteration; the GitOps path
# (gitops/bootstrap.sh + gitops/apps/ci-cd-pipeline.yaml) is the
# recommended way to deploy the full demo.
#
# Prerequisite: the OpenShift Pipelines operator must already be installed
# (run ./install-operator.sh first) — its CRDs must exist before any of
# the manifests below can be applied.
#
# Pipeline: clone → Maven package → compliance-scan /invoke.
# No Quay and no application deploy step.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
NAMESPACE="${NAMESPACE:-ci-cd-demo}"

echo "==> Checking cluster login..."
oc whoami >/dev/null || { echo "error: not logged into an OpenShift cluster (run 'oc login' first)"; exit 1; }

echo "==> Checking that the OpenShift Pipelines CRDs are registered..."
if ! oc get crd pipelines.tekton.dev >/dev/null 2>&1; then
  echo "error: Tekton CRDs not found — run ./install-operator.sh first." >&2
  exit 1
fi

echo "==> Applying namespace and RBAC..."
oc apply -f "${SCRIPT_DIR}/k8s/namespace.yaml"
oc apply -f "${SCRIPT_DIR}/k8s/rbac.yaml" -n "${NAMESPACE}"

echo "==> Applying Tasks..."
oc apply -f "${SCRIPT_DIR}/k8s/tasks/" -n "${NAMESPACE}"

echo "==> Applying Pipeline..."
oc apply -f "${SCRIPT_DIR}/k8s/pipeline.yaml" -n "${NAMESPACE}"

echo "==> Done. Pipeline 'build-deploy-app' is ready in namespace ${NAMESPACE}."
echo ""
echo "    Start a run (build + agent invoke, no deploy):"
echo "      oc create -f ${SCRIPT_DIR}/pipelinerun-example.yaml -n ${NAMESPACE}"
echo ""
echo "    Watch progress with:"
echo "      tkn pipelinerun logs -f -n ${NAMESPACE}"
