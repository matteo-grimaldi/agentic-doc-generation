#!/usr/bin/env bash
# Act 1 (screen): agent → AI Gateway config.
set -euo pipefail
# shellcheck source=lib.sh
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib.sh"

require_oc
demo_banner "1 — Agent → AI Gateway wiring"

show_yaml "${DOC_GEN_K8S}/cluster-config.yaml"

show_file_range "${DOC_GEN_K8S}/deployment.yaml" 45 68 \
  "doc-gen-agent/k8s/deployment.yaml:45-68 (INFERENCE_*)"

show_live_configmap_keys "cluster-config" "${DEMO_NS}" \
  model_base_url model_name

demo_step "Secret/external-model-credentials (name only)"
if oc get secret external-model-credentials -n "${DEMO_NS}" >/dev/null 2>&1; then
  echo "  present in ${DEMO_NS}"
else
  echo "  missing in ${DEMO_NS}"
fi
echo

demo_banner "Next: ./02-ai-gateway-access.sh"
