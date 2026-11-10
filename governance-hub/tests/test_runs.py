import json
import pytest

URL = "https://github.com/test/demo"


def report(agent="catalog-gen", **kwargs):
    return {"job_id": "job", "repo_url": URL, "agent_type": agent, "status": "completed", **kwargs}


@pytest.mark.asyncio
@pytest.mark.parametrize("agent", ["doc-gen", "openapi-gen", "catalog-gen", "compliance-scan"])
async def test_all_agent_callbacks_and_idempotent_history(client, agent):
    payload = report(agent, triggered_by="ui", pr_url="https://example.invalid/pr/1", pr_number=1)
    assert (await client.post("/api/runs", json=payload)).status_code == 200
    assert (await client.post("/api/runs", json=payload)).status_code == 200
    status = (await client.get("/api/repos/test/demo/status")).json()
    assert len(status["runs"]) == 1
    assert status["runs"][0]["triggered_by"] == "ui"
    assert status["runs"][0]["agent_type"] == agent
    assert status["compliant"] is False  # A proposal is not a merged artifact.
    if agent == "catalog-gen":
        assert status["catalog_pr_url"] == payload["pr_url"]
        assert status["last_catalog_gen_run"]
        html = (await client.get("/repo/test/demo")).text
        assert 'value="catalog-gen"' in html and "example.invalid/pr/1" in html


@pytest.mark.asyncio
async def test_catalog_remediation_uses_public_invoke_and_preserves_origin(hub, client, monkeypatch):
    import httpx
    await client.post("/api/runs", json=report(job_id="initial"))
    original = httpx.AsyncClient
    calls = []
    def handler(request):
        calls.append(request)
        assert str(request.url) == hub.CATALOG_GEN_AGENT_URL + "/invoke"
        assert json.loads(request.content) == {"prompt": URL}
        return httpx.Response(202, json={"job_id": "catalog-job"})
    monkeypatch.setattr(hub.httpx, "AsyncClient", lambda **kw: original(transport=httpx.MockTransport(handler), **kw))
    response = await client.post("/api/repos/test/demo/remediate", json={"agent_type": "catalog-gen"})
    assert response.status_code == 202 and response.json()["job_id"] == "catalog-job"
    await client.post("/api/runs", json=report(job_id="catalog-job", triggered_by="invoke"))
    runs = (await client.get("/api/repos/test/demo/status")).json()["runs"]
    run = next(r for r in runs if r["job_id"] == "catalog-job")
    assert run["triggered_by"] == "governance-hub" and run["status"] == "completed"
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_fast_callback_before_invoke_returns_does_not_duplicate_history(hub, client, monkeypatch):
    import httpx
    await client.post("/api/runs", json=report(job_id="initial"))
    original = httpx.AsyncClient
    async def handler(request):
        await hub.report_run(hub.RunReport(**report(job_id="fast")))
        return httpx.Response(202, json={"job_id": "fast"})
    monkeypatch.setattr(hub.httpx, "AsyncClient", lambda **kw: original(transport=httpx.MockTransport(handler), **kw))
    assert (await client.post("/api/repos/test/demo/remediate", json={"agent_type": "catalog-gen"})).status_code == 202
    runs = (await client.get("/api/repos/test/demo/status")).json()["runs"]
    fast = [r for r in runs if r["job_id"] == "fast"]
    assert len(fast) == 1 and fast[0]["status"] == "completed"


@pytest.mark.asyncio
async def test_artifact_status_is_independent_of_violations_and_failed_refresh(hub, client, monkeypatch):
    fetch = hub.compliance.fetch_repo_structure
    fetch.return_value = {"files": [{"path": "ARCHITECTURE.md"}]}
    await client.post("/api/runs", json=report("compliance-scan", report="| Check | FAIL | HIGH | fix |"))
    status = (await client.get("/api/repos/test/demo/status")).json()
    assert status["compliant"] is True
    assert status["violations"] == ["openapi_spec", "compliance_scan"]
    fetch.return_value = {"error": "offline"}
    response = await client.post("/api/repos/test/demo/check")
    assert response.json()["has_architecture_doc"] is True
    assert response.json()["last_compliance_check"]


def test_catalog_column_migration_preserves_existing_rows(hub):
    import models
    with models.SessionLocal() as session:
        session.add(models.TrackedRepo(owner="test", repo="demo", repo_url=URL))
        session.commit()
    with models.engine.begin() as connection:
        for column in ("last_catalog_gen_run", "catalog_pr_url", "catalog_pr_number", "catalog_pr_status"):
            connection.exec_driver_sql(f"ALTER TABLE tracked_repos DROP COLUMN {column}")
    models.init_db()
    models.init_db()
    with models.SessionLocal() as session:
        tracked = session.query(models.TrackedRepo).one()
        assert tracked.repo_url == URL and tracked.catalog_pr_url is None


@pytest.mark.asyncio
@pytest.mark.parametrize("kwargs", [{"agent_type": "bad"}, {"status": "bad"}, {"triggered_by": "bad"}])
async def test_invalid_callbacks_do_not_register_repos(client, kwargs):
    assert (await client.post("/api/runs", json=report(**kwargs))).status_code == 400
    assert (await client.get("/api/repos")).json()["total"] == 0
