"""REST and orchestration contracts, run only against this agent's modules."""
import copy
import json
from unittest.mock import AsyncMock, Mock

import pytest

URL = "https://github.com/test/demo"
STRUCTURE = {"ref": "main", "files": [{"path": "pom.xml"}, {"path": "src/controllers/DemoController.java"}]}


@pytest.mark.parametrize("prompt,expected", [("", None), ("hello", None), (URL, URL), (f"Generate ({URL}).", URL), (f"{URL} https://github.com/a/b", URL)])
def test_url_extraction(agent_modules, prompt, expected):
    assert agent_modules[1]._extract_repo_url(prompt) == expected


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["success", "http", "json-error", "non-json", "network"])
async def test_structure_fetch(agent_modules, http_mock, kind):
    import httpx
    calls = []
    def handler(request):
        assert request.method == "GET" and request.url.path == "/repo-structure"
        assert request.url.params["repo_url"] == URL
        calls.append(request)
        if kind == "network":
            raise httpx.ConnectError("offline", request=request)
        if kind == "non-json":
            return httpx.Response(502, text="bad gateway")
        return httpx.Response(500 if kind == "http" else 200, json=STRUCTURE if kind == "success" else {"error": "denied"})
    http_mock(handler)
    result = await agent_modules[1]._fetch_repo_structure(URL)
    assert len(calls) == 1
    assert result == STRUCTURE if kind == "success" else "error" in result


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["success", "network", "disabled"])
async def test_governance_payload(agent_modules, profile, monkeypatch, http_mock, kind):
    import httpx
    app = agent_modules[1]
    monkeypatch.setattr(app, "GOVERNANCE_HUB_URL", "" if kind == "disabled" else "https://hub.invalid")
    calls = []
    def handler(request):
        assert request.method == "POST" and request.url.path == "/api/runs"
        calls.append(json.loads(request.content))
        if kind == "network":
            raise httpx.ConnectError("offline", request=request)
        return httpx.Response(200, json={})
    http_mock(handler)
    kwargs = {"report": profile.output} if profile.report_only else {"pr_url": "https://example.invalid/pr/1", "pr_number": 1}
    await app._report_to_governance("job", URL, "completed", **kwargs)
    if kind == "disabled":
        assert calls == []
    else:
        assert calls == [{"job_id": "job", "repo_url": URL, "agent_type": profile.agent_type, "status": "completed", "error": None, **kwargs}]


@pytest.mark.asyncio
@pytest.mark.parametrize("blank", ["", " \n"])
async def test_empty_chat(agent_modules, profile, monkeypatch, blank):
    app = agent_modules[1]
    run, fetch = Mock(), AsyncMock()
    monkeypatch.setattr(app, "run_agent", run)
    monkeypatch.setattr(app, "_fetch_repo_structure", fetch)
    result = [part async for part in app.chat(blank, [])]
    assert len(result) == 1
    history = result[0] if profile.report_only else result[0][0]
    assert "paste a GitHub" in history[-1]["content"]
    run.assert_not_called()
    fetch.assert_not_awaited()


@pytest.mark.asyncio
async def test_chat_stream_and_clean_output(agent_modules, profile, monkeypatch):
    app = agent_modules[1]
    calls = []
    async def run(repo, **kwargs):
        calls.append((repo, kwargs))
        yield profile.stream
    fetch = AsyncMock(return_value=STRUCTURE)
    report = AsyncMock()
    monkeypatch.setattr(app, "_fetch_repo_structure", fetch)
    monkeypatch.setattr(app, "run_agent", run)
    monkeypatch.setattr(app, "_report_to_governance", report)
    proposal = AsyncMock()
    if not profile.report_only:
        monkeypatch.setattr(app, profile.proposal, proposal)
    history = [{"role": "assistant", "content": "previous"}]
    original = copy.deepcopy(history)
    result = [copy.deepcopy(part) async for part in app.chat(URL, history)]
    assert history == original
    assert calls == [(URL, {"repo_structure": STRUCTURE})]
    if profile.report_only:
        assert profile.stream in result[-1][-1]["content"]
        assert report.await_args.kwargs["report"] == profile.output
    else:
        assert all(part[1:] == ("", "") for part in result[:-1])
        assert result[-1][1:] == (URL, profile.output)
        proposal.assert_not_awaited()
    assert report.await_args.args[1:3] == (URL, "completed")


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["chat", "background"])
async def test_prefetch_failure_policy(agent_modules, profile, monkeypatch, path):
    app = agent_modules[1]
    calls = []
    async def run(repo, **kwargs):
        calls.append(kwargs)
        yield profile.stream
    monkeypatch.setattr(app, "run_agent", run)
    monkeypatch.setattr(app, "_fetch_repo_structure", AsyncMock(return_value={"error": "offline"}))
    report = AsyncMock()
    monkeypatch.setattr(app, "_report_to_governance", report)
    if not profile.report_only:
        monkeypatch.setattr(app, profile.proposal, AsyncMock(return_value={}))
    if path == "chat":
        _ = [part async for part in app.chat(URL, [])]
    else:
        await app._run_agent_in_background(URL, "job")
    stops = profile.agent_type == "openapi-gen" or (profile.report_only and path == "chat")
    assert calls == ([] if stops else [{"repo_structure": None}])
    assert report.await_args.args[2] == ("failed" if stops else "completed")


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["success", "empty", "missing-url", "stream-error"])
async def test_background_outcomes(agent_modules, profile, monkeypatch, case):
    app = agent_modules[1]
    async def run(repo, **kwargs):
        if case == "stream-error":
            raise RuntimeError("broken")
        yield "" if case == "empty" else profile.stream
    monkeypatch.setattr(app, "run_agent", run)
    fetch = AsyncMock(return_value=STRUCTURE)
    report = AsyncMock()
    proposal = AsyncMock(return_value={"pr_url": "https://example.invalid/pr/1", "pr_number": 1})
    monkeypatch.setattr(app, "_fetch_repo_structure", fetch)
    monkeypatch.setattr(app, "_report_to_governance", report)
    if not profile.report_only:
        monkeypatch.setattr(app, profile.proposal, proposal)
    await app._run_agent_in_background("no URL" if case == "missing-url" else URL, "job")
    if case == "missing-url":
        fetch.assert_not_awaited()
        report.assert_not_awaited()
    else:
        assert report.await_args.args[:3] == ("job", URL, "completed" if case == "success" else "failed")
        if case == "success":
            if profile.report_only:
                assert report.await_args.kwargs["report"] == profile.output
            else:
                proposal.assert_awaited_once_with(URL, profile.output, "job")
        else:
            proposal.assert_not_awaited()


@pytest.mark.asyncio
async def test_http_routes(agent_modules, monkeypatch):
    import httpx
    app = agent_modules[1]
    monkeypatch.setattr(app, "build_ui", lambda: object())
    monkeypatch.setattr(app.gr, "mount_gradio_app", lambda application, demo, **kw: application)
    worker = AsyncMock()
    monkeypatch.setattr(app, "_run_agent_in_background", worker)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app.build_app()), base_url="http://test") as client:
        response = await client.get("/healthz")
        assert response.status_code == 200 and response.json() == {"status": "ok"}
        for payload, status in (({}, 422), ({"prompt": None}, 422), ({"prompt": " "}, 400)):
            assert (await client.post("/invoke", json=payload)).status_code == status
        worker.assert_not_awaited()
        response = await client.post("/invoke", json={"prompt": f" {URL} "})
        assert response.status_code == 202
        data = response.json()
        import uuid
        uuid.UUID(data["job_id"])
        worker.assert_awaited_once_with(URL, data["job_id"])


@pytest.mark.asyncio
async def test_chat_empty_generation(agent_modules, profile, monkeypatch):
    app = agent_modules[1]
    monkeypatch.setattr(app, "_fetch_repo_structure", AsyncMock(return_value=STRUCTURE))
    async def run(*args, **kwargs):
        if False:
            yield ""
    monkeypatch.setattr(app, "run_agent", run)
    report = AsyncMock()
    monkeypatch.setattr(app, "_report_to_governance", report)
    result = [part async for part in app.chat(URL, [])]
    assert report.await_args.args[1:3] == (URL, "failed")
    if not profile.report_only:
        assert result[-1][2] == ""


@pytest.mark.asyncio
async def test_interleaved_chat_state(agent_modules, profile, monkeypatch):
    app = agent_modules[1]
    monkeypatch.setattr(app, "_fetch_repo_structure", AsyncMock(return_value=STRUCTURE))
    monkeypatch.setattr(app, "_report_to_governance", AsyncMock())
    async def run(repo, **kwargs):
        name = repo.rsplit("/", 1)[-1]
        yield profile.stream.replace("Demo", name).replace("demo", name).replace("README.md", f"{name}/README.md")
    monkeypatch.setattr(app, "run_agent", run)
    streams = [app.chat(f"https://github.com/test/{name}", []) for name in ("alpha", "beta")]
    try:
        for stream in streams:
            await anext(stream)
        for stream, name in zip(streams, ("alpha", "beta")):
            results = [copy.deepcopy(part) async for part in stream]
            final = results[-1]
            history = final if profile.report_only else final[0]
            expected_stream = profile.stream.replace("Demo", name).replace("demo", name).replace("README.md", f"{name}/README.md")
            assert history[-1]["content"] == expected_stream
            if not profile.report_only:
                assert final[1] == f"https://github.com/test/{name}"
                assert final[2] == profile.output.replace("Demo", name).replace("demo", name)
    finally:
        for stream in streams:
            await stream.aclose()
