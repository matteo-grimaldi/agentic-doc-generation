"""
Compliance-check logic for the Governance Hub.

A "compliance check" is a cheap, zero-LLM-cost scan of a repo's current
default-branch file tree -- via the GitHub MCP server's existing
GET /repo-structure REST route (see github-mcp-server/server.py, the same
route openapi-gen-agent's own detect_needs_openapi gate uses) -- for the
presence of the artifacts doc-gen-agent and openapi-gen-agent produce:
ARCHITECTURE.md and openapi.yaml (or common variants). It checks existence
only, not quality or recency -- matching the literal "does it have
documentation / an OpenAPI spec" scope of this demo's governance model.
"""
from __future__ import annotations

import logging
import os
import re
from datetime import datetime, timezone

import httpx
from sqlalchemy.orm import Session

from models import TrackedRepo

logger = logging.getLogger(__name__)

GITHUB_MCP_SERVER_URL = os.getenv(
    "GITHUB_MCP_SERVER_URL", "http://github-mcp-server.doc-gen-demo.svc.cluster.local:8080"
)

# Lowercased, path-only matches against get_repo_structure's flat file
# list -- covers both agents' default output paths plus common
# hand-authored variants, so a repo that already shipped its own doc/spec
# (never touched by either agent) is still correctly marked compliant.
ARCHITECTURE_DOC_CANDIDATES = {"architecture.md", "docs/architecture.md"}
OPENAPI_SPEC_CANDIDATES = {
    "openapi.yaml", "openapi.yml", "openapi.json",
    "swagger.yaml", "swagger.yml", "swagger.json",
}

_REPO_URL_RE = re.compile(r"github\.com[/:]([^/]+)/([^/]+?)(?:\.git)?/?$")


def parse_repo_url(repo_url: str) -> tuple[str, str]:
    """Extract (owner, repo) from a GitHub URL. Raises ValueError if it
    doesn't look like one -- callers should treat that as "not trackable"
    rather than guessing."""
    match = _REPO_URL_RE.search(repo_url.strip().rstrip("/"))
    if not match:
        raise ValueError(f"Could not parse owner/repo from '{repo_url}'")
    return match.group(1), match.group(2)


async def fetch_repo_structure(repo_url: str) -> dict:
    """
    Thin wrapper around the GitHub MCP server's GET /repo-structure route
    -- the same zero-model-budget call openapi-gen-agent's own gate uses.

    Returns {"error": ...} on any failure (bad response, network error,
    rate limit) rather than raising, so callers can decide how to degrade
    (run_compliance_check leaves the prior known status untouched).
    """
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(30.0)) as client:
            resp = await client.get(
                f"{GITHUB_MCP_SERVER_URL}/repo-structure", params={"repo_url": repo_url}
            )
        data = resp.json()
        if resp.status_code == 200 and "error" not in data:
            return data
        return {"error": data.get("error", resp.text[:300])}
    except Exception as exc:  # noqa: BLE001 - surfaced to caller as a status, never raised
        logger.warning("Error fetching repo structure for %s: %s", repo_url, exc)
        return {"error": str(exc)}


def _has_any(files: list[dict], candidates: set[str]) -> bool:
    for entry in files:
        if entry.get("path", "").lower() in candidates:
            return True
    return False


async def run_compliance_check(session: Session, tracked: TrackedRepo) -> TrackedRepo:
    """
    Fetch the repo's current default-branch file tree and update
    has_architecture_doc / has_openapi_spec / last_compliance_check on
    `tracked` in place, committing the change.

    On a GitHub MCP server failure, `last_compliance_check` is still
    bumped (so the dashboard shows a check was attempted) but the prior
    has_architecture_doc / has_openapi_spec values are left untouched --
    a transient GitHub API error should never silently flip a compliant
    repo to "missing".
    """
    structure = await fetch_repo_structure(tracked.repo_url)
    tracked.last_compliance_check = datetime.now(timezone.utc)

    if "error" in structure:
        logger.warning(
            "Compliance check for %s failed: %s -- leaving prior status unchanged",
            tracked.repo_url, structure["error"],
        )
        session.add(tracked)
        session.commit()
        session.refresh(tracked)
        return tracked

    files = structure.get("files", [])
    tracked.has_architecture_doc = _has_any(files, ARCHITECTURE_DOC_CANDIDATES)
    tracked.has_openapi_spec = _has_any(files, OPENAPI_SPEC_CANDIDATES)

    session.add(tracked)
    session.commit()
    session.refresh(tracked)
    return tracked
