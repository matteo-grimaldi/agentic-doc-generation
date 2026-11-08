#!/usr/bin/env bash
# Build app images and generate configuration before the first GitOps sync.
# No application Deployment, MCPServer, Service or Route manifests are applied.
# Existing :latest images are rebuilt; this is also useful for source updates.
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
if [[ "${NAMESPACE:-doc-gen-demo}" != doc-gen-demo ]]; then
  echo "error: GitOps manifests require NAMESPACE=doc-gen-demo" >&2
  exit 1
fi
oc whoami >/dev/null
for component in github-mcp-server doc-gen-agent openapi-gen-agent catalog-gen-agent \
    compliance-scan-agent governance-hub compliance-mcp-server; do
  echo "==> Preparing ${component} (images/configuration only)..."
  PREPARE_ONLY=true NAMESPACE=doc-gen-demo bash "${REPO_ROOT}/${component}/build-and-deploy.sh"
done
echo "==> Preparation complete. Install the required operators before gitops/bootstrap.sh."
