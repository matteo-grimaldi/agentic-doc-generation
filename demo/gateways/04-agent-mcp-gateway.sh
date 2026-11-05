#!/usr/bin/env bash
# Act 4 (screen): agent → MCP Gateway config.
set -euo pipefail
# shellcheck source=lib.sh
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib.sh"

require_oc
demo_banner "4 — Agent → MCP Gateway wiring"

show_file_range "${DOC_GEN_K8S}/deployment.yaml" 146 162 \
  "doc-gen-agent/k8s/deployment.yaml:146-162 (MCP_GATEWAY_* / GITHUB_MCP_SERVER_URL)"

show_file_range "${DOC_GEN_K8S}/deployment.yaml" 248 256 \
  "doc-gen-agent/k8s/deployment.yaml:248-256 (mcp-gateway-token volume)"

show_file_range "${DOC_GEN_AGENT_PY}" 48 74 \
  "doc-gen-agent/agent.py:48-74 (_mcp_gateway_tool)"

show_live_configmap_keys "cluster-config" "${DEMO_NS}" mcp_gateway_url

demo_banner "Next: ./05-mcp-gateway-access.sh"
