# AGENTS.md

Guidance for AI coding agents working in this repository.

## What this repo is

A GitOps-provisioned OpenShift demo: two independent AI agents (each its
own Gradio UI + OGX sidecar pod) that generate repo documentation for a
given GitHub repo — `doc-gen-agent` produces architecture documentation
with Mermaid C4 diagrams, and `openapi-gen-agent` produces an OpenAPI
specification for repos that look like HTTP API services. Both use a
model served by an external, configurable OpenAI-compatible endpoint (no
GPU or in-cluster model serving on this branch) and a shared GitHub MCP
server for code exploration. It is a simplified take on
[opendatahub-io/architecture-context](https://github.com/opendatahub-io/architecture-context).
The two agents are fully independent — separate pods, UIs, budgets, and
PRs; neither triggers the other.

> This is the `main` branch. The [`local-model`](../../tree/local-model)
> branch preserves the original design where the model is served
> in-cluster by vLLM on OpenShift AI (GPU required) — its own `AGENTS.md`
> documents that architecture's conventions. Don't port vLLM/RHOAI/GPU
> conventions from there back onto `main`.

Read [`README.md`](README.md) first for the full architecture diagram and
component breakdown before making changes.

## Repository layout

```
gitops/                 # ArgoCD App-of-Apps: bootstrap.sh is the entry point
  apps/                  # Child ArgoCD Application manifests (one per component)
github-mcp-server/       # FastMCP server exposing GitHub repo browsing as MCP tools
doc-gen-agent/           # OGX sidecar (agentic loop) + Gradio UI — architecture docs & diagrams
openapi-gen-agent/       # OGX sidecar (agentic loop) + Gradio UI — OpenAPI spec generation (independent)
build-and-deploy-all.sh  # Non-GitOps quick-start for the app components
```

Each deployable component (`github-mcp-server/`, `doc-gen-agent/`,
`openapi-gen-agent/`) is self-contained: source, `Dockerfile`,
`requirements.txt`, `build-and-deploy.sh`, and a `k8s/` directory with
its manifests. `doc-gen-agent` and `openapi-gen-agent` are two separate
agents, not two halves of one component — don't reintroduce
cross-calls between them (e.g. one triggering the other's generation or
budget) without the user explicitly asking for that coupling back.

## Core conventions

- **GitOps is the source of truth for deployment.** Any new Kubernetes
  resource must live under a component's `k8s/` directory so ArgoCD picks
  it up. Don't hand-apply one-off resources without also committing the
  manifest.
- **ArgoCD Application `repoURL` fields contain a placeholder**
  (`https://github.com/YOUR_ORG/agentic-doc-generation.git`). Never
  hardcode a real URL in a manifest — `gitops/bootstrap.sh` substitutes it
  at bootstrap time via `sed`.
- **Sync-wave ordering matters.** `github-mcp-server` (wave 0) must land
  before `doc-gen-agent` and `openapi-gen-agent` (both wave 1, applied
  concurrently), because of the Service DNS dependency. Preserve the
  `argocd.argoproj.io/sync-wave` annotations when adding resources.
- **ConfigMaps generated from files are not committed as inline YAML.**
  `ogx-stack-config` and `agent-knowledge` (per component) are created by
  each component's `build-and-deploy.sh` via
  `oc create configmap --from-file=...` to avoid YAML-in-YAML. If you add
  a new file-backed ConfigMap, follow the same pattern and document it
  with a comment at the top of the Deployment manifest that references
  it.
- **`doc-gen-agent` and `openapi-gen-agent` share the `cluster-config`
  ConfigMap and `external-model-credentials` Secret** (both in the
  `doc-gen-demo` namespace) — only `doc-gen-agent/k8s/cluster-config.yaml`
  is committed; `openapi-gen-agent`'s Deployment references the same
  ConfigMap name rather than duplicating it. Don't create a second
  `cluster-config.yaml` under `openapi-gen-agent/k8s/` — that would create
  a resource ArgoCD-ownership conflict between the two Applications.
- **Model identity must stay in sync across three files** when pointing
  at a different model or endpoint: `doc-gen-agent/k8s/cluster-config.yaml`
  (`model_base_url`, `model_name` — shared by both agents), and **each**
  agent's own `ogx/stack_run_config.yaml`
  (`registered_resources.models[0].model_id`/`provider_model_id` in
  `doc-gen-agent/ogx/stack_run_config.yaml` AND
  `openapi-gen-agent/ogx/stack_run_config.yaml`). The endpoint's API key
  goes in the `external-model-credentials` Secret (created out-of-band,
  never committed), not in any of those files.

## Making changes

- **Python services** (`github-mcp-server/server.py`,
  `doc-gen-agent/agent.py`, `doc-gen-agent/app.py`,
  `openapi-gen-agent/agent.py`, `openapi-gen-agent/app.py`): run
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

- Don't call an LLM API directly from `agent.py`/`app.py` — inference
  must always go through the OGX sidecar's `remote::vllm`-protocol
  provider, configured to point at whatever external OpenAI-compatible
  endpoint `cluster-config.yaml` specifies. The sidecar is the only
  component that should talk to the model endpoint.
- Don't reintroduce an in-cluster model-serving dependency (RHOAI
  operator, KServe, vLLM `ServingRuntime`/`InferenceService`, GPU
  resource requests) on `main` — that design lives on the `local-model`
  branch. If a change needs it, it belongs on that branch instead.
