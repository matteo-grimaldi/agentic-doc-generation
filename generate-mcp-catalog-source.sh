#!/usr/bin/env bash
#
# (Re)generates mcp-catalog-source.yaml — the file you import via
# Settings > MCP resources > MCP catalog sources > Add a source in the
# OpenShift AI dashboard's AI Hub MCP Catalog — by concatenating every
# component's own <component>/mcp-catalog.yaml fragment into one
# `mcp_servers:` list, matching the format in mcpcatalogsourcesample.yaml.
#
# mcp-catalog-source.yaml itself is a GENERATED file — don't hand-edit
# it, your edits will be overwritten the next time this runs. Edit the
# component's mcp-catalog.yaml fragment instead.
#
# To add a NEW MCP server to the catalog source: copy
# github-mcp-server/mcp-catalog.yaml into the new component's directory
# as a template, fill in its fields, then re-run this script — no other
# files need to change. (See also ./register-mcp-catalog.sh, the
# separate, independent import path for the Gen AI Playground tool list.)
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
OUT_FILE="${SCRIPT_DIR}/mcp-catalog-source.yaml"

echo "==> Discovering MCP catalog entry fragments (*/mcp-catalog.yaml)..."
FRAGMENTS=()
while IFS= read -r f; do
  FRAGMENTS+=("${f}")
done < <(find "${SCRIPT_DIR}" -mindepth 2 -maxdepth 2 -name 'mcp-catalog.yaml' | sort)
if [[ ${#FRAGMENTS[@]} -eq 0 ]]; then
  echo "error: no */mcp-catalog.yaml fragments found under ${SCRIPT_DIR} — nothing to generate." >&2
  exit 1
fi
for f in "${FRAGMENTS[@]}"; do
  echo "    - ${f#"${SCRIPT_DIR}"/}"
done

echo "==> Writing ${OUT_FILE}..."
{
  echo "# GENERATED FILE — do not edit directly."
  echo "#"
  echo "# Produced by ./generate-mcp-catalog-source.sh from every"
  echo "# */mcp-catalog.yaml fragment in this repo. Re-run that script"
  echo "# after adding, removing, or changing a component's fragment."
  echo "#"
  echo "# Import this file into OpenShift AI's AI Hub MCP Catalog:"
  echo "#   Settings > MCP resources > MCP catalog sources > Add a source"
  echo "#"
  echo "# This is a different, independent import path from"
  echo "# ./register-mcp-catalog.sh (which patches the gen-ai-aa-mcp-servers"
  echo "# ConfigMap for the Gen AI Playground tool list instead) — see the"
  echo "# README's \"OpenShift AI MCP catalog registration\" section. Both"
  echo "# can be used together."
  echo "#"
  echo "# \`repositoryUrl\`/\`documentationUrl\` fields below use the same"
  echo "# YOUR_ORG placeholder as gitops/apps/*.yaml — update them to match"
  echo "# your fork before importing this file."
  echo ""
  echo "source: agentic-doc-generation MCP servers"
  echo ""
  echo "mcp_servers:"
  for f in "${FRAGMENTS[@]}"; do
    sed 's/^/  /' "${f}"
  done
} > "${OUT_FILE}"

echo "==> Done. Wrote $(grep -c '^  - name:' "${OUT_FILE}") server entr$(test "$(grep -c '^  - name:' "${OUT_FILE}")" = 1 && echo y || echo ies) to ${OUT_FILE}."
