#!/usr/bin/env bash
# Act 1 (notes): what to say while the screen runs ../01-agent-ai-gateway.sh
set -euo pipefail
# shellcheck source=../lib.sh
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/../lib.sh"

demo_banner "1 — NOTES (screen: ../01-agent-ai-gateway.sh)"

demo_readout "model_base_url is the in-cluster MaaS Gateway /v1 URL — not the upstream provider."
demo_readout "model_name is the OGX-prefixed id (external-model/qwen38-27b); OGX strips the prefix before the gateway call."
demo_readout "OGX sidecar is the only inference client — agent.py/app.py never open an LLM connection."
demo_readout "INFERENCE_API_KEY comes from Secret external-model-credentials (MaaS sk-oai-...); never echo it."
demo_readout "All four agents share this cluster-config ConfigMap."

demo_banner "Next notes: ./02-ai-gateway-access.sh"
