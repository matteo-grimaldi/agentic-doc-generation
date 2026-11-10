# catalog-gen-agent tests

Run from the repository root with Python 3.11:

```bash
python3.11 -m venv /tmp/catalog-gen-agent-test-venv
source /tmp/catalog-gen-agent-test-venv/bin/activate
python -m pip install -r catalog-gen-agent/requirements-dev.txt
python -m pytest -c catalog-gen-agent/pytest.ini catalog-gen-agent/tests -q
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

Agent-specific coverage includes catalog/API classification, existing-catalog augmentation instructions, guideline loading, multi-document YAML relationships, prefetch fallback, and PR payloads.
Shared coverage includes SDK request wiring, stream/tool/error/timeout and
empty states, API validation, worker outcomes, session isolation, prompt
registry compatibility, token usage, pricing, tracing cleanup, and OpenAI
hooks using fake SDK classes. Telemetry tests import this agent's own copy;
they do not import another agent's application or tests. Workspace setup
errors retain this agent's existing exception policy.

Extraction tests characterize current behavior; they do not prove model
quality or schema correctness. YAML/Markdown fixture assertions verify local output handling, not the accuracy of generated facts or model compliance with guidelines.

Python 3.11.18 validation uses pytest 9.2.0, pytest-asyncio 1.4.1.post1,
HTTPX 0.28.1, Gradio 6.31.0, OGX client 1.1.3, MLflow 3.18.0, and PyYAML
6.0.3. Expected inventory: 166 tests. Runtime dependency ranges remain
unchanged; a passing suite does not establish live deployment health.
