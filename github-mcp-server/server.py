"""
GitHub MCP Server.

A standalone MCP server that exposes GitHub repository browsing as MCP
tools, so an agent can explore a target repo's structure and source files
without needing a local git clone or GitHub credentials of its own. Also
exposes a propose-via-PR write path so generated documentation can be
persisted back into the repo it was generated from, without ever writing
directly to a (possibly protected) existing branch.

Tools exposed:
    get_repo_info       -- repo metadata (default branch, description, language)
    get_repo_structure   -- recursive file/directory tree
    get_file_outline     -- regex-extracted imports/signatures for a file (cheap scan, no LLM call)
    read_file            -- contents of a single file (size-capped, truncated; doc files capped shorter)
    search_code          -- code search within the repo (requires GITHUB_TOKEN)

Also exposes POST /propose-doc, a plain REST route (not an MCP tool) that
creates a new branch off the target base branch, commits the given content
to it, and opens a pull request — for deterministic, non-LLM-mediated
callers, e.g. doc-gen-agent's /invoke background task, which already has
the full generated document in hand and shouldn't need to route it through
a model tool call just to persist it.

Transport: streamable-http at /mcp (same pattern as ticketing-mcp-server
and prometheus-mcp-server in the ocp-troubleshooter-demo reference repo).
"""

from __future__ import annotations

import base64
import logging
import os
import re
import uuid
from typing import Any

import httpx
from fastmcp import FastMCP
from starlette.requests import Request
from starlette.responses import JSONResponse

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s — %(message)s")
logger = logging.getLogger(__name__)

GITHUB_API_URL = os.getenv("GITHUB_API_URL", "https://api.github.com")
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN", "")
MCP_PORT = int(os.getenv("MCP_PORT", "8080"))

# Guardrails so a single agent run can't blow the token budget or hammer
# the GitHub API fetching a huge monorepo.
MAX_TREE_ENTRIES = int(os.getenv("MAX_TREE_ENTRIES", "800"))
# Lowered from 60000: at 60KB/file, up to MAX_READ_FILES (10) full reads could
# push accumulated prompt size high enough that the external model's
# time-to-first-token exceeds an upstream gateway's idle-connection timeout,
# which tears down the request before any response is returned (seen in
# production as an APIConnectionError / "server disconnected" after ~10
# read_file calls). Keeping individual files smaller bounds per-call growth.
MAX_FILE_BYTES = int(os.getenv("MAX_FILE_BYTES", "20000"))
# Documentation files are mostly prose (install steps, changelogs, API
# reference) that a budget model tends to parrot back rather than analyze.
# Cap them much shorter than code files — the purpose/overview section is
# always near the top — so a full read_file on a README doesn't consume
# the same context budget as a source file.
MAX_DOC_FILE_BYTES = int(os.getenv("MAX_DOC_FILE_BYTES", "8000"))
DOC_FILENAME_PREFIXES = ("readme", "contributing", "changelog", "history", "news")
DOC_EXTENSIONS = (".md", ".rst", ".txt", ".adoc")
# Cap for get_file_outline's extracted signatures/imports per file — an
# outline should always be far smaller than MAX_FILE_BYTES.
MAX_OUTLINE_BYTES = int(os.getenv("MAX_OUTLINE_BYTES", "4000"))
EXCLUDED_PATH_PREFIXES = (
    "node_modules/", "vendor/", ".git/", "dist/", "build/", "target/",
    ".venv/", "venv/", "__pycache__/",
)
EXCLUDED_EXTENSIONS = (
    ".png", ".jpg", ".jpeg", ".gif", ".ico", ".svg", ".woff", ".woff2",
    ".ttf", ".eot", ".pdf", ".zip", ".tar", ".gz", ".jar", ".class",
    ".so", ".dylib", ".dll", ".exe", ".lock",
    "package-lock.json", "yarn.lock", "pipfile.lock", "poetry.lock",
)


def _is_doc_file(path: str) -> bool:
    """True for README/CONTRIBUTING/CHANGELOG-style prose files, which get
    a much shorter truncation cap than code files in read_file."""
    basename = path.rsplit("/", 1)[-1].lower()
    if basename.startswith(DOC_FILENAME_PREFIXES):
        return True
    return basename.endswith(DOC_EXTENSIONS)

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


async def _get_repo_structure_data(
    owner: str, repo: str, ref: str = "", max_entries: int = MAX_TREE_ENTRIES
) -> dict[str, Any]:
    """
    Shared implementation behind both the get_repo_structure MCP tool and
    the plain REST /repo-structure route below — same filtering, same
    shape — so deterministic, non-LLM-mediated callers (e.g. doc-gen-agent
    checking whether a repo already has an OpenAPI spec before running its
    separate OpenAPI agent) get identical results to what the model sees,
    without going through an MCP round-trip or costing any model tokens.
    """
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
    return await _get_repo_structure_data(owner, repo, ref, max_entries)


def _extract_outline(path: str, text: str) -> str | None:
    """
    Best-effort structural summary of a source file: imports/package
    declarations plus top-level class/function/type signatures, extracted
    via lightweight regex (no LLM call). Returns None for file types with
    no known outline strategy, so callers fall back to raw content.

    This exists so the agent can "scan" a file's shape — what it declares,
    what it imports — at a small fraction of the token cost of reading it
    in full, and only spend a read_file call on files whose outline looks
    architecturally significant.
    """
    lower = path.lower()
    lines = text.splitlines()

    def _collect(patterns: list[str], max_lines: int = 200) -> list[str]:
        combined = re.compile("|".join(patterns))
        out = []
        for line in lines[:max_lines * 10]:  # cap how far we scan
            if combined.search(line):
                out.append(line.strip())
            if len(out) >= max_lines:
                break
        return out

    if lower.endswith(".py"):
        return "\n".join(_collect([
            r"^\s*(import|from)\s+\S",
            r"^\s*(async\s+def|def|class)\s+\w",
        ]))

    if lower.endswith((".java", ".kt")):
        return "\n".join(_collect([
            r"^\s*package\s+\S",
            r"^\s*import\s+\S",
            r"^\s*(public|private|protected)?\s*(static\s+)?(class|interface|enum|record)\s+\w",
            r"^\s*(public|private|protected)\s+[\w<>\[\],\s]+\s+\w+\s*\([^)]*\)\s*\{?\s*$",
        ]))

    if lower.endswith(".go"):
        return "\n".join(_collect([
            r"^\s*package\s+\w",
            r"^\s*import\s",
            r"^\s*\"",  # import block lines
            r"^\s*type\s+\w+\s+(struct|interface)",
            r"^\s*func\s+(\(\w+\s+\*?\w+\)\s+)?\w+\s*\(",
        ]))

    if lower.endswith((".js", ".jsx", ".ts", ".tsx")):
        return "\n".join(_collect([
            r"^\s*(import|export|const\s+\w+\s*=\s*require)\s",
            r"^\s*export\s+(default\s+)?(class|function|const|interface|type)\s+\w",
            r"^\s*(class|function)\s+\w",
        ]))

    if lower.endswith((".yaml", ".yml")):
        # Top-level keys only (no leading whitespace), plus Kubernetes kind/name hints.
        return "\n".join(_collect([
            r"^[A-Za-z_][\w-]*:",
            r"^\s*kind:\s*\S",
            r"^\s*name:\s*\S",
        ]))

    if path.rsplit("/", 1)[-1].lower().startswith("dockerfile") or lower.endswith(".dockerfile"):
        return "\n".join(_collect([
            r"^\s*(FROM|RUN|CMD|EXPOSE|ENTRYPOINT|COPY|ADD|ENV|ARG|WORKDIR|USER|VOLUME|LABEL)\s",
        ]))

    return None


@mcp.tool()
async def get_file_outline(repo_url: str, path: str, ref: str = "") -> dict[str, Any]:
    """
    Return a lightweight structural outline of a single file — imports/package
    declarations plus top-level class/function/type signatures — instead of
    its full content. Extracted via simple pattern matching, not an LLM call,
    so it costs almost nothing in tokens compared to read_file.

    Use this to SCAN candidate files (entry points, configs, manifests) before
    deciding which ones are architecturally significant enough to read in full
    with read_file. Prefer this over read_file whenever you just need to know
    what a file declares/imports, not its implementation details.

    Supported file types: Python, Java, Kotlin, Go, JS/TS, YAML, Dockerfile.
    For unsupported types (e.g. plain text, Markdown, JSON, shell scripts),
    this returns an "unsupported" note — use read_file directly for those.

    Args:
        repo_url: A GitHub URL or "owner/repo" shorthand.
        path: File path within the repo, as returned by get_repo_structure.
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
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            return {"error": f"'{path}' does not appear to be a text file (binary content)."}

        outline = _extract_outline(path, text)
        if outline is None:
            return {
                "path": path,
                "ref": ref,
                "outline": "",
                "note": (
                    "No outline strategy for this file type — call read_file directly if "
                    "this file's content is needed."
                ),
            }

        truncated = False
        if len(outline) > MAX_OUTLINE_BYTES:
            outline = outline[:MAX_OUTLINE_BYTES]
            truncated = True

        return {
            "path": path,
            "ref": ref,
            "truncated": truncated,
            "outline": outline or "(no imports or top-level declarations found)",
        }


@mcp.tool()
async def read_file(repo_url: str, path: str, ref: str = "") -> dict[str, Any]:
    """
    Read the contents of a single file from a GitHub repository.

    Files larger than ~60KB are truncated with a note, to protect the
    agent's context window budget. Documentation files (README,
    CONTRIBUTING, CHANGELOG, and other .md/.rst/.txt/.adoc files) are
    truncated much more aggressively (~8KB) since their overview/purpose
    section is always near the top and the rest (install steps, API
    reference, changelogs) rarely helps architecture analysis — prefer
    get_file_outline for code files when you only need their structure.

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
        byte_cap = MAX_DOC_FILE_BYTES if _is_doc_file(path) else MAX_FILE_BYTES
        truncated = False
        if len(raw) > byte_cap:
            raw = raw[:byte_cap]
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


async def _commit_file_to_branch(
    client: httpx.AsyncClient, owner: str, repo: str, path: str, content: str, message: str, branch: str,
) -> dict[str, Any]:
    """
    Create or update a single file on an *existing* branch, in one commit,
    via the Contents API.
    """
    # The Contents API requires the current file's sha to update it, and
    # rejects a sha on creation — look it up, tolerating a 404 (file
    # doesn't exist yet on this branch, i.e. this will be a new file).
    sha = None
    existing_resp = await _get(client, f"/repos/{owner}/{repo}/contents/{path}", params={"ref": branch})
    if existing_resp.status_code == 200:
        existing_data = existing_resp.json()
        if isinstance(existing_data, dict):
            sha = existing_data.get("sha")
    elif existing_resp.status_code != 404:
        return {"error": f"GitHub API returned {existing_resp.status_code} checking for existing file: {existing_resp.text[:300]}"}

    body: dict[str, Any] = {
        "message": message,
        "content": base64.b64encode(content.encode("utf-8")).decode("ascii"),
        "branch": branch,
    }
    if sha:
        body["sha"] = sha

    put_resp = await client.put(
        f"{GITHUB_API_URL}/repos/{owner}/{repo}/contents/{path}",
        headers=_headers(),
        json=body,
        timeout=30.0,
    )
    if put_resp.status_code not in (200, 201):
        return {"error": f"GitHub API returned {put_resp.status_code} committing file: {put_resp.text[:300]}"}

    data = put_resp.json()
    commit = data.get("commit", {})
    return {"commit_sha": commit.get("sha"), "commit_url": commit.get("html_url")}


async def _propose_doc_pr(
    owner: str, repo: str, path: str, content: str, commit_message: str,
    base_branch: str, pr_title: str, pr_body: str, branch_prefix: str = "agentic-docs",
) -> dict[str, Any]:
    """
    Create a new branch off base_branch, commit `content` to `path` on it,
    and open a pull request back into base_branch — never writing directly
    to base_branch itself, so this is safe to point at a protected
    default/feature branch. Requires GITHUB_TOKEN to be configured with
    write access — for public repos, the `public_repo` OAuth scope
    includes write access, not just read.

    branch_prefix distinguishes unrelated callers' branches/PRs from each
    other (e.g. doc-gen-agent's architecture-doc PRs use "agentic-docs",
    its independent OpenAPI-spec PRs use "agentic-openapi") — each call
    still gets its own uniquely-suffixed branch regardless.
    """
    if not GITHUB_TOKEN:
        return {"error": "Proposing a doc PR requires a GITHUB_TOKEN with write access to be configured on the MCP server."}

    async with httpx.AsyncClient() as client:
        if not base_branch:
            info_resp = await _get(client, f"/repos/{owner}/{repo}")
            if info_resp.status_code != 200:
                return {"error": f"GitHub API returned {info_resp.status_code} fetching repo info: {info_resp.text[:300]}"}
            base_branch = info_resp.json().get("default_branch", "main")

        # 1. Resolve base_branch's current HEAD commit sha, to branch from.
        ref_resp = await _get(client, f"/repos/{owner}/{repo}/git/ref/heads/{base_branch}")
        if ref_resp.status_code != 200:
            return {"error": f"GitHub API returned {ref_resp.status_code} resolving base branch '{base_branch}': {ref_resp.text[:300]}"}
        base_sha = ref_resp.json().get("object", {}).get("sha")

        # 2. Create a new, uniquely-named branch from that commit.
        new_branch = f"{branch_prefix}/{uuid.uuid4().hex[:10]}"
        create_ref_resp = await client.post(
            f"{GITHUB_API_URL}/repos/{owner}/{repo}/git/refs",
            headers=_headers(),
            json={"ref": f"refs/heads/{new_branch}", "sha": base_sha},
            timeout=30.0,
        )
        if create_ref_resp.status_code != 201:
            return {"error": f"GitHub API returned {create_ref_resp.status_code} creating branch '{new_branch}': {create_ref_resp.text[:300]}"}

        # 3. Commit the document to the new branch (never to base_branch).
        commit_result = await _commit_file_to_branch(client, owner, repo, path, content, commit_message, new_branch)
        if "error" in commit_result:
            return commit_result

        # 4. Open a pull request from the new branch back into base_branch.
        pr_resp = await client.post(
            f"{GITHUB_API_URL}/repos/{owner}/{repo}/pulls",
            headers=_headers(),
            json={"title": pr_title, "head": new_branch, "base": base_branch, "body": pr_body},
            timeout=30.0,
        )
        if pr_resp.status_code != 201:
            return {"error": f"GitHub API returned {pr_resp.status_code} opening PR: {pr_resp.text[:300]}"}
        pr_data = pr_resp.json()

        return {
            "owner": owner,
            "repo": repo,
            "path": path,
            "base_branch": base_branch,
            "head_branch": new_branch,
            "commit_sha": commit_result.get("commit_sha"),
            "pr_number": pr_data.get("number"),
            "pr_url": pr_data.get("html_url"),
        }


@mcp.custom_route("/repo-structure", methods=["GET"])
async def repo_structure_route(request: Request) -> JSONResponse:
    """
    Plain, read-only REST wrapper around _get_repo_structure_data, for
    deterministic callers that need the file tree without spending any
    model/tool-call budget — notably doc-gen-agent's OpenAPI-generation
    gate (detect_needs_openapi), which must stay free so it never eats
    into either agent's own token/iteration budget.

    Query params: repo_url (required), ref (optional, defaults to the
    repo's default branch), max_entries (optional).
    """
    repo_url = request.query_params.get("repo_url", "")
    ref = request.query_params.get("ref", "")
    max_entries_param = request.query_params.get("max_entries", "")

    if not repo_url:
        return JSONResponse({"error": "repo_url query param is required"}, status_code=400)

    try:
        owner, repo = _parse_repo_url(repo_url)
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)

    max_entries = MAX_TREE_ENTRIES
    if max_entries_param:
        try:
            max_entries = int(max_entries_param)
        except ValueError:
            return JSONResponse({"error": "max_entries must be an integer"}, status_code=400)

    result = await _get_repo_structure_data(owner, repo, ref, max_entries)
    return JSONResponse(result, status_code=200 if "error" not in result else 502)


# Deliberately NOT an @mcp.tool(): doc-gen-agent's OGX agent loop (used by
# the interactive Gradio chat UI) is handed every tool on this server via a
# single generic "type": "mcp" tool definition with require_approval:
# "never" (see doc-gen-agent/agent.py). Exposing a write capability there
# would let the model trigger unintended branches/PRs during ordinary
# read-only chat sessions. Instead, this is reachable only via the plain
# REST route below, called deterministically by doc-gen-agent's /invoke
# background task — never by the model itself.
@mcp.custom_route("/propose-doc", methods=["POST"])
async def propose_doc_route(request: Request) -> JSONResponse:
    """
    Plain REST endpoint wrapping _propose_doc_pr, for deterministic
    callers that already hold the exact document to write (e.g.
    doc-gen-agent's /invoke background task) and don't need/want an MCP
    round-trip or an LLM tool call to relay a potentially large document.

    Body: {"repo_url": ..., "path": ..., "content": ..., "commit_message":
    ..., "base_branch": ... (optional, defaults to the repo's default
    branch), "pr_title": ... (optional), "pr_body": ... (optional),
    "branch_prefix": ... (optional, default "agentic-docs" — pass a
    distinct value, e.g. "agentic-openapi", so unrelated proposals from
    the same caller don't share a branch-name prefix)}
    """
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid JSON body"}, status_code=400)

    repo_url = body.get("repo_url", "")
    path = body.get("path", "")
    content = body.get("content", "")
    commit_message = body.get("commit_message") or f"docs: update {path}"
    base_branch = body.get("base_branch", "")
    pr_title = body.get("pr_title") or f"docs: update {path}"
    pr_body = body.get("pr_body") or (
        "Auto-generated by the agentic-doc-generation doc-gen-agent in response to a CI/CD trigger. "
        "Please review before merging."
    )
    branch_prefix = body.get("branch_prefix") or "agentic-docs"

    if not repo_url or not path or not content:
        return JSONResponse({"error": "repo_url, path, and content are required"}, status_code=400)

    try:
        owner, repo = _parse_repo_url(repo_url)
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)

    result = await _propose_doc_pr(
        owner, repo, path, content, commit_message, base_branch, pr_title, pr_body, branch_prefix
    )
    return JSONResponse(result, status_code=200 if "error" not in result else 502)


if __name__ == "__main__":
    logger.info("Starting GitHub MCP server on port %d (GITHUB_TOKEN configured: %s)", MCP_PORT, bool(GITHUB_TOKEN))
    mcp.run(transport="streamable-http", host="0.0.0.0", port=MCP_PORT, path="/mcp")
