# shellcheck shell=bash
# Shared OpenShift binary BuildConfig helper for component build-and-deploy.sh scripts.
#
# `oc start-build --from-dir` keeps the HTTP request open until the build pod is
# Running (server default ~5m) or `--follow`/`--wait` for completion. A full
# `build-and-deploy-all.sh` run can queue builds; use a longer `--request-timeout`
# and poll locally instead of failing at 5 minutes.

# Print hints when a build failed (especially ephemeral-storage on workshop nodes).
_openshift_build_failure_hints() {
  local build_name="$1"
  local namespace="$2"
  local message
  message="$(oc get build "${build_name}" -n "${namespace}" -o jsonpath='{.status.message}' 2>/dev/null || true)"
  if [[ "${message}" == *ephemeral-storage* || "${message}" == *EphemeralStorage* ]]; then
    echo ""
    echo "    Build node ran out of ephemeral disk while pulling layers (common for"
    echo "    ogx-mlflow-sidecar — base image ogxai/distribution-starter is large)."
    echo "    Retry after freeing space:"
    echo "      oc delete builds -n ${namespace} --field-selector status=Complete"
    echo "      oc delete builds -n ${namespace} --field-selector status=Failed"
    echo "    Cluster admin may also run: oc adm prune builds --keep=3 --confirm"
    echo "    Re-run this script (PRUNE_BUILDS_BEFORE_BUILD=true is on by default)."
    echo "    ogx-mlflow-sidecar uses BUILD_EPHEMERAL_STORAGE_LIMIT=30Gi; if the node"
    echo "    is still too small, use OGX_MLFLOW_SIDECAR_MODE=quay and pull a pre-built image."
    echo ""
  fi
}

# True when the ImageStream has a resolvable :latest tag (not just an empty IS).
openshift_imagestream_has_latest_tag() {
  local image_name="$1"
  local namespace="$2"
  oc get imagestreamtag "${image_name}:latest" -n "${namespace}" >/dev/null 2>&1
}

# Remove finished builds in this namespace to free build-node ephemeral storage.
# Set PRUNE_BUILDS_BEFORE_BUILD=false to skip.
prune_openshift_builds_in_namespace() {
  local namespace="$1"
  if [[ "${PRUNE_BUILDS_BEFORE_BUILD:-true}" != true ]]; then
    return 0
  fi
  local names phase
  names="$(oc get builds -n "${namespace}" -o jsonpath='{range .items[*]}{.metadata.name}{"\t"}{.status.phase}{"\n"}{end}' 2>/dev/null || true)"
  if [[ -z "${names}" ]]; then
    return 0
  fi
  local deleted=0
  while IFS=$'\t' read -r name phase; do
    [[ -z "${name}" ]] && continue
    case "${phase}" in
      Complete|Failed|Cancelled|Error)
        oc delete build "${name}" -n "${namespace}" --wait=false >/dev/null 2>&1 || true
        deleted=$((deleted + 1))
        ;;
    esac
  done <<< "${names}"
  if (( deleted > 0 )); then
    echo "==> Pruned ${deleted} finished build(s) in ${namespace} (free ephemeral storage for next build)."
    sleep 3
  fi
}

# Binary builds need several GiB on the node. Set BUILD_EPHEMERAL_STORAGE_LIMIT
# only for large images (ogx-mlflow-sidecar uses 30Gi). Smaller agent images
# should leave the limit unset so the build is not capped at 10Gi.
_ensure_buildconfig_ephemeral_storage() {
  local image_name="$1"
  local namespace="$2"
  local request="${BUILD_EPHEMERAL_STORAGE_REQUEST:-4Gi}"
  local limit="${BUILD_EPHEMERAL_STORAGE_LIMIT:-}"

  if [[ -n "${limit}" ]]; then
    oc patch buildconfig "${image_name}" -n "${namespace}" --type=merge -p \
      "{\"spec\":{\"resources\":{\"requests\":{\"ephemeral-storage\":\"${request}\"},\"limits\":{\"ephemeral-storage\":\"${limit}\"}}}}" \
      >/dev/null 2>&1 || true
  else
    oc patch buildconfig "${image_name}" -n "${namespace}" --type=merge -p \
      "{\"spec\":{\"resources\":{\"requests\":{\"ephemeral-storage\":\"${request}\"}}}}" \
      >/dev/null 2>&1 || true
  fi
}

run_openshift_binary_build() {
  local image_name="$1"
  local context_dir="$2"
  local namespace="$3"
  local build_start_wait="${BUILD_START_WAIT:-900}"
  local build_complete_wait="${BUILD_COMPLETE_WAIT:-1800}"
  local request_timeout="${BUILD_REQUEST_TIMEOUT:-$((build_start_wait + 120))}"

  prune_openshift_builds_in_namespace "${namespace}"

  if ! oc get imagestream "${image_name}" -n "${namespace}" >/dev/null 2>&1; then
    oc new-build --name="${image_name}" --binary --strategy=docker -n "${namespace}"
  fi
  _ensure_buildconfig_ephemeral_storage "${image_name}" "${namespace}"

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
      _openshift_build_failure_hints "${build_name}" "${namespace}"
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
      _openshift_build_failure_hints "${build_name}" "${namespace}"
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
