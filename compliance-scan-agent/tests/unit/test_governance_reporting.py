from unittest.mock import AsyncMock

import pytest

URL = "https://github.com/test/demo"


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [200, 400, 503])
async def test_callback_http_outcome_is_optional(agent_modules, monkeypatch, http_mock, status, caplog):
    import httpx
    app = agent_modules[1]
    monkeypatch.setattr(app, "GOVERNANCE_HUB_URL", "https://hub.invalid")
    seen = []
    def handler(request):
        import json
        seen.append(json.loads(request.content))
        return httpx.Response(status, json={})
    http_mock(handler)
    await app._report_to_governance("job", URL, "completed", triggered_by="ui")
    assert seen[0]["triggered_by"] == "ui"
    assert ("failed to report" in caplog.text) == (status != 200)


@pytest.mark.asyncio
async def test_chat_stream_failure_reports_and_clears_state(agent_modules, monkeypatch):
    app = agent_modules[1]
    async def broken(*args, **kwargs):
        yield "partial"
        raise RuntimeError("backend failed")
    monkeypatch.setattr(app, "run_agent", broken)
    if hasattr(app, "_fetch_repo_structure"):
        monkeypatch.setattr(app, "_fetch_repo_structure", AsyncMock(return_value={"files": [
            {"path": "pom.xml"}, {"path": "controllers/DemoController.java"}]}))
    report = AsyncMock()
    monkeypatch.setattr(app, "_report_to_governance", report)
    results = [part async for part in app.chat(URL, [])]
    assert report.await_args.args[1:3] == (URL, "failed")
    assert report.await_args.kwargs["triggered_by"] == "ui"
    if isinstance(results[-1], tuple):
        assert results[-1][1:] == ("", "")
