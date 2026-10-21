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
import re
from typing import AsyncIterator

import httpx
from ogx_client import AsyncOgxClient

logger = logging.getLogger(__name__)

OGX_BASE_URL = os.getenv("OGX_BASE_URL", "http://localhost:8321")
GITHUB_MCP_URL = os.getenv("GITHUB_MCP_URL", "http://github-mcp-server.doc-gen-demo.svc.cluster.local:8080/mcp")
MODEL_NAME = os.getenv("MODEL_NAME", "granite-3.1-8b-instruct")
KNOWLEDGE_FILE = os.getenv("KNOWLEDGE_FILE", "/etc/agent-knowledge/knowledge.md")

AGENT_TIMEOUT_SECONDS = int(os.getenv("AGENT_TIMEOUT_SECONDS", "600"))
# Raised from 16: get_file_outline scan calls are cheap (small outputs, no
# full file content) but each still consumes one inference iteration, so the
# scan-then-read workflow below needs more iterations than straight reads did.
MAX_INFER_ITERS = int(os.getenv("MAX_INFER_ITERS", "20"))
# Lowered from 12, then from 10: get_file_outline now covers the "what does
# this file declare" need for many files, so fewer full read_file calls
# should be necessary to reach the same (or better) understanding of the
# repo. Also keeps total accumulated prompt size down — a large accumulated
# context increases the external model's time-to-first-token, which has been
# observed in production to exceed an upstream timeout and kill the
# connection (APIConnectionError / "server disconnected") after ~10
# read_file calls. See MAX_FILE_BYTES in github-mcp-server/server.py for the
# complementary per-file size guardrail.
MAX_READ_FILES = int(os.getenv("MAX_READ_FILES", "8"))

# ─────────────────────────────────────────────────────────────────────────
# OpenAPI spec generation — a second, fully independent agent (own system
# prompt, own token/iteration budget below) triggered automatically
# alongside the architecture-doc agent above whenever a repo looks like an
# HTTP API service but doesn't already ship an OpenAPI/Swagger spec. These
# constants are deliberately separate from MAX_INFER_ITERS/MAX_READ_FILES
# above so tuning one agent's guardrails never silently changes the
# other's — see run_openapi_agent() and detect_needs_openapi() below.
# ─────────────────────────────────────────────────────────────────────────
MAX_INFER_ITERS_OPENAPI = int(os.getenv("MAX_INFER_ITERS_OPENAPI", "15"))
MAX_READ_FILES_OPENAPI = int(os.getenv("MAX_READ_FILES_OPENAPI", "10"))
OPENAPI_KNOWLEDGE_FILE = os.getenv("OPENAPI_KNOWLEDGE_FILE", "/etc/agent-knowledge/openapi-knowledge.md")

# Filenames (basename, case-insensitive) that count as "this repo already
# has a spec" — checked against the file list get_repo_structure already
# returned for the architecture-doc run, so detection costs zero extra
# tool/model calls.
OPENAPI_SPEC_FILENAMES = {
    "openapi.yaml", "openapi.yml", "openapi.json",
    "swagger.yaml", "swagger.yml", "swagger.json",
    "api-spec.yaml", "api-spec.yml", "api-spec.json",
}
# Path/filename substrings suggesting the repo exposes an HTTP API — used
# only when no spec file was found, to decide whether spending the OpenAPI
# agent's model budget is even worth it. Intentionally permissive: a false
# positive just costs one extra, independently-budgeted agent run that can
# bail out cheaply; a false negative silently skips a repo that could have
# used a spec. The agent's own system prompt (_build_openapi_system_prompt)
# makes the authoritative call and bails out with NOT_AN_API if it
# disagrees with this heuristic.
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
    already fetched for the architecture-doc run."""
    return any(path.rsplit("/", 1)[-1].lower() in OPENAPI_SPEC_FILENAMES for path in file_paths)


def _looks_like_api_service(file_paths: list[str]) -> bool:
    """Phase 2 of OpenAPI-need detection: a cheap heuristic over the same
    file list — looks for path/filename signals (routes/controllers/
    handlers/api dirs, common HTTP entry-point filenames) combined with
    the presence of a recognizable project manifest. Intentionally
    permissive: the OpenAPI agent's own system prompt makes the
    authoritative call and bails out cheaply (NOT_AN_API) if this
    heuristic over-triggers."""
    lowered = [p.lower() for p in file_paths]
    has_api_signal = any(signal in path for path in lowered for signal in API_PATH_SIGNALS)
    has_manifest = any(path.rsplit("/", 1)[-1] in API_FRAMEWORK_MANIFESTS for path in lowered)
    return has_api_signal and has_manifest


def detect_needs_openapi(repo_structure: dict) -> bool:
    """
    Decide whether run_openapi_agent() should run for this repo, using
    only the file list get_repo_structure already returned for the
    architecture-doc run — adds zero extra tool/model calls, and never
    touches the architecture-doc agent's own budget.

    Returns True only when: no existing OpenAPI/Swagger spec file was
    found AND the file tree shows signals of an HTTP API service. This is
    a cheap pre-filter, not the final word — run_openapi_agent's system
    prompt instructs the model to re-confirm and bail out (NOT_AN_API)
    if it disagrees, so this heuristic being wrong never produces a bogus
    PR, only (at worst) one extra bounded agent run.

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
    logger.info("detect_needs_openapi: API-service signals found, no spec present — will run OpenAPI agent")
    return True


# Injected into the Gradio stream for UX; must not appear in committed ARCHITECTURE.md.
_TOOL_STATUS_LINE_RE = re.compile(
    r"\n\n> (?:🔧 Calling \*\*[^*]+\*\* → `[^`]+`…|"
    r"⚠️ \*\*[^*]+\*\* → `[^`]+` failed: [^\n]+)\n\n"
)
_ARCHITECTURE_DOC_START_RE = re.compile(r"^# Architecture:", re.MULTILINE)
_AGENT_ERROR_FOOTER_RE = re.compile(r"\n\n⚠️ \*\*Error:\*\*[\s\S]*$")


def extract_architecture_document(streamed: str) -> str:
    """
    Strip Gradio-only artifacts from a full run_agent() stream so PRs and
    downloads contain only the architecture Markdown document.
    """
    text = _TOOL_STATUS_LINE_RE.sub("", streamed)
    match = _ARCHITECTURE_DOC_START_RE.search(text)
    if match:
        text = text[match.start() :]
    text = _AGENT_ERROR_FOOTER_RE.sub("", text)
    return text.strip()


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
- get_file_outline(repo_url, path, ref): a CHEAP scan of one file — imports/package declarations plus
  top-level class/function/type signatures, extracted without sending you the full file content.
  Supports Python, Java, Kotlin, Go, JS/TS, YAML, and Dockerfile. Use this FIRST on any code/config
  file whose role you're unsure about — it costs a fraction of what read_file costs.
- read_file(repo_url, path, ref): full contents of one file (large files truncated; README/CHANGELOG-style
  docs are truncated hard, to ~8KB, since their overview is always near the top)
- search_code(repo_url, query): search for a pattern across the repo (requires a GitHub token; may be unavailable)

Token budget — be deliberate and follow this two-phase strategy:

PHASE 1 — Survey (cheap, do this before any read_file call):
  1. get_repo_info → note language, default branch.
  2. get_repo_structure (once) → map the project layout.
  3. get_file_outline on the 6-10 files that look most architecturally relevant (entry points,
     package manifests, k8s/deploy manifests, Dockerfiles, CI config) — this tells you what each
     file declares/imports without spending a full read on it.

PHASE 2 — Deep read (expensive, spend this budget deliberately — at most {MAX_READ_FILES} read_file calls):
  4. Call read_file ONLY on files whose outline (or whose role, for file types get_file_outline
     doesn't support — plain text/JSON/shell/README) revealed something you actually need the full
     content for: service/route definitions, data models, config values, deployment specifics.
  5. Do NOT read_file a file you already scanned with get_file_outline unless the outline showed
     something architecturally significant that needs the full implementation to explain.
  6. Read at most one README/CONTRIBUTING/CHANGELOG-style file in full, and only if get_repo_info's
     description plus the repo structure don't already make the project's purpose clear.
  7. Skip test files, generated code, lock files, and vendored dependencies unless nothing else
     explains the architecture.
  8. Do not call read_file or get_file_outline on the same path twice. Do not re-fetch
     get_repo_structure more than once per repo.
  9. If a tool call fails twice in a row, stop retrying it and proceed with what you have.

PHASE 3 — Write:
  10. Once you've surveyed and selectively deep-read, you MUST produce the final Markdown document —
      do not keep reading files indefinitely. When you reach the read_file cap, stop calling it and
      write the document from what you already have.

Workflow summary: Phase 1 (survey with get_repo_info/get_repo_structure/get_file_outline) →
Phase 2 (read_file on the few files that truly need it) → Phase 3 (synthesize the structured document
and Mermaid diagrams described below, embedded directly in fenced ```mermaid code blocks).

When you begin writing the final answer, start immediately with the heading below — no
preamble, narration, or "let me read…" commentary before it. Tool calls happen outside
your visible reply; the user only wants the finished document in your text output.

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

## C4 Diagrams

Produce a C4 model (Context, Container, Component) of the repository using plain Mermaid
`flowchart` syntax — NOT Mermaid's native `C4Context`/`C4Container` diagram types, which render
unreliably across viewers. Instead, mark each node's C4 role with a `<<Stereotype>>` on a second
line of its label (e.g. `<<Person>>`, `<<System>>`, `<<External System>>`, `<<Container>>`,
`<<Database>>`, `<<Component>>`), and label edges with the relationship/protocol.

### System Context Diagram
One box for the system you are documenting, surrounded by the people and external systems that
interact with it. Do NOT decompose the system internally here — it is a single box at this level.
```mermaid
flowchart TB
    User["Customer\n&lt;&lt;Person&gt;&gt;"]
    System["This Project\n&lt;&lt;System&gt;&gt;"]
    ExtAPI["Payment Gateway\n&lt;&lt;External System&gt;&gt;"]
    User -->|"Uses"| System
    System -->|"Calls"| ExtAPI
```

### Container Diagram
Zoom into the system: one box per separately deployable/runnable unit you actually found
(web app, API server, database, queue, worker, CLI, etc.) — not a code module. Use a `subgraph`
for the system boundary and keep external actors/systems from the Context diagram around it for
reference. If the repo is a single service with no sidecar/DB/queue, it is fine to have just one
container box.
```mermaid
flowchart TB
    User["Customer\n&lt;&lt;Person&gt;&gt;"]
    subgraph SystemBoundary["This Project"]
        WebApp["Web App\n&lt;&lt;Container: React&gt;&gt;"]
        ApiServer["API Server\n&lt;&lt;Container: FastAPI&gt;&gt;"]
        Database[("Database\n&lt;&lt;Database: PostgreSQL&gt;&gt;")]
    end
    User -->|"Uses (HTTPS)"| WebApp
    WebApp -->|"Calls (REST/JSON)"| ApiServer
    ApiServer -->|"Reads/writes (SQL)"| Database
```

### Component Diagram
Zoom into the most architecturally interesting container: one box per major module, package, or
handler group actually found in the code, with edges showing internal dependencies. Use a
`subgraph` for the container boundary. If the codebase is small enough that the Container diagram
already shows everything, it is acceptable for this diagram to repeat it at slightly more detail.
```mermaid
flowchart TB
    subgraph ApiServer["API Server"]
        Routes["Routes/Controllers\n&lt;&lt;Component&gt;&gt;"]
        Service["Business Logic\n&lt;&lt;Component&gt;&gt;"]
        Repo["Data Access\n&lt;&lt;Component&gt;&gt;"]
    end
    Database[("Database\n&lt;&lt;Database&gt;&gt;")]
    Routes -->|"Calls"| Service
    Service -->|"Calls"| Repo
    Repo -->|"Queries"| Database
```

If you could not determine the external actors for the Context diagram, or the deployment
topology for the Container diagram, say so in the surrounding prose rather than inventing boxes,
and keep that diagram as simple as the evidence supports.

The three C4 diagrams above are REQUIRED. Do not add any further diagrams (e.g. a sequenceDiagram
for Data Flow) — describe flows in prose only, as instructed in that section — unless they strictly
follow every rule below; an extra diagram that fails to render is worse than no diagram.

Mermaid syntax rules (apply to every Mermaid block you produce, of any diagram type):
- No spaces in node/participant/actor IDs (use camelCase/PascalCase/underscores).
- Quote any edge/message label or node label containing parentheses, colons, or commas.
- No explicit colors/styling, no `click` directives.
- Use literal `&lt;&lt;`/`&gt;&gt;` (HTML entities) instead of raw `<<`/`>>` inside quoted labels so
  Mermaid does not misparse them as HTML tags.
- NEVER put a semicolon (`;`) inside a label or message string — Mermaid treats `;` as a statement
  separator (it ends the current node/edge/message and starts parsing a new one), so a semicolon
  buried in text like `"...reverse; findVets() is cached; 404/500 handled..."` breaks the parse
  with an unexpected-token error. Use a comma, a dash, or separate lines/messages instead.
- If you do write a `sequenceDiagram`: declare every `participant`/`actor` on its own line before
  any messages reference it; put exactly ONE message per line in the form
  `A->>B: text` / `A-->>B: text` (no chaining multiple messages after each other on one line);
  and quote any message text containing `:`, `(`, `)`, or `;` as `A->>B: "quoted text"`.
"""


def _load_knowledge(path: str = KNOWLEDGE_FILE) -> str:
    """Load a knowledge base (style guide) from a mounted ConfigMap file.

    Defaults to KNOWLEDGE_FILE (architecture-doc agent); run_openapi_agent()
    calls this with OPENAPI_KNOWLEDGE_FILE instead, so each agent's style
    guide stays in its own file/ConfigMap rather than sharing one."""
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


# ─────────────────────────────────────────────────────────────────────────
# OpenAPI spec generation — independent agent, budget, and output from the
# architecture-doc agent above. See detect_needs_openapi() for the gate
# that decides whether this runs at all; app.py is responsible for calling
# that gate and for proposing this agent's output as its own, separate PR
# (never bundled with the architecture doc's PR).
# ─────────────────────────────────────────────────────────────────────────

_OPENAPI_YAML_BLOCK_RE = re.compile(r"```(?:ya?ml)?\s*\n(openapi\s*:\s*[\s\S]*?)\n```", re.IGNORECASE)
_NOT_AN_API_RE = re.compile(r"^\s*NOT_AN_API\s*:", re.IGNORECASE)


def _build_openapi_system_prompt() -> str:
    return f"""You are an AI API-specification assistant. Given a GitHub repository that a cheap
file-tree heuristic flagged as a possible HTTP API service, confirm that and then produce a valid
OpenAPI 3.0.x specification (YAML) describing its endpoints. This is an INDEPENDENT agent turn —
separate from, and with a smaller budget than, the architecture-documentation agent that may also be
running for this same repository. Your output is proposed as its own, separate pull request; it has no
effect on and is not affected by the architecture-documentation agent's run.

FIRST, confirm this repo actually exposes an HTTP API worth specifying. The heuristic that triggered
you can be wrong. If, after get_repo_structure and a couple of get_file_outline calls, you determine
this project does NOT expose an HTTP API (e.g. it's a CLI tool, a library with no server, a
frontend-only app, a batch job, or a service that only exposes gRPC/GraphQL/a message-queue consumer
with no REST/HTTP surface), STOP immediately and output EXACTLY one line, nothing else:

NOT_AN_API: <one short sentence explaining why>

Do not produce a partial or speculative OpenAPI document in that case — a confident "this doesn't
apply" is strictly better than a guessed spec.

Tools available (github-mcp-server) — the same server the architecture-doc agent uses:
- get_repo_info(repo_url): repo metadata, including default_branch — call this FIRST
- get_repo_structure(repo_url, ref): recursive file tree
- get_file_outline(repo_url, path, ref): a CHEAP scan of one file's imports/signatures — use this
  FIRST on any route/controller/handler/router-setup file to confirm it actually declares HTTP routes
  before spending a read_file call on it
- read_file(repo_url, path, ref): full contents of one file
- search_code(repo_url, query): search for a pattern (e.g. a route decorator, a DTO class name);
  requires a GitHub token and may be unavailable

Token budget — this agent has a SMALLER budget than the architecture-doc agent and must be even more
selective. Be deliberate and follow this two-phase strategy:

PHASE 1 — Survey (cheap, do this before any read_file call):
  1. get_repo_info → note language, default branch.
  2. get_repo_structure (once) → locate route/controller/handler files, API router/app setup, and any
     request/response model definitions (DTOs, schemas, serializers, protobuf messages).
  3. get_file_outline on the files most likely to declare HTTP routes (router/app setup, controller
     files, blueprint/views files) to confirm real route signatures before reading them in full.

PHASE 2 — Deep read (expensive — at most {MAX_READ_FILES_OPENAPI} read_file calls):
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


async def run_openapi_agent(repo_url: str) -> AsyncIterator[str]:
    """
    Stream the OpenAPI-specification agent's response for a given GitHub
    repository URL.

    This is a SEPARATE agent run from run_agent() above: its own system
    prompt (_build_openapi_system_prompt), its own iteration budget
    (MAX_INFER_ITERS_OPENAPI vs MAX_INFER_ITERS) and read-file budget
    (MAX_READ_FILES_OPENAPI vs MAX_READ_FILES, enforced via the prompt
    just like run_agent()'s), and its own knowledge file
    (OPENAPI_KNOWLEDGE_FILE). It is only ever meant to be invoked after
    detect_needs_openapi() has already decided the repo is worth the
    extra model budget — callers (app.py) own that gate, and own treating
    this run's success/failure fully independently of the
    architecture-doc run (e.g. proposing this run's output as its own,
    separate PR that can be reviewed/merged/rejected on its own).

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

    knowledge = _load_knowledge(OPENAPI_KNOWLEDGE_FILE)
    system_prompt = _build_openapi_system_prompt()
    instructions = (
        f"{system_prompt}\n\n---\n## OpenAPI Style Guide\n\n{knowledge}"
        if knowledge
        else system_prompt
    )

    user_message = (
        f"Generate an OpenAPI specification for this repository if it exposes an HTTP API: {repo_url}"
    )

    logger.info("Sending OpenAPI-agent request to OGX at %s for repo %s", OGX_BASE_URL, repo_url)

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
                extra_body={"max_infer_iters": MAX_INFER_ITERS_OPENAPI},
            ),
            timeout=60,
        )
    except asyncio.TimeoutError:
        logger.error("Timed out waiting for OGX to start streaming (OpenAPI agent)")
        yield "\n\n⚠️ **Error:** Timed out connecting to the agent backend. Please try again.\n"
        return
    except Exception as exc:
        logger.exception("Failed to create OGX stream (OpenAPI agent)")
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
        logger.error("OGX response error (OpenAPI agent) — code=%s message=%s", code, message)
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
                        logger.info("OpenAPI agent tool call started: %s → %s", server, tool)

                elif event_type == "response.output_item.done":
                    item = getattr(event, "item", None)
                    if item and getattr(item, "type", None) == "mcp_call":
                        server = getattr(item, "server_label", "unknown")
                        tool = getattr(item, "name", "unknown")
                        error = getattr(item, "error", None)
                        if error:
                            tool_failures += 1
                            logger.warning("OpenAPI agent tool call failed: %s → %s: %s", server, tool, error)
                        else:
                            logger.info("OpenAPI agent tool call completed: %s → %s", server, tool)

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
                    logger.info("OpenAPI agent stream response.completed — status=%s", status)
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
        logger.error("OpenAPI agent exceeded %ds timeout", AGENT_TIMEOUT_SECONDS)
        yield (
            f"\n\n⚠️ **Error:** The OpenAPI agent took longer than {AGENT_TIMEOUT_SECONDS}s and was "
            "stopped.\n"
        )
    except Exception as exc:
        logger.exception("Error during OpenAPI agent streaming")
        yield f"\n\n⚠️ **Error:** OpenAPI agent encountered an error: {exc}\n"

    logger.info(
        "OpenAPI agent stream finished — events=%d, text_chunks=%d, text_bytes=%d, tool_calls=%d, "
        "tool_failures=%d",
        event_count, text_chunks, text_bytes, tool_calls, tool_failures,
    )
    if text_chunks == 0:
        if ogx_error and not ogx_error_announced:
            code, message = ogx_error
            yield _format_ogx_failure_message(code, message, tool_calls)
        elif not ogx_error:
            logger.warning(
                "OpenAPI agent stream produced no text output — the model may have ended on a tool "
                "call without generating a final answer"
            )
            yield (
                "\n\n⚠️ **Error:** The OpenAPI agent finished without producing a spec. "
                f"It ran {tool_calls} tool step(s) but the model never returned a final answer.\n"
            )


def extract_openapi_spec(streamed: str) -> str:
    """
    Extract a clean OpenAPI YAML document from a full run_openapi_agent()
    stream, or "" if the agent bailed out with NOT_AN_API, hit an error,
    or produced no recognizable spec — callers (app.py) treat "" as "do
    not open a PR for this repo."
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
