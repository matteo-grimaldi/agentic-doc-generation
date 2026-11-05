#!/usr/bin/env bash
# Act 0 (screen): path diagram + live gateway status.
set -euo pipefail
# shellcheck source=lib.sh
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib.sh"

require_oc
demo_banner "0 — Overview (AI Gateway + MCP Gateway paths)"

cat <<'EOF'

  doc-gen-demo agents
  ┌─────────────────────────────┐
  │  Gradio (agent.py)          │── MCP_GATEWAY_URL + SA token
  │                             │   + X-Mcp-Virtualserver
  │  OGX sidecar                │── INFERENCE_BASE_URL + sk-oai key
  └─────────────┬───────────────┘
                │
       ┌────────┴────────┐
       ▼                 ▼
  MCP Gateway       AI Gateway (MaaS)
  (TokenReview,     (MaaSAuthPolicy,
   VirtualServer,    MaaSSubscription)
   RateLimitPolicy)
       │                 │
       ▼                 ▼
  github-mcp-server  ExternalModel qwen38-27b
                     → upstream OpenAI-compatible

EOF

show_gateway_programmed "maas-default-gateway" "${MAAS_GATEWAY_NS}"
show_gateway_programmed "mcp-gateway" "${MCP_NS}"
show_live_configmap_keys "cluster-config" "${DEMO_NS}" \
  model_base_url model_name mcp_gateway_url

demo_banner "Next: ./01-agent-ai-gateway.sh"
