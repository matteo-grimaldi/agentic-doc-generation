"""
OpenAPI-Gen Agent — OGX edition.

Given a GitHub repository URL, decide whether it looks like an HTTP API
service that doesn't already ship an OpenAPI/Swagger spec, and if so,
explore its structure and source files via the GitHub MCP server and
produce an OpenAPI 3.0.x specification (YAML), synthesized by a model
served via a configurable external OpenAI-compatible endpoint (base URL +
API key set via openapi-gen-agent/k8s/cluster-config.yaml and the
external-model-credentials Secret — no in-cluster model serving or GPU
required).

This is the independent counterpart to doc-gen-agent: it was split out of
that component so OpenAPI spec generation can be deployed, scaled, and
iterated on separately from architecture-documentation generation. It
shares the same GitHub MCP server and (by default) the same external
model endpoint, but has its own pod, its own budget, and its own PR.

Uses the OGX Responses API (server-side agentic loop) instead of
LangChain/LangGraph, matching the pattern from doc-gen-agent / the
ocp-troubleshooter-demo. The OGX server runs as a sidecar container in
the same pod and handles:
 - Tool calls to the GitHub MCP server
 - Inference via the external OpenAI-compatible endpoint
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
from typing import AsyncIterator

import httpx
from ogx_client import AsyncOgxClient

logger = logging.getLogger(__name__)

OGX_BASE_URL = os.getenv("OGX_BASE_URL", "http://localhost:8321")
GITHUB_MCP_URL = os.getenv("GITHUB_MCP_URL", "http://github-mcp-server.doc-gen-demo.svc.cluster.local:8080/mcp")
MODEL_NAME = os.getenv("MODEL_NAME", "granite-3.1-8b-instruct")
KNOWLEDGE_FILE = os.getenv("KNOWLEDGE_FILE", "/etc/agent-knowledge/openapi-knowledge.md")

AGENT_TIMEOUT_SECONDS = int(os.getenv("AGENT_TIMEOUT_SECONDS", "600"))
MAX_INFER_ITERS = int(os.getenv("MAX_INFER_ITERS", "15"))
MAX_READ_FILES = int(os.getenv("MAX_READ_FILES", "10"))

# Filenames (basename, case-insensitive) that count as "this repo already
# has a spec" — checked against the file list get_repo_structure already
# returned, so detection costs zero extra tool/model calls.
OPENAPI_SPEC_FILENAMES = {
    "openapi.yaml", "openapi.yml", "openapi.json",
    "swagger.yaml", "swagger.yml", "swagger.json",
    "api-spec.yaml", "api-spec.yml", "api-spec.json",
}
# Path/filename substrings suggesting the repo exposes an HTTP API — used
# only when no spec file was found, to decide whether spending the
# agent's model budget is even worth it. Intentionally permissive: a
# false positive just costs one extra, bounded agent run that can bail
# out cheaply; a false negative silently skips a repo that could have
# used a spec. The agent's own system prompt (_build_system_prompt) makes
# the authoritative call and bails out with NOT_AN_API if it disagrees
# with this heuristic.
API_PATH_SIGNALS = (
    "routes/", "route/", "controllers/", "controller/", "handlers/", "handler/",
    "endpoints/", "endpoint/", "api/", "resources/", "resource/",
    "views.py", "urls.py", "server.py", "main.py", "app.py",
)
# Manifest/config filenames whose presence suggests an HTTP framework is in
# play — combined with API_PATH_SIGNALS below for a slightly more confident
# heuristic; neither signal alone is required to trigger the agent.
API_FRAMEWORK_MANIFESTS = (
    "requirements.txt", "pyproject.toml", "package.json", "pom.xml",
    "build.gradle", "build.gradle.kts", "go.mod",
)


def _has_existing_openapi_spec(file_paths: list[str]) -> bool:
    """Phase 1 of OpenAPI-need detection: pure filename match against the
    repo's file list — no model/tool call beyond the get_repo_structure
    call already made."""
    return any(path.rsplit("/", 1)[-1].lower() in OPENAPI_SPEC_FILENAMES for path in file_paths)


def _looks_like_api_service(file_paths: list[str]) -> bool:
    """Phase 2 of OpenAPI-need detection: a cheap heuristic over the same
    file list — looks for path/filename signals (routes/controllers/
    handlers/api dirs, common HTTP entry-point filenames) combined with
    the presence of a recognizable project manifest. Intentionally
    permissive: the agent's own system prompt makes the authoritative
    call and bails out cheaply (NOT_AN_API) if this heuristic
    over-triggers."""
    lowered = [p.lower() for p in file_paths]
    has_api_signal = any(signal in path for path in lowered for signal in API_PATH_SIGNALS)
    has_manifest = any(path.rsplit("/", 1)[-1] in API_FRAMEWORK_MANIFESTS for path in lowered)
    return has_api_signal and has_manifest


def detect_needs_openapi(repo_structure: dict) -> bool:
    """
    Decide whether run_agent() should run for this repo, using only the
    file list get_repo_structure already returned — adds zero extra
    tool/model calls.

    Returns True only when: no existing OpenAPI/Swagger spec file was
    found AND the file tree shows signals of an HTTP API service. This is
    a cheap pre-filter, not the final word — run_agent's system prompt
    instructs the model to re-confirm and bail out (NOT_AN_API) if it
    disagrees, so this heuristic being wrong never produces a bogus PR,
    only (at worst) one extra bounded agent run.

    Args:
        repo_structure: the dict returned by the GitHub MCP server's
            get_repo_structure tool (expects a "files" list of
            {"path": ..., "size": ...} entries).
    """
    files = repo_structure.get("files") or []
    file_paths = [f.get("path", "") for f in files if isinstance(f, dict) and f.get("path")]
    if not file_paths:
        return False
    if _has_existing_openapi_spec(file_paths):
        logger.info("detect_needs_openapi: existing OpenAPI/Swagger spec found — skipping")
        return False
    if not _looks_like_api_service(file_paths):
        logger.info("detect_needs_openapi: no API-service signals found — skipping")
        return False
    logger.info("detect_needs_openapi: API-service signals found, no spec present — will run agent")
    return True


_OPENAPI_YAML_BLOCK_RE = re.compile(r"```(?:ya?ml)?\s*\n(openapi\s*:\s*[\s\S]*?)\n```", re.IGNORECASE)
_NOT_AN_API_RE = re.compile(r"^\s*NOT_AN_API\s*:", re.IGNORECASE)


def _response_as_dict(obj: object | None) -> dict:
    """Best-effort normalize an OGX/Pydantic response object to a dict."""
    if obj is None:
        return {}
    if isinstance(obj, dict):
        return obj
    dump = getattr(obj, "model_dump", None)
    if callable(dump):
        try:
            return dump()
        except Exception:
            pass
    to_dict = getattr(obj, "dict", None)
    if callable(to_dict):
        try:
            return to_dict()
        except Exception:
            pass
    return {k: getattr(obj, k) for k in ("status", "error") if hasattr(obj, k)}


def _extract_ogx_error(payload: object | None) -> tuple[str, str] | None:
    """
    Return (error_code, error_message) from an OGX response or stream error event.
    Message is truncated for UI safety.
    """
    data = _response_as_dict(payload)
    error = data.get("error")
    if error is None:
        return None
    if not isinstance(error, dict):
        return ("server_error", str(error)[:500])
    code = str(error.get("code") or error.get("type") or "server_error")
    message = str(error.get("message") or error.get("detail") or "Unknown backend error")
    if len(message) > 500:
        message = message[:497] + "..."
    return code, message


def _format_ogx_failure_message(code: str, message: str, tool_calls: int) -> str:
    """User-visible explanation when OGX ends the run with an error."""
    hint = (
        "The external model endpoint closed the connection or rejected the request. "
        "This often happens when the conversation context grows too large after many "
        f"file reads ({tool_calls} tool steps so far). Try a smaller repository, or ask "
        "your cluster admin to verify the inference endpoint is healthy."
    )
    if code in ("server_error", "api_connection_error", "connection_error"):
        body = hint
    elif "disconnect" in message.lower() or "connection" in message.lower():
        body = hint
    else:
        body = message
    return f"\n\n⚠️ **Error:** Model backend failed (`{code}`). {body}\n"


def _build_system_prompt() -> str:
    return f"""You are an AI API-specification assistant. Given a GitHub repository that a cheap
file-tree heuristic flagged as a possible HTTP API service, confirm that and then produce a valid
OpenAPI 3.0.x specification (YAML) describing its endpoints. Your output is proposed as its own pull
request against the source repo.

FIRST, confirm this repo actually exposes an HTTP API worth specifying. The heuristic that triggered
you can be wrong. If, after get_repo_structure and a couple of get_file_outline calls, you determine
this project does NOT expose an HTTP API (e.g. it's a CLI tool, a library with no server, a
frontend-only app, a batch job, or a service that only exposes gRPC/GraphQL/a message-queue consumer
with no REST/HTTP surface), STOP immediately and output EXACTLY one line, nothing else:

NOT_AN_API: <one short sentence explaining why>

Do not produce a partial or speculative OpenAPI document in that case — a confident "this doesn't
apply" is strictly better than a guessed spec.

Tools available (github-mcp-server):
- get_repo_info(repo_url): repo metadata, including default_branch — call this FIRST
- get_repo_structure(repo_url, ref): recursive file tree
- get_file_outline(repo_url, path, ref): a CHEAP scan of one file's imports/signatures — use this
  FIRST on any route/controller/handler/router-setup file to confirm it actually declares HTTP routes
  before spending a read_file call on it
- read_file(repo_url, path, ref): full contents of one file
- search_code(repo_url, query): search for a pattern (e.g. a route decorator, a DTO class name);
  requires a GitHub token and may be unavailable

Token budget — be deliberate and follow this two-phase strategy:

PHASE 1 — Survey (cheap, do this before any read_file call):
  1. get_repo_info → note language, default branch.
  2. get_repo_structure (once) → locate route/controller/handler files, API router/app setup, and any
     request/response model definitions (DTOs, schemas, serializers, protobuf messages).
  3. get_file_outline on the files most likely to declare HTTP routes (router/app setup, controller
     files, blueprint/views files) to confirm real route signatures before reading them in full.

PHASE 2 — Deep read (expensive — at most {MAX_READ_FILES} read_file calls):
  4. read_file ONLY on files whose outline confirmed route definitions, or request/response models you
     need full field/type details for.
  5. Do not read_file a file you already scanned with get_file_outline unless it showed route
     definitions or data models that genuinely need the full implementation to specify accurately.
  6. Skip test files, generated code, lock files, and vendored dependencies.
  7. If a tool call fails twice in a row, stop retrying it and proceed with what you have.

PHASE 3 — Write:
  8. Once you've identified the real HTTP endpoints, produce the OpenAPI document. Do not keep reading
     files indefinitely — when you reach the read_file cap, stop and write the spec from what you
     already have.

Output format — respond with ONLY a single fenced ```yaml code block, nothing else before or after it
(no preamble, no "Here is the spec:" narration):

```yaml
openapi: 3.0.3
info:
  title: <repo name>
  description: <one sentence, from what you found>
  version: "1.0.0"
paths:
  /example/path:
    get:
      summary: <short description>
      operationId: <operationId>
      responses:
        "200":
          description: <description>
          content:
            application/json:
              schema:
                type: object
components:
  schemas:
    ExampleModel:
      type: object
      properties:
        id:
          type: string
```

Rules for the spec itself:
- Only include paths/methods you actually found evidence for in the code — never invent endpoints.
- Derive `components.schemas` from real request/response models (DTOs, Pydantic/dataclass models,
  serializers, protobuf messages) found in the source, not guesses.
- Use the project's actual base path/prefix if one is declared (e.g. an API router mounted under
  `/api/v1`).
- If you found evidence of an endpoint but are uncertain about its exact request/response shape, still
  include the path with a best-effort schema and add `x-confidence: low` next to that operation, rather
  than omitting it or inventing detail you have no evidence for.
- The YAML must be syntactically valid — no tabs, consistent indentation, and properly quoted strings
  containing `:` or other special characters.
"""


def _load_knowledge(path: str = KNOWLEDGE_FILE) -> str:
    """Load a knowledge base (style guide) from a mounted ConfigMap file."""
    try:
        with open(path) as f:
            content = f.read().strip()
        logger.info("Loaded knowledge base from %s (%d chars)", path, len(content))
        return content
    except FileNotFoundError:
        logger.warning("Knowledge file not found at %s — running without format guidance", path)
        return ""


async def run_agent(repo_url: str) -> AsyncIterator[str]:
    """
    Stream the OpenAPI-specification agent's response for a given GitHub
    repository URL.

    The OGX server handles the full ReAct loop server-side: it calls the
    GitHub MCP tools, feeds results back to the model, and streams the
    final answer.

    Yields:
        Text chunks from the agent — either a fenced ```yaml OpenAPI
        document, or a "NOT_AN_API: ..." bail-out line if the agent
        determines (after looking) that the repo doesn't expose an HTTP
        API after all.
    """
    client = AsyncOgxClient(
        base_url=OGX_BASE_URL,
        api_key="local",
        max_retries=5,
        timeout=httpx.Timeout(connect=30.0, read=AGENT_TIMEOUT_SECONDS, write=30.0, pool=30.0),
    )

    knowledge = _load_knowledge()
    system_prompt = _build_system_prompt()
    instructions = (
        f"{system_prompt}\n\n---\n## OpenAPI Style Guide\n\n{knowledge}"
        if knowledge
        else system_prompt
    )

    user_message = (
        f"Generate an OpenAPI specification for this repository if it exposes an HTTP API: {repo_url}"
    )

    logger.info("Sending request to OGX at %s for repo %s", OGX_BASE_URL, repo_url)

    try:
        stream = await asyncio.wait_for(
            client.responses.create(
                model=MODEL_NAME,
                input=user_message,
                instructions=instructions,
                tools=[
                    {
                        "type": "mcp",
                        "server_label": "GitHub MCP",
                        "server_url": GITHUB_MCP_URL,
                        "require_approval": "never",
                    },
                ],
                stream=True,
                extra_body={"max_infer_iters": MAX_INFER_ITERS},
            ),
            timeout=60,
        )
    except asyncio.TimeoutError:
        logger.error("Timed out waiting for OGX to start streaming")
        yield "\n\n⚠️ **Error:** Timed out connecting to the agent backend. Please try again.\n"
        return
    except Exception as exc:
        logger.exception("Failed to create OGX stream")
        yield f"\n\n⚠️ **Error:** Could not reach the agent backend: {exc}\n"
        return

    event_count = 0
    text_chunks = 0
    text_bytes = 0
    tool_calls = 0
    tool_failures = 0
    ogx_error: tuple[str, str] | None = None
    ogx_error_announced = False

    def _announce_ogx_error() -> str | None:
        nonlocal ogx_error_announced
        if ogx_error is None or ogx_error_announced:
            return None
        ogx_error_announced = True
        code, message = ogx_error
        logger.error("OGX response error — code=%s message=%s", code, message)
        return _format_ogx_failure_message(code, message, tool_calls)

    try:
        async with asyncio.timeout(AGENT_TIMEOUT_SECONDS):
            async for event in stream:
                event_count += 1
                event_type = getattr(event, "type", None)

                if event_type == "response.output_text.delta":
                    text_chunks += 1
                    text_bytes += len(event.delta)
                    yield event.delta

                elif event_type == "response.output_item.added":
                    item = getattr(event, "item", None)
                    if item and getattr(item, "type", None) == "mcp_call":
                        tool_calls += 1
                        server = getattr(item, "server_label", "unknown")
                        tool = getattr(item, "name", "unknown")
                        logger.info("Tool call started: %s → %s", server, tool)

                elif event_type == "response.output_item.done":
                    item = getattr(event, "item", None)
                    if item and getattr(item, "type", None) == "mcp_call":
                        server = getattr(item, "server_label", "unknown")
                        tool = getattr(item, "name", "unknown")
                        error = getattr(item, "error", None)
                        if error:
                            tool_failures += 1
                            logger.warning("Tool call failed: %s → %s: %s", server, tool, error)
                        else:
                            logger.info("Tool call completed: %s → %s", server, tool)

                elif event_type in ("response.failed", "error"):
                    parsed = _extract_ogx_error(event) or _extract_ogx_error(
                        getattr(event, "response", None)
                    )
                    if parsed:
                        ogx_error = parsed
                    msg = _announce_ogx_error()
                    if msg:
                        yield msg

                elif event_type == "response.completed":
                    resp = getattr(event, "response", None)
                    resp_data = _response_as_dict(resp)
                    status = resp_data.get("status") or getattr(resp, "status", "unknown")
                    logger.info("Stream response.completed — status=%s", status)
                    parsed = _extract_ogx_error(resp)
                    if parsed:
                        ogx_error = parsed
                    elif status in ("failed", "incomplete", "cancelled"):
                        ogx_error = (
                            str(status),
                            "The agent backend ended the run without completing the OpenAPI spec.",
                        )
                    msg = _announce_ogx_error()
                    if msg:
                        yield msg

    except TimeoutError:
        logger.error("Agent exceeded %ds timeout", AGENT_TIMEOUT_SECONDS)
        yield (
            f"\n\n⚠️ **Error:** The agent took longer than {AGENT_TIMEOUT_SECONDS}s and was stopped.\n"
        )
    except Exception as exc:
        logger.exception("Error during agent streaming")
        yield f"\n\n⚠️ **Error:** Agent encountered an error: {exc}\n"

    logger.info(
        "Stream finished — events=%d, text_chunks=%d, text_bytes=%d, tool_calls=%d, tool_failures=%d",
        event_count, text_chunks, text_bytes, tool_calls, tool_failures,
    )
    if text_chunks == 0:
        if ogx_error and not ogx_error_announced:
            code, message = ogx_error
            yield _format_ogx_failure_message(code, message, tool_calls)
        elif not ogx_error:
            logger.warning(
                "Stream produced no text output — the model may have ended on a tool call "
                "without generating a final answer"
            )
            yield (
                "\n\n⚠️ **Error:** The agent finished without producing a spec. "
                f"It ran {tool_calls} tool step(s) but the model never returned a final answer.\n"
            )


def extract_openapi_spec(streamed: str) -> str:
    """
    Extract a clean OpenAPI YAML document from a full run_agent() stream,
    or "" if the agent bailed out with NOT_AN_API, hit an error, or
    produced no recognizable spec — callers (app.py) treat "" as "do not
    open a PR for this repo."
    """
    text = streamed.strip()
    if not text:
        return ""
    if _NOT_AN_API_RE.match(text):
        logger.info("extract_openapi_spec: agent reported NOT_AN_API — skipping PR")
        return ""
    match = _OPENAPI_YAML_BLOCK_RE.search(text)
    if match:
        return match.group(1).strip() + "\n"
    # Fallback: the model occasionally replies with raw YAML, no fence.
    if re.match(r"^\s*openapi\s*:", text, re.IGNORECASE):
        return text.strip() + "\n"
    logger.warning("extract_openapi_spec: no recognizable OpenAPI YAML found in agent output")
    return ""
