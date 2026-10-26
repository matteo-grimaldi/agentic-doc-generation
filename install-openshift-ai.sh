#!/usr/bin/env bash
#
# One-time cluster prerequisite: installs Red Hat OpenShift AI (latest,
# `stable-3.x` channel) with AI Hub (Models/MCPs/Agents), AI Studio
# (Workbenches/AI Pipelines), and MLFlow enabled.
#
# This is a platform-level operator installation, run imperatively (like
# ci-cd-pipeline/install-operator.sh and gitops/bootstrap.sh) because
# ArgoCD/oc can't apply DataScienceCluster/DSCInitialization custom
# resources before their CRDs (registered by the rhods-operator CSV)
# exist. It is independent of this repo's doc-gen-demo GitOps app-of-apps
# tree — nothing here wires the demo agents to OpenShift AI.
#
# Usage:
#   ./install-openshift-ai.sh
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

echo "==> Checking cluster login..."
oc whoami >/dev/null || { echo "error: not logged into an OpenShift cluster (run 'oc login' first)"; exit 1; }

# ---------------------------------------------------------------------------
# Step 1 -- Red Hat OpenShift Serverless operator (KServe/Knative dependency)
# ---------------------------------------------------------------------------
echo "==> Installing Red Hat OpenShift Serverless operator (if needed)..."
if ! oc get csv -n openshift-operators 2>/dev/null | grep -q serverless-operator; then
  cat <<'EOF' | oc apply -f -
apiVersion: operators.coreos.com/v1alpha1
kind: Subscription
metadata:
  name: serverless-operator
  namespace: openshift-operators
spec:
  channel: stable
  name: serverless-operator
  source: redhat-operators
  sourceNamespace: openshift-marketplace
  installPlanApproval: Automatic
EOF
  echo "==> Waiting for OpenShift Serverless operator to be installed..."
  until oc get csv -n openshift-operators 2>/dev/null | grep -q serverless-operator; do
    sleep 5
  done
else
  echo "    OpenShift Serverless operator already installed."
fi

echo "==> Waiting for Serverless CSV to reach Succeeded..."
until [[ "$(oc get csv -n openshift-operators -o jsonpath='{.items[?(@.spec.displayName=="Red Hat OpenShift Serverless")].status.phase}' 2>/dev/null)" == "Succeeded" ]]; do
  sleep 5
done

echo "==> Waiting for Knative Serving CRDs to be registered..."
until oc get crd knativeservings.operator.knative.dev >/dev/null 2>&1; do
  sleep 5
done

echo "==> Creating KnativeServing instance (if needed)..."
oc get namespace knative-serving >/dev/null 2>&1 || oc create namespace knative-serving
if ! oc get knativeserving knative-serving -n knative-serving >/dev/null 2>&1; then
  cat <<'EOF' | oc apply -f -
apiVersion: operator.knative.dev/v1beta1
kind: KnativeServing
metadata:
  name: knative-serving
  namespace: knative-serving
spec: {}
EOF
else
  echo "    KnativeServing instance already exists."
fi

# ---------------------------------------------------------------------------
# Step 2 -- Red Hat OpenShift AI operator (stable-3.x channel)
# ---------------------------------------------------------------------------
echo "==> Installing Red Hat OpenShift AI operator (stable-3.x, if needed)..."
if ! oc get csv -n openshift-operators 2>/dev/null | grep -q rhods-operator; then
  cat <<'EOF' | oc apply -f -
apiVersion: operators.coreos.com/v1alpha1
kind: Subscription
metadata:
  name: rhods-operator
  namespace: openshift-operators
spec:
  channel: stable-3.x
  name: rhods-operator
  source: redhat-operators
  sourceNamespace: openshift-marketplace
  installPlanApproval: Automatic
EOF
  echo "==> Waiting for Red Hat OpenShift AI operator to be installed..."
  until oc get csv -n openshift-operators 2>/dev/null | grep -q rhods-operator; do
    sleep 5
  done
else
  echo "    Red Hat OpenShift AI operator already installed."
fi

echo "==> Waiting for RHOAI operator CSV to reach Succeeded..."
until [[ "$(oc get csv -n openshift-operators -o jsonpath='{.items[?(@.spec.displayName=="Red Hat OpenShift AI")].status.phase}' 2>/dev/null)" == "Succeeded" ]]; do
  sleep 5
done

echo "==> Waiting for DataScienceCluster/DSCInitialization CRDs to be registered..."
until oc get crd datascienceclusters.datasciencecluster.opendatahub.io >/dev/null 2>&1 \
   && oc get crd dscinitializations.dscinitialization.opendatahub.io >/dev/null 2>&1; do
  sleep 5
done

# ---------------------------------------------------------------------------
# Step 3 -- DSCInitialization
# ---------------------------------------------------------------------------
echo "==> Applying DSCInitialization (default-dsci)..."
cat <<'EOF' | oc apply -f -
apiVersion: dscinitialization.opendatahub.io/v2
kind: DSCInitialization
metadata:
  name: default-dsci
  labels:
    app.kubernetes.io/created-by: rhods-operator
    app.kubernetes.io/instance: default-dsci
    app.kubernetes.io/name: dscinitialization
    app.kubernetes.io/part-of: rhods-operator
spec:
  applicationsNamespace: redhat-ods-applications
  monitoring:
    managementState: Managed
    metrics: {}
    namespace: redhat-ods-monitoring
  trustedCABundle:
    customCABundle: ""
    managementState: Managed
EOF

# ---------------------------------------------------------------------------
# Step 4 -- DataScienceCluster with AI Hub, AI Studio, and MLFlow enabled
# ---------------------------------------------------------------------------
echo "==> Applying DataScienceCluster (default-dsc) with AI Hub, AI Studio, and MLFlow..."
cat <<'EOF' | oc apply -f -
apiVersion: datasciencecluster.opendatahub.io/v1
kind: DataScienceCluster
metadata:
  name: default-dsc
  labels:
    app.kubernetes.io/created-by: rhods-operator
    app.kubernetes.io/instance: default-dsc
    app.kubernetes.io/name: datasciencecluster
    app.kubernetes.io/part-of: rhods-operator
spec:
  components:
    # -- AI Hub: see/register Models, MCPs, and Agents --
    dashboard:
      managementState: Managed
    kserve:
      managementState: Managed
      modelsAsService:
        managementState: Managed
      nim:
        managementState: Managed
    mcplifecycleoperator:
      managementState: Managed
    ogx:
      managementState: Managed
    modelregistry:
      managementState: Managed
      registriesNamespace: rhoai-model-registries
    # -- AI Studio: notebooks + pipelines --
    workbenches:
      managementState: Managed
    aipipelines:
      managementState: Managed
    # -- MLFlow --
    mlflowoperator:
      managementState: Managed
    # -- other components: sensible defaults for this demo cluster --
    ray:
      managementState: Managed
    trainer:
      managementState: Managed
    trustyai:
      managementState: Managed
    kueue:
      managementState: Removed
    feastoperator:
      managementState: Removed
    llamastackoperator:
      managementState: Removed
    sparkoperator:
      managementState: Removed
    aigateway:
      managementState: Removed
EOF

# ---------------------------------------------------------------------------
# Step 5 -- Wait for reconciliation and report
# ---------------------------------------------------------------------------
echo "==> Waiting for DataScienceCluster to report Ready..."
until [[ "$(oc get datasciencecluster default-dsc -o jsonpath='{.status.phase}' 2>/dev/null)" == "Ready" ]]; do
  sleep 10
done

echo "==> Waiting for RHOAI dashboard route..."
until oc get route rhods-dashboard -n redhat-ods-applications >/dev/null 2>&1; do
  sleep 5
done

# ---------------------------------------------------------------------------
# Step 6 -- Enable all opt-in Developer Preview dashboard feature flags
#
# Note: dashboardConfig also has a family of `disable*` flags (disableHome,
# disableProjects, disableKServe, etc.) — those are inverted toggles that
# turn OFF stable features and are intentionally left untouched here.
# `mlflow` and `maasAuthPolicies` are deprecated flags the CRD's validation
# rules reject any change to (must be removed or left unchanged), so they
# are omitted from this patch.
# ---------------------------------------------------------------------------
echo "==> Waiting for OdhDashboardConfig (odh-dashboard-config) to exist..."
until oc get odhdashboardconfig odh-dashboard-config -n redhat-ods-applications >/dev/null 2>&1; do
  sleep 5
done

echo "==> Enabling Developer Preview dashboard feature flags..."
cat <<'EOF' > /tmp/odh-dashboard-feature-flags-patch.json
{
  "spec": {
    "dashboardConfig": {
      "agentConfigManagement": true,
      "agentOps": true,
      "agentsCatalog": true,
      "aiAssetCustomEndpoints": true,
      "automl": true,
      "autorag": true,
      "connectionTest": true,
      "deploymentWizardYAMLViewer": true,
      "enablement": true,
      "externalModels": true,
      "externalVectorStores": true,
      "featureStoreAdmin": true,
      "genAiStudio": true,
      "genAiTracing": true,
      "globalProjectPrompts": true,
      "gpuaas": true,
      "guardrails": true,
      "llmGatewayField": true,
      "llmdTemplates": true,
      "mcpCatalog": true,
      "mcpRegistry": true,
      "modelAsService": true,
      "observabilityDashboard": true,
      "projectRBAC": true,
      "promptManagement": true,
      "roleManagement": true,
      "toolCalling": true,
      "trainingJobs": true,
      "vLLMDeploymentOnMaaS": true
    }
  }
}
EOF
oc patch odhdashboardconfig odh-dashboard-config -n redhat-ods-applications \
  --type=merge -p "$(cat /tmp/odh-dashboard-feature-flags-patch.json)"
rm -f /tmp/odh-dashboard-feature-flags-patch.json

# ---------------------------------------------------------------------------
# Step 7 -- Default MLflow tracking server instance
# ---------------------------------------------------------------------------
# The mlflowoperator component (Step 4) only installs the operator; it does
# not provision an actual MLflow tracking server. Dashboard screens that
# depend on one (Experiments, Prompts) stay empty until an `Mlflow` custom
# resource exists. `Mlflow` is cluster-scoped (one tracking server for the
# whole cluster), so this creates a single default instance backed by
# SQLite + a PVC -- sufficient for this demo cluster, not meant for HA/
# production use (swap backendStoreUri/storage for Postgres + S3 for that).
echo "==> Waiting for Mlflow CRD to be registered..."
until oc get crd mlflows.mlflow.opendatahub.io >/dev/null 2>&1; do
  sleep 5
done

echo "==> Creating default MLflow tracking server instance (if needed)..."
# The webhook requires the singleton resource to be named exactly "mlflow".
if ! oc get mlflow mlflow >/dev/null 2>&1; then
  cat <<'EOF' | oc apply -f -
apiVersion: mlflow.opendatahub.io/v1
kind: MLflow
metadata:
  name: mlflow
spec:
  backendStoreUri: "sqlite:////mlflow/mlflow.db"
  serveArtifacts: true
  storage:
    accessModes: ["ReadWriteOnce"]
    resources:
      requests:
        storage: 10Gi
EOF
else
  echo "    mlflow instance already exists."
fi

echo "==> Done. Red Hat OpenShift AI is installed with AI Hub, AI Studio, and MLFlow enabled."
DASHBOARD_ROUTE=$(oc get route rhods-dashboard -n redhat-ods-applications -o jsonpath='https://{.spec.host}' 2>/dev/null || true)
if [[ -n "${DASHBOARD_ROUTE}" ]]; then
  echo "    OpenShift AI dashboard: ${DASHBOARD_ROUTE}"
fi
echo ""
echo "    Check component status with:"
echo "      oc get datasciencecluster default-dsc -o yaml"
