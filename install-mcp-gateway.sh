#!/usr/bin/env bash
#
# Installs Red Hat Connectivity Link (RHCL) and the MCP Gateway Operator
# so this repo's GitOps tree can apply MCPGatewayExtension /
# MCPServerRegistration / MCPVirtualServer / AuthPolicy resources.
#
# Invoked automatically at the end of ./install-openshift-ai.sh. You can
# also run this script alone — idempotent — after GitOps syncs the
# mcp-gateway Application to patch cluster-config mcp_gateway_url once
# Gateway mcp-gateway is Programmed (and restart the four agents).
#
# Platform-level / imperative (like install-openshift-ai.sh) because
# ArgoCD cannot apply those CRs before their CRDs exist. The
# DataScienceCluster `aigateway` component is the *model* gateway and
# stays Removed — this script installs the MCP gateway path instead.
#
# After operators are ready, this script also:
#   - Creates a cluster Kuadrant singleton if AuthPolicy enforcement needs
#     one and none exists yet
#   - Waits for Gateway mcp-gateway (GitOps-managed) to become Programmed
#     when present, then patches cluster-config mcp_gateway_url and
#     restarts the four agent Deployments
#
# Prerequisites:
#   - OpenShift 4.19+ (Gateway API CRDs shipped in-cluster)
#   - oc login as cluster-admin
#
# Usage:
#   ./install-mcp-gateway.sh
#   # or via: ./install-openshift-ai.sh
set -euo pipefail

MCP_SYSTEM_NS="${MCP_SYSTEM_NS:-mcp-gateway}"
DEMO_NS="${DEMO_NS:-doc-gen-demo}"
MCP_GATEWAY_AUDIENCE="${MCP_GATEWAY_AUDIENCE:-mcp-gateway}"
AGENT_DEPLOYMENTS=(
  doc-gen-agent
  openapi-gen-agent
  catalog-gen-agent
  compliance-scan-agent
)

fail() {
  echo "error: $*" >&2
  exit 1
}

echo "==> Checking cluster login..."
oc whoami >/dev/null || fail "not logged into an OpenShift cluster (run 'oc login' first)"

# ---------------------------------------------------------------------------
# Step 1 -- Gateway API CRDs (OpenShift 4.19+)
# ---------------------------------------------------------------------------
echo "==> Checking Gateway API CRDs..."
for crd in gateways.gateway.networking.k8s.io httproutes.gateway.networking.k8s.io; do
  oc get crd "${crd}" >/dev/null 2>&1 \
    || fail "${crd} not found — OpenShift 4.19+ is required (Gateway API ships in-cluster)."
done
echo "    Gateway API CRDs present."

# openshift-default is the Ingress Operator's GatewayClass — it is NOT
# pre-created on every cluster. Creating it (exactly this name +
# controllerName) is the documented enablement step; the Ingress Operator
# then installs a lightweight Service Mesh control plane in
# openshift-ingress. Do not invent a second class name.
GATEWAY_CLASS_NAME="${GATEWAY_CLASS_NAME:-openshift-default}"
GATEWAY_CONTROLLER="${GATEWAY_CONTROLLER:-openshift.io/gateway-controller/v1}"
echo "==> Ensuring GatewayClass ${GATEWAY_CLASS_NAME}..."
if ! oc get gatewayclass "${GATEWAY_CLASS_NAME}" >/dev/null 2>&1; then
  cat <<EOF | oc apply -f -
apiVersion: gateway.networking.k8s.io/v1
kind: GatewayClass
metadata:
  name: ${GATEWAY_CLASS_NAME}
spec:
  controllerName: ${GATEWAY_CONTROLLER}
EOF
  echo "    Created GatewayClass/${GATEWAY_CLASS_NAME} (Ingress Operator will reconcile it)."
else
  echo "    GatewayClass/${GATEWAY_CLASS_NAME} already present."
fi

echo "==> Waiting for GatewayClass ${GATEWAY_CLASS_NAME} to be Accepted..."
_waited=0
while true; do
  accepted="$(oc get gatewayclass "${GATEWAY_CLASS_NAME}" -o jsonpath='{.status.conditions[?(@.type=="Accepted")].status}' 2>/dev/null || true)"
  if [[ "${accepted}" == "True" ]]; then
    break
  fi
  if (( _waited >= 300 )); then
    echo "    warning: GatewayClass ${GATEWAY_CLASS_NAME} not Accepted yet (status=${accepted:-unset})."
    echo "    Continuing — first Gateway create may still trigger Ingress Operator install."
    echo "    If Gateways stay unprogrammed, check: oc get gatewayclass ${GATEWAY_CLASS_NAME} -o yaml"
    echo "    and look for an OSSM v2 conflict (v2 and the Gateway-API mesh cannot coexist)."
    break
  fi
  sleep 5
  _waited=$((_waited + 5))
done
if [[ "${accepted:-}" == "True" ]]; then
  echo "    GatewayClass/${GATEWAY_CLASS_NAME} Accepted=True."
fi

# ---------------------------------------------------------------------------
# Step 2 -- Red Hat Connectivity Link (AuthPolicy / Kuadrant CRDs)
# ---------------------------------------------------------------------------
echo "==> Ensuring Red Hat Connectivity Link operator (AuthPolicy CRD)..."
if ! oc get crd authpolicies.kuadrant.io >/dev/null 2>&1; then
  # Confirm package name from the redhat-operators catalog when possible.
  RHCL_PKG="rhcl-operator"
  if oc get packagemanifests -n openshift-marketplace "${RHCL_PKG}" >/dev/null 2>&1; then
    :
  elif oc get packagemanifests -n openshift-marketplace rhcl >/dev/null 2>&1; then
    RHCL_PKG="rhcl"
  else
    # Fall through with the documented package name; Subscription apply will
    # fail clearly if the catalog entry is missing on this cluster.
    echo "    warning: packagemanifest ${RHCL_PKG} not listed yet; applying Subscription anyway."
  fi

  cat <<EOF | oc apply -f -
apiVersion: operators.coreos.com/v1alpha1
kind: Subscription
metadata:
  name: rhcl-operator
  namespace: openshift-operators
spec:
  channel: stable
  name: ${RHCL_PKG}
  source: redhat-operators
  sourceNamespace: openshift-marketplace
  installPlanApproval: Automatic
EOF

  echo "==> Waiting for authpolicies.kuadrant.io CRD..."
  until oc get crd authpolicies.kuadrant.io >/dev/null 2>&1; do
    sleep 5
  done
else
  echo "    authpolicies.kuadrant.io already present."
fi

echo "==> Waiting for a Connectivity Link / Kuadrant CSV to Succeeded (if present)..."
# CSV naming varies by release; wait until the CRD exists (done) and any
# matching CSV reaches Succeeded when one is installed.
_waited=0
while true; do
  csv_phase="$(oc get csv -A -o jsonpath='{range .items[?(@.spec.displayName)]}{.metadata.namespace}{"|"}{.metadata.name}{"|"}{.spec.displayName}{"|"}{.status.phase}{"\n"}{end}' 2>/dev/null \
    | awk -F'|' 'tolower($3) ~ /connectivity link|kuadrant/ {print $4; exit}')"
  if [[ -z "${csv_phase}" || "${csv_phase}" == "Succeeded" ]]; then
    break
  fi
  if (( _waited >= 600 )); then
    fail "timed out waiting for Connectivity Link / Kuadrant CSV to Succeeded (phase=${csv_phase})"
  fi
  sleep 5
  _waited=$((_waited + 5))
done

# ---------------------------------------------------------------------------
# Step 3 -- MCP Gateway Operator (MCPGatewayExtension CRD)
# ---------------------------------------------------------------------------
echo "==> Ensuring MCP Gateway Operator..."
oc get namespace "${MCP_SYSTEM_NS}" >/dev/null 2>&1 || oc create namespace "${MCP_SYSTEM_NS}"

if ! oc get crd mcpgatewayextensions.mcp.kuadrant.io >/dev/null 2>&1; then
  MCP_GW_PKG="mcp-gateway"
  if ! oc get packagemanifests -n openshift-marketplace "${MCP_GW_PKG}" >/dev/null 2>&1; then
    echo "    warning: packagemanifest ${MCP_GW_PKG} not listed yet; applying Subscription anyway."
  fi

  if ! oc get operatorgroup -n "${MCP_SYSTEM_NS}" mcp-gateway >/dev/null 2>&1; then
    cat <<EOF | oc apply -f -
apiVersion: operators.coreos.com/v1
kind: OperatorGroup
metadata:
  name: mcp-gateway
  namespace: ${MCP_SYSTEM_NS}
spec:
  targetNamespaces:
  - ${MCP_SYSTEM_NS}
EOF
  fi

  cat <<EOF | oc apply -f -
apiVersion: operators.coreos.com/v1alpha1
kind: Subscription
metadata:
  name: mcp-gateway
  namespace: ${MCP_SYSTEM_NS}
spec:
  channel: preview
  name: ${MCP_GW_PKG}
  source: redhat-operators
  sourceNamespace: openshift-marketplace
  installPlanApproval: Automatic
EOF

  echo "==> Waiting for mcpgatewayextensions.mcp.kuadrant.io CRD..."
  until oc get crd mcpgatewayextensions.mcp.kuadrant.io >/dev/null 2>&1; do
    sleep 5
  done
else
  echo "    mcpgatewayextensions.mcp.kuadrant.io already present."
fi

echo "==> Waiting for MCP Gateway CSV to Succeeded..."
_waited=0
while true; do
  phase="$(oc get csv -n "${MCP_SYSTEM_NS}" -l "operators.coreos.com/mcp-gateway.${MCP_SYSTEM_NS}"="" \
    -o jsonpath='{.items[0].status.phase}' 2>/dev/null || true)"
  if [[ "${phase}" == "Succeeded" ]]; then
    break
  fi
  # Fallback: any CSV whose name starts with mcp-gateway in the namespace.
  phase="$(oc get csv -n "${MCP_SYSTEM_NS}" -o jsonpath='{range .items[*]}{.metadata.name}{"|"}{.status.phase}{"\n"}{end}' 2>/dev/null \
    | awk -F'|' '$1 ~ /^mcp-gateway/ {print $2; exit}')"
  if [[ "${phase}" == "Succeeded" ]]; then
    break
  fi
  if (( _waited >= 600 )); then
    fail "timed out waiting for MCP Gateway CSV in ${MCP_SYSTEM_NS}"
  fi
  sleep 5
  _waited=$((_waited + 5))
done

# Fail early if this cluster only has the older gatewayRef-shaped CRD,
# which would reject the GitOps MCPGatewayExtension (targetRef).
echo "==> Validating MCPGatewayExtension CRD supports spec.targetRef..."
if ! oc get crd mcpgatewayextensions.mcp.kuadrant.io -o jsonpath='{.spec.versions[*].schema.openAPIV3Schema.properties.spec.properties}' 2>/dev/null \
  | grep -q 'targetRef'; then
  # Some CRDs nest properties differently; also accept if OpenAPI has targetRef anywhere under spec.
  if ! oc get crd mcpgatewayextensions.mcp.kuadrant.io -o yaml 2>/dev/null | grep -q 'targetRef'; then
    fail "MCPGatewayExtension CRD on this cluster does not expose spec.targetRef (older gatewayRef-only shape). Upgrade the MCP Gateway Operator before applying GitOps manifests."
  fi
fi
echo "    MCPGatewayExtension CRD looks compatible (targetRef present)."

# ---------------------------------------------------------------------------
# Step 4 -- Kuadrant singleton (AuthPolicy enforcement)
# ---------------------------------------------------------------------------
echo "==> Ensuring cluster Kuadrant instance (if CRD present)..."
if oc get crd kuadrants.kuadrant.io >/dev/null 2>&1; then
  KUADRANT_NS="${KUADRANT_NS:-kuadrant-system}"
  oc get namespace "${KUADRANT_NS}" >/dev/null 2>&1 || oc create namespace "${KUADRANT_NS}"
  if ! oc get kuadrant -A --no-headers 2>/dev/null | grep -q .; then
    cat <<EOF | oc apply -f -
apiVersion: kuadrant.io/v1beta1
kind: Kuadrant
metadata:
  name: kuadrant
  namespace: ${KUADRANT_NS}
spec: {}
EOF
    echo "    Created Kuadrant/kuadrant in ${KUADRANT_NS}."
  else
    echo "    Kuadrant instance already exists."
  fi
else
  echo "    kuadrants.kuadrant.io CRD not found — skipping (AuthPolicy may still work if Authorino is otherwise installed)."
fi

# ---------------------------------------------------------------------------
# Step 5 -- Patch mcp_gateway_url once Gateway is Programmed (optional here)
# ---------------------------------------------------------------------------
# The Gateway itself is GitOps-managed (mcp-gateway/k8s). If it is already
# present (re-run after bootstrap), write the in-cluster /mcp URL into
# cluster-config and bounce the agents so they pick it up.
echo "==> Looking for Gateway mcp-gateway in ${MCP_SYSTEM_NS}..."
if oc get gateway mcp-gateway -n "${MCP_SYSTEM_NS}" >/dev/null 2>&1; then
  echo "==> Waiting for Gateway mcp-gateway to become Programmed..."
  oc wait --for=condition=Programmed gateway/mcp-gateway -n "${MCP_SYSTEM_NS}" --timeout=5m \
    || fail "Gateway mcp-gateway did not become Programmed"

  addr="$(oc get gateway mcp-gateway -n "${MCP_SYSTEM_NS}" -o jsonpath='{.status.addresses[0].value}' 2>/dev/null || true)"
  [[ -n "${addr}" ]] || fail "Gateway mcp-gateway has no status.addresses[0].value"

  # Prefer HTTP :8080 (listener name mcp). If the programmed address is
  # already an https host, use https://…/mcp and note trust-store needs.
  if [[ "${addr}" == https://* ]]; then
    mcp_url="${addr%/}/mcp"
    echo "    warning: Gateway address is HTTPS (${mcp_url}); agent pods may need the cluster CA to trust it."
  elif [[ "${addr}" == http://* ]]; then
    mcp_url="${addr%/}/mcp"
  else
    # Bare hostname / IP from Gateway status — use the HTTP mcp listener port.
    mcp_url="http://${addr}:8080/mcp"
  fi
  echo "    MCP gateway URL: ${mcp_url} (audience ${MCP_GATEWAY_AUDIENCE})"

  if oc get configmap cluster-config -n "${DEMO_NS}" >/dev/null 2>&1; then
    oc patch configmap cluster-config -n "${DEMO_NS}" --type=merge \
      -p "{\"data\":{\"mcp_gateway_url\":\"${mcp_url}\"}}"
    echo "    Patched configmap/cluster-config mcp_gateway_url."

    for dep in "${AGENT_DEPLOYMENTS[@]}"; do
      if oc get deployment "${dep}" -n "${DEMO_NS}" >/dev/null 2>&1; then
        oc rollout restart "deployment/${dep}" -n "${DEMO_NS}"
        echo "    Restarted deployment/${dep}."
      fi
    done
  else
    echo "    configmap/cluster-config not in ${DEMO_NS} yet — re-run this script after GitOps syncs doc-gen-agent."
  fi
else
  echo "    Gateway mcp-gateway not found yet (expected before first GitOps sync)."
  echo "    After ArgoCD syncs gitops/apps/mcp-gateway.yaml, re-run this script to patch mcp_gateway_url."
fi

echo "==> MCP Gateway prerequisites installed."
echo "    Next: ensure GitOps syncs mcp-gateway (wave 0), then re-run this script if mcp_gateway_url is still empty."
