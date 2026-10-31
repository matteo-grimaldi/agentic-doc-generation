"""
Catalog-Gen Agent — OGX edition.

Given a GitHub repository URL, explore it via the shared GitHub MCP server
and generate (or augment) a Backstage `catalog-info.yaml` — a Component
entity with correct metadata/annotations/tags plus any API entities the
repo's code actually evidences (REST/OpenAPI, gRPC, GraphQL, AsyncAPI) —
synthesized by a model served via a configurable external OpenAI-compatible
endpoint (base url + API key set via the shared cluster-config ConfigMap
and the external-model-credentials Secret — no in-cluster model serving or
GPU required).

This is the fourth, independent agent in this repo (alongside doc-gen-agent,
openapi-gen-agent, and compliance-scan-agent): it helps when a repo is
already registered in a Backstage/RHDH catalog but its own catalog-info.yaml
is missing or incomplete because the author never filled it in. Like
doc-gen-agent and openapi-gen-agent (and unlike compliance-scan-agent), this
agent DOES open a PR with its output — the generated catalog-info.yaml is
only useful once it lives in the target repo.

Uses the OGX Responses API (server-side agentic loop) instead of
LangChain/LangGraph, matching the pattern from the other three agents. The
OGX server runs as a sidecar container in the same pod and handles:
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
# Each file here is a focused, independently-maintained guideline mounted
# from a ConfigMap (see build-and-deploy.sh) — keeping them separate (rather
# than one big knowledge.md) lets each guideline evolve on its own, same
# rationale as compliance-scan-agent's GUIDELINE_FILES.
GUIDELINES_DIR = os.getenv("GUIDELINES_DIR", "/etc/agent-knowledge/guidelines")
GUIDELINE_FILES = ("catalog-component.md", "api-detection.md")

AGENT_TIMEOUT_SECONDS = int(os.getenv("AGENT_TIMEOUT_SECONDS", "600"))
# Same tight budget profile as compliance-scan-agent/openapi-gen-agent: this
# model has Qwen3-style "thinking" always on by default (burns the whole
# token budget on hidden reasoning before any visible content), and the
# external endpoint enforces a fixed ~100s request timeout — disabling
# thinking plus a right-sized INFERENCE_MAX_TOKENS (see deployment.yaml)
# and a tight inference-iteration budget keep this agent inside both
# constraints. Generating a catalog-info.yaml (classify the repo, look at a
# handful of manifests/build files, write YAML) is comparable in complexity
# to compliance-scan-agent's task, so the same limits apply here.
MAX_INFER_ITERS = int(os.getenv("MAX_INFER_ITERS", "10"))
MAX_READ_FILES = int(os.getenv("MAX_READ_FILES", "6"))
CHAT_TEMPLATE_KWARGS = {"enable_thinking": False}

# Path/filename signals used to build a zero-model-budget reading list from
# the file tree get_repo_structure already returned — the same
# "classify in Python, let the model only read" strategy openapi-gen-agent's
# _classify_api_files and compliance-scan-agent's _classify_compliance_files
# use, so the model's own tool-call budget is spent reading exactly the
# files relevant to generating catalog-info.yaml instead of discovering
# them itself.
_EXISTING_CATALOG_FILENAMES = {"catalog-info.yaml", "catalog-info.yml"}
_K8S_DIR_SIGNALS = ("k8s/", "kubernetes/", "openshift/", "manifests/", "deploy/", "chart/", "charts/", "helm/")
_K8S_NAME_SUFFIXES = (".yaml", ".yml")
_K8S_NAME_EXCLUDE_SUFFIXES = ("docker-compose.yaml", "docker-compose.yml")
_ARGOCD_NAME_HINTS = ("argocd", "application")
_DOCKERFILE_NAME_PREFIXES = ("dockerfile",)
_BUILD_MANIFEST_FILENAMES = (
    "pom.xml", "build.gradle", "build.gradle.kts", "package.json",
    "go.mod", "pyproject.toml", "requirements.txt", "setup.py", "cargo.toml",
)
_OPENAPI_SPEC_FILENAMES = {
    "openapi.yaml", "openapi.yml", "openapi.json",
    "swagger.yaml", "swagger.yml", "swagger.json",
}
_ASYNCAPI_SPEC_FILENAMES = {"asyncapi.yaml", "asyncapi.yml", "asyncapi.json"}
_API_PATH_SIGNALS = (
    "routes/", "route/", "controllers/", "controller/", "handlers/", "handler/",
    "endpoints/", "endpoint/", "resources/", "resource/",
)
_GRAPHQL_NAME_SUFFIXES = (".graphql", ".gql")
_CI_DIR_SIGNALS = (".github/workflows/", ".tekton/")
_CI_NAME_SIGNALS = ("jenkinsfile", ".gitlab-ci.yml")
_PROJECT_STRUCTURE_FILENAMES = {"readme.md", "codeowners", "owners", "owners.md"}
_TECHDOCS_NAME_HINTS = ("mkdocs.yml", "mkdocs.yaml")

# How many of each category to hand the model — kept small and fixed so the
# system prompt's tool-call budget is never exceeded regardless of repo size.
MAX_CLASSIFIED_K8S_FILES = 3
MAX_CLASSIFIED_API_FILES = 4
MAX_CLASSIFIED_PROTO_FILES = 2


def _classify_catalog_files(file_paths: list[str]) -> dict:
    """
    Zero-model-budget pass over the file list already returned by
    get_repo_structure: identifies the small set of files relevant to
    generating catalog-info.yaml (an existing catalog file to augment, K8s/
    ArgoCD manifests, build manifests for tech-stack tags, API surface
    signals for each API type, ownership/CI/techdocs markers) by path/
    filename convention alone, so the model's own tool-call budget is spent
    reading exactly those files instead of first discovering them.

    Returns:
        {
            "existing_catalog_info": str | None,
            "k8s_manifests": [path, ...]       (at most MAX_CLASSIFIED_K8S_FILES),
            "argocd_manifests": [path, ...],
            "dockerfile": str | None,
            "build_manifests": [path, ...],
            "openapi_spec": str | None,
            "asyncapi_spec": str | None,
            "proto_files": [path, ...]         (at most MAX_CLASSIFIED_PROTO_FILES),
            "graphql_schemas": [path, ...],
            "api_signal_paths": [path, ...]    (at most MAX_CLASSIFIED_API_FILES),
            "has_readme": bool,
            "has_codeowners": bool,
            "has_ci_config": bool,
            "has_techdocs_config": bool,
        }
    """
    lowered_pairs = [(p, p.lower()) for p in file_paths]
    basenames = {lo.rsplit("/", 1)[-1] for _, lo in lowered_pairs}

    existing_catalog_info = next(
        (p for p, lo in lowered_pairs if lo.rsplit("/", 1)[-1] in _EXISTING_CATALOG_FILENAMES), None
    )

    def _is_k8s_manifest(lowered: str) -> bool:
        if lowered.endswith(_K8S_NAME_EXCLUDE_SUFFIXES):
            return False
        if not lowered.endswith(_K8S_NAME_SUFFIXES):
            return False
        return any(signal in lowered for signal in _K8S_DIR_SIGNALS)

    k8s_manifests = [p for p, lo in lowered_pairs if _is_k8s_manifest(lo)]
    argocd_manifests = [
        p for p, lo in lowered_pairs
        if lo.endswith(_K8S_NAME_SUFFIXES) and any(hint in lo for hint in _ARGOCD_NAME_HINTS)
    ][:MAX_CLASSIFIED_K8S_FILES]
    k8s_manifests = k8s_manifests[:MAX_CLASSIFIED_K8S_FILES]

    dockerfile = next(
        (p for p, lo in lowered_pairs if lo.rsplit("/", 1)[-1].startswith(_DOCKERFILE_NAME_PREFIXES)), None
    )
    build_manifests = [p for p, lo in lowered_pairs if lo.rsplit("/", 1)[-1] in _BUILD_MANIFEST_FILENAMES]

    openapi_spec = next((p for p, lo in lowered_pairs if lo.rsplit("/", 1)[-1] in _OPENAPI_SPEC_FILENAMES), None)
    asyncapi_spec = next((p for p, lo in lowered_pairs if lo.rsplit("/", 1)[-1] in _ASYNCAPI_SPEC_FILENAMES), None)
    proto_files = [p for p, lo in lowered_pairs if lo.endswith(".proto")][:MAX_CLASSIFIED_PROTO_FILES]
    graphql_schemas = [p for p, lo in lowered_pairs if lo.endswith(_GRAPHQL_NAME_SUFFIXES)][:MAX_CLASSIFIED_API_FILES]
    api_signal_paths = [
        p for p, lo in lowered_pairs if any(signal in lo for signal in _API_PATH_SIGNALS)
    ][:MAX_CLASSIFIED_API_FILES]

    has_readme = any(b.startswith("readme") for b in basenames)
    has_codeowners = any(b in _PROJECT_STRUCTURE_FILENAMES for b in basenames)
    has_ci_config = any(
        signal in lo for _, lo in lowered_pairs for signal in _CI_DIR_SIGNALS
    ) or any(b in _CI_NAME_SIGNALS for b in basenames)
    has_techdocs_config = any(b in _TECHDOCS_NAME_HINTS for b in basenames)

    return {
        "existing_catalog_info": existing_catalog_info,
        "k8s_manifests": k8s_manifests,
        "argocd_manifests": argocd_manifests,
        "dockerfile": dockerfile,
        "build_manifests": build_manifests,
        "openapi_spec": openapi_spec,
        "asyncapi_spec": asyncapi_spec,
        "proto_files": proto_files,
        "graphql_schemas": graphql_schemas,
        "api_signal_paths": api_signal_paths,
        "has_readme": has_readme,
        "has_codeowners": has_codeowners,
        "has_ci_config": has_ci_config,
        "has_techdocs_config": has_techdocs_config,
    }


_CATALOG_YAML_BLOCK_RE = re.compile(r"```(?:ya?ml)?\s*\n([\s\S]*?)\n```", re.IGNORECASE)
_API_VERSION_RE = re.compile(r"^\s*apiVersion\s*:\s*backstage\.io/", re.IGNORECASE | re.MULTILINE)


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
    return f"""You are a Backstage catalog-metadata generator. The repo's default branch and a short,
pre-classified list of candidate files (found by path convention — an existing catalog-info.yaml,
K8s/ArgoCD manifests, build manifests, API surface signals for REST/gRPC/GraphQL/AsyncAPI, and
project-structure/CI/techdocs markers) are already given to you in the user message below — do NOT
call get_repo_info or get_repo_structure; that work is already done.

You generate (or augment) a Backstage `catalog-info.yaml` following two separate guideline
documents, provided below under "## Backstage Catalog Guidelines" — one for the Component entity,
one for API entity detection. Only produce the pieces the guidelines say are warranted by the
evidence you were given; never invent a value, path, or schema you have no evidence for.

Tools (github-mcp-server) — use ONLY these two, on ONLY the files you were given:
- get_file_outline(repo_url, path, ref): CHEAP — structural scan (YAML top-level keys plus kind:/
  name: hints for manifests; imports + top-level signatures/annotations for code files, including
  REST framework annotations like @RestController/@RequestMapping/@GetMapping or Express/FastAPI
  route decorators). Try this first on every candidate file.
- read_file(repo_url, path, ref): EXPENSIVE — full content. Use only for an existing
  catalog-info.yaml (you need its exact current values to preserve them), a build manifest whose
  outline didn't clearly show the dependencies/framework you need, or an API spec file you intend
  to reference via `$text` (confirm it parses as a real spec before referencing it).

STRICT BUDGET — {MAX_INFER_ITERS} total inference iterations, shared across tool calls AND your
final write. Process files ONE AT A TIME and keep a running mental note of what you've confirmed
(language, framework, deployment signals, API evidence) as you go — do not wait until every file is
scanned to start forming the document. Stop calling tools well before the budget runs out: write the
catalog-info.yaml from what you have rather than risk having zero iterations left to produce it.

If the user message says an existing catalog-info.yaml was found, read_file it first and follow the
"Augmenting an existing catalog-info.yaml" section of the Component guideline — preserve every
already-filled field, only fill in what's missing or an obvious placeholder.

Output ONLY a fenced ```yaml block — no preamble, no narration. The block must be a valid
multi-document (`---`-separated) Backstage catalog YAML:
- Exactly one `kind: Component` entity.
- Zero or more `kind: API` entities, one per distinct API surface you found real evidence for.
- Every document must start with `apiVersion: backstage.io/v1alpha1`.

Rules:
- Never fabricate an annotation value, owner name, API path, or schema field you have no evidence
  for — omit the field instead (see each guideline's rules on when to omit vs. include).
- If you found no API evidence at all, output only the Component entity — that is a correct,
  complete result.
- Keep the YAML valid: no tabs, consistent indentation, quote any string containing `:`.
"""


def _build_user_message(repo_url: str, repo_structure: dict | None) -> str:
    """
    Build the user-turn message. When repo_structure is available (the
    caller's zero-model-budget get_repo_structure / /repo-structure call —
    see app.py's _fetch_repo_structure), embed a pre-classified reading
    list (see _classify_catalog_files) so the model never spends its own
    inference-iteration budget on get_repo_info or get_repo_structure, or
    on scanning a potentially hundreds-of-entries file list itself.
    """
    if not repo_structure or "error" in repo_structure:
        return (
            f"Generate a catalog-info.yaml for this repository: {repo_url}\n\nNo pre-fetched file "
            f"list is available — call get_repo_structure yourself first, then proceed as instructed."
        )

    ref = repo_structure.get("ref") or ""
    files = repo_structure.get("files") or []
    file_paths = [f.get("path", "") for f in files if isinstance(f, dict) and f.get("path")]
    classified = _classify_catalog_files(file_paths)

    ref_suffix = f" (ref: {ref})" if ref else ""
    ref_note = f' and ref "{ref}"' if ref else ""
    lines = [f"Generate (or augment) catalog-info.yaml for: {repo_url}{ref_suffix}"]

    if classified["existing_catalog_info"]:
        lines.append(
            f"\nExisting catalog-info.yaml found (read_file it FIRST, then augment rather than "
            f"replace):\n- {classified['existing_catalog_info']}"
        )
    else:
        lines.append("\nNo existing catalog-info.yaml found — generate a new one.")

    if classified["k8s_manifests"]:
        lines.append("\nK8s/OpenShift manifest candidates (get_file_outline each):")
        lines.extend(f"- {p}" for p in classified["k8s_manifests"])
    if classified["argocd_manifests"]:
        lines.append("\nPossible ArgoCD Application manifests (get_file_outline each):")
        lines.extend(f"- {p}" for p in classified["argocd_manifests"])
    if classified["dockerfile"]:
        lines.append(f"\nDockerfile found (get_file_outline):\n- {classified['dockerfile']}")

    if classified["build_manifests"]:
        lines.append("\nBuild/dependency manifests for tech-stack tags (get_file_outline each; read_file "
                      "only if the outline doesn't clearly show language/framework):")
        lines.extend(f"- {p}" for p in classified["build_manifests"])

    if classified["openapi_spec"]:
        lines.append(f"\nExisting OpenAPI/Swagger spec found (confirm with read_file, then reference via "
                      f"$text — do not inline it):\n- {classified['openapi_spec']}")
    if classified["asyncapi_spec"]:
        lines.append(f"\nExisting AsyncAPI spec found (confirm with read_file, then reference via "
                      f"$text):\n- {classified['asyncapi_spec']}")
    if classified["proto_files"]:
        lines.append("\ngRPC .proto files found (get_file_outline each if supported, else read_file):")
        lines.extend(f"- {p}" for p in classified["proto_files"])
    if classified["graphql_schemas"]:
        lines.append("\nGraphQL schema files found:")
        lines.extend(f"- {p}" for p in classified["graphql_schemas"])
    if classified["api_signal_paths"]:
        lines.append("\nPossible REST route/controller/handler files (get_file_outline each to confirm "
                      "HTTP framework annotations/decorators before treating as API evidence):")
        lines.extend(f"- {p}" for p in classified["api_signal_paths"])
    if not any([
        classified["openapi_spec"], classified["asyncapi_spec"],
        classified["proto_files"], classified["graphql_schemas"], classified["api_signal_paths"],
    ]):
        lines.append("\nNo API surface signals found — likely no API entity is warranted; confirm from "
                      "the Component's spec.type instead of searching further.")

    lines.append(
        "\nProject-structure signals already determined from the file list (zero tool calls needed "
        "for these — use them directly):"
    )
    lines.append(f"- README present: {classified['has_readme']}")
    lines.append(f"- CODEOWNERS/OWNERS present: {classified['has_codeowners']}")
    lines.append(f"- CI/CD config present: {classified['has_ci_config']}")
    lines.append(f"- TechDocs (mkdocs) config present: {classified['has_techdocs_config']}")

    lines.append(f'\nUse these exact paths with repo_url "{repo_url}"{ref_note} — do not call '
                 f"get_repo_info or get_repo_structure.")
    return "\n".join(lines)


def _load_guidelines(directory: str = GUIDELINES_DIR, filenames: tuple[str, ...] = GUIDELINE_FILES) -> str:
    """Load and concatenate the separate per-topic guideline Markdown files
    (mounted from a ConfigMap) into one block for the system prompt, each
    under its own heading so the model can tell which topic a rule belongs
    to."""
    sections = []
    for filename in filenames:
        path = os.path.join(directory, filename)
        try:
            with open(path) as f:
                content = f.read().strip()
            if content:
                sections.append(f"### {filename}\n\n{content}")
                logger.info("Loaded guideline %s (%d chars)", filename, len(content))
        except FileNotFoundError:
            logger.warning("Guideline file not found at %s — skipping", path)
    return "\n\n".join(sections)


async def run_agent(repo_url: str, repo_structure: dict | None = None) -> AsyncIterator[str]:
    """
    Stream the catalog-gen agent's response for a given GitHub repository
    URL.

    The OGX server handles the full ReAct loop server-side: it calls the
    GitHub MCP tools, feeds results back to the model, and streams the
    final answer.

    Args:
        repo_url: The target GitHub repository.
        repo_structure: The dict the caller already fetched via the GitHub
            MCP server's zero-model-budget get_repo_structure /
            /repo-structure route — see app.py's _fetch_repo_structure.
            Passing it here lets _build_user_message hand the model a
            pre-classified reading list instead of making it spend its own
            tool-call budget on get_repo_info/get_repo_structure.

    Yields:
        Text chunks from the agent — a fenced ```yaml catalog-info.yaml
        document.
    """
    client = AsyncOgxClient(
        base_url=OGX_BASE_URL,
        api_key="local",
        max_retries=5,
        timeout=httpx.Timeout(connect=30.0, read=AGENT_TIMEOUT_SECONDS, write=30.0, pool=30.0),
    )

    guidelines = _load_guidelines()
    system_prompt = _build_system_prompt()
    instructions = (
        f"{system_prompt}\n\n---\n## Backstage Catalog Guidelines\n\n{guidelines}"
        if guidelines
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
                            "The agent backend ended the run without completing the catalog-info.yaml.",
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
                "\n\n⚠️ **Error:** The agent finished without producing a catalog-info.yaml. "
                f"It ran {tool_calls} tool step(s) but the model never returned a final answer.\n"
            )


def extract_catalog_info(streamed: str) -> str:
    """
    Extract a clean Backstage catalog-info.yaml document from a full
    run_agent() stream, or "" if the agent produced no recognizable catalog
    document — callers (app.py) treat "" as "do not open a PR for this
    repo."

    Unlike openapi-gen-agent, this agent has no NOT_AN_API-style bail-out:
    every repo gets at least a Component entity, so the only failure mode
    is a malformed/missing fenced block or a block with no
    backstage.io apiVersion.
    """
    text = streamed.strip()
    if not text:
        return ""
    match = _CATALOG_YAML_BLOCK_RE.search(text)
    candidate = match.group(1).strip() if match else text
    if not _API_VERSION_RE.search(candidate):
        logger.warning("extract_catalog_info: no 'apiVersion: backstage.io/...' found in agent output")
        return ""
    return candidate + "\n"
