# AGENTS.md

Guidance for AI coding agents working in this repository.

## What this repo is

A GitOps-provisioned OpenShift AI demo: an AI agent (Gradio UI + OGX
sidecar) that generates architecture documentation and Mermaid diagrams
for a given GitHub repo, using a locally-served model (vLLM on OpenShift
AI) and a GitHub MCP server for code exploration. It is a simplified,
single-agent take on
[opendatahub-io/architecture-context](https://github.com/opendatahub-io/architecture-context).

Read [`README.md`](README.md) first for the full architecture diagram and
component breakdown before making changes.

## Repository layout

```
gitops/                 # ArgoCD App-of-Apps: bootstrap.sh is the entry point
  operators/rhoai/       # RHOAI operator + DSCInitialization/DataScienceCluster (sync-wave 0)
  model-serving/         # Namespace + vLLM ServingRuntime + InferenceService (sync-wave 1)
  apps/                  # Child ArgoCD Application manifests (one per component)
github-mcp-server/       # FastMCP server exposing GitHub repo browsing as MCP tools
doc-gen-agent/           # OGX sidecar (agentic loop) + Gradio UI (agent.py / app.py)
build-and-deploy-all.sh  # Non-GitOps quick-start for the two app components
```

Each deployable component (`github-mcp-server/`, `doc-gen-agent/`) is
self-contained: source, `Dockerfile`, `requirements.txt`,
`build-and-deploy.sh`, and a `k8s/` directory with its manifests.

## Core conventions

- **GitOps is the source of truth for deployment.** Any new Kubernetes
  resource must live under a component's `k8s/` directory (or
  `gitops/operators/` or `gitops/model-serving/`) so ArgoCD picks it up.
  Don't hand-apply one-off resources without also committing the manifest.
- **ArgoCD Application `repoURL` fields contain a placeholder**
  (`https://github.com/YOUR_ORG/agentic-doc-generation.git`). Never
  hardcode a real URL in a manifest — `gitops/bootstrap.sh` substitutes it
  at bootstrap time via `sed`.
- **Sync-wave ordering matters.** `rhoai-operator` (wave 0) must land
  before `model-serving`/`github-mcp-server` (wave 1), which must land
  before `doc-gen-agent` (wave 2), because of CRD and Service DNS
  dependencies. Preserve the `argocd.argoproj.io/sync-wave` annotations
  when adding resources.
- **ConfigMaps generated from files are not committed as inline YAML.**
  `ogx-stack-config` and `agent-knowledge` are created by
  `build-and-deploy.sh` via `oc create configmap --from-file=...` to avoid
  YAML-in-YAML. If you add a new file-backed ConfigMap, follow the same
  pattern and document it with a comment at the top of the Deployment
  manifest that references it.
- **Model identity must stay in sync across three files** when changing
  the served model: `gitops/model-serving/inference-service.yaml`
  (`storageUri`), `doc-gen-agent/k8s/cluster-config.yaml` (`model_name`),
  and `doc-gen-agent/ogx/stack_run_config.yaml`
  (`registered_resources.models[0].model_id`/`provider_model_id`).

## Making changes

- **Python services** (`github-mcp-server/server.py`,
  `doc-gen-agent/agent.py`, `doc-gen-agent/app.py`): run
  `python3 -m py_compile <file>` after edits; there is no test suite yet.
- **YAML manifests**: validate with
  `python3 -c "import yaml,sys; yaml.safe_load_all(open(sys.argv[1]))" <file>`
  (or run it across all files with glob) before committing — this repo has
  no CI yet, so this is the only automated check.
- **Shell scripts**: run `bash -n <script>` after edits.
- Keep container images non-root (UID 1001, `chmod g=u`) and set
  `resources.requests`/`limits` on every container, matching the existing
  Deployments.

## What NOT to do

- Don't introduce a Service Mesh/Knative dependency for model serving —
  this demo deliberately uses KServe `RawDeployment` mode
  (`dsci.serviceMesh.managementState: Removed`) to keep the footprint
  small.
- Don't add components to `gitops/operators/rhoai/dsc.yaml` without
  setting `managementState: Removed` explicitly for anything not needed —
  the DSC lists every component explicitly to keep the cluster footprint
  predictable.
- Don't call external LLM APIs from the agent — the whole point of the
  demo is that inference happens in-cluster via the vLLM
  `InferenceService`.
