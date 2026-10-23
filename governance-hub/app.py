"""
Governance Hub -- single-pane-of-glass compliance dashboard for
architectural governance teams.

Repos are discovered purely by agent activity: a repo first appears here
the moment doc-gen-agent, openapi-gen-agent, or compliance-scan-agent
reports a completed run against it (POST /api/runs), or the moment a
governance-team member triggers one from this dashboard
(POST /api/repos/{owner}/{repo}/remediate -- the "Fix it" button). There
is no background GitHub-org scan.

Compliance itself ("does this repo have an architecture doc / OpenAPI
spec") is checked on demand via the GitHub MCP server's zero-LLM-cost
GET /repo-structure route (see compliance.py) -- never by invoking either
agent's model.

This process serves two things on one port (GOVERNANCE_PORT, default
7860):
    - A server-rendered (Jinja2) dashboard UI at "/"
    - A plain REST API under "/api/*" for both the UI's own forms and any
      external scripting (CI/CD, other tooling)

Other AI agents that want to query this compliance state programmatically
should use the separate compliance-mcp-server component, which proxies
read-only MCP tool calls through this process's REST API above -- it does
NOT talk to this process's database directly.
"""
from __future__ import annotations

import logging
import os
import re
import uuid
from datetime import datetime, timezone

import httpx
from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

import compliance
from models import AgentRun, TrackedRepo, get_or_create_repo, get_session, init_db

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s — %(message)s")
logger = logging.getLogger(__name__)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# URLs of the three agents this Hub can trigger remediation against. Empty
# strings are tolerated (the remediate endpoint just returns an error for
# that agent_type) so the Hub still runs standalone for local iteration.
DOC_GEN_AGENT_URL = os.getenv("DOC_GEN_AGENT_URL", "http://doc-gen-agent.doc-gen-demo.svc.cluster.local:7860")
OPENAPI_GEN_AGENT_URL = os.getenv(
    "OPENAPI_GEN_AGENT_URL", "http://openapi-gen-agent.doc-gen-demo.svc.cluster.local:7860"
)
# compliance-scan-agent never opens a PR -- triggering it here just fires
# its POST /invoke the same way as the other two, but its "fix" is a
# report, not a PR (see report_run's report_text handling below).
COMPLIANCE_SCAN_AGENT_URL = os.getenv(
    "COMPLIANCE_SCAN_AGENT_URL", "http://compliance-scan-agent.doc-gen-demo.svc.cluster.local:7860"
)

AGENT_URLS = {
    "doc-gen": DOC_GEN_AGENT_URL,
    "openapi-gen": OPENAPI_GEN_AGENT_URL,
    "compliance-scan": COMPLIANCE_SCAN_AGENT_URL,
}

# A report "has violations" if its Markdown table contains at least one
# FAIL row -- a simple, format-matching check rather than re-parsing the
# whole table, since compliance-scan-agent's own system prompt is the
# source of truth for the table's exact shape (see
# compliance-scan-agent/agent.py's _build_system_prompt).
_REPORT_FAIL_ROW_RE = re.compile(r"\|\s*FAIL\s*\|")


def _report_has_violations(report_text: str | None) -> bool | None:
    """None if no report text was given (nothing to judge), else whether
    the report's table contains any FAIL row."""
    if not report_text:
        return None
    return bool(_REPORT_FAIL_ROW_RE.search(report_text))

templates = Jinja2Templates(directory=os.path.join(BASE_DIR, "templates"))


# ── Serialization helpers ───────────────────────────────────────────────────

def _repo_to_dict(tracked: TrackedRepo) -> dict:
    return {
        "owner": tracked.owner,
        "repo": tracked.repo,
        "repo_url": tracked.repo_url,
        "first_seen": tracked.first_seen.isoformat() if tracked.first_seen else None,
        "has_architecture_doc": tracked.has_architecture_doc,
        "has_openapi_spec": tracked.has_openapi_spec,
        "compliant": tracked.compliant,
        "violations": tracked.violations,
        "last_compliance_check": tracked.last_compliance_check.isoformat() if tracked.last_compliance_check else None,
        "last_doc_gen_run": tracked.last_doc_gen_run.isoformat() if tracked.last_doc_gen_run else None,
        "last_openapi_gen_run": tracked.last_openapi_gen_run.isoformat() if tracked.last_openapi_gen_run else None,
        "last_compliance_scan_run": (
            tracked.last_compliance_scan_run.isoformat() if tracked.last_compliance_scan_run else None
        ),
        "compliance_scan_has_violations": tracked.compliance_scan_has_violations,
        "doc_pr_url": tracked.doc_pr_url,
        "doc_pr_number": tracked.doc_pr_number,
        "doc_pr_status": tracked.doc_pr_status,
        "openapi_pr_url": tracked.openapi_pr_url,
        "openapi_pr_number": tracked.openapi_pr_number,
        "openapi_pr_status": tracked.openapi_pr_status,
    }


def _run_to_dict(run: AgentRun) -> dict:
    return {
        "job_id": run.job_id,
        "agent_type": run.agent_type,
        "status": run.status,
        "started_at": run.started_at.isoformat() if run.started_at else None,
        "completed_at": run.completed_at.isoformat() if run.completed_at else None,
        "pr_url": run.pr_url,
        "pr_number": run.pr_number,
        "error_message": run.error_message,
        "triggered_by": run.triggered_by,
        "report_text": run.report_text,
    }


def _lookup_repo_or_404(session, owner: str, repo: str) -> TrackedRepo:
    tracked = (
        session.query(TrackedRepo)
        .filter_by(owner=owner, repo=repo)
        .one_or_none()
    )
    if tracked is None:
        raise HTTPException(status_code=404, detail=f"{owner}/{repo} is not tracked by the Governance Hub")
    return tracked


async def _remediate(tracked_repo_id: int, repo_url: str, agent_type: str) -> dict:
    """
    Fire a remediation request: POST {agent_url}/invoke with the repo URL,
    exactly as a CI/CD pipeline would (see ci-cd-pipeline/), then record an
    "accepted" AgentRun row so the dashboard shows the run as in-flight
    until the agent reports completion via POST /api/runs.

    Returns {"job_id": ...} on success or {"error": ...} on failure (bad
    agent_type, agent unreachable, non-202 response) -- never raises, so
    callers (the JSON API and the HTML form handler) can both react.
    """
    agent_url = AGENT_URLS.get(agent_type)
    if not agent_url:
        return {"error": f"Unknown agent_type '{agent_type}' (expected 'doc-gen', 'openapi-gen', or 'compliance-scan')"}

    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(10.0)) as client:
            resp = await client.post(f"{agent_url}/invoke", json={"prompt": repo_url})
        if resp.status_code != 202:
            return {"error": f"{agent_type} agent returned {resp.status_code}: {resp.text[:300]}"}
        job_id = resp.json().get("job_id", str(uuid.uuid4()))
    except Exception as exc:  # noqa: BLE001 - surfaced to the caller, never raised
        logger.warning("Failed to trigger %s agent for %s: %s", agent_type, repo_url, exc)
        return {"error": f"Could not reach {agent_type} agent: {exc}"}

    session = get_session()
    try:
        session.add(
            AgentRun(
                job_id=job_id,
                repo_id=tracked_repo_id,
                agent_type=agent_type,
                status="accepted",
                triggered_by="governance-hub",
            )
        )
        session.commit()
    finally:
        session.close()

    logger.info("Triggered %s remediation for %s, job_id=%s", agent_type, repo_url, job_id)
    return {"job_id": job_id}


# ── FastAPI app ─────────────────────────────────────────────────────────
#
# init_db() runs eagerly below, at import time, rather than in an
# @app.on_event("startup") handler, simply to keep startup ordering
# obvious and match this file's style elsewhere -- no custom `lifespan=`
# is set here, so on_event handlers would work fine too if ever added.

app = FastAPI(title="Governance Hub")
app.mount("/static", StaticFiles(directory=os.path.join(BASE_DIR, "static")), name="static")

init_db()
logger.info(
    "Governance Hub started (doc-gen agent: %s, openapi-gen agent: %s, "
    "compliance-scan agent: %s, github-mcp: %s)",
    DOC_GEN_AGENT_URL, OPENAPI_GEN_AGENT_URL, COMPLIANCE_SCAN_AGENT_URL, compliance.GITHUB_MCP_SERVER_URL,
)


@app.get("/healthz")
async def healthz() -> dict:
    return {"status": "ok"}


# ── Dashboard UI (server-rendered Jinja2, no JS framework) ─────────────────

@app.get("/")
async def dashboard(request: Request):
    session = get_session()
    try:
        repos = session.query(TrackedRepo).order_by(TrackedRepo.first_seen.desc()).all()
        total = len(repos)
        compliant_count = sum(1 for r in repos if r.compliant)
        violation_count = sum(1 for r in repos if r.violations)
        return templates.TemplateResponse(
            request,
            "dashboard.html",
            {
                "repos": repos,
                "total": total,
                "compliant_count": compliant_count,
                "violation_count": violation_count,
                "compliant_pct": round(100 * compliant_count / total) if total else 0,
            },
        )
    finally:
        session.close()


@app.get("/repo/{owner}/{repo}")
async def repo_detail(request: Request, owner: str, repo: str):
    session = get_session()
    try:
        tracked = _lookup_repo_or_404(session, owner, repo)
        return templates.TemplateResponse(request, "repo_detail.html", {"repo": tracked})
    finally:
        session.close()


@app.post("/repo/{owner}/{repo}/check-form")
async def repo_check_form(owner: str, repo: str):
    session = get_session()
    try:
        tracked = _lookup_repo_or_404(session, owner, repo)
        await compliance.run_compliance_check(session, tracked)
    finally:
        session.close()
    return RedirectResponse(url=f"/repo/{owner}/{repo}", status_code=303)


@app.post("/repo/{owner}/{repo}/remediate-form")
async def repo_remediate_form(owner: str, repo: str, agent_type: str = Form(...)):
    session = get_session()
    try:
        tracked = _lookup_repo_or_404(session, owner, repo)
        await _remediate(tracked.id, tracked.repo_url, agent_type)
    finally:
        session.close()
    return RedirectResponse(url=f"/repo/{owner}/{repo}", status_code=303)


# ── REST API ─────────────────────────────────────────────────────────────

class RunReport(BaseModel):
    """Body for POST /api/runs -- what doc-gen-agent / openapi-gen-agent /
    compliance-scan-agent POST when a background /invoke run finishes
    (see each agent's _report_to_governance helper in app.py).
    `report` is only ever set by compliance-scan-agent (its full
    Markdown compliance report -- it never opens a PR, so this is the
    run's entire deliverable)."""

    job_id: str
    repo_url: str
    agent_type: str  # "doc-gen" | "openapi-gen" | "compliance-scan"
    status: str  # "completed" | "failed" | "skipped"
    pr_url: str | None = None
    pr_number: int | None = None
    report: str | None = None
    error: str | None = None


@app.post("/api/runs", status_code=200)
async def report_run(report: RunReport) -> dict:
    """
    Callback endpoint: an agent reports the outcome of a completed (or
    skipped/failed) background run here. Registers the repo as tracked if
    this is the first time it's been seen, updates (or inserts) the
    matching AgentRun row by job_id, updates TrackedRepo's PR-link/status
    fields for a successful run, and kicks off a fresh compliance check so
    the dashboard's architecture-doc / OpenAPI-spec badges reflect reality
    as soon as possible (note: a freshly opened PR does NOT flip a repo to
    compliant until it's merged into the default branch -- see
    TrackedRepo.compliant's docstring).
    """
    try:
        owner, repo = compliance.parse_repo_url(report.repo_url)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    if report.agent_type not in ("doc-gen", "openapi-gen", "compliance-scan"):
        raise HTTPException(
            status_code=400, detail="agent_type must be 'doc-gen', 'openapi-gen', or 'compliance-scan'"
        )

    session = get_session()
    try:
        tracked = get_or_create_repo(session, report.repo_url, owner, repo)

        run = session.query(AgentRun).filter_by(job_id=report.job_id, repo_id=tracked.id).one_or_none()
        if run is None:
            run = AgentRun(job_id=report.job_id, repo_id=tracked.id, agent_type=report.agent_type, status="accepted")
            session.add(run)

        run.status = report.status
        run.completed_at = datetime.now(timezone.utc)
        run.pr_url = report.pr_url
        run.pr_number = report.pr_number
        run.report_text = report.report
        run.error_message = report.error

        now = datetime.now(timezone.utc)
        if report.agent_type == "doc-gen":
            tracked.last_doc_gen_run = now
            if report.pr_url:
                tracked.doc_pr_url = report.pr_url
                tracked.doc_pr_number = report.pr_number
                tracked.doc_pr_status = "open"
        elif report.agent_type == "openapi-gen":
            tracked.last_openapi_gen_run = now
            if report.pr_url:
                tracked.openapi_pr_url = report.pr_url
                tracked.openapi_pr_number = report.pr_number
                tracked.openapi_pr_status = "open"
            elif report.status == "skipped":
                # A confirmed, deliberate "no spec needed" (not applicable
                # or already present) is itself a compliance signal distinct
                # from "never checked" -- see TrackedRepo.violations. The
                # compliance check below is still the source of truth for
                # whether a spec file actually already existed.
                pass
        else:  # "compliance-scan"
            tracked.last_compliance_scan_run = now
            if report.status == "completed":
                tracked.compliance_scan_has_violations = _report_has_violations(report.report)
            # "failed" leaves the prior known violations status
            # untouched -- a failed run (e.g. a transient model/tool
            # error) should never silently flip a clean repo to
            # "violating" or vice versa.

        session.add(tracked)
        session.commit()

        await compliance.run_compliance_check(session, tracked)
    finally:
        session.close()

    logger.info(
        "Recorded %s run %s for %s: status=%s pr=%s",
        report.agent_type, report.job_id, report.repo_url, report.status, report.pr_url,
    )
    return {"status": "recorded"}


@app.get("/api/repos")
async def api_list_repos() -> dict:
    session = get_session()
    try:
        repos = session.query(TrackedRepo).order_by(TrackedRepo.first_seen.desc()).all()
        return {"total": len(repos), "repos": [_repo_to_dict(r) for r in repos]}
    finally:
        session.close()


@app.get("/api/repos/{owner}/{repo}/status")
async def api_repo_status(owner: str, repo: str) -> dict:
    session = get_session()
    try:
        tracked = _lookup_repo_or_404(session, owner, repo)
        result = _repo_to_dict(tracked)
        result["runs"] = [_run_to_dict(r) for r in tracked.runs]
        return result
    finally:
        session.close()


@app.post("/api/repos/{owner}/{repo}/check")
async def api_repo_check(owner: str, repo: str) -> dict:
    """Trigger a fresh, on-demand compliance check (GitHub file-tree scan,
    zero model cost) for a tracked repo."""
    session = get_session()
    try:
        tracked = _lookup_repo_or_404(session, owner, repo)
        tracked = await compliance.run_compliance_check(session, tracked)
        return _repo_to_dict(tracked)
    finally:
        session.close()


class RemediateRequest(BaseModel):
    agent_type: str  # "doc-gen" | "openapi-gen" | "compliance-scan"


@app.post("/api/repos/{owner}/{repo}/remediate", status_code=202)
async def api_repo_remediate(owner: str, repo: str, body: RemediateRequest) -> dict:
    """
    "Fix it" -- trigger doc-gen-agent, openapi-gen-agent, or
    compliance-scan-agent's POST /invoke against this repo (for
    compliance-scan-agent this runs a scan and reports a report, not a
    PR). Never happens automatically; this is only ever called by an
    explicit governance-team action (the dashboard's Fix-it button, or a
    deliberate API call), matching the "never auto-remediate without
    human action" guardrail the rest of this project's agents also follow
    for proposing PRs.
    """
    session = get_session()
    try:
        tracked = _lookup_repo_or_404(session, owner, repo)
        repo_url = tracked.repo_url
        tracked_id = tracked.id
    finally:
        session.close()

    result = await _remediate(tracked_id, repo_url, body.agent_type)
    if "error" in result:
        raise HTTPException(status_code=502, detail=result["error"])
    return {"status": "accepted", **result}


if __name__ == "__main__":
    import uvicorn

    port = int(os.getenv("GOVERNANCE_PORT", "7860"))
    uvicorn.run(app, host="0.0.0.0", port=port)
