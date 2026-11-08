# Testing rules for project agents

## Scope and current baseline

These rules apply to the four independent agents in this project:

| Agent directory | Deliverable | PR behavior |
| --- | --- | --- |
| `doc-gen-agent/` | Architecture Markdown with three Mermaid C4 flowcharts | Explicit UI button or `/invoke` background run |
| `openapi-gen-agent/` | OpenAPI specification, when needed | Explicit UI button or eligible `/invoke` background run |
| `catalog-gen-agent/` | Backstage `catalog-info.yaml` | Explicit UI button or `/invoke` background run |
| `compliance-scan-agent/` | Markdown compliance report | Never creates a PR; reports findings only |

The shared rules cover each agent's OGX streaming loop (`agent.py`),
Gradio/FastAPI entry point (`app.py`), local MLflow helpers
(`mlflow_telemetry.py`), and deployment files. Agent-specific rules below
preserve their different outputs, preflight decisions, and callbacks.
Follow `AGENTS.md` for architecture and deployment constraints. Tests must
preserve agent independence: no agent triggers another or shares its run
budget, and generation must work without Governance Hub being available.

Other components exist, including `github-mcp-server`,
`compliance-mcp-server`, `governance-hub`, `mcp-gateway`, and
`ogx-mlflow-sidecar`. This is an **agent testing guide**, not a testing
specification for every service or infrastructure component. Treat those
components as external boundaries in agent tests; test their own internals
in separate suites appropriate to their contracts.

All four agents now provide offline suites under their own `tests/`
directories, with local `pytest.ini` and `requirements-dev.txt` files.
Run them separately using `scripts/run-agent-tests.sh`, or supply both the
configuration and explicit test directory to each pytest invocation.
The layout below remains a convention for future additions.

The doc-gen telemetry syntax blocker identified during initial review has
been repaired by removing an orphaned exception-handler fragment. Source
syntax remains a prerequisite: the doc-gen suite checks it before execution
and never stubs a broken application module to make tests pass.

## Framework and organization

Use **pytest** with **pytest-asyncio**, Python's `unittest.mock`, and HTTPX's
built-in test transports. Use Python 3.11, matching all four agents'
Dockerfiles. Keep test dependencies in a future
`<agent-directory>/requirements-dev.txt`, separate from runtime dependencies;
include the runtime requirements with `-r requirements.txt` and record
compatible, verified test dependency versions when introducing the suite.
`pytest-cov` is optional for finding untested branches, not a substitute
for useful assertions.

Use explicit `@pytest.mark.asyncio` markers and function-scoped async
fixtures/event loops. In strict mode, async fixtures use
`@pytest_asyncio.fixture`. Do not combine pytest-asyncio and AnyIO markers
on a test. These choices follow the
[pytest-asyncio isolation and strict-mode model](https://pytest-asyncio.readthedocs.io/en/stable/concepts.html).

Suggested layout in each agent (choose output-specific fixture names):

```text
<agent-directory>/
  requirements-dev.txt
  pytest.ini
  tests/
    conftest.py
    unit/
      test_output_extraction.py
      test_agent_stream.py
      test_app_handlers.py
      test_telemetry.py
    integration/
      test_http_api.py
    live/
      test_deployed_agent.py
    fixtures/
      expected_output.md-or-yaml
      knowledge-or-guidelines/
```

Suggested `<agent-directory>/pytest.ini`:

```ini
[pytest]
testpaths = tests
asyncio_mode = strict
asyncio_default_fixture_loop_scope = function
markers =
    live: requires explicitly configured deployed services and model budget
```

Run each agent's suite in its own pytest process. Their flat imports
(`agent`, `app`, `mlflow_telemetry`) collide with names in other agents.
Do not import another component's modules into that process. Keep imports
of application modules inside fixtures or test functions so environment
setup runs before import; an autouse fixture cannot protect a module
imported during test collection.

## Defining useful tests

Name files `test_*.py` and tests `test_<behavior>_<condition>`. Arrange the
input and dependencies, perform one behavior, and assert its observable
result and meaningful side effects. Parametrize input/error variants.
Prefer small synthetic repositories and short documents to copied real
repository data. Assert content, status, destinations, and payloads;
avoid brittle whole-prompt snapshots, exact log sentences, or private
call sequences that do not affect behavior.

Use three levels:

| Level | What runs | What is replaced |
| --- | --- | --- |
| Unit | Parsers, extraction, handlers, stream processing, telemetry helpers | OGX client, outbound HTTP, MLflow services, files and clocks as needed |
| Local integration | FastAPI routes and real component helpers in process | External services and Gradio mounting when testing only REST |
| Live smoke/evaluation | An already deployed agent through its supported interfaces | Only explicitly selected dependencies; requires test configuration |

Default unit and integration runs must be deterministic, offline, and
credential-free. Unexpected outbound requests must fail rather than fall
through to a real endpoint. Live tests must be marked `live`, opt in
explicitly, and use a disposable repository when exercising `/invoke` or
the PR button of a generating agent: both paths can create a real PR.
Chat generation alone must never create a PR. Compliance tests must reject
any attempt to call `/propose-doc` or another GitHub write endpoint,
including in live runs.

## Mocking dependencies

Patch names **where the code under test looks them up**. For example,
patch `agent.AsyncOgxClient` for `run_agent`, and `app.run_agent` for
`chat` or `_run_agent_in_background`; patching `agent.run_agent` after
`app` imported it will not replace `app`'s binding. Use function-scoped
`monkeypatch` or `patch` contexts so changes are restored automatically.
See [pytest's monkeypatch guidance](https://docs.pytest.org/en/stable/how-to/monkeypatch.html).

### OGX and async generators

`run_agent()` and `chat()` are async generators, not coroutines. Replace
them with `async def` functions containing `yield`, and consume them with
`async for` or `anext`. Use `AsyncMock` for awaited operations such as
`client.responses.create()`, `_fetch_repo_structure()`, or a PR helper
where present. The awaited
`responses.create()` result must itself be an async iterator of event
objects with the attributes the implementation reads (`type`, `delta`,
`item`, `response`). A plain list or an unconfigured `AsyncMock` is not
a realistic stream.

Example for `doc-gen-agent`, assuming `agent_modules` supplies its isolated
modules as described below. Reuse the stream fake pattern for other agents,
using their actual loaders, inputs, and expected outputs; do not impose an
architecture-document assertion on a spec, catalog, or report generator.

```python
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest


@pytest.mark.asyncio
async def test_run_agent_streams_document(agent_modules, monkeypatch):
    agent, _, _ = agent_modules

    async def events():
        yield SimpleNamespace(
            type="response.output_text.delta", delta="# Architecture: demo\n"
        )
        yield SimpleNamespace(type="response.output_text.delta", delta="Details.")
        yield SimpleNamespace(
            type="response.completed", response={"status": "completed"}
        )

    create = AsyncMock(return_value=events())
    constructor = Mock(
        return_value=SimpleNamespace(responses=SimpleNamespace(create=create))
    )
    monkeypatch.setattr(agent, "AsyncOgxClient", constructor)
    monkeypatch.setattr(agent, "_load_knowledge", lambda: "Test style guide")
    monkeypatch.setattr(agent, "_mcp_gateway_tool", lambda: {"type": "mcp"})

    chunks = [chunk async for chunk in agent.run_agent("https://github.com/test/demo")]

    assert chunks == ["# Architecture: demo\n", "Details."]
    create.assert_awaited_once()
    request = create.call_args.kwargs
    assert request["stream"] is True
    assert request["model"] == agent.MODEL_NAME
    assert request["extra_body"]["max_infer_iters"] == agent.MAX_INFER_ITERS
    assert "Test style guide" in request["instructions"]
```

Also test genuine incremental delivery: pause a fake stream on an
`asyncio.Event`, assert the first chunk arrives before releasing the next,
then release and finish it. Close partially consumed generators with
`aclose()` in the same task/context that consumed them. Trigger timeout
branches with injected exceptions or a controlled blocked iterator and
a short deadline; never wait for the production 60/600-second timeouts.
Do not replace `asyncio.wait_for` with a mock that leaves its coroutine
argument unawaited.

### HTTP, files, and telemetry

- For `GET /repo-structure`, `/propose-doc` where supported, and governance
  callbacks, use `httpx.MockTransport`
  with exact method/path checks and synthetic `httpx.Response` objects.
  Have the handler raise on every unrecognized request. Cover success,
  HTTP errors, error JSON, malformed/non-JSON responses, and network
  exceptions. Verify request payloads as well as returned values.
- The REST helpers construct `httpx.AsyncClient` internally. Capture the
  real constructor before patching it, then supply a factory that creates
  clients with the mock transport. `app.httpx` is the shared imported
  module: changing `app.httpx.AsyncClient` also affects other code using
  that module. Build any ASGI test client before this patch, or scope the
  patch narrowly to the helper call. Do not let the factory call its own
  patched constructor recursively.
- Use `tmp_path` for knowledge/guideline files and gateway tokens. Test missing,
  empty, whitespace-padded, and rotated tokens. `_mcp_gateway_tool()` must
  reread the token each time, use raw `authorization`, include
  `X-Mcp-Virtualserver`, and fail closed without a gateway URL. Never read
  an actual service-account token or use real credentials in fixtures.
- Replace MLflow prompt registry, spans, exporters, pricing lookup, and
  propagation APIs with narrow fakes or mocks. Span fakes used by `with`
  need synchronous `__enter__`/`__exit__`. Test enabled telemetry separately
  from the default disabled mode; assert production outputs still work
  for failures the helper is intended to swallow.
- Patch `_agent_debug_log` to a no-op in ordinary tests: it has a hardcoded
  repository log-path fallback, so setting `CURSOR_DEBUG_LOG_PATH` alone
  does not guarantee isolation. Test that writer separately with all file
  writes intercepted. Patch clock/UUID sources only when the assertion
  needs controlled expiry or identifiers; do not use real sleeps for cache
  expiry.

HTTPX documents both [mock and ASGI transports](https://www.python-httpx.org/advanced/transports/).

## Context providers and global state

There is no React context provider or application dependency container in
these agents. Their relevant context providers are the MLflow trace/span
context managers, three `ContextVar` instances, Gradio's per-session
`gr.State` in the three generating UIs, and environment-backed module
constants. Compliance chat uses history without PR state outputs. Test these actual
boundaries rather than introducing a frontend provider harness.

### Imports, environment, and module globals

A function-scoped `agent_modules` fixture in each agent's suite should
perform these steps for that agent only:

1. Add the selected agent directory to `sys.path` with
   `monkeypatch.syspath_prepend`.
   Set `MLFLOW_TRACKING_URI=""`, `GRADIO_ANALYTICS_ENABLED="False"`, and
   `MLFLOW_OGX_TRACE_PROPAGATION="false"` before importing application
   modules. Use dummy URLs and temporary token/knowledge/guideline files. Clear or
   override inherited service credentials and relevant `MLFLOW_*`
   configuration so developer shell settings cannot activate services.
2. Import `mlflow_telemetry` first; patch `_agent_debug_log` and establish
   disabled tracing. Then import `agent` and `app`. `app` calls
   `configure_mlflow()` at import time. Do not build or launch the UI in
   this fixture. Return `(agent, app, mlflow_telemetry)`.
3. Use `monkeypatch.setattr` to establish and restore telemetry flags
   `_CONFIGURED`, `_ENABLED`, `_logged_missing_chat_usage`, and
   `_logged_missing_registry_rates`, plus `_usd_rates_cache`,
   `_usd_rates_cache_model`, and `_usd_rates_cache_at`. Give each test
   fresh mutable fakes and trackers; never session-scope them.
4. Restore context variables as described below, close resources, and
   let monkeypatch undo environment/global changes after each test.

Most configuration in `agent.py` and `app.py` is read once at import.
After import, patch the consuming module's constants, not just
`os.environ`. Doc/OpenAPI `_load_knowledge(path=KNOWLEDGE_FILE)` and
catalog/compliance `_load_guidelines(directory=GUIDELINES_DIR,
filenames=GUIDELINE_FILES)` capture defaults at definition time. Pass
explicit temporary paths and filename tuples in loader tests, or patch
the selected loader in orchestration tests. Merely patching the constants
will not change those defaults. Restore mutable configuration such as
`CHAT_TEMPLATE_KWARGS` with a fresh copy when a test changes it.

The four `mlflow_telemetry.py` files are local copies, not one shared
runtime module. Apply telemetry checks to each copy in its own process;
passing tests for one copy does not validate the others. Share small test
utilities only if useful, without importing another agent's application
modules or assuming all copies remain identical.

For tests specifically verifying import-time environment parsing, run a
fresh Python subprocess with a controlled environment. Avoid indiscriminate
`importlib.reload`: it can split module identity, retain stale imported
functions/decorators, and replace `ContextVar` objects. Do not use module
reloads as a substitute for state cleanup.

`configure_mlflow()` changes SDK state and may register processors or
autolog hooks. Resetting the local flags does not undo those SDK changes.
Mock the SDK setup calls for unit tests; use a separate subprocess for
tests that deliberately install real SDK hooks. Restore any patched
OpenAI class methods after hook tests. Application inference must continue
through OGX and the AI Gateway; hook tests must not call a model directly.

### Context variables and concurrent runs

The telemetry context variables are `_run_session_id`,
`_active_prompt_ref`, and `_mcp_tool_tracker`. Save tokens and reset them
in `finally`, in the same task/context that set them. Reset restores an
outer value as well as the default; assigning `None` alone does not.
Python describes this protocol in its
[ContextVar documentation](https://docs.python.org/3.11/library/contextvars.html).

Use this pattern inside an async test or an async fixture whose setup and
teardown share that context:

```python
variables = (telemetry._run_session_id,
             telemetry._active_prompt_ref,
             telemetry._mcp_tool_tracker)
tokens = [(variable, variable.set(None)) for variable in variables]
try:
    # Execute the behavior, then assert cleanup before the test restores it.
    ...
finally:
    tracker = telemetry._mcp_tool_tracker.get()
    try:
        if tracker is not None:
            tracker.close_all()
    finally:
        for variable, token in reversed(tokens):
            variable.reset(token)
```

Use function-scoped `@pytest_asyncio.fixture` fixtures for async context
setup; do not assume a synchronous fixture's context mutation will be
restored in the test task. Close and await all spawned tasks and streams
before resetting their contexts. Child tasks inherit the creator's
context, including references to mutable tracker objects; start concurrent
runs from a clean parent context and assert trackers are distinct.

Test two interleaved traced runs using events/barriers: session IDs,
prompt references, headers, and tool spans must remain associated with
their own run. Check restoration after normal completion, exceptions,
cancellation, and early generator closure. Check nested/outer context
restoration where supported. Assert cleanup before fixture teardown so
the fixture cannot hide a production leak. If a regression fails, report
and fix the behavior rather than weakening the assertion.

### Gradio state and FastAPI dependencies

Pass fresh history lists and state inputs to each `chat()` test. Because
yielded history includes mutable dictionaries, copy each yielded history
when retaining intermediate snapshots. Verify that caller-owned history
is unchanged. In doc/OpenAPI/catalog tests, PR state must be empty during
streaming and the final state must contain that run's repository URL and
cleaned artifact when one is produced. For compliance, assert history
yields and the report callback; do not unpack a three-value PR-state tuple.
Interleave two
handler calls to check they cannot exchange histories or documents; use
a separate UI smoke test to validate actual Gradio session wiring.

Call `build_app()` per API test. For REST-only tests, patch `build_ui` and
`gr.mount_gradio_app` to avoid constructing the UI, while keeping actual
FastAPI route registration. Use HTTPX `ASGITransport` and `AsyncClient` to
exercise `/healthz`, request validation, and `/invoke` without opening a
port. Keep real UI construction in a separate smoke test.

There are currently no FastAPI `Depends` providers to override. If added,
use the new app instance's `dependency_overrides` and restore it in
`finally`. Do not accumulate overrides on a shared application.

`BackgroundTasks` is not an independent queue. In-process ASGI clients
can wait for background work before the test call completes. For route
tests, replace `_run_agent_in_background` with a quick `AsyncMock` and
assert its arguments; test the real worker separately with fake streams
and REST helpers. A `202` assertion alone does not establish response
latency. Use explicit response/task instrumentation or a live smoke test
if response ordering or latency is the behavior under test.

## Shared behavioral coverage when introducing tests

| Area | Cases and assertions |
| --- | --- |
| Prompt and knowledge/guidelines | Present/missing/empty files, selected loader inclusion, configured budgets, and agent-specific output instructions. The read-file cap is prompt guidance, not a Python-enforced counter. |
| Gateway wiring | Missing URL, missing/unreadable/empty token, trimmed token, rotation between calls, virtual-server header, no direct MCP fallback. |
| OGX stream | Text deltas, successful/failed MCP items, unknown events, failed/error/completed events, incomplete/cancelled status, error deduplication, no text, create failure/timeout, stream failure/timeout, incremental delivery and cancellation cleanup. |
| Error normalization | Dict and SDK-like objects (`model_dump`, legacy `dict`, attribute fallback), absent/malformed errors, long-message truncation and connection-error hints. |
| Chat | Blank input, successive chunks, independent histories, correct output shape and terminal state, no PR during chat, agent-specific preflight and callbacks. |
| REST and background worker | `/healthz` 200; `/invoke` valid 202/job ID, whitespace 400, missing/invalid body 422; URL extraction, missing repository URL, empty cleaned output, worker exceptions, optional/unavailable governance. |
| PR-capable agents only | Cleaned artifact payload, configured path and optional base branch, distinct branch prefix where sent, proposal success/failure, button with missing state; no proposal on empty extracted output. |
| Telemetry | Disabled no-op behavior; trace/prompt/session headers and propagation; prompt reuse/update/failure; span close/deduplication; context restoration and concurrency; usage normalization; cost arithmetic using `pytest.approx`; pricing cache hit, model change, expiry and forced refresh. |

Assert each agent's actual request contract, including `repo_structure`
where accepted, budget parameters, and `CHAT_TEMPLATE_KWARGS` where used.
Do not copy one agent's request-key assertions without checking the
implementation: doc generation supplies `max_infer_iters` in `extra_body`,
while the other agents use the first-class SDK field.

### doc-gen-agent

- Test `extract_architecture_document()` with tool status lines, narration
  before `# Architecture:`, error footers, empty/partial documents, and
  preserved Mermaid fences.
- Test knowledge loading from `knowledge.md`, required architecture
  sections, and instructions for three C4 flowcharts. The survey/discovery
  happens through OGX's MCP tools; this app has no REST structure prefetch.
- Test `_propose_architecture_doc()` with `ARCHITECTURE_DOC_PATH` and
  `ARCHITECTURE_DOC_BASE_BRANCH`. Assert background-run governance reports
  use `agent_type="doc-gen"` and PR metadata. Chat reports terminal outcomes
  with `triggered_by="ui"`; explicit proposal outcomes use `ui-pr`.

### openapi-gen-agent

- Parametrize `detect_needs_openapi()` for existing OpenAPI/Swagger files,
  case variants, missing/empty file lists, ignored non-dict entries, API
  signals with/without a recognized build manifest, and non-API projects.
  Assert skipped detection performs no OGX request and no PR proposal.
- Test `_classify_api_files()` for Java controllers/resources, DTO/model
  candidates, build files, candidate limits, and non-Java fallback.
  Verify `_build_user_message()` carries exact candidate paths and ref
  when available. Supply structure fixtures shaped like the actual REST
  result: `{"ref": "main", "files": [{"path": "pom.xml", "size": 100}]}`.
- Test `_fetch_repo_structure()` and both callers: a fetch error must
  terminate chat/background generation and report failure; a negative
  detection must report `skipped`. A positive result must be passed as
  `repo_structure` to the agent. Mock `_fetch_repo_structure` in handler
  tests; keep the real detector when testing the preflight decision.
- Test `NOT_AN_API:` output, fenced/raw YAML, leading comments/narration,
  empty output, unrelated fenced blocks, and output mixed with backend
  errors in `extract_openapi_spec()`. Exercise the real YAML/OpenAPI 3.0
  validator in extraction and immediately before proposals; malformed output
  must not reach the proposal server. Schema validity does not establish
  source accuracy.
- Verify `_propose_openapi_spec()` uses `OPENAPI_SPEC_PATH`, optional
  `OPENAPI_SPEC_BASE_BRANCH`, and `branch_prefix="agentic-openapi"`.
  Both chat and background paths report `agent_type="openapi-gen"` with
  completed/failed/skipped outcomes as applicable; chat completion alone
  must not propose a PR.

### catalog-gen-agent

- Test `_classify_catalog_files()` for existing catalogs, K8s and ArgoCD
  manifests, Docker/build files, OpenAPI/AsyncAPI specs, protobuf/GraphQL
  files, API route hints, ownership/CI/TechDocs markers, exclusions, and
  candidate limits. Test empty trees and supported filename case variants.
- Verify `_build_user_message()` preserves the supplied ref and paths,
  asks to augment an existing catalog, and handles absent API evidence.
  Unlike OpenAPI, this agent has no eligibility gate: every repository
  should be considered for at least a Component entity.
- Test both chat and background prefetch failures: generation continues
  with `repo_structure=None`. A failed optimization must not be mistaken
  for an OpenAPI-style skip. With successful prefetch, pass the returned
  structure into the agent.
- Test `_load_guidelines()` with explicit temporary directories and
  filename tuples for `catalog-component.md` and `api-detection.md`;
  cover order/headings, missing files, empty files, and prompt inclusion.
- Test `extract_catalog_info()` for fenced/raw output, absent
  `backstage.io` apiVersion, empty output, and multiple YAML documents.
  Parse all returned documents with `yaml.safe_load_all`; test Component
  and API entity relationships with meaningful fixtures. Extraction alone
  does not validate entity schemas or preserve human-authored fields.
- Validate guideline-driven output in live evaluations: existing entity
  names/non-placeholder fields are preserved, API entities follow source
  evidence, existing specs use `$text` references, and `providesApis`
  relationships match generated entities. Do not invoke another agent to
  supply missing API information.
- Verify `_propose_catalog_info()` uses `CATALOG_INFO_PATH`, optional
  `CATALOG_INFO_BASE_BRANCH`, and `branch_prefix="agentic-catalog"`.
  Both chat and background paths report `agent_type="catalog-gen"`;
  only the button/background path may propose a PR.

### compliance-scan-agent

- Test `_classify_compliance_files()` for K8s/OpenShift YAML candidates,
  excluded filenames, Dockerfile variants, `pom.xml`, project-structure
  markers, CI signals, and candidate limits. Cover non-Maven/non-K8s
  repositories: project-structure checks remain applicable.
- Test `_load_guidelines()` with temporary `k8s-manifests.md`,
  `project-structure.md`, and `pom-xml.md` files. Verify selected order,
  headings, missing/empty files, and prompt inclusion. Keep checks in
  guideline files rather than duplicating their full contents in tests.
- Test `_build_user_message()` with preclassified paths/ref and absent
  categories. Separately test the two prefetch-error policies: chat stops
  and reports failure, while the background worker continues with
  `repo_structure=None` for model discovery.
- Test `extract_compliance_report()` with fenced `markdown`/`md` blocks,
  raw heading-led reports, empty blocks, narration, and malformed output.
  Validate report structure in evaluation fixtures: summary counts,
  category/check/status/severity/details columns, evidence for FAIL rows,
  remediation hints, and SKIP for unverified checks. A valid Markdown
  table is not proof that the findings are correct.
- Assert `_report_to_governance()` includes the full cleaned `report`,
  correct job/repository identifiers, and `agent_type="compliance-scan"`
  for chat and background completion. Test failed generation and disabled
  or unavailable governance without coupling execution to the hub.
- Use a strict HTTP fake accepting only the expected read and governance
  routes; reject `/propose-doc` and every unexpected write. Assert no PR
  controls are wired into its UI. Do not add a fictitious proposal helper
  merely to reuse another agent's PR tests.

### Output validation and known limitations

Distinguish intended behavior from current limitations. For example,
`extract_architecture_document()` can retain partial text before an error
footer, and the background worker proposes any nonempty cleaned result.
An empty-output test does not prove failed or partial documents are never
proposed. Add an explicit regression test and address the policy in source
if that guarantee is required; do not claim it from a passing mock.
OpenAPI extraction now requires YAML and OpenAPI 3.0 schema validity, and
the proposal helper revalidates the final content before any write request.
Test rejection of malformed output, external/unresolved refs, and aliases;
only local JSON-pointer refs are supported. This does not verify endpoints
against source evidence. Catalog extraction only checks for a `backstage.io`
apiVersion, and compliance extraction does not verify table content.
Add explicit malformed-output and error-mixed-output cases rather than
assuming these extractors are schema validators.

Unit tests with fabricated model output verify orchestration, not model
quality, schema validity, Mermaid rendering, or the correctness of
compliance findings. Live evaluations should use a small fixed
repository/ref and apply that agent's output checks above against source
evidence. Avoid exact prose matching. Record agent/model ID, repository
revision, prompt/knowledge/guideline revision, budgets, duration, and
outcome without storing credentials.

## Running checks

Run commands from the repository root. Select one of the four agent
directories and create its Python 3.11 virtual environment. Install its
runtime requirements plus pytest/pytest-asyncio before introducing the
suite. Once that agent's `requirements-dev.txt` exists, use it instead of
the two installation commands below and preserve the verified versions
there. Repeat setup for each selected agent; avoid assuming their runtime
requirements will remain identical.

```bash
AGENT_TEST_DIR=catalog-gen-agent
python3.11 -m venv "/tmp/${AGENT_TEST_DIR}-test-venv"
source "/tmp/${AGENT_TEST_DIR}-test-venv/bin/activate"
python -m pip install -r "${AGENT_TEST_DIR}/requirements.txt"
python -m pip install pytest pytest-asyncio

# Run first for the selected agent.
python -m py_compile "${AGENT_TEST_DIR}/agent.py" "${AGENT_TEST_DIR}/app.py" "${AGENT_TEST_DIR}/mlflow_telemetry.py"

# Available after implementing the proposed suite and pytest.ini.
python -m pytest -c "${AGENT_TEST_DIR}/pytest.ini" "${AGENT_TEST_DIR}/tests" -m 'not live' -q
python -m pytest -c "${AGENT_TEST_DIR}/pytest.ini" "${AGENT_TEST_DIR}/tests/unit/test_agent_stream.py" -q

# Only with explicit test service/repository configuration and budget.
python -m pytest -c "${AGENT_TEST_DIR}/pytest.ini" "${AGENT_TEST_DIR}/tests/live" -m live -q
```

After all four suites/configurations exist, run them in separate processes
using their prepared environments. This loop continues through failures
and returns a failing exit status if any agent's checks fail:

```bash
bash <<'SH'
test_status=0
for agent_dir in doc-gen-agent openapi-gen-agent catalog-gen-agent compliance-scan-agent; do
    agent_python="/tmp/${agent_dir}-test-venv/bin/python"
    if ! "$agent_python" -m py_compile "$agent_dir/agent.py" "$agent_dir/app.py" "$agent_dir/mlflow_telemetry.py"; then
        test_status=1
        continue
    fi
    if ! "$agent_python" -m pytest -c "$agent_dir/pytest.ini" "$agent_dir/tests" -m 'not live' -q; then
        test_status=1
    fi
done
exit "$test_status"
SH
```

A source syntax error fails the affected agent's check without suppressing
results for the others. Missing environments, suites, or configs are setup
failures, not skipped/passing test runs.

For shell changes, run `bash -n` on each modified script. For YAML changes,
parse every document, including multi-document manifests, with PyYAML.
The following checks all four agents without deploying them:

```bash
for agent_dir in doc-gen-agent openapi-gen-agent catalog-gen-agent compliance-scan-agent; do
    bash -n "$agent_dir/start.sh"
    bash -n "$agent_dir/build-and-deploy.sh"
done
python - <<'PY'
from pathlib import Path
import yaml

agents = ("doc-gen-agent", "openapi-gen-agent",
          "catalog-gen-agent", "compliance-scan-agent")
for agent in agents:
    root = Path(agent)
    paths = sorted((root / "k8s").rglob("*.yaml"))
    paths.append(root / "ogx/stack_run_config.yaml")
    for path in paths:
        with path.open() as stream:
            list(yaml.safe_load_all(stream))
        print(f"Parsed {path}")
PY
```

Install PyYAML in the development environment for that YAML check. Parsing
does not validate Kubernetes schemas or prove deployment health. Keep
model identity, sidecar images, resources, non-root behavior, and shared
ConfigMap ownership consistent with `AGENTS.md`; validate relevant
cross-file relationships when those configurations change. Never run
deployment/bootstrap scripts as unit-test setup.

For a change, run its relevant tests and the required syntax checks;
run the affected agents' full offline suites when fixtures, configuration,
or orchestration change. Changes copied across all four telemetry modules
require checks in all four suites. Report commands and results, including blocked,
skipped, or absent tests. A syntax check or pytest's "no tests collected"
result is not a passing functional suite. Do not add trivial tests solely
for documentation edits or a coverage number.
