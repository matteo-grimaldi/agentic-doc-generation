import copy
import json
from unittest.mock import AsyncMock

import pytest

URL = "https://github.com/test/demo"
DOC = "# Architecture: demo\nDetails."


@pytest.mark.parametrize("prompt,expected", [
    ("", None), ("hello", None), ("https://gitlab.com/a/b", None),
    (URL, URL), (f"Generate ({URL}).", URL),
    (f"{URL} https://github.com/other/repo", URL),
])
def test_extract_repo_url(agent_modules, prompt, expected):
    assert agent_modules[1]._extract_repo_url(prompt) == expected


@pytest.mark.asyncio
@pytest.mark.parametrize("message", ["", " ", "\n\t"])
async def test_empty_chat(agent_modules, monkeypatch, message):
    app = agent_modules[1]
    run = AsyncMock()
    monkeypatch.setattr(app, "run_agent", run)
    history = [{"role": "user", "content": "old"}]
    result = [part async for part in app.chat(message, history)]
    assert len(result) == 1 and result[0][1:] == ("", "")
    assert "paste a GitHub" in result[0][0][-1]["content"]
    assert history == [{"role": "user", "content": "old"}]
    run.assert_not_called()


@pytest.mark.asyncio
async def test_chat_stream_and_pr_state(agent_modules, monkeypatch):
    app = agent_modules[1]
    async def run(repo):
        assert repo == URL
        yield "thinking\n"
        yield DOC
    monkeypatch.setattr(app, "run_agent", run)
    proposal, report = AsyncMock(), AsyncMock()
    monkeypatch.setattr(app, "_propose_architecture_doc", proposal)
    monkeypatch.setattr(app, "_report_to_governance", report)
    history = [{"role": "assistant", "content": "previous"}]
    original = copy.deepcopy(history)
    result = [copy.deepcopy(part) async for part in app.chat(f" {URL} ", history)]
    assert all(part[1:] == ("", "") for part in result[:-1])
    assert result[-1][1:] == (URL, DOC)
    assert result[-1][0][-1]["content"] == "thinking\n" + DOC
    assert history == original
    proposal.assert_not_awaited()
    report.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("repo,content", [("", DOC), (URL, ""), (URL, " \n")])
async def test_button_empty_state(agent_modules, monkeypatch, repo, content):
    app = agent_modules[1]
    proposal = AsyncMock()
    monkeypatch.setattr(app, "_propose_architecture_doc", proposal)
    assert "Generate an architecture doc" in await app.propose_pr_clicked(repo, content)
    proposal.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("result,fragment", [
    ({"error": "denied"}, "denied"),
    ({"pr_number": 4, "pr_url": "https://example.invalid/pr/4", "head_branch": "new", "base_branch": "main"}, "PR #4"),
])
async def test_button_result(agent_modules, monkeypatch, result, fragment):
    app = agent_modules[1]
    proposal = AsyncMock(return_value=result)
    monkeypatch.setattr(app, "_propose_architecture_doc", proposal)
    assert fragment in await app.propose_pr_clicked(URL, DOC)
    assert proposal.await_args.args[:2] == (URL, DOC)


@pytest.mark.asyncio
@pytest.mark.parametrize("branch", ["", "release"])
async def test_proposal_payload(agent_modules, monkeypatch, http_mock, branch):
    import httpx
    app = agent_modules[1]
    monkeypatch.setattr(app, "ARCHITECTURE_DOC_PATH", "docs/architecture.md")
    monkeypatch.setattr(app, "ARCHITECTURE_DOC_BASE_BRANCH", branch)
    requests = []
    def handler(request):
        assert request.method == "POST" and request.url.path == "/propose-doc"
        requests.append(json.loads(request.content))
        return httpx.Response(200, json={"pr_number": 1, "pr_url": "https://example.invalid/pr/1"})
    http_mock(handler)
    assert (await app._propose_architecture_doc(URL, DOC, "job"))["pr_number"] == 1
    payload = requests[0]
    assert payload["repo_url"] == URL and payload["content"] == DOC
    assert payload["path"] == "docs/architecture.md"
    assert "job" in payload["commit_message"]
    assert payload.get("base_branch", "") == branch
    assert ("base_branch" in payload) == bool(branch)


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["http", "error-json", "non-json", "network"])
async def test_proposal_errors(agent_modules, http_mock, kind):
    import httpx
    def handler(request):
        assert request.url.path == "/propose-doc"
        if kind == "network":
            raise httpx.ConnectError("offline", request=request)
        if kind == "non-json":
            return httpx.Response(502, text="bad gateway")
        return httpx.Response(500 if kind == "http" else 200, json={"error": "denied"})
    http_mock(handler)
    result = await agent_modules[1]._propose_architecture_doc(URL, DOC, "job")
    assert "error" in result and result["error"]


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["success", "network", "disabled"])
async def test_governance_optional_and_payload(agent_modules, monkeypatch, http_mock, kind):
    import httpx
    app = agent_modules[1]
    monkeypatch.setattr(app, "GOVERNANCE_HUB_URL", "" if kind == "disabled" else "https://hub.invalid")
    requests = []
    def handler(request):
        assert request.method == "POST" and request.url.path == "/api/runs"
        requests.append(json.loads(request.content))
        if kind == "network":
            raise httpx.ConnectError("offline", request=request)
        return httpx.Response(200, json={})
    http_mock(handler)
    await app._report_to_governance("job", URL, "completed", pr_url="https://example.invalid/pr/1", pr_number=1)
    if kind == "disabled":
        assert requests == []
    else:
        assert requests == [{"job_id": "job", "repo_url": URL, "agent_type": "doc-gen", "status": "completed", "pr_url": "https://example.invalid/pr/1", "pr_number": 1, "error": None}]


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["success", "empty", "error-only", "missing-url", "proposal-failed", "stream-exception"])
async def test_background_outcomes(agent_modules, monkeypatch, case):
    app = agent_modules[1]
    async def run(prompt):
        if case == "stream-exception":
            raise RuntimeError("stream failed")
        yield "" if case == "empty" else "\n\n⚠️ **Error:** failed" if case == "error-only" else "thinking\n" + DOC
    monkeypatch.setattr(app, "run_agent", run)
    proposal = AsyncMock(return_value={"error": "denied"} if case == "proposal-failed" else {"pr_url": "https://example.invalid/pr/1", "pr_number": 1})
    report = AsyncMock()
    monkeypatch.setattr(app, "_propose_architecture_doc", proposal)
    monkeypatch.setattr(app, "_report_to_governance", report)
    await app._run_agent_in_background("no URL" if case == "missing-url" else URL, "job")
    if case == "missing-url":
        proposal.assert_not_awaited()
        report.assert_not_awaited()
    elif case in ("empty", "error-only", "stream-exception"):
        proposal.assert_not_awaited()
        assert report.await_args.args[:3] == ("job", URL, "failed")
    else:
        proposal.assert_awaited_once_with(URL, DOC, "job")
        assert report.await_args.args[:3] == ("job", URL, "failed" if case == "proposal-failed" else "completed")


@pytest.mark.asyncio
async def test_http_routes(agent_modules, monkeypatch):
    import httpx
    app = agent_modules[1]
    monkeypatch.setattr(app, "build_ui", lambda: object())
    monkeypatch.setattr(app.gr, "mount_gradio_app", lambda application, demo, **kw: application)
    worker = AsyncMock()
    monkeypatch.setattr(app, "_run_agent_in_background", worker)
    application = app.build_app()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=application), base_url="http://test") as client:
        response = await client.get("/healthz")
        assert response.status_code == 200 and response.json() == {"status": "ok"}
        for payload, status in (({}, 422), ({"prompt": None}, 422), ({"prompt": " "}, 400)):
            assert (await client.post("/invoke", json=payload)).status_code == status
        worker.assert_not_awaited()
        response = await client.post("/invoke", json={"prompt": f" {URL} "})
        assert response.status_code == 202
        data = response.json()
        assert data["status"] == "accepted"
        import uuid
        uuid.UUID(data["job_id"])
        worker.assert_awaited_once_with(URL, data["job_id"])


@pytest.mark.asyncio
async def test_interleaved_chats_keep_artifacts_separate(agent_modules, monkeypatch):
    app = agent_modules[1]
    async def run(repo):
        yield f"# Architecture: {repo.rsplit('/', 1)[-1]}\n"
        yield "details"
    monkeypatch.setattr(app, "run_agent", run)
    a = app.chat("https://github.com/test/alpha", [])
    b = app.chat("https://github.com/test/beta", [])
    try:
        assert (await anext(a))[1:] == ("", "")
        assert (await anext(b))[1:] == ("", "")
        a_results, b_results = [], []
        async for part in a:
            a_results.append(copy.deepcopy(part))
        async for part in b:
            b_results.append(copy.deepcopy(part))
        assert a_results[-1][1:] == ("https://github.com/test/alpha", "# Architecture: alpha\ndetails")
        assert b_results[-1][1:] == ("https://github.com/test/beta", "# Architecture: beta\ndetails")
    finally:
        await a.aclose()
        await b.aclose()


@pytest.mark.asyncio
async def test_partial_error_output_current_proposal_policy(agent_modules, monkeypatch):
    """Characterize current behavior; extraction is not a completeness gate."""
    app = agent_modules[1]
    async def run(prompt):
        yield DOC
        yield "\n\n⚠️ **Error:** backend disconnected"
    monkeypatch.setattr(app, "run_agent", run)
    proposal = AsyncMock(return_value={"pr_url": "https://example.invalid/pr/1"})
    monkeypatch.setattr(app, "_propose_architecture_doc", proposal)
    monkeypatch.setattr(app, "_report_to_governance", AsyncMock())
    await app._run_agent_in_background(URL, "job")
    proposal.assert_awaited_once_with(URL, DOC, "job")
