"""Offline fixtures. Never substitute or rewrite application source."""
import importlib
import os
from pathlib import Path
import socket
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

ROOT = Path(__file__).resolve().parents[1]


def pytest_collection_finish(session):
    # Collection can inventory tests without importing the broken component.
    if session.config.option.collectonly:
        return
    for name in ("mlflow_telemetry.py", "agent.py", "app.py"):
        path = ROOT / name
        try:
            compile(path.read_text(), str(path), "exec")
        except SyntaxError as exc:
            raise pytest.UsageError(
                f"Component syntax blocks test execution: {path}:{exc.lineno}: "
                f"{exc.msg}. Source was not modified; see TESTING_RULES.md."
            ) from exc


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    attempts = []

    def deny(*args, **kwargs):
        attempts.append(args[1:])
        raise AssertionError("Unexpected outbound network connection")

    monkeypatch.setattr(socket.socket, "connect", deny)
    monkeypatch.setattr(socket.socket, "connect_ex", deny)
    for name in tuple(os.environ):
        if name.startswith("MLFLOW_") or name in ("GITHUB_TOKEN", "EXTERNAL_MODEL_API_KEY", "UPSTREAM_MODEL_API_KEY"):
            monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("MLFLOW_TRACKING_URI", "")
    monkeypatch.setenv("GRADIO_ANALYTICS_ENABLED", "False")
    monkeypatch.setenv("MLFLOW_OGX_TRACE_PROPAGATION", "false")
    yield
    assert not attempts, f"Unmocked network access: {attempts}"


@pytest.fixture
def agent_modules(monkeypatch, tmp_path):
    monkeypatch.syspath_prepend(str(ROOT))
    # This suite must run separately from other agents' flat modules.
    for name in ("agent", "app", "mlflow_telemetry"):
        existing = sys.modules.get(name)
        if existing is not None:
            assert Path(existing.__file__).parent == ROOT, f"Foreign module: {name}"
    monkeypatch.setenv("KNOWLEDGE_FILE", str(tmp_path / "missing.md"))
    monkeypatch.setenv("MCP_GATEWAY_URL", "https://gateway.invalid/mcp")
    telemetry = importlib.import_module("mlflow_telemetry")
    for name, value in {
        "_CONFIGURED": False, "_ENABLED": False,
        "_logged_missing_chat_usage": False, "_logged_missing_registry_rates": False,
        "_usd_rates_cache": None, "_usd_rates_cache_model": "", "_usd_rates_cache_at": 0.0,
    }.items():
        monkeypatch.setattr(telemetry, name, value)
    monkeypatch.setattr(telemetry, "_agent_debug_log", Mock())
    agent = importlib.import_module("agent")
    app = importlib.import_module("app")
    monkeypatch.setattr(app, "GOVERNANCE_HUB_URL", "")
    monkeypatch.setattr(app, "GITHUB_MCP_SERVER_URL", "https://github-mcp.invalid")
    yield agent, app, telemetry


@pytest.fixture
def ogx(agent_modules, monkeypatch):
    agent, _, _ = agent_modules
    create = AsyncMock()
    tracker = Mock()
    constructor = Mock(return_value=SimpleNamespace(responses=SimpleNamespace(create=create)))
    monkeypatch.setattr(agent, "AsyncOgxClient", constructor)
    monkeypatch.setattr(agent, "_load_knowledge", lambda: "")
    monkeypatch.setattr(agent, "_mcp_gateway_tool", lambda: {"type": "mcp", "server_url": "https://gateway.invalid"})
    monkeypatch.setattr(agent, "get_mcp_tool_span_tracker", lambda: tracker)
    return SimpleNamespace(agent=agent, create=create, tracker=tracker, constructor=constructor)


@pytest.fixture
def http_mock(agent_modules, monkeypatch):
    import httpx
    _, app, _ = agent_modules
    real_client = httpx.AsyncClient

    def install(handler):
        transport = httpx.MockTransport(handler)
        monkeypatch.setattr(app.httpx, "AsyncClient", lambda **kw: real_client(transport=transport, **kw))
    return install
