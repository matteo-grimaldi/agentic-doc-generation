import asyncio
from types import SimpleNamespace as NS

import pytest

pytestmark = pytest.mark.asyncio


async def events(*items):
    for item in items:
        if isinstance(item, BaseException):
            raise item
        yield item


async def drain(agent):
    return "".join([part async for part in agent.run_agent("https://github.com/test/demo")])


async def test_request_and_incremental_output(ogx, monkeypatch):
    release = asyncio.Event()

    async def stream():
        yield NS(type="response.output_text.delta", delta="first")
        await release.wait()
        yield NS(type="response.output_text.delta", delta="second")

    monkeypatch.setattr(ogx.agent, "_load_knowledge", lambda: "style-guide")
    ogx.create.return_value = stream()
    run = ogx.agent.run_agent("https://github.com/test/demo")
    try:
        assert await anext(run) == "first"
        release.set()
        assert await anext(run) == "second"
        with pytest.raises(StopAsyncIteration):
            await anext(run)
    finally:
        release.set()
        await run.aclose()
    request = ogx.create.call_args.kwargs
    assert request["stream"] is True
    assert request["model"] == ogx.agent.MODEL_NAME
    assert request["extra_body"] == {"max_infer_iters": ogx.agent.MAX_INFER_ITERS}
    assert "style-guide" in request["instructions"]
    assert "https://github.com/test/demo" in request["input"]
    assert request["tools"][0]["type"] == "mcp"


async def test_tool_events(ogx):
    added = NS(type="mcp_call", id="1", name="read_file", server_label="GitHub MCP")
    done = NS(type="mcp_call", id="1", name="read_file", server_label="GitHub MCP", error="not found")
    ogx.create.return_value = events(NS(type="ignored"), NS(type="response.output_item.added", item=None), NS(type="response.output_item.added", item=added), NS(type="response.output_item.done", item=done), NS(type="response.output_text.delta", delta="# Architecture: demo"))
    result = await drain(ogx.agent)
    assert "Calling **GitHub MCP**" in result and "failed: not found" in result
    ogx.tracker.on_mcp_call_added.assert_called_once_with(added)
    ogx.tracker.on_mcp_call_done.assert_called_once_with(done)


@pytest.mark.parametrize("status", ["failed", "incomplete", "cancelled"])
async def test_terminal_status(ogx, status):
    ogx.create.return_value = events(NS(type="response.completed", response={"status": status}))
    result = await drain(ogx.agent)
    assert result.count("⚠️ **Error:**") == 1
    assert f"`{status}`" in result


async def test_error_events_deduplicated(ogx):
    payload = {"error": {"code": "denied", "message": "not allowed"}}
    ogx.create.return_value = events(NS(type="response.failed", response=payload), NS(type="error", error=payload["error"]), NS(type="response.completed", response=payload))
    result = await drain(ogx.agent)
    assert result.count("⚠️ **Error:**") == 1
    assert "not allowed" in result


@pytest.mark.parametrize("items", [(), (NS(type="unknown"),), (NS(type="response.completed", response={"status": "completed"}),)])
async def test_no_text(ogx, items):
    ogx.create.return_value = events(*items)
    assert "without producing documentation" in await drain(ogx.agent)


@pytest.mark.parametrize("error,fragment", [(TimeoutError(), "Timed out connecting"), (RuntimeError("offline"), "Could not reach")])
async def test_create_failure(ogx, error, fragment):
    ogx.create.side_effect = error
    assert fragment in await drain(ogx.agent)


@pytest.mark.parametrize("error,fragment", [(TimeoutError(), "took longer"), (RuntimeError("broken stream"), "encountered an error")])
async def test_stream_failure_preserves_partial_text(ogx, error, fragment):
    ogx.create.return_value = events(NS(type="response.output_text.delta", delta="partial"), error)
    result = await drain(ogx.agent)
    assert result.startswith("partial") and fragment in result


async def test_missing_gateway_never_starts_request(ogx, monkeypatch):
    def fail():
        raise RuntimeError("missing gateway")
    monkeypatch.setattr(ogx.agent, "_mcp_gateway_tool", fail)
    assert "missing gateway" in await drain(ogx.agent)
    ogx.create.assert_not_awaited()


async def test_cancellation_propagates(ogx):
    ogx.create.return_value = events(asyncio.CancelledError())
    with pytest.raises(asyncio.CancelledError):
        await drain(ogx.agent)


async def test_successful_tool_completion(ogx):
    item = NS(type="mcp_call", id="1", name="read_file", server_label="GitHub MCP", error=None)
    ogx.create.return_value = events(NS(type="response.output_item.added", item=item), NS(type="response.output_item.done", item=item), NS(type="response.output_text.delta", delta="document"))
    result = await drain(ogx.agent)
    assert "failed:" not in result and result.endswith("document")
    ogx.tracker.on_mcp_call_done.assert_called_once_with(item)


async def test_real_stream_deadline(ogx, monkeypatch):
    monkeypatch.setattr(ogx.agent, "AGENT_TIMEOUT_SECONDS", .02)
    async def blocked():
        yield NS(type="response.output_text.delta", delta="partial")
        await asyncio.Event().wait()
    stream = blocked()
    ogx.create.return_value = stream
    try:
        result = await asyncio.wait_for(drain(ogx.agent), 2)
        assert result.startswith("partial") and "took longer" in result
    finally:
        await stream.aclose()
