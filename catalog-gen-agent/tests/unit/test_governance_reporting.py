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


@pytest.mark.asyncio
@pytest.mark.parametrize("failed", [False, True])
async def test_ui_proposal_outcome_is_reported(agent_modules, monkeypatch, failed):
    app = agent_modules[1]
    proposal_name = next(n for n in ("_propose_architecture_doc", "_propose_openapi_spec", "_propose_catalog_info") if hasattr(app, n))
    result = {"error": "denied"} if failed else {"pr_url": "https://example.invalid/pr/1", "pr_number": 1}
    monkeypatch.setattr(app, proposal_name, AsyncMock(return_value=result))
    report = AsyncMock()
    monkeypatch.setattr(app, "_report_to_governance", report)
    await app.propose_pr_clicked(URL, "generated content")
    assert report.await_args.args[1:3] == (URL, "failed" if failed else "completed")
    assert report.await_args.kwargs["triggered_by"] == "ui-pr"
    assert report.await_args.kwargs["pr_url"] == result.get("pr_url")
