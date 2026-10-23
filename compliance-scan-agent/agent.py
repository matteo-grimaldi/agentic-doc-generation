"""
Compliance-Scan Agent — OGX edition.

Given a GitHub repository URL, scan it for architectural bad practices in
three areas — Kubernetes/OpenShift manifests, overall project structure,
and (for Java projects) pom.xml content — against a small set of
separately-maintained guideline Markdown files, and produce a structured
compliance report, synthesized by a model served via a configurable
external OpenAI-compatible endpoint (base URL + API key set via
compliance-scan-agent/k8s/cluster-config.yaml, shared with doc-gen-agent,
and the external-model-credentials Secret — no in-cluster model serving
or GPU required).

This is a third, independent counterpart to doc-gen-agent and
openapi-gen-agent: it was split out as its own component so compliance
scanning can be deployed, scaled, and iterated on separately. It shares
the same GitHub MCP server and (by default) the same external model
endpoint, but has its own pod, its own budget, and its own PR-free
reporting path (this agent never opens a PR — it only reports findings
back to the Governance Hub for a human to act on).

Uses the OGX Responses API (server-side agentic loop) instead of
LangChain/LangGraph, matching the pattern from doc-gen-agent /
openapi-gen-agent. The OGX server runs as a sidecar container in the same
pod and handles:
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
# Each file here is a focused, independently-maintained compliance
# guideline mounted from a ConfigMap (see build-and-deploy.sh) — keeping
# them separate (rather than one big knowledge.md) lets each check
# category evolve on its own and keeps any single file small, since the
# full set is concatenated into the system prompt below and this agent's
# token budget is tight.
GUIDELINES_DIR = os.getenv("GUIDELINES_DIR", "/etc/agent-knowledge/guidelines")
GUIDELINE_FILES = ("k8s-manifests.md", "project-structure.md", "pom-xml.md")

AGENT_TIMEOUT_SECONDS = int(os.getenv("AGENT_TIMEOUT_SECONDS", "600"))
# Same tight budget profile as openapi-gen-agent: this model has
# Qwen3-style "thinking" always on by default (burns the whole token
# budget on hidden reasoning before any visible content), and the
# external endpoint enforces a fixed ~100s request timeout — disabling
# thinking plus a right-sized INFERENCE_MAX_TOKENS (see deployment.yaml)
# and a tight inference-iteration budget keep this agent inside both
# constraints. A compliance scan (read a handful of manifests, compare
# against guidelines, write a report) is comparable in complexity to
# openapi-gen-agent's task, so the same limits apply here.
MAX_INFER_ITERS = int(os.getenv("MAX_INFER_ITERS", "10"))
MAX_READ_FILES = int(os.getenv("MAX_READ_FILES", "6"))
CHAT_TEMPLATE_KWARGS = {"enable_thinking": False}

# Path/filename signals used to build a zero-model-budget reading list
# from the file tree get_repo_structure already returned — the same
# "classify in Python, let the model only read" strategy
# openapi-gen-agent's _classify_api_files uses, so the model's own
# tool-call budget is spent reading exactly the files relevant to the
# guidelines instead of discovering them itself.
_K8S_DIR_SIGNALS = ("k8s/", "kubernetes/", "openshift/", "manifests/", "deploy/", "chart/", "charts/", "helm/")
_K8S_NAME_SUFFIXES = (".yaml", ".yml")
_K8S_NAME_EXCLUDE_SUFFIXES = ("docker-compose.yaml", "docker-compose.yml")
_DOCKERFILE_NAME_PREFIXES = ("dockerfile",)
_POM_FILENAME = "pom.xml"
_PROJECT_STRUCTURE_FILENAMES = {
    "readme.md", ".gitignore", "license", "license.md", "license.txt",
}
_CI_DIR_SIGNALS = (".github/workflows/", ".tekton/")
_CI_NAME_SIGNALS = ("jenkinsfile", ".gitlab-ci.yml")

# How many of each category to hand the model — kept small and fixed so
# the system prompt's tool-call budget is never exceeded regardless of
# repo size.
MAX_CLASSIFIED_K8S_FILES = 4
MAX_CLASSIFIED_DOCKERFILES = 2


def _classify_compliance_files(file_paths: list[str]) -> dict:
    """
    Zero-model-budget pass over the file list already returned by
    get_repo_structure: identifies the small set of files relevant to
    this agent's three guideline categories (K8s/OpenShift manifests,
    project-structure markers, pom.xml) by path/filename convention
    alone, so the model's own tool-call budget is spent reading exactly
    those files instead of first discovering them.

    Returns:
        {
            "k8s_manifests": [path, ...]  (at most MAX_CLASSIFIED_K8S_FILES),
            "dockerfiles": [path, ...]    (at most MAX_CLASSIFIED_DOCKERFILES),
            "pom_xml": str | None,
            "has_readme": bool,
            "has_gitignore": bool,
            "has_license": bool,
            "has_ci_config": bool,
        }
    """
    lowered_pairs = [(p, p.lower()) for p in file_paths]

    def _is_k8s_manifest(lowered: str) -> bool:
        if lowered.endswith(_K8S_NAME_EXCLUDE_SUFFIXES):
            return False
        if not lowered.endswith(_K8S_NAME_SUFFIXES):
            return False
        return any(signal in lowered for signal in _K8S_DIR_SIGNALS)

    k8s_manifests = [p for p, lo in lowered_pairs if _is_k8s_manifest(lo)][:MAX_CLASSIFIED_K8S_FILES]
    dockerfiles = [
        p for p, lo in lowered_pairs
        if lo.rsplit("/", 1)[-1].startswith(_DOCKERFILE_NAME_PREFIXES)
    ][:MAX_CLASSIFIED_DOCKERFILES]
    pom_xml = next((p for p, lo in lowered_pairs if lo.rsplit("/", 1)[-1] == _POM_FILENAME), None)

    basenames = {lo.rsplit("/", 1)[-1] for _, lo in lowered_pairs}
    has_readme = any(b.startswith("readme") for b in basenames)
    has_gitignore = ".gitignore" in basenames
    has_license = any(b.startswith("license") for b in basenames)
    has_ci_config = any(
        signal in lo for _, lo in lowered_pairs for signal in _CI_DIR_SIGNALS
    ) or any(b in _CI_NAME_SIGNALS for b in basenames)

    return {
        "k8s_manifests": k8s_manifests,
        "dockerfiles": dockerfiles,
        "pom_xml": pom_xml,
        "has_readme": has_readme,
        "has_gitignore": has_gitignore,
        "has_license": has_license,
        "has_ci_config": has_ci_config,
    }


_REPORT_BLOCK_RE = re.compile(r"```(?:markdown|md)?\s*\n([\s\S]*?)\n```", re.IGNORECASE)
_NOT_APPLICABLE_RE = re.compile(r"^\s*NOT_APPLICABLE\s*:", re.IGNORECASE)


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
    return f"""You are an architectural-compliance scanner. The repo's default branch and a short,
pre-classified list of candidate files (found by path convention — K8s/OpenShift manifests,
Dockerfiles, pom.xml, and project-structure markers) are already given to you in the user message
below — do NOT call get_repo_info or get_repo_structure; that work is already done.

You check the repo against three separate guideline documents, provided below under "## Compliance
Guidelines" — one section per category (K8s/OpenShift manifests, project structure, pom.xml). Only
check a category against files that were actually found; if the user message says no files were
found for a category (e.g. no pom.xml because the repo isn't a Maven project), skip that category's
checks entirely rather than reporting failures for files that don't exist.

Tools (github-mcp-server) — use ONLY these two, on ONLY the files you were given:
- get_file_outline(repo_url, path, ref): CHEAP — structural scan (YAML top-level keys plus
  kind:/name: hints for manifests; FROM/RUN/CMD/etc. for Dockerfiles). Try this first on YAML/
  Dockerfile candidates — it is often enough to check for missing keys like resources/
  securityContext/probes.
- read_file(repo_url, path, ref): EXPENSIVE — full content. Required for pom.xml (its guideline
  checks need actual dependency/version values an outline can't show), and for any manifest whose
  outline didn't give a clear enough answer.

STRICT BUDGET — {MAX_INFER_ITERS} total inference iterations, shared across tool calls AND your
final write. Process files ONE AT A TIME and keep a running mental note of findings as you go — do
not wait until every file is scanned to start forming the report. Stop calling tools well before the
budget runs out: write the report from what you have rather than risk having zero iterations left to
produce it.

If none of the three categories have any applicable files at all (no K8s manifests, no Dockerfile,
no pom.xml, and the project-structure guideline's checks are about repo-root files which are always
checkable), still produce the report — project-structure checks are always applicable.

Output ONLY a fenced ```markdown block — no preamble, no narration. The report MUST have exactly
this shape:

```markdown
# Compliance Report

**Summary:** X passed, Y failed, Z skipped

| Category | Check | Status | Severity | Details |
|---|---|---|---|---|
| <category> | <check name> | PASS/FAIL/SKIP | HIGH/MEDIUM/LOW | <one-line evidence or remediation hint> |
```

Rules:
- One table row per check defined in the guidelines you were given, for every category that had at
  least one applicable file (project structure is always applicable).
- Only mark a check FAIL if you found concrete evidence against it in a file you actually read or
  outlined — never guess or invent findings. If you could not determine a check from the files you
  looked at, mark it SKIP with a one-line reason, not FAIL.
- Severity comes from the guideline document for that check; if a guideline doesn't specify one, use
  MEDIUM.
- For every FAIL row, the Details column must include a short, actionable remediation hint.
- Keep Details to one line (no embedded newlines, no pipe characters) so the table stays valid
  Markdown.
"""


def _build_user_message(repo_url: str, repo_structure: dict | None) -> str:
    """
    Build the user-turn message. When repo_structure is available (the
    caller's zero-model-budget get_repo_structure / /repo-structure call —
    see app.py's _fetch_repo_structure), embed a pre-classified reading
    list (see _classify_compliance_files) so the model never spends its
    own inference-iteration budget on get_repo_info or get_repo_structure,
    or on scanning a potentially hundreds-of-entries file list itself.
    """
    if not repo_structure or "error" in repo_structure:
        return (
            f"Run a compliance scan for this repository: {repo_url}\n\nNo pre-fetched file list is "
            f"available — call get_repo_structure yourself first, then proceed as instructed."
        )

    ref = repo_structure.get("ref") or ""
    files = repo_structure.get("files") or []
    file_paths = [f.get("path", "") for f in files if isinstance(f, dict) and f.get("path")]
    classified = _classify_compliance_files(file_paths)

    ref_suffix = f" (ref: {ref})" if ref else ""
    lines = [f"Run a compliance scan for: {repo_url}{ref_suffix}"]
    ref_note = f' and ref "{ref}"' if ref else ""

    if classified["k8s_manifests"]:
        lines.append("\nK8s/OpenShift manifest candidates (get_file_outline each first):")
        lines.extend(f"- {p}" for p in classified["k8s_manifests"])
    else:
        lines.append("\nNo K8s/OpenShift manifest files found — skip that category's checks.")

    if classified["dockerfiles"]:
        lines.append("\nDockerfile candidates (get_file_outline each):")
        lines.extend(f"- {p}" for p in classified["dockerfiles"])

    if classified["pom_xml"]:
        lines.append(f"\nMaven build file found (read_file required for its checks):\n- {classified['pom_xml']}")
    else:
        lines.append("\nNo pom.xml found — skip the pom.xml category's checks (not a Maven project).")

    lines.append(
        "\nProject-structure signals already determined from the file list (zero tool calls needed "
        "for these — use them directly when scoring that category's checks):"
    )
    lines.append(f"- README present: {classified['has_readme']}")
    lines.append(f"- .gitignore present: {classified['has_gitignore']}")
    lines.append(f"- LICENSE present: {classified['has_license']}")
    lines.append(f"- CI/CD config present: {classified['has_ci_config']}")
    lines.append(f"- Dockerfile present: {bool(classified['dockerfiles'])}")

    lines.append(f'\nUse these exact paths with repo_url "{repo_url}"{ref_note} — do not call get_repo_info or get_repo_structure.')
    return "\n".join(lines)


def _load_guidelines(directory: str = GUIDELINES_DIR, filenames: tuple[str, ...] = GUIDELINE_FILES) -> str:
    """Load and concatenate the separate per-category guideline Markdown
    files (mounted from a ConfigMap) into one block for the system
    prompt, each under its own heading so the model can tell which
    category a rule belongs to."""
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
    Stream the compliance-scan agent's response for a given GitHub
    repository URL.

    The OGX server handles the full ReAct loop server-side: it calls the
    GitHub MCP tools, feeds results back to the model, and streams the
    final answer.

    Args:
        repo_url: The target GitHub repository.
        repo_structure: The dict the caller already fetched via the
            GitHub MCP server's zero-model-budget get_repo_structure /
            /repo-structure route — see app.py's _fetch_repo_structure.
            Passing it here lets _build_user_message hand the model a
            pre-classified reading list instead of making it spend its
            own tool-call budget on get_repo_info/get_repo_structure.

    Yields:
        Text chunks from the agent — a fenced ```markdown compliance
        report.
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
        f"{system_prompt}\n\n---\n## Compliance Guidelines\n\n{guidelines}"
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
                            "The agent backend ended the run without completing the compliance report.",
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
                "\n\n⚠️ **Error:** The agent finished without producing a report. "
                f"It ran {tool_calls} tool step(s) but the model never returned a final answer.\n"
            )


def extract_compliance_report(streamed: str) -> str:
    """
    Extract a clean Markdown compliance report from a full run_agent()
    stream, or "" if the agent produced no recognizable report — callers
    (app.py) treat "" as "nothing usable to report to the Governance Hub".

    Unlike doc-gen-agent/openapi-gen-agent, this agent has no NOT_AN_API-
    style bail-out: a compliance report is always producible (at minimum,
    project-structure checks are always applicable), so the only failure
    mode is a malformed/missing fenced block.
    """
    text = streamed.strip()
    if not text:
        return ""
    match = _REPORT_BLOCK_RE.search(text)
    if match:
        report = match.group(1).strip()
        if report:
            return report + "\n"
        logger.warning("extract_compliance_report: fenced block was empty")
        return ""
    # Fallback: the model occasionally replies without a fence.
    if text.lstrip().startswith("#"):
        return text + "\n"
    logger.warning("extract_compliance_report: no recognizable compliance report found in agent output")
    return ""
