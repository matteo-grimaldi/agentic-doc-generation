import json
from unittest.mock import AsyncMock

import pytest

URL = "https://github.com/test/demo"


@pytest.mark.asyncio
@pytest.mark.parametrize("branch", ["", "release"])
async def test_proposal_payload(agent_modules, profile, monkeypatch, http_mock, branch):
    import httpx
    app = agent_modules[1]
    monkeypatch.setattr(app, profile.path_var, "docs/output.yaml")
    monkeypatch.setattr(app, profile.branch_var, branch)
    calls = []
    def handler(request):
        assert request.method == "POST" and request.url.path == "/propose-doc"
        calls.append(json.loads(request.content))
        return httpx.Response(200, json={"pr_number": 1})
    http_mock(handler)
    assert (await getattr(app, profile.proposal)(URL, profile.output, "job"))["pr_number"] == 1
    payload = calls[0]
    assert payload["repo_url"] == URL and payload["content"] == profile.output
    assert payload["path"] == "docs/output.yaml"
    assert payload["branch_prefix"] == profile.branch_prefix
    assert ("base_branch" in payload) == bool(branch)
    assert payload.get("base_branch", "") == branch


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["http", "error-json", "non-json", "network"])
async def test_proposal_errors(agent_modules, profile, http_mock, kind):
    import httpx
    def handler(request):
        assert request.method == "POST" and request.url.path == "/propose-doc"
        if kind == "network":
            raise httpx.ConnectError("offline", request=request)
        if kind == "non-json":
            return httpx.Response(502, text="bad gateway")
        return httpx.Response(500 if kind == "http" else 200, json={"error": "denied"})
    http_mock(handler)
    assert "error" in await getattr(agent_modules[1], profile.proposal)(URL, profile.output, "job")


@pytest.mark.asyncio
@pytest.mark.parametrize("repo,content", [("", "text"), (URL, ""), (URL, " \n")])
async def test_button_empty(agent_modules, profile, monkeypatch, repo, content):
    app = agent_modules[1]
    proposal = AsyncMock()
    monkeypatch.setattr(app, profile.proposal, proposal)
    assert "first" in await app.propose_pr_clicked(repo, content)
    proposal.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("failed", [False, True])
async def test_button_and_worker_proposal_result(agent_modules, profile, monkeypatch, failed):
    app = agent_modules[1]
    result = {"error": "denied"} if failed else {"pr_number": 4, "pr_url": "https://example.invalid/pr/4"}
    proposal = AsyncMock(return_value=result)
    report = AsyncMock()
    monkeypatch.setattr(app, profile.proposal, proposal)
    monkeypatch.setattr(app, "_report_to_governance", report)
    monkeypatch.setattr(app, "_fetch_repo_structure", AsyncMock(return_value={"files": [{"path": "pom.xml"}, {"path": "controllers/Demo.java"}]}))
    async def run(*args, **kwargs):
        yield profile.stream
    monkeypatch.setattr(app, "run_agent", run)
    assert ("denied" if failed else "PR #4") in await app.propose_pr_clicked(URL, profile.output)
    await app._run_agent_in_background(URL, "job")
    assert proposal.await_count == 2
    assert report.await_args.args[:3] == ("job", URL, "failed" if failed else "completed")
