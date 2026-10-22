"""
Compliance MCP Server.

A standalone, read-only MCP server that lets other AI agents -- not just
doc-gen-agent / openapi-gen-agent -- query the Governance Hub's compliance
state ("does repo X have an architecture doc / OpenAPI spec?") and act on
the answer, without scraping its dashboard or touching its database.

This server holds no state of its own: every tool below is a thin
read-only proxy over the Governance Hub's existing REST API
(GET /api/repos, GET /api/repos/{owner}/{repo}/status) -- the same API the
dashboard's own Jinja2 UI and external scripting already use. There is no
shared database/PVC between this process and governance-hub; if
governance-hub is unreachable, each tool below returns an "error" key
rather than raising or crashing.

Tools exposed:
    get_compliance_status  -- is this specific repo compliant? what's missing?
    list_violations         -- every tracked repo with at least one missing artifact
    list_tracked_repos      -- every repo the Hub knows about, with its current status
    get_repo_history        -- full run history (jobs, outcomes, PR links) for a repo

Transport: streamable-http at /mcp (same pattern as github-mcp-server).
"""
from __future__ import annotations

import logging
import os
import re
from typing import Any

import httpx
from fastmcp import FastMCP

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s — %(message)s")
logger = logging.getLogger(__name__)

GOVERNANCE_HUB_URL = os.getenv(
    "GOVERNANCE_HUB_URL", "http://governance-hub.doc-gen-demo.svc.cluster.local:7860"
)
MCP_PORT = int(os.getenv("MCP_PORT", "8080"))

_REPO_URL_RE = re.compile(r"github\.com[/:]([^/]+)/([^/]+?)(?:\.git)?/?$")

mcp = FastMCP("governance-compliance")


def parse_repo_url(repo_url: str) -> tuple[str, str]:
    """Extract (owner, repo) from a GitHub URL. Raises ValueError if it
    doesn't look like one -- callers should treat that as "not trackable"
    rather than guessing. Mirrors governance-hub/compliance.py's helper of
    the same name, kept in sync since both parse the same URL shape."""
    match = _REPO_URL_RE.search(repo_url.strip().rstrip("/"))
    if not match:
        raise ValueError(f"Could not parse owner/repo from '{repo_url}'")
    return match.group(1), match.group(2)


async def _get_repo_status(owner: str, repo: str) -> dict[str, Any]:
    """
    GET /api/repos/{owner}/{repo}/status on the Governance Hub.

    Returns {"not_found": True} if the Hub has never tracked this repo
    (404 -- not the same as "non-compliant"), or {"error": ...} on any
    other failure (network error, unexpected status), never raising.
    """
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(15.0)) as client:
            resp = await client.get(f"{GOVERNANCE_HUB_URL}/api/repos/{owner}/{repo}/status")
        if resp.status_code == 404:
            return {"not_found": True}
        if resp.status_code != 200:
            return {"error": f"Governance Hub returned {resp.status_code}: {resp.text[:300]}"}
        return resp.json()
    except Exception as exc:  # noqa: BLE001 - surfaced to caller as a status, never raised
        logger.warning("Error fetching repo status for %s/%s: %s", owner, repo, exc)
        return {"error": f"Could not reach Governance Hub: {exc}"}


async def _list_repos() -> dict[str, Any]:
    """
    GET /api/repos on the Governance Hub. Returns {"error": ...} on any
    failure rather than raising.
    """
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(15.0)) as client:
            resp = await client.get(f"{GOVERNANCE_HUB_URL}/api/repos")
        if resp.status_code != 200:
            return {"error": f"Governance Hub returned {resp.status_code}: {resp.text[:300]}"}
        return resp.json()
    except Exception as exc:  # noqa: BLE001 - surfaced to caller as a status, never raised
        logger.warning("Error listing repos: %s", exc)
        return {"error": f"Could not reach Governance Hub: {exc}"}


@mcp.tool()
async def get_compliance_status(repo_url: str) -> dict:
    """
    Report whether a specific repository is compliant with this org's
    documentation governance policy: does it have an architecture
    document, and (if applicable) an OpenAPI specification.

    Call this before deciding whether a repo needs remediation. If the
    repo has never been tracked by the Governance Hub (no agent has run
    against it yet), `tracked` is false and all compliance fields are
    absent -- that is NOT the same as "non-compliant".

    Args:
        repo_url: A GitHub URL, e.g. "https://github.com/owner/repo".
    """
    try:
        owner, repo = parse_repo_url(repo_url)
    except ValueError as exc:
        return {"error": str(exc)}

    status = await _get_repo_status(owner, repo)
    if "error" in status:
        return status
    if status.get("not_found"):
        return {"repo_url": repo_url, "tracked": False}

    status.pop("runs", None)
    status["tracked"] = True
    return status


@mcp.tool()
async def list_violations() -> dict:
    """
    List every tracked repository that currently has at least one
    compliance violation (missing architecture document, or -- for repos
    openapi-gen-agent has confirmed need one -- a missing OpenAPI spec).

    Use this to get a governance team's current worklist without having to
    call get_compliance_status for every repo individually.
    """
    result = await _list_repos()
    if "error" in result:
        return result

    repos = result.get("repos", [])
    violators = [r for r in repos if r.get("violations")]
    return {"total": len(violators), "repos": violators}


@mcp.tool()
async def list_tracked_repos() -> dict:
    """
    List every repository currently tracked by the Governance Hub, with
    its current compliance status. A repo appears here once an agent
    (doc-gen-agent or openapi-gen-agent) has run against it at least once.
    """
    return await _list_repos()


@mcp.tool()
async def get_repo_history(repo_url: str) -> dict:
    """
    Return the full history of doc-gen-agent / openapi-gen-agent runs
    recorded for a given repository, most recent first, including any
    resulting pull request URLs.

    Args:
        repo_url: A GitHub URL, e.g. "https://github.com/owner/repo".
    """
    try:
        owner, repo = parse_repo_url(repo_url)
    except ValueError as exc:
        return {"error": str(exc)}

    status = await _get_repo_status(owner, repo)
    if "error" in status:
        return status
    if status.get("not_found"):
        return {"repo_url": repo_url, "tracked": False, "runs": []}

    return {"repo_url": repo_url, "tracked": True, "runs": status.get("runs", [])}


if __name__ == "__main__":
    logger.info(
        "Starting Compliance MCP server on port %d (Governance Hub: %s)", MCP_PORT, GOVERNANCE_HUB_URL
    )
    mcp.run(transport="streamable-http", host="0.0.0.0", port=MCP_PORT, path="/mcp")
