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
MAX_INFER_ITERS = int(os.getenv("MAX_INFER_ITERS", "10"))
MAX_READ_FILES = int(os.getenv("MAX_READ_FILES", "5"))
# The external endpoint enforces a fixed ~100s request timeout and this
# model has Qwen3-style "thinking" always on by default, which can burn
# the entire token budget on hidden reasoning before any visible content
# is produced. Disabling it at the vLLM level (OGX's own reasoning_effort
# param is a no-op for this adapter) is required for the model to ever
# emit visible text within the wall-clock budget — combined with a
# right-sized INFERENCE_MAX_TOKENS (see deployment.yaml) and a tight
# inference-iteration/tool-call budget (see _build_system_prompt below).
CHAT_TEMPLATE_KWARGS = {"enable_thinking": False}

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


# Path/filename substrings that identify Java HTTP-entry-point files by
# naming convention alone — no file content needs to be read to find
# these, which is the whole point: build the model's reading list in
# Python (zero model/tool-call budget) instead of letting it spend
# inference iterations scanning get_repo_structure's full file list
# (which can be hundreds of entries) to find them itself.
_JAVA_CONTROLLER_DIR_SIGNALS = ("controller/", "controllers/", "resource/", "resources/", "endpoint/", "endpoints/", "rest/", "web/")
_JAVA_CONTROLLER_NAME_SUFFIXES = ("controller.java", "resource.java", "endpoint.java", "restapi.java")
_JAVA_MODEL_DIR_SIGNALS = ("dto/", "dtos/", "model/", "models/", "entity/", "entities/", "domain/", "payload/", "vo/")
_JAVA_MODEL_NAME_SUFFIXES = ("dto.java", "entity.java", "request.java", "response.java", "requestdto.java", "responsedto.java")
_JAVA_BUILD_FILENAMES = ("pom.xml", "build.gradle", "build.gradle.kts")
# How many of each category to hand the model — kept small and fixed so
# the system prompt's tool-call budget (a handful of get_file_outline
# calls) is never exceeded regardless of repo size.
MAX_CLASSIFIED_CONTROLLERS = 4
MAX_CLASSIFIED_MODELS = 3


def _classify_api_files(file_paths: list[str]) -> dict:
    """
    Java-aware, zero-model-budget pass over the file list already
    returned by get_repo_structure: identifies the small set of files the
    agent actually needs to look at (controllers/resources and their
    DTOs/entities) by path/filename convention alone, so the model's own
    tool-call budget is spent outlining exactly those files instead of
    first discovering them.

    This assumes a Java project (Spring MVC/Spring Boot or JAX-RS style
    naming conventions) — the dominant case for this agent's budget. Repos
    that don't match (no controller/resource-named files found) fall back
    to the model doing its own (bounded) discovery per the system prompt.

    Returns:
        {
            "is_java": bool,
            "build_file": str | None,
            "controllers": [path, ...]  (at most MAX_CLASSIFIED_CONTROLLERS),
            "models": [path, ...]       (at most MAX_CLASSIFIED_MODELS),
        }
    """
    build_file = next(
        (p for p in file_paths if p.rsplit("/", 1)[-1] in _JAVA_BUILD_FILENAMES), None
    )
    is_java = build_file is not None or any(p.lower().endswith(".java") for p in file_paths)

    def _matches(path: str, dir_signals: tuple[str, ...], name_suffixes: tuple[str, ...]) -> bool:
        lowered = path.lower()
        if not lowered.endswith(".java"):
            return False
        if any(signal in lowered for signal in dir_signals):
            return True
        basename = lowered.rsplit("/", 1)[-1]
        return any(basename.endswith(suffix) for suffix in name_suffixes)

    controllers = [
        p for p in file_paths
        if _matches(p, _JAVA_CONTROLLER_DIR_SIGNALS, _JAVA_CONTROLLER_NAME_SUFFIXES)
    ][:MAX_CLASSIFIED_CONTROLLERS]
    models = [
        p for p in file_paths
        if _matches(p, _JAVA_MODEL_DIR_SIGNALS, _JAVA_MODEL_NAME_SUFFIXES)
    ][:MAX_CLASSIFIED_MODELS]

    return {
        "is_java": is_java,
        "build_file": build_file,
        "controllers": controllers,
        "models": models,
    }


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
    return f"""You are an OpenAPI-spec generator. The repo's default branch and a short list of
likely controller/DTO files (found by path convention, mostly assuming a Java project) are already
given to you in the user message below — do NOT call get_repo_info or get_repo_structure; that
work is already done.

If the user message says no candidate files were found, or after a quick look these files don't
expose an HTTP API (CLI, library, frontend-only, batch job, gRPC/GraphQL-only), output EXACTLY:
NOT_AN_API: <reason>

Tools (github-mcp-server) — use ONLY these two, on ONLY the files you were given:
- get_file_outline(repo_url, path, ref): CHEAP — imports + function/class signatures + annotations
  (Java: @RestController/@RequestMapping/@GetMapping/@PostMapping/@Path/@GET/@POST etc. are all
  captured). Always try this first for every candidate file.
- read_file(repo_url, path, ref): EXPENSIVE — full content. Only call this on a model/DTO file, and
  only if its outline didn't already show field names/types clearly. Never call it on a controller
  — outlines always show enough (method signatures + annotations) to derive paths.

STRICT BUDGET — {MAX_INFER_ITERS} total inference iterations, shared across tool calls AND your
final write. Process files ONE AT A TIME and keep a running mental note of the paths you've found
(method + route + params) as you go — do not wait until every file is scanned to start forming the
spec. Stop calling tools well before the budget runs out: write the spec from what you have rather
than risk having zero iterations left to produce it.

Output ONLY a fenced ```yaml block — no preamble, no narration. Required top-level keys: openapi,
info (title, version), paths, components.schemas (omit components if no models were found).

Spec rules:
- Only include paths/methods you found annotation/signature evidence for — never invent endpoints.
- Derive schema field types from real models/DTOs; use a minimal `type: object` if a field's type
  wasn't confirmed rather than guessing.
- Use the project's actual base path/prefix (e.g. a class-level @RequestMapping/@Path).
- Mark uncertain operations with `x-confidence: low`.
- Valid YAML: no tabs, consistent indent, quote strings containing `:`.
"""


def _build_user_message(repo_url: str, repo_structure: dict | None) -> str:
    """
    Build the user-turn message. When repo_structure is available (the
    caller's zero-model-budget get_repo_structure/\\/repo-structure call —
    see app.py's _fetch_repo_structure), embed a pre-classified reading
    list (see _classify_api_files) so the model never spends its own
    inference-iteration budget on get_repo_info or get_repo_structure, or
    on scanning a potentially hundreds-of-entries file list itself. That
    pre-work used to cost 2+ of the agent's 10 total iterations before any
    real exploration even started.
    """
    if not repo_structure or "error" in repo_structure:
        return (
            f"Generate an OpenAPI specification for this repository if it exposes an HTTP API: "
            f"{repo_url}\n\nNo pre-fetched file list is available — call get_repo_structure "
            f"yourself first, then proceed as instructed."
        )

    ref = repo_structure.get("ref") or ""
    files = repo_structure.get("files") or []
    file_paths = [f.get("path", "") for f in files if isinstance(f, dict) and f.get("path")]
    classified = _classify_api_files(file_paths)

    ref_suffix = f" (ref: {ref})" if ref else ""
    lines = [f"Generate an OpenAPI 3.0.x YAML spec for: {repo_url}{ref_suffix}"]

    if not classified["controllers"] and not classified["models"]:
        lines.append(
            "\nNo controller/DTO files were found by naming convention (this may not be a Java "
            "project, or it uses unconventional names). Call get_repo_structure yourself to look "
            "for route/handler files in another language, or output NOT_AN_API if nothing "
            "suggests an HTTP API."
        )
        return "\n".join(lines)

    if classified["is_java"] and classified["build_file"]:
        lines.append(f"\nJava project detected ({classified['build_file']}).")

    if classified["controllers"]:
        lines.append("\nLikely controller/resource files (get_file_outline each):")
        lines.extend(f"- {p}" for p in classified["controllers"])
    if classified["models"]:
        lines.append("\nLikely DTO/model files (outline first; read_file only if needed):")
        lines.extend(f"- {p}" for p in classified["models"])

    lines.append(
        f'\nUse these exact paths with repo_url "{repo_url}"'
        + (f' and ref "{ref}"' if ref else "")
        + " — do not call get_repo_info or get_repo_structure."
    )
    return "\n".join(lines)


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


async def run_agent(repo_url: str, repo_structure: dict | None = None) -> AsyncIterator[str]:
    """
    Stream the OpenAPI-specification agent's response for a given GitHub
    repository URL.

    The OGX server handles the full ReAct loop server-side: it calls the
    GitHub MCP tools, feeds results back to the model, and streams the
    final answer.

    Args:
        repo_url: The target GitHub repository.
        repo_structure: The dict the caller already fetched via the
            GitHub MCP server's zero-model-budget get_repo_structure /
            /repo-structure route (app.py's _fetch_repo_structure) —
            typically already on hand since callers use it for
            detect_needs_openapi() right before calling run_agent().
            Passing it here lets _build_user_message hand the model a
            pre-classified reading list instead of making it spend its
            own tool-call budget on get_repo_info/get_repo_structure.

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

    user_message = _build_user_message(repo_url, repo_structure)

    logger.info("Sending request to OGX at %s for repo %s", OGX_BASE_URL, repo_url)
    logger.info(
        "run_agent request — max_infer_iters=%s max_read_files=%s instructions_chars=%d user_message_chars=%d",
        MAX_INFER_ITERS, MAX_READ_FILES, len(instructions), len(user_message),
    )

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
                # max_infer_iters is a first-class field on responses.create()
                # (confirmed in ogx_client's ResponseCreateParamsBase) — pass
                # it directly, not through extra_body.
                max_infer_iters=MAX_INFER_ITERS,
                stream=True,
                # OGX's server-side forwarding code does
                # `request.model_extra.get("extra_body")`: it only forwards
                # params nested one level deeper under a literal key named
                # "extra_body" inside the unrecognized top-level fields —
                # NOT the top-level fields themselves. The SDK's own
                # extra_body= kwarg merges its dict as top-level JSON
                # siblings, so to actually reach the inner chat-completion
                # call we must nest our payload under that exact key here.
                extra_body={
                    "extra_body": {
                        # Disable Qwen3 "thinking" at the vLLM level — see
                        # CHAT_TEMPLATE_KWARGS's definition above. OGX's own
                        # reasoning_effort param does not reach the backend
                        # for this adapter.
                        "chat_template_kwargs": CHAT_TEMPLATE_KWARGS,
                    },
                },
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
                    if text_chunks == 1:
                        logger.info("First text delta received after %d tool_calls, %d events", tool_calls, event_count)
                    yield event.delta

                elif event_type == "response.output_item.added":
                    item = getattr(event, "item", None)
                    if item and getattr(item, "type", None) == "mcp_call":
                        tool_calls += 1
                        server = getattr(item, "server_label", "unknown")
                        tool = getattr(item, "name", "unknown")
                        logger.info("Tool call started: %s → %s (tool_calls=%d so far)", server, tool, tool_calls)

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
                    logger.info(
                        "Stream response.completed — status=%s, text_chunks=%d, text_bytes=%d, tool_calls=%d, events=%d",
                        status, text_chunks, text_bytes, tool_calls, event_count,
                    )
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
