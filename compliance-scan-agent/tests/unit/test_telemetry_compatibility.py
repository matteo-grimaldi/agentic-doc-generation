"""SDK boundary tests using local fakes; no installed SDK globals are patched."""
import contextlib
import sys
from types import SimpleNamespace as NS
from unittest.mock import Mock

import pytest


@pytest.mark.parametrize("enabled,propagation,broken", [
    (False, "true", False), (True, "false", False),
    (True, "true", False), (True, "yes", True),
])
def test_trace_propagation(agent_modules, monkeypatch, enabled, propagation, broken):
    t = agent_modules[2]
    monkeypatch.setattr(t, "_ENABLED", enabled)
    monkeypatch.setenv("MLFLOW_OGX_TRACE_PROPAGATION", propagation)
    get_headers = Mock(return_value={"traceparent": "test-parent"})
    if broken:
        get_headers.side_effect = RuntimeError("propagation unavailable")
    monkeypatch.setitem(sys.modules, "mlflow.tracing.distributed", NS(get_tracing_context_headers_for_http_request=get_headers))
    result = t.mlflow_outbound_trace_headers()
    assert result == ({"traceparent": "test-parent"} if enabled and propagation == "true" else {})
    assert get_headers.call_count == int(enabled and propagation != "false")


@pytest.mark.parametrize("available,broken", [(False, False), (True, False), (True, True)])
def test_span_processor_registration(agent_modules, monkeypatch, available, broken):
    t = agent_modules[2]
    configure = Mock()
    if broken:
        configure.side_effect = RuntimeError("SDK unavailable")
    monkeypatch.setitem(sys.modules, "mlflow", NS(tracing=NS(configure=configure) if available else NS()))
    t._register_span_processors()
    if available:
        configure.assert_called_once_with(span_processors=[t.enrich_span_usage_and_cost])
    else:
        configure.assert_not_called()


@pytest.mark.parametrize("legacy", [False, True])
def test_trace_session_context_sdk_versions(agent_modules, monkeypatch, legacy):
    t = agent_modules[2]
    entered = []
    @contextlib.contextmanager
    def context(**kwargs):
        entered.append(kwargs)
        yield
        entered.append("closed")
    monkeypatch.setitem(sys.modules, "mlflow", NS(tracing=NS() if legacy else NS(context=context)))
    with t._trace_session_context("session"):
        assert entered == ([] if legacy else [{"session_id": "session"}])
    assert entered == ([] if legacy else [{"session_id": "session"}, "closed"])


@pytest.mark.parametrize("failure", [False, True])
def test_legacy_prompt_loader(agent_modules, monkeypatch, failure):
    t = agent_modules[2]
    monkeypatch.setattr(t, "_ENABLED", True)
    calls = []
    def load(uri, **kwargs):
        calls.append((uri, kwargs))
        if "link_to_model" in kwargs:
            raise TypeError("unsupported argument")
        if failure:
            raise RuntimeError("offline")
        return NS(template="instructions", version=7)
    genai = NS(load_prompt=load, register_prompt=Mock(side_effect=RuntimeError("offline")))
    monkeypatch.setitem(sys.modules, "mlflow", NS(genai=genai))
    token = t._active_prompt_ref.set(None)
    try:
        result = t.ensure_and_link_agent_prompt(agent_name="test", instructions="instructions")
        assert result == (None if failure else ("test-instructions", "7"))
        assert len(calls) >= 2
    finally:
        t._active_prompt_ref.reset(token)


@pytest.mark.parametrize("broken", [False, True])
def test_prompt_link_legacy_fallback(agent_modules, monkeypatch, broken):
    t = agent_modules[2]
    monkeypatch.setattr(t, "_ENABLED", True)
    loader = Mock(side_effect=[TypeError("old SDK"), RuntimeError("offline") if broken else None])
    monkeypatch.setitem(sys.modules, "mlflow", NS(genai=NS(load_prompt=loader)))
    t.link_prompt_ref_to_active_span("prompt", "2")
    assert loader.call_count == 2
    assert loader.call_args.args == ("prompts:/prompt/2",)


@pytest.mark.asyncio
async def test_openai_hooks_preserve_calls_and_install_once(agent_modules, monkeypatch):
    t = agent_modules[2]
    sync_calls, async_calls = [], []
    class Completions:
        def create(self, **kwargs):
            sync_calls.append(kwargs)
            return "sync-result"
    class AsyncCompletions:
        async def create(self, **kwargs):
            async_calls.append(kwargs)
            return "async-result"
    monkeypatch.setattr(t.importlib.util, "find_spec", lambda name: object())
    monkeypatch.setitem(sys.modules, "openai.resources.chat.completions", NS(Completions=Completions, AsyncCompletions=AsyncCompletions))
    link = Mock()
    monkeypatch.setattr(t, "link_prompt_ref_to_active_span", link)
    token = t._active_prompt_ref.set(("prompt", "3"))
    try:
        t.install_openai_chat_completions_hooks()
        installed = Completions.create
        t.install_openai_chat_completions_hooks()
        assert Completions.create is installed
        assert Completions().create(stream=True, model="demo", stream_options={"other": True}) == "sync-result"
        assert await AsyncCompletions().create(stream=False, model="demo") == "async-result"
        assert sync_calls == [{"stream": True, "model": "demo", "stream_options": {"other": True, "include_usage": True}}]
        assert async_calls == [{"stream": False, "model": "demo"}]
        assert link.call_count == 2
        link.assert_called_with("prompt", "3")
    finally:
        t._active_prompt_ref.reset(token)


def test_missing_openai_hooks_are_nonfatal(agent_modules, monkeypatch):
    t = agent_modules[2]
    monkeypatch.setattr(t.importlib.util, "find_spec", lambda name: None)
    t.install_openai_chat_completions_hooks()


def test_tool_span_failures_and_missing_ids(agent_modules, monkeypatch):
    t = agent_modules[2]
    monkeypatch.setattr(t, "_ENABLED", True)
    span = Mock()
    span.set_inputs.side_effect = RuntimeError("unsupported")
    span.set_outputs.side_effect = RuntimeError("unsupported")
    cm = Mock(__enter__=Mock(return_value=span), __exit__=Mock(side_effect=RuntimeError("close failed")))
    start = Mock(return_value=cm)
    monkeypatch.setitem(sys.modules, "mlflow", NS(start_span=start))
    tracker = t.McpToolSpanTracker()
    tracker.on_mcp_call_added(NS(type="mcp_call"))
    start.assert_not_called()
    tracker.on_mcp_call_added(NS(type="mcp_call", id="1"))
    tracker.close_all()
    tracker.close_all()
    cm.__exit__.assert_called_once()
