# shellcheck shell=bash
# Shared OpenShift binary BuildConfig helper for component build-and-deploy.sh scripts.
#
# `oc start-build --from-dir` keeps the HTTP request open until the build pod is
# Running (server default ~5m) or `--follow`/`--wait` for completion. A full
# `build-and-deploy-all.sh` run can queue builds; use a longer `--request-timeout`
# and poll locally instead of failing at 5 minutes.
run_openshift_binary_build() {
  local image_name="$1"
  local context_dir="$2"
  local namespace="$3"
  local build_start_wait="${BUILD_START_WAIT:-900}"
  local build_complete_wait="${BUILD_COMPLETE_WAIT:-1800}"
  local request_timeout="${BUILD_REQUEST_TIMEOUT:-$((build_start_wait + 120))}"

  if ! oc get imagestream "${image_name}" -n "${namespace}" >/dev/null 2>&1; then
    oc new-build --name="${image_name}" --binary --strategy=docker -n "${namespace}"
  fi

  echo "==> Starting build ${image_name} (cluster may queue if build slots are busy)..."
  local build_ref
  build_ref="$(oc start-build "${image_name}" --from-dir="${context_dir}" -n "${namespace}" \
    --request-timeout="${request_timeout}s" -o name)"
  local build_name="${build_ref##*/}"
  echo "    Build ${build_name} queued."

  local elapsed=0
  while true; do
    local phase
    phase="$(oc get build "${build_name}" -n "${namespace}" -o jsonpath='{.status.phase}' 2>/dev/null || echo New)"
    if [[ "${phase}" == "Running" || "${phase}" == "Complete" ]]; then
      break
    fi
    if [[ "${phase}" == "Failed" || "${phase}" == "Cancelled" || "${phase}" == "Error" ]]; then
      echo "error: build ${build_name} entered phase ${phase}"
      oc get build "${build_name}" -n "${namespace}" -o jsonpath='message={.status.message}{"\n"}' 2>/dev/null || true
      return 1
    fi
    if (( elapsed >= build_start_wait )); then
      echo "error: timed out after ${build_start_wait}s waiting for build ${build_name} to start (phase=${phase})"
      return 1
    fi
    sleep 5
    elapsed=$((elapsed + 5))
  done

  echo "==> Following build ${build_name} logs..."
  oc logs -f "build/${build_name}" -n "${namespace}" &
  local log_pid=$!
  elapsed=0
  while true; do
    phase="$(oc get build "${build_name}" -n "${namespace}" -o jsonpath='{.status.phase}')"
    if [[ "${phase}" == "Complete" ]]; then
      break
    fi
    if [[ "${phase}" == "Failed" || "${phase}" == "Cancelled" || "${phase}" == "Error" ]]; then
      wait "${log_pid}" 2>/dev/null || true
      echo "error: build ${build_name} failed with phase ${phase}"
      oc get build "${build_name}" -n "${namespace}" -o jsonpath='message={.status.message}{"\n"}' 2>/dev/null || true
      return 1
    fi
    if (( elapsed >= build_complete_wait )); then
      wait "${log_pid}" 2>/dev/null || true
      echo "error: timed out after ${build_complete_wait}s waiting for build ${build_name} to complete"
      return 1
    fi
    sleep 5
    elapsed=$((elapsed + 5))
  done
  wait "${log_pid}" 2>/dev/null || true
  echo "    Build ${build_name} complete."
}
