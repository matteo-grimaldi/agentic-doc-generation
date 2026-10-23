#!/usr/bin/env bash
# Waits for the OGX sidecar (running in the same pod) to be ready before
# starting the Gradio UI, so the first user request doesn't race the
# sidecar's own startup.
set -euo pipefail

OGX_BASE_URL="${OGX_BASE_URL:-http://localhost:8321}"
MAX_WAIT_SECONDS="${OGX_WAIT_TIMEOUT:-120}"

echo "==> Waiting for OGX sidecar at ${OGX_BASE_URL} (timeout: ${MAX_WAIT_SECONDS}s)..."
elapsed=0
until curl -fsS "${OGX_BASE_URL}/v1/models" >/dev/null 2>&1; do
  if [[ "${elapsed}" -ge "${MAX_WAIT_SECONDS}" ]]; then
    echo "==> WARNING: OGX sidecar not ready after ${MAX_WAIT_SECONDS}s, starting UI anyway."
    break
  fi
  sleep 3
  elapsed=$((elapsed + 3))
done
echo "==> OGX sidecar check complete. Starting Gradio UI."

exec python3 app.py
