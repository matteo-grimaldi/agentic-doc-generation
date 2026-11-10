#!/usr/bin/env bash
# Run every agent in its own process to isolate flat module names.
# Usage: AGENT_TEST_PYTHON=/path/to/venv/bin/python bash scripts/run-agent-tests.sh
set -uo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
AGENT_TEST_PYTHON="${AGENT_TEST_PYTHON:-python3}"
cd "$REPO_ROOT" || exit 1
result=0
for agent_dir in doc-gen-agent openapi-gen-agent catalog-gen-agent compliance-scan-agent; do
    echo "Running ${agent_dir}"
    if ! "$AGENT_TEST_PYTHON" -m pytest -c "$agent_dir/pytest.ini" "$agent_dir/tests" "$@"; then
        result=1
    fi
done
exit "$result"
