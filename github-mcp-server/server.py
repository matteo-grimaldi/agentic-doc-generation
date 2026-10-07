"""
GitHub MCP Server.

A standalone MCP server that exposes read-only GitHub repository browsing
as MCP tools, so an agent can explore a target repo's structure and source
files without needing a local git clone or GitHub credentials of its own.

Tools exposed:
    get_repo_info       -- repo metadata (default branch, description, language)
    get_repo_structure   -- recursive file/directory tree
    read_file            -- contents of a single file (size-capped, truncated)
    search_code          -- code search within the repo (requires GITHUB_TOKEN)

Transport: streamable-http at /mcp (same pattern as ticketing-mcp-server
and prometheus-mcp-server in the ocp-troubleshooter-demo reference repo).
"""

from __future__ import annotations

import base64
import logging
import os
import re
from typing import Any

import httpx
from fastmcp import FastMCP

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s — %(message)s")
logger = logging.getLogger(__name__)

GITHUB_API_URL = os.getenv("GITHUB_API_URL", "https://api.github.com")
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN", "")
MCP_PORT = int(os.getenv("MCP_PORT", "8080"))

# Guardrails so a single agent run can't blow the token budget or hammer
# the GitHub API fetching a huge monorepo.
MAX_TREE_ENTRIES = int(os.getenv("MAX_TREE_ENTRIES", "800"))
MAX_FILE_BYTES = int(os.getenv("MAX_FILE_BYTES", "60000"))
EXCLUDED_PATH_PREFIXES = (
    "node_modules/", "vendor/", ".git/", "dist/", "build/", "target/",
    ".venv/", "venv/", "__pycache__/",
)
EXCLUDED_EXTENSIONS = (
    ".png", ".jpg", ".jpeg", ".gif", ".ico", ".svg", ".woff", ".woff2",
    ".ttf", ".eot", ".pdf", ".zip", ".tar", ".gz", ".jar", ".class",
    ".so", ".dylib", ".dll", ".exe", ".lock",
)

mcp = FastMCP("github-mcp-server")


def _headers() -> dict[str, str]:
    headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
    if GITHUB_TOKEN:
        headers["Authorization"] = f"Bearer {GITHUB_TOKEN}"
    return headers


def _parse_repo_url(repo_url: str) -> tuple[str, str]:
    """Extract (owner, repo) from a GitHub URL or an 'owner/repo' shorthand."""
    repo_url = repo_url.strip().rstrip("/")
    repo_url = re.sub(r"\.git$", "", repo_url)
    match = re.search(r"github\.com[/:]([^/]+)/([^/]+)$", repo_url)
    if match:
        return match.group(1), match.group(2)
    if "/" in repo_url and "://" not in repo_url:
        owner, repo = repo_url.split("/", 1)
        return owner, repo
    raise ValueError(f"Could not parse owner/repo from '{repo_url}'. Use a GitHub URL or 'owner/repo'.")


async def _get(client: httpx.AsyncClient, path: str, params: dict[str, Any] | None = None) -> httpx.Response:
    resp = await client.get(f"{GITHUB_API_URL}{path}", headers=_headers(), params=params, timeout=30.0)
    return resp


@mcp.tool()
async def get_repo_info(repo_url: str) -> dict[str, Any]:
    """
    Fetch top-level metadata for a GitHub repository: default branch,
    description, primary language, star count, and topics.

    Call this FIRST for any new repo to discover its default branch,
    which is required by get_repo_structure and read_file.

    Args:
        repo_url: A GitHub URL (e.g. "https://github.com/owner/repo") or
                   "owner/repo" shorthand.
    """
    owner, repo = _parse_repo_url(repo_url)
    async with httpx.AsyncClient() as client:
        resp = await _get(client, f"/repos/{owner}/{repo}")
        if resp.status_code != 200:
            return {"error": f"GitHub API returned {resp.status_code}: {resp.text[:300]}"}
        data = resp.json()
        return {
            "owner": owner,
            "repo": repo,
            "default_branch": data.get("default_branch"),
            "description": data.get("description"),
            "language": data.get("language"),
            "stargazers_count": data.get("stargazers_count"),
            "topics": data.get("topics", []),
            "size_kb": data.get("size"),
        }


@mcp.tool()
async def get_repo_structure(repo_url: str, ref: str = "", max_entries: int = MAX_TREE_ENTRIES) -> dict[str, Any]:
    """
    Return the recursive file/directory tree of a GitHub repository.

    Generated/vendor directories (node_modules, vendor, dist, build,
    target, .venv, __pycache__, .git) and common binary file extensions
    are filtered out automatically to keep the result focused on source
    code relevant to architecture analysis.

    Args:
        repo_url: A GitHub URL or "owner/repo" shorthand.
        ref: Branch, tag, or commit SHA. Defaults to the repo's default branch
             (call get_repo_info first if unsure).
        max_entries: Safety cap on the number of entries returned.
    """
    owner, repo = _parse_repo_url(repo_url)
    async with httpx.AsyncClient() as client:
        if not ref:
            info_resp = await _get(client, f"/repos/{owner}/{repo}")
            if info_resp.status_code != 200:
                return {"error": f"GitHub API returned {info_resp.status_code}: {info_resp.text[:300]}"}
            ref = info_resp.json().get("default_branch", "main")

        resp = await _get(client, f"/repos/{owner}/{repo}/git/trees/{ref}", params={"recursive": "1"})
        if resp.status_code != 200:
            return {"error": f"GitHub API returned {resp.status_code}: {resp.text[:300]}"}

        data = resp.json()
        entries = []
        for item in data.get("tree", []):
            path = item.get("path", "")
            if item.get("type") != "blob":
                continue
            if any(path.startswith(p) or f"/{p}" in path for p in EXCLUDED_PATH_PREFIXES):
                continue
            if path.lower().endswith(EXCLUDED_EXTENSIONS):
                continue
            entries.append({"path": path, "size": item.get("size", 0)})
            if len(entries) >= max_entries:
                break

        return {
            "owner": owner,
            "repo": repo,
            "ref": ref,
            "truncated": data.get("truncated", False) or len(entries) >= max_entries,
            "file_count": len(entries),
            "files": entries,
        }


@mcp.tool()
async def read_file(repo_url: str, path: str, ref: str = "") -> dict[str, Any]:
    """
    Read the contents of a single file from a GitHub repository.

    Files larger than ~60KB are truncated with a note, to protect the
    agent's context window budget.

    Args:
        repo_url: A GitHub URL or "owner/repo" shorthand.
        path: File path within the repo, as returned by get_repo_structure
              (e.g. "src/main.py").
        ref: Branch, tag, or commit SHA. Defaults to the repo's default branch.
    """
    owner, repo = _parse_repo_url(repo_url)
    async with httpx.AsyncClient() as client:
        if not ref:
            info_resp = await _get(client, f"/repos/{owner}/{repo}")
            if info_resp.status_code != 200:
                return {"error": f"GitHub API returned {info_resp.status_code}: {info_resp.text[:300]}"}
            ref = info_resp.json().get("default_branch", "main")

        resp = await _get(client, f"/repos/{owner}/{repo}/contents/{path}", params={"ref": ref})
        if resp.status_code != 200:
            return {"error": f"GitHub API returned {resp.status_code}: {resp.text[:300]}"}

        data = resp.json()
        if isinstance(data, list):
            return {"error": f"'{path}' is a directory, not a file. Use get_repo_structure to list its contents."}
        if data.get("encoding") != "base64":
            return {"error": f"Unsupported encoding '{data.get('encoding')}' for '{path}'."}

        raw = base64.b64decode(data["content"])
        truncated = False
        if len(raw) > MAX_FILE_BYTES:
            raw = raw[:MAX_FILE_BYTES]
            truncated = True

        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            return {"error": f"'{path}' does not appear to be a text file (binary content)."}

        return {
            "path": path,
            "ref": ref,
            "size": data.get("size"),
            "truncated": truncated,
            "content": text,
        }


@mcp.tool()
async def search_code(repo_url: str, query: str) -> dict[str, Any]:
    """
    Search for a code pattern within a GitHub repository (e.g. to find
    where a specific framework, annotation, or config key is used).

    Requires a GITHUB_TOKEN to be configured on the server (GitHub's code
    search API only supports authenticated requests).

    Args:
        repo_url: A GitHub URL or "owner/repo" shorthand.
        query: Search terms, e.g. "FastAPI" or "kind: Deployment".
    """
    if not GITHUB_TOKEN:
        return {"error": "search_code requires a GITHUB_TOKEN to be configured on the MCP server."}

    owner, repo = _parse_repo_url(repo_url)
    full_query = f"{query} repo:{owner}/{repo}"
    async with httpx.AsyncClient() as client:
        resp = await _get(client, "/search/code", params={"q": full_query, "per_page": 20})
        if resp.status_code != 200:
            return {"error": f"GitHub API returned {resp.status_code}: {resp.text[:300]}"}
        data = resp.json()
        return {
            "total_count": data.get("total_count", 0),
            "results": [
                {"path": item.get("path"), "url": item.get("html_url")}
                for item in data.get("items", [])
            ],
        }


if __name__ == "__main__":
    logger.info("Starting GitHub MCP server on port %d (GITHUB_TOKEN configured: %s)", MCP_PORT, bool(GITHUB_TOKEN))
    mcp.run(transport="streamable-http", host="0.0.0.0", port=MCP_PORT, path="/mcp")
