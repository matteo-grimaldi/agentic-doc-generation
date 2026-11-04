#!/usr/bin/env bash
#
# One-time cluster prerequisite: installs Red Hat OpenShift AI (latest,
# `stable-3.x` channel) with AI Hub (Models/MCPs/Agents), AI Studio
# (Workbenches/AI Pipelines), MLFlow, and AI Gateway / Models-as-a-Service
# so every demo agent reaches the model through the in-cluster gateway
# then runs
# ./install-mcp-gateway.sh for Connectivity Link + the MCP Gateway
# Operator (agents' MCP tool path — not DataScienceCluster aigateway).
#
# This is a platform-level operator installation, run imperatively (like
# ci-cd-pipeline/install-operator.sh and gitops/bootstrap.sh) because
# ArgoCD/oc can't apply DataScienceCluster/DSCInitialization custom
# resources before their CRDs (registered by the rhods-operator CSV)
# exist.
#
# AI Gateway prerequisites (Connectivity Link / Kuadrant, demo Postgres,
# maas-default-gateway, ExternalModel registration) also live here —
# they need cluster-admin and must exist before MaaS is enabled. Reviewable
# YAML is under ai-gateway/k8s/; this script applies it.
#
# Environment:
#   UPSTREAM_MODEL_API_KEY  Required to register the workshop ExternalProvider
#                           and mint a MaaS key for the agents. This is the
#                           upstream provider key (not the sk-oai- MaaS key).
#   EXTERNAL_MODEL_API_KEY  Optional override: if already set, the script
#                           skips minting and leaves your MaaS key alone.
#   NAMESPACE               Agent namespace for external-model-credentials
#                           (default: doc-gen-demo).
# It is independent of this repo's doc-gen-demo GitOps app-of-apps
# tree — nothing here wires the demo agents to OpenShift AI beyond the
# MCPServer / MCP Gateway CRDs those Applications need.
#
# After GitOps syncs Gateway mcp-gateway, re-run ./install-mcp-gateway.sh
# alone if cluster-config mcp_gateway_url is still empty (that script is
# idempotent and safe to call without redoing the RHOAI install).
#
# Usage:
#   UPSTREAM_MODEL_API_KEY=sk-... ./install-openshift-ai.sh
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=scripts/lib-demo-agents.sh
source "${SCRIPT_DIR}/scripts/lib-demo-agents.sh"
AI_GATEWAY_DIR="${SCRIPT_DIR}/ai-gateway/k8s"
NAMESPACE="${NAMESPACE:-${DEMO_NS}}"
INFRA_NAMESPACE="${INFRA_NAMESPACE:-redhat-ai-gateway-infra}"

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
# Step 4 -- DataScienceCluster (AI Hub / AI Studio / MLFlow; AI Gateway later)
#
# Fresh installs omit deprecated kserve.modelsAsService. Re-runs leave that
# field alone when already Managed (immutable through OpenShift AI 3.6).
# aigateway stays Removed until platform prereqs (Step 5) are in place.
# ---------------------------------------------------------------------------
echo "==> Ensuring DataScienceCluster (default-dsc) base components..."
if ! oc get datasciencecluster default-dsc >/dev/null 2>&1; then
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
    dashboard:
      managementState: Managed
    kserve:
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
    workbenches:
      managementState: Managed
    aipipelines:
      managementState: Managed
    mlflowoperator:
      managementState: Managed
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
else
  echo "    default-dsc already exists — leaving kserve.modelsAsService untouched."
  # Ensure core components without rewriting aigateway or modelsAsService yet.
  oc patch datasciencecluster default-dsc --type=merge -p '{
    "spec": {
      "components": {
        "dashboard": {"managementState": "Managed"},
        "kserve": {"managementState": "Managed", "nim": {"managementState": "Managed"}},
        "mcplifecycleoperator": {"managementState": "Managed"},
        "ogx": {"managementState": "Managed"},
        "modelregistry": {"managementState": "Managed", "registriesNamespace": "rhoai-model-registries"},
        "workbenches": {"managementState": "Managed"},
        "aipipelines": {"managementState": "Managed"},
        "mlflowoperator": {"managementState": "Managed"},
        "ray": {"managementState": "Managed"},
        "trainer": {"managementState": "Managed"},
        "trustyai": {"managementState": "Managed"}
      }
    }
  }'
fi

echo "==> Waiting for DataScienceCluster to report Ready..."
until [[ "$(oc get datasciencecluster default-dsc -o jsonpath='{.status.phase}' 2>/dev/null)" == "Ready" ]]; do
  sleep 10
done

_ensure_dsc_component_managed() {
  local component="$1"
  local spec_state status_state
  spec_state="$(oc get datasciencecluster default-dsc \
    -o jsonpath="{.spec.components.${component}.managementState}" 2>/dev/null || true)"
  if [[ "${spec_state}" != "Managed" ]]; then
    echo "    Re-applying ${component}=Managed (spec was ${spec_state:-unset})..."
    oc patch datasciencecluster default-dsc --type=merge \
      -p "{\"spec\":{\"components\":{\"${component}\":{\"managementState\":\"Managed\"}}}}"
  fi
  echo "==> Waiting for ${component} status to report Managed..."
  local waited=0
  while true; do
    status_state="$(oc get datasciencecluster default-dsc \
      -o jsonpath="{.status.components.${component}.managementState}" 2>/dev/null || true)"
    if [[ "${status_state}" == "Managed" ]]; then
      break
    fi
    if (( waited >= 600 )); then
      echo "error: timed out waiting for ${component} to reach Managed."
      echo "       Check: oc get datasciencecluster default-dsc -o yaml | grep -A6 ${component}"
      exit 1
    fi
    sleep 5
    waited=$((waited + 5))
  done
}

for _component in mlflowoperator mcplifecycleoperator ogx; do
  _ensure_dsc_component_managed "${_component}"
done

echo "==> Waiting for RHOAI dashboard route..."
until oc get route rhods-dashboard -n redhat-ods-applications >/dev/null 2>&1; do
  sleep 5
done

# ---------------------------------------------------------------------------
# Step 5 -- AI Gateway platform prerequisites (before enabling MaaS)
# ---------------------------------------------------------------------------
echo "==> Enabling User Workload Monitoring (required by MaaS)..."
if ! oc get namespace openshift-user-workload-monitoring >/dev/null 2>&1; then
  if oc get configmap cluster-monitoring-config -n openshift-monitoring >/dev/null 2>&1; then
    # Merge enableUserWorkload into existing config without clobbering other keys.
    EXISTING_CFG="$(oc get configmap cluster-monitoring-config -n openshift-monitoring \
      -o jsonpath='{.data.config\.yaml}' 2>/dev/null || true)"
    if echo "${EXISTING_CFG}" | grep -q 'enableUserWorkload:\s*true'; then
      echo "    enableUserWorkload already true in cluster-monitoring-config."
    else
      TMP_CMC="$(mktemp)"
      if [[ -n "${EXISTING_CFG}" ]]; then
        printf '%s\n' "${EXISTING_CFG}" > "${TMP_CMC}"
        if grep -q '^enableUserWorkload:' "${TMP_CMC}"; then
          sed -i.bak 's/^enableUserWorkload:.*/enableUserWorkload: true/' "${TMP_CMC}"
          rm -f "${TMP_CMC}.bak"
        else
          printf '\nenableUserWorkload: true\n' >> "${TMP_CMC}"
        fi
      else
        printf 'enableUserWorkload: true\n' > "${TMP_CMC}"
      fi
      oc create configmap cluster-monitoring-config -n openshift-monitoring \
        --from-file=config.yaml="${TMP_CMC}" \
        --dry-run=client -o yaml | oc apply -f -
      rm -f "${TMP_CMC}"
    fi
  else
    cat <<'EOF' | oc apply -f -
apiVersion: v1
kind: ConfigMap
metadata:
  name: cluster-monitoring-config
  namespace: openshift-monitoring
data:
  config.yaml: |
    enableUserWorkload: true
EOF
  fi
  echo "==> Waiting for openshift-user-workload-monitoring namespace..."
  local_uwm_waited=0
  until oc get namespace openshift-user-workload-monitoring >/dev/null 2>&1; do
    if (( local_uwm_waited >= 300 )); then
      echo "error: timed out waiting for openshift-user-workload-monitoring."
      exit 1
    fi
    sleep 5
    local_uwm_waited=$((local_uwm_waited + 5))
  done
else
  echo "    openshift-user-workload-monitoring already present."
fi

echo "==> Installing Red Hat Connectivity Link (rhcl-operator) if needed..."
oc get namespace kuadrant-system >/dev/null 2>&1 || oc create namespace kuadrant-system
if ! oc get subscription rhcl-operator -n openshift-operators >/dev/null 2>&1 \
   && ! oc get subscription rhcl-operator -n kuadrant-system >/dev/null 2>&1; then
  RHCL_CHANNEL="$(oc get packagemanifest rhcl-operator -n openshift-marketplace \
    -o jsonpath='{.status.defaultChannel}' 2>/dev/null || echo stable)"
  cat <<EOF | oc apply -f -
apiVersion: operators.coreos.com/v1alpha1
kind: Subscription
metadata:
  name: rhcl-operator
  namespace: openshift-operators
spec:
  channel: ${RHCL_CHANNEL}
  name: rhcl-operator
  source: redhat-operators
  sourceNamespace: openshift-marketplace
  installPlanApproval: Automatic
EOF
else
  echo "    rhcl-operator Subscription already present."
fi

echo "==> Waiting for Connectivity Link / Kuadrant CSV to succeed..."
_rhcl_waited=0
while true; do
  if oc get csv -A 2>/dev/null | grep -E 'rhcl-operator|kuadrant' | grep -q Succeeded; then
    break
  fi
  if (( _rhcl_waited >= 600 )); then
    echo "error: timed out waiting for rhcl-operator CSV Succeeded."
    exit 1
  fi
  sleep 5
  _rhcl_waited=$((_rhcl_waited + 5))
done

echo "==> Waiting for Kuadrant CRD..."
until oc get crd kuadrants.kuadrant.io >/dev/null 2>&1; do
  sleep 5
done

echo "==> Creating Kuadrant instance in kuadrant-system..."
if ! oc get kuadrant kuadrant -n kuadrant-system >/dev/null 2>&1; then
  cat <<'EOF' | oc apply -f -
apiVersion: kuadrant.io/v1beta1
kind: Kuadrant
metadata:
  name: kuadrant
  namespace: kuadrant-system
EOF
else
  echo "    Kuadrant instance already exists."
fi

echo "==> Waiting for Kuadrant to become Ready (best-effort up to 10m)..."
_kuadrant_waited=0
while true; do
  if oc wait kuadrant/kuadrant -n kuadrant-system --for=condition=Ready --timeout=15s >/dev/null 2>&1; then
    break
  fi
  # Some builds expose Ready via status.conditions differently; accept Ready=True jsonpath.
  _kr="$(oc get kuadrant kuadrant -n kuadrant-system -o jsonpath='{.status.conditions[?(@.type=="Ready")].status}' 2>/dev/null || true)"
  if [[ "${_kr}" == "True" ]]; then
    break
  fi
  if (( _kuadrant_waited >= 600 )); then
    echo "warning: Kuadrant not Ready after 10m — continuing; MaaS may degrade until it is."
    break
  fi
  sleep 5
  _kuadrant_waited=$((_kuadrant_waited + 5))
done

echo "==> Waiting for GatewayClass/openshift-default..."
until oc get gatewayclass openshift-default >/dev/null 2>&1; do
  sleep 5
done

echo "==> Creating infrastructure namespace ${INFRA_NAMESPACE}..."
oc get namespace "${INFRA_NAMESPACE}" >/dev/null 2>&1 || oc create namespace "${INFRA_NAMESPACE}"
oc label namespace "${INFRA_NAMESPACE}" maas.opendatahub.io/gateway-access=true --overwrite >/dev/null

echo "==> Deploying demo Postgres for MaaS API keys..."
oc apply -f "${AI_GATEWAY_DIR}/postgres.yaml"
oc adm policy add-scc-to-user anyuid -z maas-postgres -n "${INFRA_NAMESPACE}" >/dev/null 2>&1 || true
echo "==> Waiting for maas-postgres to become ready..."
oc rollout status deployment/maas-postgres -n "${INFRA_NAMESPACE}" --timeout=300s

echo "==> Creating maas-db-config Secret in ${INFRA_NAMESPACE}..."
oc create secret generic maas-db-config \
  -n "${INFRA_NAMESPACE}" \
  --from-literal=DB_CONNECTION_URL="postgresql://maas:maas@maas-postgres.${INFRA_NAMESPACE}.svc.cluster.local:5432/maasdb?sslmode=disable" \
  --dry-run=client -o yaml | oc apply -f -

CLUSTER_DOMAIN="$(oc get ingresses.config.openshift.io cluster -o jsonpath='{.spec.domain}')"
if [[ -z "${CLUSTER_DOMAIN}" ]]; then
  echo "error: could not resolve cluster apps domain from ingresses.config.openshift.io/cluster"
  exit 1
fi
echo "==> Applying maas-default-gateway (hostname maas.${CLUSTER_DOMAIN})..."
TMP_GW="$(mktemp)"
sed "s/__CLUSTER_DOMAIN__/${CLUSTER_DOMAIN}/g" "${AI_GATEWAY_DIR}/gateway.yaml" > "${TMP_GW}"
oc apply -f "${AI_GATEWAY_DIR}/gateway-options.yaml"
oc apply -f "${TMP_GW}"
rm -f "${TMP_GW}"

echo "==> Waiting for Gateway/maas-default-gateway to be Programmed..."
_gw_waited=0
while true; do
  if oc wait --for=condition=Programmed gateway/maas-default-gateway -n openshift-ingress --timeout=15s >/dev/null 2>&1; then
    break
  fi
  _prog="$(oc get gateway maas-default-gateway -n openshift-ingress \
    -o jsonpath='{.status.conditions[?(@.type=="Programmed")].status}' 2>/dev/null || true)"
  if [[ "${_prog}" == "True" ]]; then
    break
  fi
  if (( _gw_waited >= 300 )); then
    echo "warning: Gateway not Programmed after 5m — continuing."
    break
  fi
  sleep 5
  _gw_waited=$((_gw_waited + 5))
done

# Ensure an OpenShift Route exists for the gateway (ClusterIP + Route mode).
GW_SVC="$(oc get svc -n openshift-ingress -l gateway.networking.k8s.io/gateway-name=maas-default-gateway \
  -o jsonpath='{.items[0].metadata.name}' 2>/dev/null || true)"
if [[ -z "${GW_SVC}" ]]; then
  # OpenShift Gateway controller naming: <gateway>-<gatewayclass>
  GW_SVC="maas-default-gateway-openshift-default"
fi
if ! oc get route -n openshift-ingress -l gateway.networking.k8s.io/gateway-name=maas-default-gateway >/dev/null 2>&1 \
   && ! oc get route maas-gateway-route -n openshift-ingress >/dev/null 2>&1; then
  if oc get svc "${GW_SVC}" -n openshift-ingress >/dev/null 2>&1; then
    echo "==> Creating Route maas-gateway-route → ${GW_SVC}..."
    cat <<EOF | oc apply -f -
apiVersion: route.openshift.io/v1
kind: Route
metadata:
  name: maas-gateway-route
  namespace: openshift-ingress
  labels:
    app.kubernetes.io/part-of: agentic-doc-generation
spec:
  host: maas.${CLUSTER_DOMAIN}
  to:
    kind: Service
    name: ${GW_SVC}
    weight: 100
  port:
    targetPort: https
  tls:
    termination: reencrypt
    insecureEdgeTerminationPolicy: Redirect
  wildcardPolicy: None
EOF
  else
    echo "    Gateway Service not found yet — Route skipped (controller may auto-provision)."
  fi
fi

echo "==> Configuring Authorino TLS for MaaS..."
AUTHORINO_NS=""
for _ns in kuadrant-system rh-connectivity-link openshift-operators; do
  if oc get authorino -n "${_ns}" >/dev/null 2>&1; then
    AUTHORINO_NS="${_ns}"
    break
  fi
done
if [[ -z "${AUTHORINO_NS}" ]]; then
  AUTHORINO_NS="$(oc get authorino -A -o jsonpath='{.items[0].metadata.namespace}' 2>/dev/null || true)"
fi
if [[ -n "${AUTHORINO_NS}" ]]; then
  AUTHORINO_NAME="$(oc get authorino -n "${AUTHORINO_NS}" -o jsonpath='{.items[0].metadata.name}' 2>/dev/null || echo authorino)"
  AUTHORINO_SVC="$(oc get svc -n "${AUTHORINO_NS}" -o name 2>/dev/null | grep -E 'authorino.*authorization|authorino-authorino' | head -1 | sed 's#service/##' || true)"
  if [[ -z "${AUTHORINO_SVC}" ]]; then
    AUTHORINO_SVC="$(oc get svc -n "${AUTHORINO_NS}" -o jsonpath='{.items[?(@.metadata.name=="authorino-authorino-authorization")].metadata.name}' 2>/dev/null || true)"
  fi
  if [[ -z "${AUTHORINO_SVC}" ]]; then
    AUTHORINO_SVC="authorino-authorino-authorization"
  fi
  echo "    Authorino namespace=${AUTHORINO_NS} name=${AUTHORINO_NAME} svc=${AUTHORINO_SVC}"
  if oc get svc "${AUTHORINO_SVC}" -n "${AUTHORINO_NS}" >/dev/null 2>&1; then
    oc annotate svc "${AUTHORINO_SVC}" -n "${AUTHORINO_NS}" \
      service.beta.openshift.io/serving-cert-secret-name=authorino-server-cert --overwrite >/dev/null
  fi
  oc patch authorino "${AUTHORINO_NAME}" -n "${AUTHORINO_NS}" --type=merge --patch '{
    "spec": {
      "listener": {
        "tls": {
          "enabled": true,
          "certSecretRef": { "name": "authorino-server-cert" }
        }
      }
    }
  }' 2>/dev/null || true
  AUTHORINO_DEPLOY="$(oc get deploy -n "${AUTHORINO_NS}" -o jsonpath='{.items[?(@.metadata.name=="authorino")].metadata.name}' 2>/dev/null || true)"
  if [[ -z "${AUTHORINO_DEPLOY}" ]]; then
    AUTHORINO_DEPLOY="$(oc get deploy -n "${AUTHORINO_NS}" -o name 2>/dev/null | grep authorino | head -1 | sed 's#deployment.apps/##' || true)"
  fi
  if [[ -n "${AUTHORINO_DEPLOY}" ]]; then
    oc -n "${AUTHORINO_NS}" set env "deployment/${AUTHORINO_DEPLOY}" \
      SSL_CERT_FILE=/etc/ssl/certs/openshift-service-ca/service-ca-bundle.crt \
      REQUESTS_CA_BUNDLE=/etc/ssl/certs/openshift-service-ca/service-ca-bundle.crt >/dev/null 2>&1 || true
  fi
  oc annotate gateway maas-default-gateway -n openshift-ingress \
    security.opendatahub.io/authorino-tls-bootstrap="true" --overwrite >/dev/null
else
  echo "warning: Authorino CR not found yet — TLS bootstrap skipped; re-run after Kuadrant reconciles."
fi

# ---------------------------------------------------------------------------
# Step 6 -- Enable AI Gateway + Models-as-a-Service on the DSC
# ---------------------------------------------------------------------------
echo "==> Enabling aigateway + modelsAsAService on DataScienceCluster..."
oc patch datasciencecluster default-dsc --type=merge -p '{
  "spec": {
    "components": {
      "aigateway": {
        "managementState": "Managed",
        "modelsAsAService": {
          "managementState": "Managed"
        }
      }
    }
  }
}'

_ensure_dsc_component_managed "aigateway"

echo "==> Waiting for maas-api Deployment in ${INFRA_NAMESPACE}..."
_maas_waited=0
while true; do
  if oc get deploy maas-api -n "${INFRA_NAMESPACE}" >/dev/null 2>&1; then
    break
  fi
  # Controller may still be creating the infra namespace / workloads.
  if (( _maas_waited >= 600 )); then
    echo "error: timed out waiting for deployment/maas-api in ${INFRA_NAMESPACE}."
    echo "       Check: oc get datasciencecluster default-dsc -o yaml | grep -A20 aigateway"
    exit 1
  fi
  sleep 5
  _maas_waited=$((_maas_waited + 5))
done
oc rollout status deployment/maas-api -n "${INFRA_NAMESPACE}" --timeout=600s || true

echo "==> Waiting for MaasTenantConfig/default-tenant..."
_tenant_waited=0
until oc get maastenantconfig default-tenant -n models-as-a-service >/dev/null 2>&1; do
  if (( _tenant_waited >= 600 )); then
    echo "error: timed out waiting for MaasTenantConfig/default-tenant in models-as-a-service."
    exit 1
  fi
  sleep 5
  _tenant_waited=$((_tenant_waited + 5))
done
oc label namespace models-as-a-service maas.opendatahub.io/gateway-access=true --overwrite >/dev/null 2>&1 || true

# ---------------------------------------------------------------------------
# Step 7 -- Register the workshop external model behind the gateway
# ---------------------------------------------------------------------------
if [[ -z "${UPSTREAM_MODEL_API_KEY:-}" ]]; then
  echo "error: UPSTREAM_MODEL_API_KEY is unset."
  echo "       Export the upstream provider API key (workshop / OpenAI-compatible"
  echo "       endpoint credential), then re-run. Without it the gateway cannot"
  echo "       authenticate to the ExternalProvider."
  exit 1
fi

echo "==> Creating llm namespace and upstream provider Secret..."
oc apply -f "${AI_GATEWAY_DIR}/namespace-llm.yaml"
oc create secret generic workshop-model-api-key \
  -n llm \
  --from-literal=api-key="${UPSTREAM_MODEL_API_KEY}" \
  --dry-run=client -o yaml | oc apply -f -
oc label secret workshop-model-api-key -n llm \
  inference.llm-d.ai/ipp-managed=true --overwrite >/dev/null

echo "==> Waiting for ExternalProvider / ExternalModel CRDs..."
until oc get crd externalproviders.inference.opendatahub.io >/dev/null 2>&1 \
   && oc get crd externalmodels.inference.opendatahub.io >/dev/null 2>&1; do
  sleep 5
done

echo "==> Applying ExternalProvider / ExternalModel / MaaSModelRef..."
# Prefer auth.type=apikey; if the CRD rejects it, retry with simple.
if ! oc apply -f "${AI_GATEWAY_DIR}/external-provider.yaml" 2>/tmp/ext-provider-err; then
  if grep -qiE 'apikey|Unsupported value|invalid' /tmp/ext-provider-err; then
    echo "    auth.type=apikey rejected — retrying with type=simple..."
    TMP_EP="$(mktemp)"
    sed 's/type: apikey/type: simple/' "${AI_GATEWAY_DIR}/external-provider.yaml" > "${TMP_EP}"
    oc apply -f "${TMP_EP}"
    rm -f "${TMP_EP}"
  else
    cat /tmp/ext-provider-err >&2
    exit 1
  fi
fi
rm -f /tmp/ext-provider-err
oc apply -f "${AI_GATEWAY_DIR}/external-model.yaml"
oc apply -f "${AI_GATEWAY_DIR}/maas-model-ref.yaml"

echo "==> Waiting for ExternalProvider/workshop-maas Ready..."
_ep_waited=0
while true; do
  _phase="$(oc get externalprovider workshop-maas -n llm -o jsonpath='{.status.phase}' 2>/dev/null || true)"
  if [[ "${_phase}" == "Ready" ]]; then
    break
  fi
  if (( _ep_waited >= 300 )); then
    echo "warning: ExternalProvider not Ready (phase=${_phase:-unset}) — continuing."
    break
  fi
  sleep 5
  _ep_waited=$((_ep_waited + 5))
done

echo "==> Applying MaaSSubscription, MaaSAuthPolicy, and doc-gen-agents Group..."
oc apply -f "${AI_GATEWAY_DIR}/group-doc-gen-agents.yaml"
oc apply -f "${AI_GATEWAY_DIR}/maas-subscription.yaml"
oc apply -f "${AI_GATEWAY_DIR}/maas-auth-policy.yaml"

CURRENT_USER="$(oc whoami)"
echo "==> Adding ${CURRENT_USER} to Group/doc-gen-agents..."
# Patch users list idempotently.
EXISTING_USERS="$(oc get group doc-gen-agents -o jsonpath='{.users[*]}' 2>/dev/null || true)"
if [[ " ${EXISTING_USERS} " != *" ${CURRENT_USER} "* ]]; then
  oc patch group doc-gen-agents --type=json -p "[{\"op\":\"add\",\"path\":\"/users/-\",\"value\":\"${CURRENT_USER}\"}]" >/dev/null
fi

# ---------------------------------------------------------------------------
# Step 8 -- Mint a MaaS API key into doc-gen-demo/external-model-credentials
# ---------------------------------------------------------------------------
oc get namespace "${NAMESPACE}" >/dev/null 2>&1 || oc create namespace "${NAMESPACE}"

if [[ -n "${EXTERNAL_MODEL_API_KEY:-}" ]]; then
  echo "==> EXTERNAL_MODEL_API_KEY set — writing it to external-model-credentials (skip mint)."
  oc create secret generic external-model-credentials \
    --from-literal=api_key="${EXTERNAL_MODEL_API_KEY}" \
    -n "${NAMESPACE}" --dry-run=client -o yaml | oc apply -f -
else
  echo "==> Minting MaaS API key for subscription doc-gen-agents..."
  MAAS_API_SVC="$(oc get svc -n "${INFRA_NAMESPACE}" -o jsonpath='{range .items[*]}{.metadata.name}{"\n"}{end}' 2>/dev/null \
    | grep -E '^maas-api' | head -1 || true)"
  if [[ -z "${MAAS_API_SVC}" ]]; then
    echo "error: could not find maas-api Service in ${INFRA_NAMESPACE}"
    exit 1
  fi
  MAAS_API_PORT="$(oc get svc "${MAAS_API_SVC}" -n "${INFRA_NAMESPACE}" -o jsonpath='{.spec.ports[0].port}')"
  OC_TOKEN="$(oc whoami -t)"
  PF_LOCAL_PORT=18080
  oc port-forward -n "${INFRA_NAMESPACE}" "svc/${MAAS_API_SVC}" "${PF_LOCAL_PORT}:${MAAS_API_PORT}" >/tmp/maas-pf.log 2>&1 &
  PF_PID=$!
  cleanup_pf() { kill "${PF_PID}" >/dev/null 2>&1 || true; }
  trap cleanup_pf EXIT
  # Wait for port-forward to accept connections.
  _pf_waited=0
  until curl -sf "http://127.0.0.1:${PF_LOCAL_PORT}/" >/dev/null 2>&1 \
     || curl -sf "http://127.0.0.1:${PF_LOCAL_PORT}/health" >/dev/null 2>&1 \
     || curl -sf "http://127.0.0.1:${PF_LOCAL_PORT}/v1/models" >/dev/null 2>&1 \
     || nc -z 127.0.0.1 "${PF_LOCAL_PORT}" >/dev/null 2>&1; do
    if (( _pf_waited >= 30 )); then
      echo "error: port-forward to maas-api did not become ready."
      cat /tmp/maas-pf.log >&2 || true
      exit 1
    fi
    sleep 1
    _pf_waited=$((_pf_waited + 1))
  done

  mint_key() {
    local path="$1"
    curl -sS -X POST "http://127.0.0.1:${PF_LOCAL_PORT}${path}" \
      -H "Authorization: Bearer ${OC_TOKEN}" \
      -H "Content-Type: application/json" \
      -d '{"name":"doc-gen-agents","description":"Demo agents inference key","expiresIn":"90d","subscription":"doc-gen-agents"}'
  }

  API_KEY_RESPONSE="$(mint_key /maas-api/v1/api-keys 2>/dev/null || true)"
  MAAS_KEY="$(printf '%s' "${API_KEY_RESPONSE}" | python3 -c 'import sys,json; d=json.load(sys.stdin); print(d.get("key") or d.get("apiKey") or "")' 2>/dev/null || true)"
  if [[ -z "${MAAS_KEY}" ]]; then
    API_KEY_RESPONSE="$(mint_key /v1/api-keys 2>/dev/null || true)"
    MAAS_KEY="$(printf '%s' "${API_KEY_RESPONSE}" | python3 -c 'import sys,json; d=json.load(sys.stdin); print(d.get("key") or d.get("apiKey") or "")' 2>/dev/null || true)"
  fi
  cleanup_pf
  trap - EXIT

  if [[ -z "${MAAS_KEY}" || "${MAAS_KEY}" == "None" ]]; then
    echo "error: failed to mint MaaS API key. Response was:"
    echo "${API_KEY_RESPONSE}"
    echo "       Create one in the OpenShift AI dashboard (Gen AI studio → API keys)"
    echo "       and set EXTERNAL_MODEL_API_KEY / create secret external-model-credentials."
    exit 1
  fi

  echo "==> Writing minted MaaS key to secret/external-model-credentials in ${NAMESPACE}..."
  oc create secret generic external-model-credentials \
    --from-literal=api_key="${MAAS_KEY}" \
    -n "${NAMESPACE}" --dry-run=client -o yaml | oc apply -f -
fi

# Shared by all four agents (doc-gen, openapi-gen, compliance-scan, catalog-gen).
INCLUSTER_MODEL_URL="https://${GW_SVC}.openshift-ingress.svc.cluster.local:443/v1"
echo "==> Patching cluster-config model_base_url for all demo agents..."
if patch_cluster_config_data "${NAMESPACE}" "{\"data\":{\"model_base_url\":\"${INCLUSTER_MODEL_URL}\"}}"; then
  echo "    In-cluster MaaS base URL: ${INCLUSTER_MODEL_URL}"
  restart_demo_agent_deployments "${NAMESPACE}"
fi

# ---------------------------------------------------------------------------
# Step 9 -- Enable opt-in Developer Preview dashboard feature flags
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
# Step 10 -- Default MLflow tracking server instance
# ---------------------------------------------------------------------------
echo "==> Waiting for Mlflow CRD to be registered..."
MLFLOW_CRD_WAIT_SEC="${MLFLOW_CRD_WAIT_SEC:-600}"
_mlflow_crd_waited=0
until oc get crd mlflows.mlflow.opendatahub.io >/dev/null 2>&1; do
  if (( _mlflow_crd_waited >= MLFLOW_CRD_WAIT_SEC )); then
    echo "error: timed out waiting for Mlflow CRD (mlflows.mlflow.opendatahub.io)."
    echo "       mlflowoperator status:"
    oc get datasciencecluster default-dsc \
      -o jsonpath='{.status.components.mlflowoperator}{"\n"}' 2>/dev/null || true
    exit 1
  fi
  sleep 5
  _mlflow_crd_waited=$((_mlflow_crd_waited + 5))
done

echo "==> Creating default MLflow tracking server instance (if needed)..."
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

echo "==> Done. Red Hat OpenShift AI is installed with AI Hub, AI Studio, MLFlow, and AI Gateway."
DASHBOARD_ROUTE=$(oc get route rhods-dashboard -n redhat-ods-applications -o jsonpath='https://{.spec.host}' 2>/dev/null || true)
if [[ -n "${DASHBOARD_ROUTE}" ]]; then
  echo "    OpenShift AI dashboard: ${DASHBOARD_ROUTE}"
fi
echo "    MaaS gateway hostname: https://maas.${CLUSTER_DOMAIN}"
echo "    In-cluster agent base URL: https://${GW_SVC}.openshift-ingress.svc.cluster.local:443/v1"
echo "    Upstream model: ExternalModel/qwen38-27b in llm (via workshop-maas)"
echo ""
echo "    Check component status with:"
echo "      oc get datasciencecluster default-dsc -o yaml"
echo "      oc get externalmodel,externalprovider,maasmodelref -n llm"
echo "      oc get maassubscription,maasauthpolicy -n models-as-a-service"

# ---------------------------------------------------------------------------
# Step 8 -- MCP Gateway (Connectivity Link + MCP Gateway Operator)
# ---------------------------------------------------------------------------
# Same operator-before-CRD pattern as mcplifecycleoperator above, but the
# MCP gateway is *not* a DataScienceCluster component (aigateway is the
# model gateway and stays Removed). Delegate to install-mcp-gateway.sh so
# the URL-patch / re-run path after GitOps stays a single script.
echo ""
echo "==> Installing MCP Gateway prerequisites (Connectivity Link + MCP Gateway Operator)..."
bash "${SCRIPT_DIR}/install-mcp-gateway.sh"
