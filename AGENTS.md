# AGENTS.md

Guidance for AI coding agents working in this repository.

## What this repo is

A GitOps-provisioned OpenShift demo: three independent AI agents (each its
own Gradio UI + OGX sidecar pod) that generate repo documentation and
check architectural compliance for a given GitHub repo — `doc-gen-agent`
produces architecture documentation with Mermaid C4 diagrams,
`openapi-gen-agent` produces an OpenAPI specification for repos that look
like HTTP API services, and `compliance-scan-agent` scans K8s/OpenShift
manifests, project structure, and `pom.xml` content for bad practices
against separately-maintained guideline files and reports findings (it
never opens a PR). All three use a model served by an external,
configurable OpenAI-compatible endpoint (no GPU or in-cluster model
serving on this branch) and a shared GitHub MCP server for code
exploration. It is a simplified take on
[opendatahub-io/architecture-context](https://github.com/opendatahub-io/architecture-context).
The three agents are fully independent — separate pods, UIs, and
budgets; none of them trigger each other.

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
compliance-scan-agent/   # OGX sidecar (agentic loop) + Gradio UI — architectural compliance scan (independent, report-only)
governance-hub/          # FastAPI compliance dashboard + REST API
compliance-mcp-server/   # Standalone, read-only Compliance MCP server (proxies governance-hub's REST API)
build-and-deploy-all.sh  # Non-GitOps quick-start for the app components
```

Each deployable component (`github-mcp-server/`, `doc-gen-agent/`,
`openapi-gen-agent/`, `compliance-scan-agent/`, `governance-hub/`,
`compliance-mcp-server/`) is self-contained: source, `Dockerfile`,
`requirements.txt`, `build-and-deploy.sh`, and a `k8s/` directory with
its manifests. `doc-gen-agent`, `openapi-gen-agent`, and
`compliance-scan-agent` are three separate agents, not slices of one
component — don't reintroduce cross-calls between them (e.g. one
triggering another's generation or budget) without the user explicitly
asking for that coupling back. Unlike the other two, `compliance-scan-agent`
never opens a PR — its only deliverable is a Markdown report, returned in
its chat UI and (when invoked via `/invoke`) sent to `governance-hub`'s
`POST /api/runs` callback. `governance-hub` is a fourth, independent
component: it only *observes* all three agents (via their optional
`POST /api/runs` callback, controlled by each agent's
`GOVERNANCE_HUB_URL` env var) and *triggers* them (via their existing
`POST /invoke`, exactly like the CI/CD pipeline does for the first two) —
it never reads any agent's internals, and no agent depends on
`governance-hub` being present or healthy. `compliance-mcp-server` is a
fifth, independent component: a standalone MCP server that lets other AI
agents query governance-hub's compliance state. It holds no state and no
database of its own — every tool it exposes is a thin read-only proxy
over governance-hub's existing REST API (`GET /api/repos`,
`GET /api/repos/{owner}/{repo}/status`). Don't give it direct database
access (e.g. a shared PVC mount) — that would couple it to
governance-hub's storage layer instead of its public API.

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
  before `doc-gen-agent`, `openapi-gen-agent`, `compliance-scan-agent`,
  `governance-hub`, and `compliance-mcp-server` (all five wave 1, applied
  concurrently), because of the Service DNS dependency.
  `compliance-mcp-server` depends on `governance-hub`'s Service DNS name
  too but stays wave 1 rather than a stricter wave 2 — it tolerates
  governance-hub being briefly unavailable (each tool returns an
  `"error"` key until reachable), the same tolerance governance-hub
  itself has for all three agents. Preserve the
  `argocd.argoproj.io/sync-wave` annotations when adding resources.
- **ConfigMaps generated from files are not committed as inline YAML.**
  `ogx-stack-config` and `agent-knowledge`/`openapi-agent-knowledge`/
  `compliance-scan-knowledge` (per component) are created by each
  component's `build-and-deploy.sh` via
  `oc create configmap --from-file=...` to avoid YAML-in-YAML. If you add
  a new file-backed ConfigMap, follow the same pattern and document it
  with a comment at the top of the Deployment manifest that references
  it.
- **`doc-gen-agent`, `openapi-gen-agent`, and `compliance-scan-agent`
  share the `cluster-config` ConfigMap and `external-model-credentials`
  Secret** (both in the `doc-gen-demo` namespace) — only
  `doc-gen-agent/k8s/cluster-config.yaml` is committed; the other two
  agents' Deployments reference the same ConfigMap name rather than
  duplicating it. Don't create a second `cluster-config.yaml` under
  `openapi-gen-agent/k8s/` or `compliance-scan-agent/k8s/` — that would
  create a resource ArgoCD-ownership conflict between the Applications.
- **`governance-hub` persists its compliance database (tracked repos +
  agent run history) to a PVC** (`governance-hub-data`, mounted at `/data`,
  `DATABASE_PATH` env var), not a ConfigMap — it's a SQLite file that the
  app itself writes to, not a static file checked into git. Don't swap
  this for a ConfigMap/Secret-backed approach.
- **Model identity must stay in sync across four files** when pointing
  at a different model or endpoint: `doc-gen-agent/k8s/cluster-config.yaml`
  (`model_base_url`, `model_name` — shared by all three agents), and
  **each** agent's own `ogx/stack_run_config.yaml`
  (`registered_resources.models[0].model_id`/`provider_model_id` in
  `doc-gen-agent/ogx/stack_run_config.yaml`,
  `openapi-gen-agent/ogx/stack_run_config.yaml`, AND
  `compliance-scan-agent/ogx/stack_run_config.yaml`). The endpoint's API
  key goes in the `external-model-credentials` Secret (created
  out-of-band, never committed), not in any of those files.
- **`compliance-scan-agent`'s compliance checks live in separate
  guideline Markdown files** under `compliance-scan-agent/guidelines/`
  (one per check category: K8s/OpenShift manifests, project structure,
  `pom.xml`), concatenated into the agent's system prompt at runtime (see
  `agent.py`'s `_load_guidelines`). Add a new check category as a new
  guideline file plus a `GUIDELINE_FILES` entry, not by inlining more
  rules into the Python system-prompt string — that keeps each category
  independently reviewable and small enough for the tight Qwen token
  budget this agent runs under (see `MAX_INFER_ITERS`/`INFERENCE_MAX_TOKENS`
  in `compliance-scan-agent/k8s/deployment.yaml`).

## Making changes

- **Python services** (`github-mcp-server/server.py`,
  `doc-gen-agent/agent.py`, `doc-gen-agent/app.py`,
  `openapi-gen-agent/agent.py`, `openapi-gen-agent/app.py`,
  `compliance-scan-agent/agent.py`, `compliance-scan-agent/app.py`,
  `governance-hub/app.py`, `governance-hub/models.py`,
  `governance-hub/compliance.py`, `compliance-mcp-server/server.py`): run
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
