#!/usr/bin/env bash
# Act 2 (notes): what to say while the screen runs ../02-ai-gateway-access.sh
set -euo pipefail
# shellcheck source=../lib.sh
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/../lib.sh"

demo_banner "2 — NOTES (screen: ../02-ai-gateway-access.sh)"

demo_readout "Auth binds OpenShift group doc-gen-agents to bare model id qwen38-27b via MaaSAuthPolicy."
demo_readout "ExternalModel is the client-facing model the gateway routes to the upstream OpenAI-compatible endpoint."
demo_readout "install-openshift-ai.sh adds the workshop user to group doc-gen-agents after apply."
demo_readout "Later, demo/governance will swap this auth to a nobody group to show a live 403."

demo_banner "Next notes: ./03-ai-gateway-budget.sh"
