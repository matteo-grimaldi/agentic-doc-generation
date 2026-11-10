# README gap remediation backlog

Recorded: 2026-11-06. Source: static comparison of the current README against
repository source, manifests, scripts, and existing documentation. Deployed
cluster behavior was not tested in the initial static audit. These are findings to revalidate before
remediation, not declarations that every implementation should change.

Audit baseline commit: `3b1d6e83ff3b8626dcbdf2384f15bb2ec1bc342b`.

Read-only environment verification on 2026-11-06 confirmed four ready agent
pods, a bound Governance Hub PVC, and DSC `aigateway` plus nested
`modelsAsAService` both `Managed`. On 2026-11-07 the user approved
documentation corrections for RG-001 through RG-007; their resolutions
are recorded below. No runtime behavior or deployment was changed.

On 2026-11-08 the user approved implementation of RG-008 through RG-013.
Deployment scripts, configuration generation, and operating instructions
were updated and checked offline; no live builds, rollouts, or platform
changes were executed for this batch.

## Working agreement

- All items start **pending**; no remediation has been authorized by this file.
- The user selects an ID or explicitly authorizes a batch. Work only on that
  scope, and ask for direction when choosing between changing documented
  expectations and changing application behavior would affect architecture.
- Recheck the evidence against the current checkout before implementing an item.
- Preserve IDs when editing or reordering this backlog. Reference related IDs
  when one change resolves several findings.
- For each completed item, record the resolution, affected files, validation,
  and commit if available in the resolution log. Check the box only after the
  stated completion criterion is met; record deferrals or rejected findings too.
- Follow AGENTS.md and applicable validation rules for any implementation work.
  This audit itself makes no README or application changes.

Evidence links are relative to this file. Suggested resolutions below are
proposals for supervised work, not instructions to execute immediately.

## Architecture and component coverage

- [x] **RG-001 — Outdated two-agent overview.** Introduction, architecture
  diagram, walkthrough, and project tree focus on two agents, whereas four are
  deployed and later README sections already refer to four.
  Evidence: [component list](../build-and-deploy-all.sh).
  Completion: consistently describe all four independent agents throughout.

- [x] **RG-002 — Missing compliance-scan workflow.** Explain Kubernetes/OpenShift,
  project-structure, and Maven checks; separate guideline files; Markdown report;
  UI and invocation behavior; and the absence of PR creation.
  Evidence: [agent](../compliance-scan-agent/agent.py),
  [application](../compliance-scan-agent/app.py).
  Completion: add a component description and usable walkthrough matching code.

- [x] **RG-003 — Missing catalog-generation workflow.** Document generation or
  augmentation of catalog-info.yaml, Component and applicable API entities,
  guidelines, independent UI/invocation, and agentic-catalog/* PRs.
  Evidence: [agent](../catalog-gen-agent/agent.py),
  [application](../catalog-gen-agent/app.py).
  Completion: cover the component in the overview, tree, and workflows.

- [x] **RG-004 — Missing Governance Hub backend architecture.** The plugin is
  described, but its independent FastAPI backend, dashboard, history, callbacks,
  checks, remediation triggers, and SQLite/PVC persistence are largely omitted.
  Evidence: [application](../governance-hub/app.py),
  [models](../governance-hub/models.py), [PVC](../governance-hub/k8s/pvc.yaml).
  Completion: explain the backend, storage, interfaces, and optional relationship
  to agents separately from the portal plugin.

- [x] **RG-005 — Missing Compliance MCP functionality.** Explain its read-only
  REST proxy design and get_compliance_status, list_violations,
  list_tracked_repos, and get_repo_history tools.
  Evidence: [server](../compliance-mcp-server/server.py).
  Completion: describe tools, transport, dependency, and lack of database access.

- [x] **RG-006 — Incorrect architecture deployment arrows and boundaries.** The
  diagram shows App-of-Apps deploying Keycloak and RHDH despite no child
  Applications, and places the in-cluster model gateway outside the cluster.
  Evidence: [Applications](../gitops/apps/),
  [model Gateway](../ai-gateway/k8s/gateway.yaml).
  Completion: align diagram ownership and cluster boundaries with manifests.

- [x] **RG-007 — Obsolete DSC aigateway state claim.** README says it stays
  Removed; installer step 6 enables aigateway and modelsAsAService. AGENTS.md
  repeats the Removed convention, so reconcile intended behavior with the user.
  Evidence: [installer](../install-openshift-ai.sh), [guidance](../AGENTS.md).
  Completion: agree intended state and align documentation and implementation
  within explicitly approved scope.

## Deployment and configuration workflows

- [x] **RG-008 — Fresh GitOps quick start lacks image/ConfigMap preparation.**
  Bootstrap does not build internal application images or generate required
  OGX/knowledge ConfigMaps; Deployments depend on them.
  Evidence: [doc-gen build](../doc-gen-agent/build-and-deploy.sh),
  [Deployment](../doc-gen-agent/k8s/deployment.yaml),
  [bootstrap](../gitops/bootstrap.sh).
  Completion: document or implement an agreed complete fresh-install sequence.

- [x] **RG-009 — GitOps quick start omits mandatory Tekton installation.**
  Bootstrap preflight fails without the Pipeline CRD, but the quick-start
  commands omit installing OpenShift Pipelines.
  Evidence: [preflight](../gitops/preflight.sh),
  [operator installer](../ci-cd-pipeline/install-operator.sh).
  Completion: show prerequisite installation before bootstrap.

- [x] **RG-010 — Non-GitOps default scope is broader than described.** The
  aggregate script also deploys CI/CD, Keycloak, and Developer Hub, although the
  README introduces it as application-components-only and deploys the portal
  again separately.
  Evidence: [aggregate deploy](../build-and-deploy-all.sh).
  Completion: make examples and scope match the agreed script behavior.

- [x] **RG-011 — Non-GitOps MCP Gateway deployment is incomplete.** Aggregate
  deployment omits the gateway resources; the gateway installer expects those
  resources to exist, and agents fail closed with an empty mcp_gateway_url.
  Evidence: [aggregate deploy](../build-and-deploy-all.sh),
  [gateway installer](../install-mcp-gateway.sh),
  [agent check](../doc-gen-agent/agent.py).
  Completion: establish and document a complete non-GitOps gateway sequence.

- [x] **RG-012 — Automatic configuration rollout is overstated.** ArgoCD watches
  k8s/, while build scripts generate ogx-stack-config from files outside it.
  ConfigMap environment changes need restarts, and gateway URL keys have
  ignoreDifferences entries.
  Evidence: [Application](../gitops/apps/doc-gen-agent.yaml),
  [build script](../doc-gen-agent/build-and-deploy.sh).
  Completion: give accurate regeneration, deployment, and restart instructions
  for model identity, stack configuration, and installer-managed URLs.

- [x] **RG-013 — Shared OGX ConfigMap is undocumented.** All four agent build
  scripts overwrite the same ogx-stack-config, and all four Deployments mount
  it despite separate source files.
  Evidence: [doc-gen build](../doc-gen-agent/build-and-deploy.sh),
  [catalog build](../catalog-gen-agent/build-and-deploy.sh).
  Completion: explain the shared resource and operational effects; any move to
  independent ConfigMaps requires a separately approved implementation choice.

## Agent behavior and limitations

- [x] **RG-014 — Stale doc-gen file-reading workflow.** README promises roughly
  15–20 reads; prompt surveys 6–10 outlines and allows eight full-file reads,
  matching Deployment MAX_READ_FILES=8.
  Evidence: [prompt](../doc-gen-agent/agent.py),
  [budget](../doc-gen-agent/k8s/deployment.yaml).
  Completion: describe the current survey/deep-read strategy and bounded budget.

- [x] **RG-015 — OpenAPI exploration constraints are underdocumented.** Candidate
  classification favors Java controllers/DTOs; the system prompt restricts
  discovery and full controller reads, while non-Java fallback text asks for
  discovery. This qualifies the general workflow and FastAPI example.
  Evidence: [classifier and prompts](../openapi-gen-agent/agent.py).
  Completion: agree whether to document current limits or improve discovery,
  then align examples, instructions, and behavior.

- [x] **RG-016 — OpenAPI guarantees exceed validation.** Validity and endpoint
  evidence are prompt requirements, but extraction only recognizes an openapi:
  document; no YAML/OpenAPI schema or endpoint-evidence check guards proposals.
  Evidence: [extraction](../openapi-gen-agent/agent.py),
  [proposal path](../openapi-gen-agent/app.py).
  Completion: qualify guarantees or add explicitly approved validation behavior.

- [x] **RG-017 — Server-side truncation limits are omitted.** Defaults cap trees
  at 800 entries, ordinary reads at 20,000 bytes, document reads at 8,000 bytes,
  and outlines at 4,000 characters. Truncated trees also feed detection and governance.
  Evidence: [server limits and tree implementation](../github-mcp-server/server.py).
  Completion: describe caps, configuration, and consequences for coverage.

- [x] **RG-018 — Public-only limitation is not enforced.** Server requests attach
  the configured token and do not reject private repositories. Actual access
  depends on token permissions; private access was not exercised in this audit.
  Evidence: [headers and repository requests](../github-mcp-server/server.py).
  Completion: distinguish supported/demo scope from implementation capability.

- [x] **RG-019 — PR prerequisites need repository permission qualification.**
  public_repo scope does not grant write permission to arbitrary repositories.
  The server creates branches in the source repository and has no fork workflow.
  Evidence: [PR implementation](../github-mcp-server/server.py).
  Completion: clarify write-access requirements, including upstream demo URLs.

- [x] **RG-020 — Governance callback differences and catalog mismatch are omitted.**
  OpenAPI/compliance chat outcomes report to the Hub; doc-gen chat and UI PR
  handlers do not. Catalog sends catalog-gen callbacks, which the Hub rejects,
  and the Hub cannot trigger catalog generation.
  Evidence: [doc-gen app](../doc-gen-agent/app.py),
  [OpenAPI app](../openapi-gen-agent/app.py),
  [compliance app](../compliance-scan-agent/app.py),
  [catalog app](../catalog-gen-agent/app.py), [Hub](../governance-hub/app.py).
  Completion: document actual coverage or implement user-approved integration
  changes, preserving independent agents and optional Hub availability.

- [x] **RG-021 — Governance status semantics are undocumented.** Checks inspect
  default-branch artifacts, so open PRs do not establish compliance. The
  compliant property only considers architecture documentation; violations can
  separately include missing OpenAPI and scan findings.
  Evidence: [models](../governance-hub/models.py),
  [checks](../governance-hub/compliance.py).
  Completion: explain these distinct statuses and refresh behavior accurately.

- [x] **RG-022 — Invocation lifecycle is undocumented.** /invoke accepts a prompt
  and returns a job ID, then runs an in-process FastAPI background task. There is
  no durable job queue or agent-side job-status endpoint.
  Evidence: [invoke handler](../doc-gen-agent/app.py).
  Completion: document request/response contracts and operational limitations.

## Observability

- [x] **RG-023 — Incorrect MLflow defaults.** README says tracing defaults off
  and insecure TLS is unset; committed cluster-config sets a tracking URI and
  insecure TLS to true.
  Evidence: [configuration](../doc-gen-agent/k8s/cluster-config.yaml).
  Completion: align stated defaults with the agreed committed configuration.

- [x] **RG-024 — Stale MLflow cost-chart expectations.** README expects populated
  Overview widgets; a repository investigation records empty widgets despite
  populated trace costs on the observed OpenShift AI MLflow build.
  Evidence: [investigation](mlflow-overview-cost-breakdown-bug.md).
  Completion: qualify the smoke test by observed version and explain trace-level
  cost inspection; revalidate before claiming the platform issue is resolved.

- [x] **RG-025 — Contradictory sidecar build default.** Project-tree annotation
  calls the cluster build the default, while the script defaults to Quay.
  Evidence: [aggregate deploy](../build-and-deploy-all.sh).
  Completion: consistently identify Quay as the current default.

## CI/CD

- [x] **RG-026 — Pipeline compatibility is overstated.** A Dockerfile alone is
  insufficient: generic deployment assumes one HTTP container, a configured
  port, TCP probes, and no app-specific dependencies/configuration. Checkout
  defaults to main.
  Evidence: [pipeline](../ci-cd-pipeline/k8s/pipeline.yaml).
  Completion: qualify compatible applications and explain relevant parameters
  (demo pipeline is build + agent invoke only; no deploy).

- [x] **RG-027 — tkn example references an unprovisioned PVC.** No committed
  resource creates ci-cd-pipeline-workspace. The example PipelineRun instead
  provisions storage with volumeClaimTemplate.
  Evidence: [PipelineRun](../ci-cd-pipeline/pipelinerun-example.yaml),
  [deploy script](../ci-cd-pipeline/build-and-deploy.sh).
  Completion: provide a runnable workspace example or explicit PVC preparation.

- [x] **RG-028 — Generated documentation is not tied to the built revision.**
  repo-revision controls checkout, while invocation tasks pass only the URL.
  Agents may analyze the default branch rather than the build's branch/commit.
  Evidence: [pipeline](../ci-cd-pipeline/k8s/pipeline.yaml).
  Completion: document this limitation or implement agreed revision propagation.

- [x] **RG-029 — Immediate pipeline continuation needs qualification.** Checkout
  waits for both invocation tasks; generation is asynchronous but invocation
  includes task startup and up to a five-second curl timeout.
  Evidence: [pipeline](../ci-cd-pipeline/k8s/pipeline.yaml),
  [invoke task](../ci-cd-pipeline/k8s/tasks/invoke-doc-gen-agent-task.yaml).
  Completion: distinguish waiting for invocation from waiting for generation.

- [x] **RG-040 — RHDH PetClinic Software Template + build/agent pipeline.**
  Developer Hub had scaffolder RBAC but no Software Template, and the Tekton
  demo invoked agents before build (doc + OpenAPI only).
  Evidence: [template](../developer-hub/k8s/templates/spring-petclinic-rest/),
  [pipeline](../ci-cd-pipeline/k8s/pipeline.yaml).
  Completion: register existing spring-petclinic-rest-legacy in the catalog
  (no new GitHub repo); pipeline builds to the internal registry then invokes
  doc-gen + compliance in parallel (no Quay, no deploy).

## Developer portal

- [ ] **RG-030 — Incorrect Keycloak Route ownership.** The script applies the
  Route explicitly; the Keycloak CR disables operator-managed ingress.
  Evidence: [deploy script](../keycloak/build-and-deploy.sh),
  [Keycloak CR](../keycloak/k8s/instance/keycloak.yaml).
  Completion: correct the operator-generated Route claim.

- [ ] **RG-031 — RHDH Secret drift-protection claim lacks an Application.** README
  cites ignoreDifferences protection for rhdh-secrets, but no RHDH child
  Application is shipped.
  Evidence: [Applications](../gitops/apps/),
  [deploy script](../developer-hub/build-and-deploy.sh).
  Completion: describe current imperative ownership and qualify any future
  GitOps instructions.

- [ ] **RG-032 — Architect-group access is overstated.** RBAC assigns the role
  directly to user:default/architect, not the group. The plugin permission check
  gates its iframe; the standalone Hub routes have no equivalent auth check.
  Evidence: [policy](../developer-hub/k8s/instance/rbac-policy.yaml),
  [plugin guard](../governance-hub-plugin/src/components/GovHubPage/GovHubPage.tsx),
  [Hub routes](../governance-hub/app.py).
  Completion: accurately document access boundaries; obtain user direction before
  changing group grants or backend authentication.

- [ ] **RG-033 — Enabled portal plugins are omitted.** Topology and Tekton are
  enabled in addition to the README's listed plugins.
  Evidence: [dynamic plugins](../developer-hub/k8s/instance/dynamic-plugins-configmap.yaml).
  Completion: update the enabled-plugin inventory and describe relevant views.

## Gateway governance and supporting workflows

- [ ] **RG-034 — Shared gateway quotas and compliance virtual server omitted.**
  Independent per-run budgets coexist with a shared MaaS key/subscription,
  10-million-token/hour model quota, and 10,000-request/hour MCP limit. The
  compliance-tools virtual server is also available.
  Evidence: [subscription](../ai-gateway/k8s/maas-subscription.yaml),
  [MCP limit](../mcp-gateway/k8s/rate-limit-policy.yaml),
  [virtual servers](../mcp-gateway/k8s/virtual-servers.yaml).
  Completion: distinguish per-agent iteration limits from shared gateway quotas
  and document compliance-tool access.

- [ ] **RG-035 — Gateway compatibility automation omitted.** A PostSync Job
  rebuilds virtual-server configuration in the broker Secret and restarts the
  broker to work around an operator namespace mismatch.
  Evidence: [sync Job](../mcp-gateway/k8s/virtualserver-config-sync.yaml).
  Completion: document this operational dependency and when it runs.

- [ ] **RG-036 — Gateway demonstration workflows omitted.** Read-only wiring
  tours and authorization/quota demos exercise 403/429 responses, temporary
  GitOps suspension, and restoration.
  Evidence: [tour](../demo/gateways/README.md),
  [governance demo](../demo/governance/README.md).
  Completion: link both runbooks and explain their purposes and restoration step.

- [ ] **RG-037 — Catalog reload workflow omitted.** A helper restarts the catalog
  backend after source imports/edits to address stale indexing; README presents
  import as the complete workflow.
  Evidence: [reload helper](../reload-mcp-catalog.sh).
  Completion: explain the helper and qualify its use by affected platform behavior.

- [ ] **RG-038 — Existing agent tests are undocumented.** Four offline pytest
  suites, requirements-dev files, pytest configurations, an aggregate runner,
  and a testing guide exist.
  Evidence: [runner](../scripts/run-agent-tests.sh),
  [guide](../TESTING_RULES.md).
  Completion: add test setup/run instructions and project-tree coverage.

- [ ] **RG-039 — Placeholder local-model branch link.** README links to YOUR_ORG
  instead of the actual fork used by Applications.
  Evidence: [README](../README.md),
  [Application URL](../gitops/apps/doc-gen-agent.yaml).
  Completion: replace the placeholder with the intended branch URL.

## Resolution log

Completed items retain their original finding above; this log describes
the resulting documentation and implementation. RG-001–RG-023 and RG-025
are resolved; RG-024 is marked done at the user's request with remediation
left to the user. RG-026–RG-029 and RG-040 are resolved on the
`feature/rhdh-petclinic-scaffold` work; RG-030–RG-039 remain pending.
Earlier resolution rows describe the state at the time of that batch; later
rows supersede any integration limitations since addressed.

| Date | IDs | User direction / decision | Resolution and affected files | Validation | Commit / remaining work |
| --- | --- | --- | --- | --- | --- |
| 2026-11-07 | RG-001 | Approved documentation correction | README overview, architecture diagram, route commands, project tree, walkthroughs, and shared-agent/PR descriptions now cover all four independent agents; AGENTS.md component counts aligned. | Compared component inventory, deployments, and agent entry points; checked agent coverage, local links, Markdown fences, and diff whitespace. | Not committed. Documentation only. |
| 2026-11-07 | RG-002 | Approved documentation correction | README explains all three compliance guideline files, report-only behavior, chat and invocation workflows, optional Hub reporting, and a usable scan walkthrough. | Compared guideline loading, report prompt, chat handler, and background invocation in compliance-scan-agent; verified linked files exist. | Not committed. No scan or PR triggered. |
| 2026-11-07 | RG-003 | Approved documentation correction | README covers catalog generation/augmentation, Component and evidenced API entities, separate guidelines, UI/invocation, review steps, and agentic-catalog/* proposals. | Compared catalog agent prompt/guidelines, proposal payload, branch/base settings, chat and background paths; verified linked files exist. | Not committed. No generation or PR triggered. |
| 2026-11-07 | RG-004 | Approved documentation correction | README adds independent Hub backend architecture, dashboard/history, API table, checks, explicit triggers, optional callback boundaries, and SQLite/PVC persistence; plugin section links to backend. AGENTS.md reflects actual supported agent types. | Compared governance-hub/app.py, models.py, compliance.py, and PVC manifest; prior live check confirmed bound PVC. | Not committed. Catalog integration remains unchanged; RG-020/RG-021 remain pending. |
| 2026-11-07 | RG-005 | Approved documentation correction | README documents four Compliance MCP tools, streamable HTTP /mcp, Hub REST dependencies and errors, and stateless/no-shared-database design; tree and AGENTS.md aligned. | Checked tool names/signatures, REST calls, transport, and source ownership against compliance-mcp-server/server.py; local links checked. | Not committed. No service changes. |
| 2026-11-07 | RG-006 | Approved documentation correction | README diagram places model gateway inside OpenShift, separates operator-workstation scripts from cluster workloads, removes App-of-Apps ownership of Keycloak/RHDH, and explains MCPServer operator reconciliation and installer-owned model wiring. | Compared gitops/apps, portal deploy scripts, and AI Gateway manifests; checked diagram subgraph balance, model-gateway boundary, and absence of GitOps deployment arrows to portal components. | Not committed. Mermaid structure reviewed; visual rendering not exercised. |
| 2026-11-07 | RG-007 | Approved documentation correction accepting current Managed routing/MaaS design | README, AGENTS.md, .cursor/rules/gitops-conventions.mdc, and comments in both gateway installers now describe step 6 enabling aigateway/modelsAsAService as Managed, without local model serving. The initial temporary Removed state before prerequisites is preserved. | Compared step 6 and prior live DSC observation; bash -n passed for both installers; executable lines verified identical to HEAD; git diff --check passed. | Not committed. No cluster or executable-code changes. |
| 2026-11-08 | RG-008 | Approved deployment/configuration implementation | Added gitops/prepare.sh and PREPARE_ONLY=true to seven component build scripts. Preparation builds images and generates ConfigMaps/credentials before workload reconciliation; preflight now requires all application image tags and generated maps. README quick start includes preparation and the gateway URL finalization step; bootstrap prints that step. | Offline preparation scenario builds all seven tags, generates six required ConfigMaps, applies no workload manifests, and performs no restarts; subsequent preflight passes. Missing-image/map cases fail. Shell syntax, modified YAML, and links checked. | Uncommitted. Fresh-cluster builds/sync were not executed; validated with fake oc. |
| 2026-11-08 | RG-009 | Approved deployment/configuration implementation | README GitOps and full non-GitOps quick starts install OpenShift Pipelines before bootstrap/deployment. Aggregate deployment checks the Pipeline CRD before deploying components when the pipeline is selected. | Checked sequence against ci-cd-pipeline/install-operator.sh and preflight; offline aggregate/preflight scenarios pass. | Uncommitted. No operator installation performed. |
| 2026-11-08 | RG-010 | Approved deployment/configuration implementation | README describes full aggregate scope, including gateway, pipeline, Keycloak, and RHDH; provides an explicit application-only selection and portal add-on order without duplicate portal deployment. Aggregate selection is validated before component mutations. | Offline default aggregate scenario confirms pipeline and portal inclusion and their order; README local links and shell syntax checked. | Uncommitted. Default full-stack scope retained, with required gateway wiring added. |
| 2026-11-08 | RG-011 | Approved deployment/configuration implementation | Added mcp-gateway/build-and-deploy.sh using committed manifests, compatibility Job recreation/completion, broker rollout wait, installer URL setup, and empty-URL rejection. Aggregate provisions gateway before agents; standalone agent deploys reject an empty URL. MCP backend scripts wait for operator-created Deployments before rollout. README documents fresh non-GitOps order. | Offline successful aggregate/gateway scenarios confirm ordering and retained URL; failed compatibility Job prevents URL setup; empty gateway blocks an agent before image build. Shell syntax and ShellCheck warning/error checks pass. | Uncommitted. No live gateway Job or rollout executed. |
| 2026-11-08 | RG-012 | Approved deployment/configuration implementation | Added scripts/configure-agents.sh for regeneration without image builds, preserving installer-managed URLs before applying committed shared settings. Installer helper seeds missing cluster-config before initial URL discovery is saved. doc-gen Application now respects ignored URL keys during sync. Normal app deploys restart pods for mutable image/config changes. README provides model/stack/knowledge/Secret/URL update and explicit restart procedures; guidance aligned. | Offline URL preservation, fresh installer URL seeding, read-error fail-closed behavior, and ordinary restart scenarios pass. All nine deployment tests pass via python3 -m unittest discover -s scripts/tests -v; bash -n, ShellCheck at warning/error severity, modified YAML parsing, links/fences, and git diff --check pass. | Uncommitted. Argo CD sync and live rollouts were not exercised. |
| 2026-11-08 | RG-013 | Approved deployment/configuration implementation retaining shared ConfigMap | All agent build scripts now generate shared ogx-stack-config from canonical doc-gen-agent/ogx/stack_run_config.yaml through scripts/configure-agents.sh; other stack files remain references. README explains shared effects, per-agent knowledge maps, and restart scope; four Deployment comments, AGENTS.md, and Cursor guidance aligned. No independent stack ConfigMaps introduced. | Offline catalog-only regeneration uses exact canonical stack content while generating only catalog knowledge; full preparation generates all knowledge maps. Modified YAML and documentation checks pass. | Uncommitted. Shared stack ownership remains intentional; agent runs and budgets remain independent. |
| 2026-11-08 | RG-015 | User clarified that the demo scope is Java applications only; document current exploration limits | README and AGENTS.md establish Java demo scope without claiming a runtime language gate. README replaces the FastAPI example with Java service selection criteria, documents four-controller/three-model classification caps, outline-first exploration, no full controller reads or further system-prompt discovery, and naming/outline coverage limitations. Walkthrough inputs now consistently target Java. | Compared _classify_api_files, MAX_CLASSIFIED_CONTROLLERS/MAX_CLASSIFIED_MODELS, system prompt, and fallback text in openapi-gen-agent/agent.py; Markdown fences, local links, and git diff --check pass. | Uncommitted. Documentation resolution; broader discovery is out of scope. Existing conflicting fallback text is disclosed, not changed; RG-016 validation remains pending. |
| 2026-11-08 | RG-018 | User requested changes only for statements contradicting actual behavior; avoid additional demo detail | README now states that the demo uses public repositories, removing the unsupported claim that analysis is public-only. | Rechecked token-bearing requests in github-mcp-server/server.py; Markdown and diff checks pass. | Uncommitted. No implementation changes or private-repository guidance added. |
| 2026-11-08 | RG-019 | Same limited correction scope | README prerequisites and pipeline section simply require token write access to the target repository, removing the misleading scope-sufficiency wording. | Rechecked source-repository branch/PR implementation; Markdown and diff checks pass. | Uncommitted. No implementation changes or expanded token/fork instructions. |
| 2026-11-08 | RG-014 | User approved remaining Agent behavior and limitations items | README now describes the 6–10-outline survey followed by selected deep reads, the eight-read prompt budget, one README allowance, and avoiding duplicate reads; walkthrough aligned. | Compared doc-gen system prompt and Deployment MAX_READ_FILES=8; documentation checks pass. | Uncommitted. No budget changes. |
| 2026-11-08 | RG-016 | Same batch approval | OpenAPI extraction now rejects unrelated/prose output and validates YAML/OpenAPI 3.0; proposal helper revalidates final content before any PR request. Runtime requirements include PyYAML and openapi-spec-validator. Specs must use local JSON-pointer refs; external/unresolved refs and aliases are rejected. README explicitly distinguishes schema validity from unverified source/endpoint accuracy. OpenAPI tests and TESTING_RULES.md aligned. | All 200 OpenAPI tests pass with the real validator, including malformed specs, invalid direct proposals, local refs, external/unresolved refs, and cyclic aliases. All four agent suites total 700 passing tests on Python 3.11.18; agent suites also pass on Python 3.15.2. | Uncommitted. No live generation/PR; semantic source-evidence validation remains a human review responsibility, as documented. |
| 2026-11-08 | RG-017 | Same batch approval | README documents tree/read/outline defaults, truncation flags, configuration/override locations and consequences for detection and Hub checks. Corrected byte units for full/document reads versus character units for outlines. Updated stale 60KB read_file docstring in github-mcp-server/server.py. | Compared tree, read_file, outline, and REST implementations; server compiles; Markdown links/fences and diff checks pass. | Uncommitted. Server limits and behavior unchanged. |
| 2026-11-08 | RG-020 | Same batch approval; complete integration while preserving independent agents and optional Hub | All four agents report chat outcomes, three PR-capable agents report UI proposal outcomes, and callback HTTP failures are logged without affecting generation. Hub accepts/triggers catalog-gen, records catalog timestamp/PR links through additive SQLite migration, and offers a catalog action in repo_detail.html. Added explicit UI/invoke origin metadata and handled completion callbacks arriving before invoke returns. Updated Hub Deployment env, README diagram/coverage, AGENTS.md, and Compliance MCP history descriptions. | Four agent suites: 700 tests pass. Hub suite: 11 tests pass covering four callback types, idempotent history, catalog trigger through public /invoke, early-callback race, rendered catalog action, and migration preserving existing rows. All tested on Python 3.11.18; Python compile/YAML checks pass. | Uncommitted. No cluster rollout, live database migration, agent invocation, or PR performed. |
| 2026-11-08 | RG-021 | Same batch approval; document existing status policy | README explains default-branch presence checks, independent compliant/violations values, unknown fields, catalog history without a new compliance requirement, explicit refresh/callback refresh, stale values after failed checks, truncated-tree limits, and proposal metadata versus merge status. Corrected misleading model docstring about OpenAPI checks requiring prior generation. | Hub tests verify proposals do not establish compliance, architecture-present can coexist with OpenAPI/scan violations, and failed refresh retains artifact values; documentation checks pass. | Uncommitted. Compliance policy unchanged. |
| 2026-11-08 | RG-022 | Same batch approval; document existing lifecycle | README specifies each agent's /invoke request, 202/job_id response, 400/422 cases, background PR versus report behavior, in-process/non-durable execution, logs/optional Hub monitoring, restart/callback loss, lack of agent status endpoint, and non-deduplicated reruns. | Compared all four FastAPI handlers and background paths; existing API tests pass as part of the four agent suites; Markdown links/fences and diff checks pass. | Uncommitted. No durable queue or status endpoint introduced. |
| 2026-11-08 | RG-023 | User requested resolution | README now states the committed MLflow Service URI and insecure-TLS=true defaults, shows matching YAML, and explains disabling tracing with an empty URI and agent restarts. Configuration remains unchanged. | Compared README values with doc-gen-agent/k8s/cluster-config.yaml; Markdown links/fences and git diff --check pass. | Uncommitted. Documentation only. |
| 2026-11-08 | RG-024 | User explicitly requested marking done without work; user will handle later | Checkbox marked done only. No remediation, investigation, cost-chart changes, or platform-fix claim made. | Verified the README smoke-test section and investigation file remain unchanged. | User-owned follow-up; underlying issue is not claimed resolved. |
| 2026-11-08 | RG-025 | User requested resolution | README project tree identifies the in-cluster sidecar build as opt-in and Quay as the default, matching the existing mode table and aggregate script. | Compared build-and-deploy-all.sh mode/image defaults and cluster-config image; documentation and diff checks pass. | Uncommitted. No image or deployment changes. |
| 2026-11-10 | RG-026 | Approved as part of PetClinic register + build/agent pipeline | README qualifies apps that need a Dockerfile; pipeline params cover Dockerfile path and revision. Deploy step removed from the demo pipeline. | YAML parse of pipeline/tasks; docs updated. | On feature/rhdh-petclinic-scaffold. |
| 2026-11-10 | RG-027 | Same batch | pipelinerun-example and build-and-deploy.sh use volumeClaimTemplate only; removed claimName=ci-cd-pipeline-workspace guidance. | Manifest review. | On feature/rhdh-petclinic-scaffold. |
| 2026-11-10 | RG-028 | Same batch; partial implementation + documentation | Invoke tasks pass `repo-url (revision …)` in the prompt. Agents still extract the github.com URL and may analyze the default branch if tooling ignores the hint — README documents that residual limit. | Compared invoke task scripts. | Residual: true ref-pinning inside agents not implemented. |
| 2026-11-10 | RG-029 | Same batch | Pipeline invokes agents after build, in parallel (no deploy). README distinguishes invocation (202 + ≤5s curl) from generation. | Pipeline DAG review. | On feature/rhdh-petclinic-scaffold. |
| 2026-11-10 | RG-040 | User requested RHDH register-existing + build/agents; Quay and deploy dropped | Software Template catalog:register only. Pipeline: internal-registry build then parallel doc-gen + compliance invoke. Removed quay/ and deploy-app Task. | YAML parse offline. | On feature/rhdh-petclinic-scaffold. |
