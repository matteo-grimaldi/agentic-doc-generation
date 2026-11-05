# Bug: Overview Cost Breakdown stays empty despite per-trace cost being present

## Summary

On OpenShift AI’s bundled MLflow **3.14.0**, GenAI experiment **Overview → Cost Breakdown** (and related cost-over-time widgets that query span cost metrics) stay empty / `$0.00`, even when:

- LLM spans carry `mlflow.llm.cost` and OpenInference `llm.usage.*_cost` attributes
- Trace metadata includes `mlflow.trace.cost`
- `TraceInfo.cost` is non-null and shows real USD amounts (e.g. `$0.50`–`$1.50` per call after demo pricing)
- Overview **token** charts work (they use trace-level token columns, not the spans cost path)

**Root cause observed on this cluster:** Overview Cost Breakdown does **not** aggregate from `mlflow.trace.cost` / span attributes. It queries SQL span cost metrics (`view_type=SPANS`, `metric_name=total_cost`). On this server the `spans` table has **0 rows** and **no** `total_cost` / `model_name` columns, so the metrics API returns an empty payload.

This looks like incomplete span-analytics / cost-metrics support (or a missing migration / logging path) in the OpenShift AI MLflow 3.14 build, not an application pricing misconfiguration.

## Environment

| Component | Value |
|---|---|
| Platform | OpenShift AI (RHOAI) / Open Data Hub MLflow operator |
| MLflow server version | **3.14.0** (`status.version` on `MLflow` CR) |
| MLflow server image | `registry.redhat.io/rhoai/odh-mlflow-rhel9@sha256:0be528dc82781989d7d3fcddbf1f9825946c5ae5693c88106037c931f0a7120f` |
| MLflow UI | OpenShift AI modular MLflow UI (`odh-mod-arch-mlflow-rhel9`), served under the RH AI / MLflow route |
| Tracking URI (in-cluster) | `https://mlflow.redhat-ods-applications.svc.cluster.local:8443/mlflow` |
| Public UI host (this cluster) | `https://rh-ai.apps.cluster-c54bn.dyn.redhatworkshops.io/mlflow` |
| Backend store | `sqlite:////mlflow/mlflow.db` |
| Workspaces | Enabled (`--enable-workspaces`, `--workspace-store-uri=kubernetes://`) |
| Auth | `--app-name=kubernetes-auth` |
| Client (agent sidecar) | `mlflow==3.17.2` (OGX MLflow sidecar image) |
| Workspace / experiment | workspace `doc-gen-demo`, experiment `doc-gen-agent` (id `1`) |
| Model under test | Custom OpenAI-compatible id `external-model/qwen38-27b` (not in LiteLLM built-in price catalog) |

Application context (for reproduction only): agents in this repo set cost manually from MLflow Model Registry USD rates because the custom model is unpriced. The same Overview failure mode would apply to any workload that only populates span/trace cost attributes without SQL span cost rows.

## Expected behavior

1. After LLM traces are logged with token usage + cost attributes / `mlflow.trace.cost` metadata, **Overview → Cost Breakdown** shows non-zero total cost, broken down by model (and optionally provider).
2. **Cost over time** shows a series for that model (not empty / `NULL_VALUE` only).
3. Per-trace cost in the Traces list / trace detail remains consistent with Overview totals.

## Actual behavior

| Surface | Result |
|---|---|
| Trace detail / span attributes (`mlflow.llm.cost`, `llm.usage.total_cost`) | **Present** |
| Trace metadata `mlflow.trace.cost` / `TraceInfo.cost` | **Present** (after client writes it) |
| Overview token usage charts | **Work** |
| Overview **Cost Breakdown** | **Empty** (`$0` / no slices) |
| Overview cost-over-time (span cost path) | **Empty** |

Raising USD rates (e.g. $50 / $150 per 1M tokens) makes per-trace costs large and obvious, but **does not** populate Cost Breakdown.

## How to reproduce

### A. Minimal conceptual reproduction

1. Deploy OpenShift AI MLflow 3.14.0 with workspaces enabled (operator-managed `MLflow` CR as above).
2. From a client app, enable tracing against that server and log OpenAI-compatible chat completions (e.g. `mlflow.openai.autolog()`).
3. On each LLM/`CHAT_MODEL` span, set at least:

   ```python
   span.set_attribute("mlflow.chat.tokenUsage", {
       "input_tokens": 1000,
       "output_tokens": 500,
       "total_tokens": 1500,
   })
   span.set_attribute("mlflow.llm.cost", {
       "input_cost": 0.50,
       "output_cost": 0.75,
       "total_cost": 1.25,
   })
   span.set_attribute("mlflow.llm.model", "external-model/qwen38-27b")
   span.set_attribute("mlflow.llm.provider", "vllm")
   # OpenInference-style (often cited for Overview widgets)
   span.set_attribute("llm.model_name", "external-model/qwen38-27b")
   span.set_attribute("llm.model_provider", "vllm")
   span.set_attribute("llm.usage.prompt_tokens", 1000)
   span.set_attribute("llm.usage.completion_tokens", 500)
   span.set_attribute("llm.usage.total_tokens", 1500)
   span.set_attribute("llm.usage.prompt_tokens_cost", 0.50)
   span.set_attribute("llm.usage.completion_tokens_cost", 0.75)
   span.set_attribute("llm.usage.total_cost", 1.25)
   ```

4. Also set trace metadata (self-hosted clients do **not** always auto-aggregate cost the way Databricks does):

   ```python
   mlflow.update_current_trace(metadata={
       "mlflow.trace.cost": '{"input_cost":0.50,"output_cost":0.75,"total_cost":1.25}'
   })
   ```

5. Open the MLflow UI → workspace → experiment → **Overview**.
6. Observe: token widgets can be non-empty; **Cost Breakdown** stays empty.
7. Open the same run under **Traces**: cost attributes / `TraceInfo.cost` are visible.

### B. Reproduction on this demo cluster (agentic-doc-generation)

1. Ensure MLflow tracking is enabled via `doc-gen-agent/k8s/cluster-config.yaml` (`mlflow_tracking_uri`, insecure TLS as needed) and agents are deployed with the OGX MLflow sidecar.
2. Seed Model Registry pricing (laptop needs `oc` + reachability; in-cluster DNS requires port-forward):

   ```bash
   python3 -m venv .venv-mlflow && source .venv-mlflow/bin/activate
   pip install 'mlflow>=3.13'
   ./register-llm-pricing.py --from-cluster \
     --input-usd-per-million 50 \
     --output-usd-per-million 150
   ```

3. Run `doc-gen-agent` against any small public repo so sidecar autolog traces are written.
4. Confirm per-trace cost via SDK (port-forward `svc/mlflow` if needed):

   ```python
   import mlflow
   mlflow.set_tracking_uri("https://127.0.0.1:<local-port>/mlflow")
   mlflow.set_workspace("doc-gen-demo")
   df = mlflow.search_traces(experiment_ids=["1"], max_results=5)
   t = mlflow.get_trace(df.iloc[0]["trace_id"])
   print(t.info.token_usage)  # non-null
   print(t.info.cost)         # non-null, e.g. total_cost ~ 0.5–1.5
   print(t.data.spans[0].attributes.get("mlflow.llm.cost"))
   ```

5. Open UI Overview for `doc-gen-agent` → **Cost Breakdown** empty.
6. Probe the same API the UI chart uses:

   ```bash
   # After oc port-forward -n redhat-ods-applications svc/mlflow 8443:8443
   curl -sk -X POST \
     -H "Authorization: Bearer $(oc whoami -t)" \
     -H "X-Mlflow-Workspace: doc-gen-demo" \
     -H "Content-Type: application/json" \
     -d '{
       "experiment_ids": ["1"],
       "view_type": "SPANS",
       "metric_name": "total_cost",
       "aggregations": [{"aggregation_type": "SUM"}],
       "dimensions": ["span_model_name"]
     }' \
     "https://127.0.0.1:8443/mlflow/ajax-api/3.0/mlflow/traces/metrics"
   ```

   **Observed:** HTTP 200 with empty body / no `data_points` (e.g. `{}`).

7. Compare token metrics (works):

   ```bash
   curl -sk -X POST \
     -H "Authorization: Bearer $(oc whoami -t)" \
     -H "X-Mlflow-Workspace: doc-gen-demo" \
     -H "Content-Type: application/json" \
     -d '{
       "experiment_ids": ["1"],
       "view_type": "TRACES",
       "metric_name": "total_tokens",
       "aggregations": [{"aggregation_type": "SUM"}],
       "dimensions": []
     }' \
     "https://127.0.0.1:8443/mlflow/ajax-api/3.0/mlflow/traces/metrics"
   ```

   **Observed:** HTTP 200 with `data_points` and a large `SUM` (e.g. `1090209.0`).

8. Inspect the backend DB:

   ```bash
   oc -n redhat-ods-applications exec deploy/mlflow -c mlflow -- \
     sqlite3 /mlflow/mlflow.db \
     "SELECT COUNT(*) FROM trace_info; SELECT COUNT(*) FROM spans; SELECT COUNT(*) FROM span_metrics; PRAGMA table_info(spans);"
   ```

   **Observed on this cluster (2026-11-05):**

   | Query | Result |
   |---|---|
   | `COUNT(*) FROM trace_info` | `145` |
   | `COUNT(*) FROM spans` | `0` |
   | `COUNT(*) FROM span_metrics` | `0` |
   | `PRAGMA table_info(spans)` | Columns: `trace_id`, `experiment_id`, `span_id`, `parent_span_id`, `name`, `type`, `status`, `start_time_unix_nano`, `end_time_unix_nano`, `content`, `dimension_attributes` — **no** `input_cost` / `output_cost` / `total_cost` / `model_name` / `model_provider` |

## Technical analysis

### What the Overview Cost Breakdown UI queries

In current upstream MLflow UI (`TraceCostBreakdownChart`), the chart loads metrics with approximately:

- `viewType`: `SPANS`
- `metricName`: `total_cost`
- `aggregations`: `SUM`
- `dimensions`: `span_model_name` (or provider)

Backend route (upstream): `POST /ajax-api/3.0/mlflow/traces/metrics` (`QueryTraceMetrics`).

Upstream store maps those metrics to **SQL span columns** such as `SqlSpan.total_cost`, grouped by model name/provider — not to `trace_metadata["mlflow.trace.cost"]`.

### Why tokens work but cost does not

- **Tokens:** aggregated onto `trace_info` / trace metadata (`mlflow.trace.tokenUsage`) and exposed via `view_type=TRACES` metrics. Rows exist → charts fill.
- **Cost Breakdown:** requires span-level cost rows in SQL (`spans` / rollups). Here `spans` is empty and lacks cost columns → chart empty even when artifact-backed span JSON contains `mlflow.llm.cost`.

### Client-side note (related, but not the Overview gap)

Upstream `BaseMlflowSpanProcessor` only writes `mlflow.trace.cost` when `should_compute_cost_client_side()` is true (Databricks / SageMaker tracking URIs). Self-hosted OpenShift AI therefore may leave `TraceInfo.cost` null unless the app sets `mlflow.trace.cost` itself. **This demo does set that metadata**, and `TraceInfo.cost` is populated — yet Overview Cost Breakdown still fails, which confirms the Overview path is the SQL span-metrics path above.

## Related upstream context

- MLflow docs: [Token Usage and Cost Tracking](https://mlflow.org/docs/latest/genai/tracing/token-usage-cost/) — Overview charts mentioned; server should be started with `mlflow[genai]`.
- Span cost metrics / querying stack (upstream), e.g. [PR #20403](https://github.com/mlflow/mlflow/pull/20403) and related cost-chart PRs — Overview charts depend on span cost metric storage/query.
- Similar user reports of Overview cost not aggregating while per-trace cost exists: [mlflow#23435](https://github.com/mlflow/mlflow/issues/23435) (Databricks-oriented; workaround discusses dual attribute sets — **insufficient alone** on this ODH 3.14 server because SQL `spans` never receive cost rows).
- Autolog cost gaps: [mlflow#21809](https://github.com/mlflow/mlflow/issues/21809).

## Workaround for demos

- Show **Traces** tab / open a recent trace and point at span or trace-level cost (works today).
- Do **not** rely on Overview **Cost Breakdown** on this MLflow 3.14.0 build until span cost metrics are persisted in SQL (schema + `log_spans` population).

## Asks / questions for maintainers

1. Is SQL span logging + cost columns (`total_cost`, `model_name`, …) supposed to be enabled for OpenShift AI MLflow 3.14.0 with `--enable-workspaces` and sqlite?
2. If yes: is there a missing migration / feature flag / known bug leaving `spans` at 0 rows while trace artifacts contain spans?
3. If Overview is expected to fall back to `mlflow.trace.cost` metadata when span SQL metrics are absent, that fallback appears not to be implemented here.
4. Recommended upgrade path (ODH/MLflow version) where Cost Breakdown is known to work for self-hosted / workspace-enabled servers.

## Checklist for issue filing

- [ ] Attach this document (or paste Summary + Environment + Reproduce + DB evidence).
- [ ] Include server version `3.14.0` and image digest above.
- [ ] Include empty `POST .../traces/metrics` (SPANS/`total_cost`) vs working TRACES/`total_tokens` responses.
- [ ] Include `PRAGMA table_info(spans)` and row counts.
- [ ] Clarify that raising client-side USD rates does **not** change Overview Cost Breakdown.
- [ ] Optional: link a single `trace_id` from the environment where `TraceInfo.cost` is non-null (example from this cluster: any recent `doc-gen-agent` trace after registry rates were set to 50/150 USD per 1M tokens).
