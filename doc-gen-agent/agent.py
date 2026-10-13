"""
Doc-Gen Agent — OGX edition.

A simplified, single-agent take on opendatahub-io/architecture-context:
given a GitHub repository URL, explore its structure and source files via
the GitHub MCP server, then produce an architecture summary (Markdown)
plus Mermaid diagrams, synthesized by a model served via a configurable
external OpenAI-compatible endpoint (base URL + API key set via
doc-gen-agent/k8s/cluster-config.yaml and the external-model-credentials
Secret — no in-cluster model serving or GPU required).

Uses the OGX Responses API (server-side agentic loop) instead of
LangChain/LangGraph, matching the pattern from ocp-troubleshooter-demo.
The OGX server runs as a sidecar container in the same pod and handles:
 - Tool calls to the GitHub MCP server
 - Inference via the external OpenAI-compatible endpoint
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import AsyncIterator

import httpx
from ogx_client import AsyncOgxClient

logger = logging.getLogger(__name__)

OGX_BASE_URL = os.getenv("OGX_BASE_URL", "http://localhost:8321")
GITHUB_MCP_URL = os.getenv("GITHUB_MCP_URL", "http://github-mcp-server.doc-gen-demo.svc.cluster.local:8080/mcp")
MODEL_NAME = os.getenv("MODEL_NAME", "granite-3.1-8b-instruct")
KNOWLEDGE_FILE = os.getenv("KNOWLEDGE_FILE", "/etc/agent-knowledge/knowledge.md")

AGENT_TIMEOUT_SECONDS = int(os.getenv("AGENT_TIMEOUT_SECONDS", "600"))
MAX_INFER_ITERS = int(os.getenv("MAX_INFER_ITERS", "16"))
MAX_READ_FILES = int(os.getenv("MAX_READ_FILES", "12"))


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
    return f"""You are an AI architecture-documentation assistant. Given a GitHub repository,
explore its structure and source files, then produce a clear architecture summary with diagrams —
a simplified version of the opendatahub-io/architecture-context pipeline, run as a single agent turn.

Tools available (github-mcp-server):
- get_repo_info(repo_url): repo metadata, including default_branch — call this FIRST
- get_repo_structure(repo_url, ref): recursive file tree (vendor/build dirs and binaries are pre-filtered)
- read_file(repo_url, path, ref): read one file's contents (large files are truncated)
- search_code(repo_url, query): search for a pattern across the repo (requires a GitHub token; may be unavailable)

Token budget — be deliberate about what you read:
- Read at most {MAX_READ_FILES} files total (hard limit). When you reach this limit, stop calling read_file
  and write the final document from what you already have.
- Prioritize: README*, main entry points (main.*, app.*, index.*, cmd/),
  package manifests (pom.xml, package.json, go.mod, pyproject.toml, requirements.txt, Cargo.toml),
  Dockerfile*, docker-compose*.yaml, k8s/*.yaml or deploy/*.yaml, CI config (.github/workflows/*).
- Skip test files, generated code, and vendored dependencies unless nothing else explains the architecture.
- Do not call read_file on the same path twice. Do not re-fetch get_repo_structure more than once per repo.
- If a tool call fails twice in a row, stop retrying it and proceed with what you have.
- After exploration, you MUST produce the final Markdown document; do not keep reading files indefinitely.

Workflow:
1. Call get_repo_info to learn the default branch and language.
2. Call get_repo_structure once to map the project layout.
3. Read the most architecturally relevant files from the tree (up to the {MAX_READ_FILES}-file cap).
4. Synthesize findings into the structured document described below.
5. Produce the Mermaid diagrams described below, embedded directly in fenced ```mermaid code blocks.

Output format (produce ALL sections, in this order):

# Architecture: <repo name>

## Project Overview
One paragraph: purpose, problem it solves, primary language/framework.

## Technology Stack
Bullet list of languages, frameworks, key libraries, and runtime/platform (e.g. container base images).

## Component Architecture
For each major component/module found, a short bullet with its responsibility.

## Data Flow
Narrative description of how data/requests move through the system.

## External Dependencies and Integrations
APIs, databases, message queues, third-party services this project talks to.

## Deployment Architecture
How this project is packaged and deployed (container image, k8s manifests, CI/CD), based on what you found
in Dockerfiles, k8s/deploy manifests, and CI config.

## Security Considerations
Notable auth mechanisms, secrets handling, or RBAC found in the code (only report what you actually observed).

## Diagrams

### Component Diagram
```mermaid
flowchart TB
    %% nodes and edges reflecting the actual components you found
```

### Data Flow Diagram
```mermaid
sequenceDiagram
    %% key request/data flow you identified
```

### Dependency Diagram
```mermaid
flowchart LR
    %% this project's dependencies on external services/libraries
```

Mermaid syntax rules: no spaces in node IDs (use camelCase/PascalCase/underscores), quote any edge label or
node label containing parentheses/colons/commas, no explicit colors/styling, no `click` directives.
"""


def _load_knowledge() -> str:
    """Load the architecture-documentation knowledge base from the mounted ConfigMap file."""
    try:
        with open(KNOWLEDGE_FILE) as f:
            content = f.read().strip()
        logger.info("Loaded knowledge base from %s (%d chars)", KNOWLEDGE_FILE, len(content))
        return content
    except FileNotFoundError:
        logger.warning("Knowledge file not found at %s — running without format guidance", KNOWLEDGE_FILE)
        return ""


async def run_agent(repo_url: str) -> AsyncIterator[str]:
    """
    Stream the agent's architecture-documentation response for a given
    GitHub repository URL.

    The OGX server handles the full ReAct loop server-side: it calls the
    GitHub MCP tools, feeds results back to the model, and streams the
    final answer.

    Yields:
        Text chunks (and tool-call status lines) from the agent.
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
        f"{system_prompt}\n\n---\n## Documentation Style Guide\n\n{knowledge}"
        if knowledge
        else system_prompt
    )

    user_message = f"Generate architecture documentation and diagrams for this repository: {repo_url}"

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
                        yield f"\n\n> 🔧 Calling **{server}** → `{tool}`…\n\n"

                elif event_type == "response.output_item.done":
                    item = getattr(event, "item", None)
                    if item and getattr(item, "type", None) == "mcp_call":
                        server = getattr(item, "server_label", "unknown")
                        tool = getattr(item, "name", "unknown")
                        error = getattr(item, "error", None)
                        if error:
                            tool_failures += 1
                            logger.warning("Tool call failed: %s → %s: %s", server, tool, error)
                            yield f"\n\n> ⚠️ **{server}** → `{tool}` failed: {error}\n\n"
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
                            "The agent backend ended the run without completing documentation.",
                        )
                    msg = _announce_ogx_error()
                    if msg:
                        yield msg

    except TimeoutError:
        logger.error("Agent exceeded %ds timeout", AGENT_TIMEOUT_SECONDS)
        yield (
            f"\n\n⚠️ **Error:** The agent took longer than {AGENT_TIMEOUT_SECONDS}s and was stopped. "
            "This usually means the model got stuck in a tool-calling loop, or the repository is very "
            "large. Try again, or point the agent at a smaller repository.\n"
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
        elif ogx_error:
            logger.warning(
                "Stream produced no text output after OGX error (already reported to user)"
            )
        else:
            logger.warning(
                "Stream produced no text output — the model may have ended on a tool call "
                "without generating a final answer"
            )
            yield (
                "\n\n⚠️ **Error:** The agent finished without producing documentation. "
                f"It ran {tool_calls} tool step(s) but the model never returned a final answer. "
                "Try again or use a smaller repository.\n"
            )
