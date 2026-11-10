# openapi-gen-agent tests

Run from the repository root with Python 3.11:

```bash
python3.11 -m venv /tmp/openapi-gen-agent-test-venv
source /tmp/openapi-gen-agent-test-venv/bin/activate
python -m pip install -r openapi-gen-agent/requirements-dev.txt
python -m pytest -c openapi-gen-agent/pytest.ini openapi-gen-agent/tests -q
```

Always supply the explicit test directory when invoking pytest from the
repository root. Each agent runs in its own process because application
modules have the same flat names. To run all four using a prepared
environment that satisfies their development requirements:

```bash
AGENT_TEST_PYTHON=/path/to/venv/bin/python bash scripts/run-agent-tests.sh -q
```

The suite follows `TESTING_RULES.md`: lazy local module imports, isolated
mutable state/context variables, temporary files, synthetic async OGX
events, and strict HTTPX transports. Unexpected socket connections fail
teardown even if application code catches the original error. No live
model, GitHub write, cluster, or MLflow service is used.

Agent-specific coverage includes eligibility detection, Java file classification, NOT_AN_API outcomes, knowledge loading, YAML extraction, and PR payloads.
Shared coverage includes SDK request wiring, stream/tool/error/timeout and
empty states, API validation, worker outcomes, session isolation, prompt
registry compatibility, token usage, pricing, tracing cleanup, and OpenAI
hooks using fake SDK classes. Telemetry tests import this agent's own copy;
they do not import another agent's application or tests. Workspace setup
errors retain this agent's existing exception policy.

Extraction and proposal tests use the real YAML/OpenAPI 3.0 validator and
reject malformed specs, external or unresolved references, and YAML aliases.
They do not prove that generated endpoints match repository evidence.
OpenAPI background model rejection currently reports failed after empty
extraction, whereas chat reports skipped.

Python 3.11.18 validation uses pytest 9.2.0, pytest-asyncio 1.4.1.post1,
HTTPX 0.28.1, Gradio 6.31.0, OGX client 1.1.3, MLflow 3.18.0, and PyYAML
6.0.3. Expected inventory: 182 tests. Runtime dependency ranges remain
unchanged; a passing suite does not establish live deployment health.
