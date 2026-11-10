#!/usr/bin/env bash
#
# One-time cluster prerequisite for the ci-cd-pipeline demo: installs the
# Red Hat OpenShift Pipelines operator (Tekton) if it isn't already
# present, and waits for its CRDs to be registered. Run this once, before
# ci-cd-pipeline/build-and-deploy.sh (or before letting ArgoCD sync
# gitops/apps/ci-cd-pipeline.yaml) — ArgoCD/oc can't apply Pipeline/Task
# resources before the CRDs that define them exist.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

echo "==> Checking cluster login..."
oc whoami >/dev/null || { echo "error: not logged into an OpenShift cluster (run 'oc login' first)"; exit 1; }

echo "==> Installing OpenShift Pipelines operator (if needed)..."
if ! oc get csv -n openshift-operators 2>/dev/null | grep -q openshift-pipelines-operator; then
  oc apply -f "${SCRIPT_DIR}/openshift-pipelines-subscription.yaml"
  echo "==> Waiting for OpenShift Pipelines operator to be installed..."
  until oc get csv -n openshift-operators 2>/dev/null | grep -q openshift-pipelines-operator; do
    sleep 5
  done
else
  echo "    OpenShift Pipelines operator already installed."
fi

echo "==> Waiting for Tekton CRDs to be registered..."
until oc get crd pipelines.tekton.dev >/dev/null 2>&1 && oc get crd tasks.tekton.dev >/dev/null 2>&1; do
  sleep 5
done

# The operator deploys ConsolePlugin/pipelines-console-plugin, but the
# cluster Console must list it under spec.plugins before the Pipelines
# nav appears in the OpenShift web console.
echo "==> Enabling Pipelines console plugin in the OpenShift Console..."
until oc get consoleplugin pipelines-console-plugin >/dev/null 2>&1; do
  echo "  Waiting for ConsolePlugin/pipelines-console-plugin..."
  sleep 5
done
ENABLED=$(oc get consoles.operator.openshift.io cluster \
  -o jsonpath='{.spec.plugins[*]}' 2>/dev/null || true)
if echo " ${ENABLED} " | grep -q ' pipelines-console-plugin '; then
  echo "    pipelines-console-plugin already enabled on Console."
else
  # Merge into existing plugins list (preserve odf/monitoring/etc.).
  oc get consoles.operator.openshift.io cluster -o json \
    | python3 -c '
import json,sys
obj=json.load(sys.stdin)
plugins=list(obj.get("spec",{}).get("plugins") or [])
if "pipelines-console-plugin" not in plugins:
    plugins.append("pipelines-console-plugin")
print(json.dumps({"spec":{"plugins":plugins}}))
' | oc patch consoles.operator.openshift.io cluster --type=merge --patch-file=/dev/stdin
  echo "    Enabled pipelines-console-plugin on Console."
fi

echo "==> Done. OpenShift Pipelines is ready (operator + console UI)."
echo "    Next: ./ci-cd-pipeline/build-and-deploy.sh"
echo "    Console: refresh the OpenShift web console — Pipelines should appear in the nav."
