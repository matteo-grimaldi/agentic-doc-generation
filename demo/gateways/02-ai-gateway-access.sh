#!/usr/bin/env bash
# Act 2 (screen): AI Gateway access config.
set -euo pipefail
# shellcheck source=lib.sh
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib.sh"

require_oc
demo_banner "2 — AI Gateway access (Authorino / MaaSAuthPolicy)"

show_yaml "${AI_GATEWAY_DIR}/group-doc-gen-agents.yaml"
show_yaml "${AI_GATEWAY_DIR}/maas-auth-policy.yaml"
show_yaml "${AI_GATEWAY_DIR}/external-model.yaml"

show_live group doc-gen-agents
show_live maasauthpolicy doc-gen-agents "${MAAS_NS}"
show_live externalmodel qwen38-27b "${LLM_NS}"

demo_banner "Next: ./03-ai-gateway-budget.sh"
