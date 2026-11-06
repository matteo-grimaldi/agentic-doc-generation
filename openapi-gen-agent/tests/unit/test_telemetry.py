import asyncio
import contextlib
import sys
import time
from types import SimpleNamespace as NS
from unittest.mock import Mock

import pytest


@pytest.mark.parametrize("raw,expected", [
    (None, None), ({}, None), ([], None),
    ({"input_tokens": "bad"}, None),
    ({"input_tokens": 0}, {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}),
    ({"prompt_tokens": "2", "completion_tokens": "3"}, {"input_tokens": 2, "output_tokens": 3, "total_tokens": 5}),
    ({"input_tokens": 2, "output_tokens": 3, "total_tokens": "bad"}, {"input_tokens": 2, "output_tokens": 3, "total_tokens": 5}),
    ({"output_tokens": 4, "total_tokens": 8}, {"input_tokens": 0, "output_tokens": 4, "total_tokens": 8}),
])
def test_usage(agent_modules, raw, expected):
    assert agent_modules[2]._normalize_usage_dict(raw) == expected


@pytest.mark.parametrize("raw,expected", [(" provider/model:x% ", "provider--model_x_pct_"), ("", ""), (None, "")])
def test_registry_name(agent_modules, raw, expected):
    assert agent_modules[2].registry_model_name_for(raw) == expected


@pytest.mark.parametrize("pair,expected", [((None, 1), None), (("bad", 1), None), ((" 2.5 ", "0"), (2.5, 0.0))])
def test_rate_parsing(agent_modules, pair, expected):
    assert agent_modules[2]._parse_usd_pair(*pair) == expected


def test_cost(agent_modules):
    calculate = agent_modules[2].compute_llm_cost_usd
    assert calculate(input_tokens=0, output_tokens=0, input_usd_per_million=2, output_usd_per_million=4)["total_cost"] == 0
    result = calculate(input_tokens=1000, output_tokens=500, input_usd_per_million=2, output_usd_per_million=4)
    assert result == pytest.approx({"input_cost": .002, "output_cost": .002, "total_cost": .004})


def test_pricing_cache(agent_modules, monkeypatch):
    t = agent_modules[2]
    fetch = Mock(return_value=(1.0, 2.0))
    clock = Mock(return_value=1000.0)
    monkeypatch.setattr(t, "_fetch_usd_rates_from_registry", fetch)
    monkeypatch.setattr(time, "monotonic", clock)
    assert t._usd_rates_for_model("a") == (1.0, 2.0)
    t._usd_rates_for_model("a")
    assert fetch.call_count == 1
    clock.return_value += t._USD_RATES_CACHE_TTL_SEC + 1
    t._usd_rates_for_model("a")
    t._usd_rates_for_model("b")
    t._usd_rates_for_model("b", force_refresh=True)
    assert fetch.call_count == 4


def test_disabled_headers_and_configuration(agent_modules):
    t = agent_modules[2]
    assert t.mlflow_ogx_request_headers() == {}
    assert t.mlflow_outbound_trace_headers() == {}
    assert t.ensure_and_link_agent_prompt(agent_name="test", instructions="test") is None
    assert t.configure_mlflow(service_name="test") is False
    assert t.configure_mlflow(service_name="again") is False


@pytest.mark.parametrize("template,expected", [("plain", "plain"), ("{{value}}", "{% raw %}{{value}}{% endraw %}"), ("{% x %}", "{% raw %}{% x %}{% endraw %}")])
def test_prompt_escape(agent_modules, template, expected):
    assert agent_modules[2]._prompt_template_for_registry(template) == expected


@pytest.mark.parametrize("stream", [False, True])
def test_stream_usage_options_preserve_input(agent_modules, stream):
    original = {"stream": stream, "stream_options": {"other": True}}
    result = agent_modules[2].ensure_stream_usage_kwargs(original)
    assert original == {"stream": stream, "stream_options": {"other": True}}
    assert result["stream_options"].get("include_usage", False) == stream
    assert result["stream_options"]["other"] is True


@pytest.mark.asyncio
async def test_context_headers_restored(agent_modules, monkeypatch):
    t = agent_modules[2]
    monkeypatch.setattr(t, "_ENABLED", True)
    tokens = [(var, var.set(None)) for var in (t._run_session_id, t._active_prompt_ref, t._mcp_tool_tracker)]
    try:
        t._run_session_id.set("session")
        t.set_active_prompt_ref("prompt", "2")
        assert t.get_active_prompt_ref() == ("prompt", "2")
        assert t.mlflow_ogx_request_headers() == {t.MLFLOW_TRACE_SESSION_HEADER: "session", t.MLFLOW_PROMPT_NAME_HEADER: "prompt", t.MLFLOW_PROMPT_VERSION_HEADER: "2"}
        t.set_active_prompt_ref(None, None)
        assert t.get_active_prompt_ref() is None
    finally:
        for var, token in reversed(tokens):
            var.reset(token)


@pytest.mark.parametrize("existing,failure", [(True, False), (False, False), (False, True)])
def test_prompt_registry(agent_modules, monkeypatch, existing, failure):
    t = agent_modules[2]
    monkeypatch.setattr(t, "_ENABLED", True)
    prompt = NS(template="instructions", version=3)
    genai = NS(load_prompt=Mock(return_value=prompt if existing else None), register_prompt=Mock(return_value=prompt), set_prompt_alias=Mock())
    if failure:
        genai.register_prompt.side_effect = RuntimeError("offline")
    monkeypatch.setitem(sys.modules, "mlflow", NS(genai=genai))
    token = t._active_prompt_ref.set(None)
    try:
        result = t.ensure_and_link_agent_prompt(agent_name="test", instructions="instructions")
        if failure:
            assert result is None
            assert t.get_active_prompt_ref() is None
        else:
            assert result == ("test-instructions", "3")
            assert genai.register_prompt.call_count == (0 if existing else 1)
            assert t.get_active_prompt_ref() == result
    finally:
        t._active_prompt_ref.reset(token)


def test_tool_tracker_deduplication_and_cleanup(agent_modules, monkeypatch):
    t = agent_modules[2]
    monkeypatch.setattr(t, "_ENABLED", True)
    span = Mock()
    cm = Mock(__enter__=Mock(return_value=span), __exit__=Mock(return_value=False))
    fake = NS(start_span=Mock(return_value=cm))
    monkeypatch.setitem(sys.modules, "mlflow", fake)
    tracker = t.McpToolSpanTracker()
    item = NS(type="mcp_call", id="1", name="read_file", server_label="github")
    tracker.on_mcp_call_added(None)
    tracker.on_mcp_call_added(NS(type="other"))
    tracker.on_mcp_call_added(item)
    tracker.on_mcp_call_added(item)
    fake.start_span.assert_called_once()
    tracker.on_mcp_call_done(NS(type="mcp_call", id="missing"))
    tracker.on_mcp_call_done(NS(type="mcp_call", id="1", error="denied"))
    span.set_outputs.assert_called_with({"error": "denied", "status": "failed"})
    tracker.on_mcp_call_done(item)
    assert cm.__exit__.call_count == 1
    tracker.on_mcp_call_added(item)
    tracker.close_all()
    tracker.close_all()
    assert cm.__exit__.call_count == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["complete", "exception", "cancel", "close"])
async def test_trace_context_cleanup(agent_modules, monkeypatch, outcome):
    t = agent_modules[2]
    monkeypatch.setattr(t, "_ENABLED", True)
    fake = NS(start_span=Mock(side_effect=lambda **kw: contextlib.nullcontext(Mock())), update_current_trace=Mock(), flush_trace_async_logging=Mock(), get_last_active_trace_id=Mock(return_value=None))
    monkeypatch.setitem(sys.modules, "mlflow", fake)
    monkeypatch.setattr(t, "_trace_session_context", lambda session: contextlib.nullcontext())
    tokens = [(v, v.set(value)) for v, value in ((t._run_session_id, "outer"), (t._active_prompt_ref, ("outer", "1")), (t._mcp_tool_tracker, None))]
    tracker = Mock()
    @t.trace_agent_run(name="test", agent_name="test")
    async def generate():
        assert t._run_session_id.get() != "outer"
        assert t._active_prompt_ref.get() is None
        t._mcp_tool_tracker.set(tracker)
        yield "first"
        if outcome == "exception":
            raise ValueError("broken")
        if outcome == "cancel":
            raise asyncio.CancelledError()
    run = generate()
    try:
        assert await anext(run) == "first"
        if outcome == "close":
            await run.aclose()
        elif outcome in ("exception", "cancel"):
            with pytest.raises(ValueError if outcome == "exception" else asyncio.CancelledError):
                await anext(run)
        else:
            with pytest.raises(StopAsyncIteration):
                await anext(run)
        tracker.close_all.assert_called_once()
        assert t._run_session_id.get() == "outer"
        assert t._active_prompt_ref.get() == ("outer", "1")
        assert t._mcp_tool_tracker.get() is None
    finally:
        await run.aclose()
        for var, token in reversed(tokens):
            var.reset(token)


@pytest.mark.asyncio
async def test_concurrent_trace_contexts(agent_modules, monkeypatch):
    t = agent_modules[2]
    monkeypatch.setattr(t, "_ENABLED", True)
    monkeypatch.setitem(sys.modules, "mlflow", NS(start_span=lambda **kw: contextlib.nullcontext(Mock()), update_current_trace=Mock()))
    monkeypatch.setattr(t, "_trace_session_context", lambda session: contextlib.nullcontext())
    monkeypatch.setattr(t, "_flush_mlflow_traces", Mock())
    release = asyncio.Event()
    ready = [asyncio.Event(), asyncio.Event()]
    seen = []
    @t.trace_agent_run(name="test", agent_name="test")
    async def generate(index):
        t.set_active_prompt_ref(f"prompt-{index}", "1")
        tracker = t.get_mcp_tool_span_tracker()
        session = t._run_session_id.get()
        ready[index].set()
        await release.wait()
        assert t._run_session_id.get() == session
        assert t.get_active_prompt_ref() == (f"prompt-{index}", "1")
        seen.append((session, tracker))
        yield "done"
    async def consume(index):
        return [part async for part in generate(index)]
    tokens = [(var, var.set(None)) for var in (t._run_session_id, t._active_prompt_ref, t._mcp_tool_tracker)]
    tasks = [asyncio.create_task(consume(i)) for i in range(2)]
    try:
        await asyncio.wait_for(asyncio.gather(*(event.wait() for event in ready)), 2)
        release.set()
        assert await asyncio.gather(*tasks) == [["done"], ["done"]]
        assert seen[0][0] != seen[1][0] and seen[0][1] is not seen[1][1]
        assert all(var.get() is None for var, _ in tokens)
    finally:
        release.set()
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        for var, token in reversed(tokens):
            var.reset(token)


@pytest.mark.parametrize("uri,explicit,expected", [
    ("https://mlflow.ns.svc.cluster.local", None, "true"),
    ("https://example.invalid", None, "(unset)"),
    ("http://mlflow.ns.svc.cluster.local", None, "(unset)"),
    ("https://mlflow.ns.svc.cluster.local", "false", "false"),
    ("https://example.invalid", "true", "true"),
])
def test_tls_configuration(agent_modules, monkeypatch, uri, explicit, expected):
    if explicit is not None:
        monkeypatch.setenv("MLFLOW_TRACKING_INSECURE_TLS", explicit)
    assert agent_modules[2]._apply_mlflow_tls_env(tracking_uri=uri) == expected


@pytest.mark.parametrize("kind", ["empty", "attributes", "outputs", "broken-attributes"])
def test_usage_sources(agent_modules, kind):
    t = agent_modules[2]
    usage = {"input_tokens": 2, "output_tokens": 3}
    span = NS(attributes={"mlflow.chat.tokenUsage": usage} if kind == "attributes" else {}, outputs={"usage": usage} if kind in ("outputs", "broken-attributes") else {})
    if kind == "broken-attributes":
        span.attributes = []
    result = t._extract_usage_from_span(span)
    assert result == (None if kind == "empty" else {**usage, "total_tokens": 5})


@pytest.mark.parametrize("kind,rates", [("LLM", (2, 4)), ("AGENT", (2, 4)), ("TOOL", (2, 4)), ("LLM", None)])
def test_span_enrichment(agent_modules, monkeypatch, kind, rates):
    t = agent_modules[2]
    monkeypatch.setenv("MODEL_NAME", "external-model/demo")
    fetch = Mock(return_value=rates)
    publish = Mock()
    monkeypatch.setattr(t, "_usd_rates_for_model", fetch)
    monkeypatch.setattr(t, "_publish_trace_level_cost", publish)
    span = NS(attributes={"mlflow.spanType": kind, "mlflow.chat.tokenUsage": {"input_tokens": 1000, "output_tokens": 500}}, set_attribute=Mock())
    t.enrich_span_usage_and_cost(span)
    attrs = dict(call.args for call in span.set_attribute.call_args_list)
    assert attrs["mlflow.chat.tokenUsage"]["total_tokens"] == 1500
    if kind in ("LLM", "AGENT"):
        assert attrs["llm.model_name"] == "external-model/demo"
    if kind == "LLM" and rates:
        assert attrs["mlflow.llm.cost"]["total_cost"] == pytest.approx(.004)
        publish.assert_called_once()
    else:
        assert "mlflow.llm.cost" not in attrs
        publish.assert_not_called()


def test_missing_usage_warns_once(agent_modules, caplog):
    t = agent_modules[2]
    span = NS(attributes={"mlflow.spanType": "LLM"}, outputs=None, set_attribute=Mock())
    t.enrich_span_usage_and_cost(span)
    t.enrich_span_usage_and_cost(span)
    assert sum("without token usage" in record.message for record in caplog.records) == 1


@pytest.mark.parametrize("kind", ["success", "missing", "offline", "malformed"])
def test_registry_rates(agent_modules, monkeypatch, kind):
    t = agent_modules[2]
    tags = {} if kind == "missing" else {t.REGISTRY_TAG_INPUT_USD: "bad" if kind == "malformed" else "2", t.REGISTRY_TAG_OUTPUT_USD: "4"}
    client = Mock()
    client.get_registered_model.return_value = NS(tags=tags)
    if kind == "offline":
        client.get_registered_model.side_effect = RuntimeError("offline")
    monkeypatch.setitem(sys.modules, "mlflow", NS(MlflowClient=Mock(return_value=client)))
    assert t._fetch_usd_rates_from_registry("") is None
    assert t._fetch_usd_rates_from_registry("provider/model") == ((2, 4) if kind == "success" else None)
    client.get_registered_model.assert_called_once_with("provider--model")


def test_trace_flush_failure_is_nonfatal(agent_modules, monkeypatch):
    t = agent_modules[2]
    monkeypatch.setattr(t, "_ENABLED", True)
    flush = Mock(side_effect=RuntimeError("offline"))
    monkeypatch.setitem(sys.modules, "mlflow", NS(flush_trace_async_logging=flush))
    t._flush_mlflow_traces(location="unit-test")
    flush.assert_called_once_with()


@pytest.mark.asyncio
async def test_disabled_decorator_preserves_stream(agent_modules):
    t = agent_modules[2]
    @t.trace_agent_run(name="test", agent_name="test")
    async def generate():
        yield "a"
        yield "b"
    assert [part async for part in generate()] == ["a", "b"]


@pytest.mark.parametrize("outcome", ["success", "experiment-error", "workspace-error"])
def test_configuration_preserves_setup_and_error_policy(agent_modules, monkeypatch, outcome):
    t = agent_modules[2]
    monkeypatch.setattr(t, "_CONFIGURED", False)
    monkeypatch.setenv("MLFLOW_TRACKING_URI", "https://tracking.invalid")
    fake = NS(set_tracking_uri=Mock(), set_workspace=Mock(), set_experiment=Mock())
    if outcome == "experiment-error":
        fake.set_experiment.side_effect = RuntimeError("experiment unavailable")
    if outcome == "workspace-error":
        fake.set_workspace.side_effect = RuntimeError("workspace unavailable")
    monkeypatch.setitem(sys.modules, "mlflow", fake)
    processors = Mock()
    monkeypatch.setattr(t, "_register_span_processors", processors)
    monkeypatch.setattr(t, "_usd_rates_for_model", Mock(return_value=None))
    if outcome == "experiment-error":
        with pytest.raises(RuntimeError, match="experiment unavailable"):
            t.configure_mlflow(service_name="test")
        processors.assert_not_called()
        assert not t._ENABLED
    elif outcome == "workspace-error":
        with pytest.raises(RuntimeError, match="workspace unavailable"):
            t.configure_mlflow(service_name="test")
        fake.set_experiment.assert_not_called()
        processors.assert_not_called()
    else:
        assert t.configure_mlflow(service_name="test") is True
        assert t.configure_mlflow(service_name="test") is True
        fake.set_experiment.assert_called_once_with("test")
        processors.assert_called_once_with()
        assert t._CONFIGURED and t._ENABLED
