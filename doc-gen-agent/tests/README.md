# Doc-gen unit tests

This suite follows the root `TESTING_RULES.md`: lazy application imports,
function-scoped mocks, synthetic async OGX streams, strict HTTPX transports,
temporary files, and isolated telemetry context. Network connections are
blocked and recorded, including attempts swallowed by application handlers.
No model, GitHub token, cluster, tracking server, or PR is used.

Install with Python 3.11 (the container runtime), then run from the repo root:

```bash
python3.11 -m venv /tmp/doc-gen-unit-venv
source /tmp/doc-gen-unit-venv/bin/activate
python -m pip install -r doc-gen-agent/requirements-dev.txt
python -m pytest -c doc-gen-agent/pytest.ini doc-gen-agent/tests
```

Run this suite separately from other agents because their flat module names
collide. Tests cover extraction, gateway/token errors, knowledge loading,
response normalization, streaming and deadlines, chat/PR state, REST payloads,
background outcomes, API validation, telemetry headers, prompts, spans,
pricing/usage, SDK compatibility, OpenAI hook installation, and concurrent
chat/trace context cleanup. The API test uses a real
in-process FastAPI app with Gradio mounting and background execution mocked;
it does not assert production response latency or model output quality.

## Syntax preflight and validation

The original indentation error in `mlflow_telemetry.py` was repaired, with
user authorization, by removing an orphaned exception-handler fragment.
Regression tests preserve successful/idempotent setup, workspace-failure
fallback, and the existing experiment-failure exception policy. The suite
checks source syntax before executing tests; it never rewrites or stubs
an application module to bypass syntax errors.

You can inventory all tests without runtime application dependencies:

```bash
python -m pytest -c doc-gen-agent/pytest.ini doc-gen-agent/tests --collect-only -q
```

Validation: all 161 tests passed on both Python 3.11.18 (the container's
Python minor version) and Python 3.15.2. Both environments used pytest
9.2.0, pytest-asyncio 1.4.1.post1, HTTPX 0.28.1, Gradio 6.31.0, and MLflow
3.18.0. Dependency resolution selected OGX client 1.1.3 on Python 3.11 and
1.5.0 on Python 3.15. Application and test sources also compile. Coverage
percentages remain unmeasured. Runtime dependency ranges remain those of
the component's existing `requirements.txt`.

The suite deliberately tests current behavior for partial architecture
output: extraction can retain partial text before an error footer. It does
not claim that every backend failure prevents a PR, that Mermaid renders,
or that generated architecture claims are accurate.
