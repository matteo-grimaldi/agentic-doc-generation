# AGENTS.md

Guidance for AI coding agents working in this repository.

## What this repo is

A GitOps-provisioned OpenShift demo: four independent AI agents (each its
own Gradio UI + OGX sidecar pod) that generate repo documentation and
check architectural compliance for a given GitHub repo — `doc-gen-agent`
produces architecture documentation with Mermaid C4 diagrams,
`openapi-gen-agent` produces an OpenAPI specification for repos that look
like HTTP API services, and `compliance-scan-agent` scans K8s/OpenShift
manifests, project structure, and `pom.xml` content for bad practices
against separately-maintained guideline files and reports findings (it
never opens a PR). `catalog-gen-agent` generates or augments Backstage
`catalog-info.yaml` with a Component and applicable API entities, and
can propose it as a PR. All four reach the model through the in-cluster
OpenShift AI AI Gateway / Models-as-a-Service
(no GPU or in-cluster vLLM serving on this branch — the gateway fronts
an upstream OpenAI-compatible endpoint) and a shared GitHub MCP server
for code exploration. It is a simplified take on
[opendatahub-io/architecture-context](https://github.com/opendatahub-io/architecture-context).
The four agents are fully independent — separate pods, UIs, and
budgets; none of them trigger each other.

> This is the `main` branch. The [`local-model`](../../tree/local-model)
> branch preserves the original design where the model is served
> in-cluster by vLLM on OpenShift AI (GPU required) — its own `AGENTS.md`
> documents that architecture's conventions. Don't port vLLM/GPU
> serving conventions from there back onto `main`. AI Gateway / MaaS
> (routing/governance only, no local weights) is intentional on `main`.

Read [`README.md`](README.md) first for the full architecture diagram and
component breakdown before making changes.

## Repository layout

```
gitops/                 # ArgoCD App-of-Apps: bootstrap.sh is the entry point
  prepare.sh             # Build seven app images/configuration without applying workloads
  argocd-namespace-rolebindings.yaml  # Imperative Argo CD admin in demo namespaces
  argocd-instance-demo.yaml           # Argo CD UI RBAC + rhdh-local-user (merge patch)
  preflight.sh           # CRD/optional runtime checks before bootstrap
  apps/                  # Child ArgoCD Application manifests (one per component)
github-mcp-server/       # FastMCP server exposing GitHub repo browsing as MCP tools
mcp-gateway/             # Connectivity Link MCP Gateway wiring (Gateway, AuthPolicy, registrations)
ogx-mlflow-sidecar/      # MLflow sidecar image (Dockerfile + cluster build-and-deploy.sh)
ai-gateway/              # MaaS/AI Gateway manifests (ExternalModel, Gateway, demo Postgres); applied by install-openshift-ai.sh
doc-gen-agent/           # OGX sidecar (agentic loop) + Gradio UI — architecture docs & diagrams
openapi-gen-agent/       # OGX sidecar (agentic loop) + Gradio UI — OpenAPI spec generation (independent)
compliance-scan-agent/   # OGX sidecar (agentic loop) + Gradio UI — architectural compliance scan (independent, report-only)
catalog-gen-agent/       # OGX sidecar (agentic loop) + Gradio UI — Backstage catalog-info.yaml generation
governance-hub/          # FastAPI compliance dashboard + REST API
compliance-mcp-server/   # Standalone, read-only Compliance MCP server (proxies governance-hub's REST API)
install-openshift-ai.sh  # Imperative RHOAI + calls install-mcp-gateway.sh + AI Gateway / MaaS bootstrap (run before GitOps)
install-mcp-gateway.sh   # RHCL + MCP Gateway Operator; re-run to patch mcp_gateway_url
build-and-deploy-all.sh  # Non-GitOps quick-start for the app components
```

Each deployable component (`github-mcp-server/`, `doc-gen-agent/`,
`openapi-gen-agent/`, `compliance-scan-agent/`, `catalog-gen-agent/`,
`governance-hub/`, `compliance-mcp-server/`) is self-contained: source,
`Dockerfile`, `requirements.txt`, `build-and-deploy.sh`, and a `k8s/`
directory with its manifests. `ogx-mlflow-sidecar/` holds the sidecar
`Dockerfile`, `start_ogx.py`, `mlflow_telemetry.py`, in-cluster
`build-and-deploy.sh`, and optional `build-and-push-quay.sh` for
[`quay.io/mgrimald/ogx-mlflow-sidecar`](https://quay.io/repository/mgrimald/ogx-mlflow-sidecar).
Agent Deployments default to **`quay.io/mgrimald/ogx-mlflow-sidecar:latest`**
(`cluster-config` `ogx_mlflow_sidecar_image` must stay in sync). Use
`OGX_MLFLOW_SIDECAR_MODE=cluster` to build on OpenShift and patch to the
namespace ImageStream instead. `doc-gen-agent`, `openapi-gen-agent`,
`compliance-scan-agent`, and `catalog-gen-agent` are four separate agents,
not slices of one component — don't reintroduce cross-calls between them (e.g. one
triggering another's generation or budget) without the user explicitly
asking for that coupling back. Unlike the other three, `compliance-scan-agent`
never opens a PR — its only deliverable is a Markdown report, returned in
its chat UI and (when invoked via `/invoke`) sent to `governance-hub`'s
`POST /api/runs` callback. Chat outcomes also use this optional callback.
`governance-hub` is an independent FastAPI dashboard and REST API: it
*observes* doc-gen, OpenAPI, and compliance-scan outcomes via optional
`POST /api/runs` callbacks, controlled by each agent's
`GOVERNANCE_HUB_URL` env var, and explicitly *triggers* those three via
their existing `POST /invoke` endpoints. Doc-gen chat does not currently
report outcomes; OpenAPI and compliance chat do. Catalog callbacks use
`catalog-gen`, which the Hub currently rejects, and the Hub cannot trigger
catalog generation. Preserve these boundaries until integration changes
are explicitly requested. The Hub never reads an agent's internals, and
no agent depends on it being present or healthy. `compliance-mcp-server`
is another independent component: a standalone MCP server that lets other AI
agents query governance-hub's compliance state. It holds no state and no
database of its own — every tool it exposes is a thin read-only proxy
over governance-hub's existing REST API (`GET /api/repos`,
`GET /api/repos/{owner}/{repo}/status`). Its streamable HTTP `/mcp`
transport exposes `get_compliance_status`, `list_violations`,
`list_tracked_repos`, and `get_repo_history`. Don't give it direct database
access (e.g. a shared PVC mount) — that would couple it to
governance-hub's storage layer instead of its public API.

## Core conventions

- **GitOps is the source of truth for deployment.** Any new Kubernetes
  resource must live under a component's `k8s/` directory so ArgoCD picks
  it up. Don't hand-apply one-off resources without also committing the
  manifest.
- **ArgoCD Application `repoURL` fields use this fork's real GitHub URL**
  (`https://github.com/matteo-grimaldi/agentic-doc-generation.git`), same
  as the other child apps. App-of-Apps syncs those manifests as-is — a
  `YOUR_ORG` placeholder left in git never gets rewritten and breaks
  sync. `gitops/bootstrap.sh` can still rewrite the URL when bootstrapping
  a different fork.
- **OpenShift GitOps RBAC:** `gitops/bootstrap.sh` applies
  `gitops/argocd-namespace-rolebindings.yaml` imperatively (not via ArgoCD)
  so the application-controller can patch workloads in `doc-gen-demo`,
  `ci-cd-demo`, and `mcp-gateway`. Don't remove those RoleBindings on
  clusters using GitOps.
- **Sync-wave ordering matters.** Wave 0 is `github-mcp-server` and
  `mcp-gateway` (Connectivity Link MCP Gateway wiring). Wave 1 is
  `doc-gen-agent`, `openapi-gen-agent`, `compliance-scan-agent`,
  `catalog-gen-agent`, `governance-hub`, and `compliance-mcp-server`
  (applied concurrently). Agents use the gateway for MCP tools
  (`MCP_GATEWAY_URL` + projected SA token + virtual server
  `mcp-gateway/github-tools`) and the GitHub MCP Service DNS for REST
  (`GITHUB_MCP_SERVER_URL`). `compliance-mcp-server` depends on
  `governance-hub`'s Service DNS name too but stays wave 1 — it
  tolerates governance-hub being briefly unavailable. Preserve the
  `argocd.argoproj.io/sync-wave` annotations when adding resources.
- **`github-mcp-server` and `compliance-mcp-server` are deployed as
  `MCPServer` custom resources** (`k8s/mcpserver.yaml`), not a
  hand-written `Deployment`/`Service` — RHOAI's `mcplifecycleoperator`
  DataScienceCluster component reconciles each into an operator-owned
  Deployment/Service/NetworkPolicy with the same name/Service DNS, which
  is also what makes them visible under OpenShift AI's AI Hub "Deployed
  MCP Servers" tab. This is a deliberate, documented exception to this
  branch's otherwise-zero in-cluster-operator-dependency design (see
  `.cursor/rules/gitops-conventions.mdc`): both now hard-require
  `./install-openshift-ai.sh` to have been run first, same
  imperative-prerequisite pattern as `ci-cd-pipeline`'s Tekton CRDs.
  Don't apply an `MCPServer` with the same name as a resource it doesn't
  already own — the operator refuses to adopt it (delete the old
  resource first). Every other component in this repo still uses plain
  `Deployment`/`Service` manifests; don't convert them without the same
  explicit tradeoff discussion.
- **MCP Gateway** (Technology Preview) is installed by
  `./install-openshift-ai.sh` via a call to `./install-mcp-gateway.sh`
  (Connectivity Link + MCP Gateway Operator). Re-run
  `./install-mcp-gateway.sh` alone after GitOps syncs `mcp-gateway/k8s`
  to patch `cluster-config` `mcp_gateway_url` if still empty. The
  DataScienceCluster `aigateway` component is the *model* gateway;
  `install-openshift-ai.sh` step 6 sets it and its nested
  `modelsAsAService` component to `Managed` after platform prerequisites
  are ready. This enables external-model routing/governance, not local
  model serving or GPUs. Gen AI Playground `mcp-catalog-entry.yaml` URLs stay
  on the direct MCP Service — that ConfigMap cannot send a TokenReview
  bearer or `X-Mcp-Virtualserver`.
- **ConfigMaps generated from files are not committed as inline YAML.**
  `ogx-stack-config` and `agent-knowledge`/`openapi-agent-knowledge`/
  `compliance-scan-knowledge`/`catalog-gen-knowledge` are generated by
  `scripts/configure-agents.sh`, called by each agent build script, via
  `oc create configmap --from-file=...` to avoid YAML-in-YAML.
  All four agents mount one shared `ogx-stack-config`, generated from the
  canonical `doc-gen-agent/ogx/stack_run_config.yaml`; other agents' stack
  files are reference copies, not deployment inputs. Knowledge maps stay
  separate. The generator preserves existing installer-managed
  `model_base_url` and `mcp_gateway_url` and does not restart pods. If you add
  a new file-backed ConfigMap, follow the same pattern and document it
  with a comment at the top of the Deployment manifest that references
  it.
- **`doc-gen-agent`, `openapi-gen-agent`, `compliance-scan-agent`, and
  `catalog-gen-agent` share the `cluster-config` ConfigMap and
  `external-model-credentials` Secret** (both in the `doc-gen-demo`
  namespace) — only `doc-gen-agent/k8s/cluster-config.yaml` is committed;
  the other agents' Deployments reference the same ConfigMap name rather
  than duplicating it. That ConfigMap also holds optional
  `mlflow_tracking_uri` (empty = tracing off). Don't create a second
  `cluster-config.yaml` under another agent's `k8s/` — that would create
  a resource ArgoCD-ownership conflict between the Applications.
- **`governance-hub` persists its compliance database (tracked repos +
  agent run history) to a PVC** (`governance-hub-data`, mounted at `/data`,
  `DATABASE_PATH` env var), not a ConfigMap — it's a SQLite file that the
  app itself writes to, not a static file checked into git. Don't swap
  this for a ConfigMap/Secret-backed approach.
- **Model identity must stay in sync** when pointing at a different
  model: `doc-gen-agent/k8s/cluster-config.yaml` (`model_base_url` =
  in-cluster AI Gateway `/v1` Service URL, `model_name` =
  `external-model/<id>` — shared by all four agents), **each** agent's
  `ogx/stack_run_config.yaml` (`model_id` / `provider_model_id` = bare
  id; doc-gen's is canonical and the others are reference copies), and
  the `ExternalModel` / `MaaSModelRef` under `ai-gateway/k8s/`
  (same bare id). The MaaS API key (`sk-oai-...`) goes in
  `external-model-credentials` (minted by `install-openshift-ai.sh` or
  set via `EXTERNAL_MODEL_API_KEY`); the upstream provider key stays in
  `llm/workshop-model-api-key` from `UPSTREAM_MODEL_API_KEY`.
- **Deployment preparation and rollouts:** install OpenShift AI and Tekton
  before GitOps, then run `bash gitops/prepare.sh` before bootstrap.
  `PREPARE_ONLY=true` on the seven application build scripts stops before
  workload manifests/rollout; it still builds images and updates configuration.
  Non-GitOps aggregate deployment includes `mcp-gateway` before the agents;
  its script waits for the compatibility Job and configures the MCP URL.
  File-backed maps require regeneration and environment/stack changes require
  agent restarts; Argo CD does not automatically perform those steps.
- **OGX MLflow sidecar image** defaults to Quay: keep
  `cluster-config` `ogx_mlflow_sidecar_image` in sync with the
  `ogx-server` `image` field in all four agent `k8s/deployment.yaml`
  files. Use `OGX_MLFLOW_SIDECAR_MODE=cluster` only when building
  `ogx-mlflow-sidecar` into the namespace ImageStream instead.
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
  provider, which talks to the OpenShift AI AI Gateway URL in
  `cluster-config.yaml` (not the upstream provider). The sidecar is the
  only agent component that should open an inference connection; the
  gateway is the only path to the upstream model.
- Don't reintroduce in-cluster model *serving* (vLLM
  `ServingRuntime`/`InferenceService`, GPU resource requests) on `main`
  — that design lives on the `local-model` branch. AI Gateway / MaaS
  (routing and governance in front of an external endpoint) is expected
  on `main`.
