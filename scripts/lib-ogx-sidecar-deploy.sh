# shellcheck shell=bash
# MLflow-instrumented OGX sidecar: in-cluster ImageStream build or pull from Quay.

# Build ogx-mlflow-sidecar ImageStream on the cluster (needs large build-node disk).
ensure_ogx_mlflow_sidecar_image_cluster() {
  local namespace="$1"
  local repo_root="$2"

  if openshift_imagestream_has_latest_tag ogx-mlflow-sidecar "${namespace}" \
    && [[ "${OGX_MLFLOW_FORCE_REBUILD:-false}" != true ]]; then
    echo "==> ogx-mlflow-sidecar:latest already present — skipping sidecar image build."
    echo "    Set OGX_MLFLOW_FORCE_REBUILD=true to rebuild."
    return 0
  fi

  echo "==> Building ogx-mlflow-sidecar ImageStream in-cluster (large build-node disk)..."
  BUILD_EPHEMERAL_STORAGE_REQUEST="${BUILD_EPHEMERAL_STORAGE_REQUEST:-8Gi}" \
  BUILD_EPHEMERAL_STORAGE_LIMIT="${BUILD_EPHEMERAL_STORAGE_LIMIT:-30Gi}" \
  NAMESPACE="${namespace}" "${repo_root}/ogx-mlflow-sidecar/build-and-deploy.sh"
}

# Point ogx-server at ogx-mlflow-sidecar (Quay or namespace registry).
apply_ogx_mlflow_sidecar_image() {
  local deployment="$1"
  local namespace="$2"
  local mode="${OGX_MLFLOW_SIDECAR_MODE:-quay}"
  local image="${OGX_MLFLOW_SIDECAR_IMAGE:-}"

  if [[ "${mode}" == cluster ]]; then
    local tag="${OGX_MLFLOW_IMAGE_TAG:-latest}"
    image="image-registry.openshift-image-registry.svc:5000/${namespace}/ogx-mlflow-sidecar:${tag}"
  elif [[ -z "${image}" ]]; then
    image="quay.io/mgrimald/ogx-mlflow-sidecar:latest"
  fi

  echo "==> Patching ${deployment} ogx-server → ${image}"

  local idx=-1
  local i name
  for i in 0 1 2 3; do
    name="$(oc get deployment "${deployment}" -n "${namespace}" \
      -o "jsonpath={.spec.template.spec.containers[${i}].name}" 2>/dev/null || true)"
    [[ "${name}" == "ogx-server" ]] && idx="${i}" && break
  done
  if (( idx < 0 )); then
    echo "error: deployment ${deployment} has no ogx-server container"
    return 1
  fi

  oc patch "deployment/${deployment}" -n "${namespace}" --type=json -p="[
    {\"op\": \"replace\", \"path\": \"/spec/template/spec/initContainers\", \"value\": []},
    {\"op\": \"replace\", \"path\": \"/spec/template/spec/containers/${idx}/image\", \"value\": \"${image}\"},
    {\"op\": \"remove\", \"path\": \"/spec/template/spec/containers/${idx}/command\"}
  ]" 2>/dev/null || oc patch "deployment/${deployment}" -n "${namespace}" --type=json -p="[
    {\"op\": \"replace\", \"path\": \"/spec/template/spec/initContainers\", \"value\": []},
    {\"op\": \"replace\", \"path\": \"/spec/template/spec/containers/${idx}/image\", \"value\": \"${image}\"}
  ]"

  oc patch "deployment/${deployment}" -n "${namespace}" --type=strategic -p "
spec:
  template:
    spec:
      containers:
      - name: ogx-server
        imagePullPolicy: Always
        env:
        - name: PYTHONPATH
          value: /app/ogx-mlflow
"
}

# Before agent deploy.
# OGX_MLFLOW_SIDECAR_MODE:
#   quay (default)    — pull OGX_MLFLOW_SIDECAR_IMAGE / cluster-config ogx_mlflow_sidecar_image
#   cluster           — ogx-mlflow-sidecar/build-and-deploy.sh (OpenShift binary build)
ensure_ogx_mlflow_sidecar() {
  local namespace="$1"
  local repo_root="$2"
  local mode="${OGX_MLFLOW_SIDECAR_MODE:-quay}"

  case "${mode}" in
    cluster)
      ensure_ogx_mlflow_sidecar_image_cluster "${namespace}" "${repo_root}"
      ;;
    quay)
      echo "==> OGX_MLFLOW_SIDECAR_MODE=quay — using ${OGX_MLFLOW_SIDECAR_IMAGE:-quay.io/mgrimald/ogx-mlflow-sidecar:latest}"
      echo "    Publish the image with ogx-mlflow-sidecar/build-and-push-quay.sh or a Quay build trigger."
      ;;
    *)
      echo "error: unknown OGX_MLFLOW_SIDECAR_MODE=${mode} (use cluster|quay)"
      return 1
      ;;
  esac
}

# After oc apply deployment — aligns ogx-server image with mode (Quay default in git).
maybe_apply_ogx_mlflow_sidecar_image() {
  local deployment="$1"
  local namespace="$2"
  apply_ogx_mlflow_sidecar_image "${deployment}" "${namespace}"
}
