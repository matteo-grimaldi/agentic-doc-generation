"""
Doc-Gen Agent — Gradio Chat UI.

Launches a Gradio chat interface. The user pastes a GitHub repository
URL; the agent (OGX sidecar + GitHub MCP server + an external, configurable
OpenAI-compatible model endpoint) explores the repo and streams back an
architecture summary with Mermaid diagrams.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import uuid

# Disable Gradio telemetry — prevents outbound calls to api.gradio.app / HuggingFace
os.environ.setdefault("GRADIO_ANALYTICS_ENABLED", "False")

import gradio as gr
import httpx
import uvicorn
from fastapi import BackgroundTasks, FastAPI, HTTPException
from pydantic import BaseModel

from agent import (
    MODEL_NAME,
    detect_needs_openapi,
    extract_architecture_document,
    extract_openapi_spec,
    run_agent,
    run_openapi_agent,
)

# Base URL of the GitHub MCP server's plain REST API (not its /mcp MCP
# endpoint) — used only by the /invoke background task below to propose the
# generated document back to its source repo via a PR. Keep separate from
# agent.py's GITHUB_MCP_URL (which points OGX's MCP tool at the /mcp path):
# one is an MCP tool endpoint for the model, this is a deterministic
# server-to-server REST call that never goes through the model.
GITHUB_MCP_SERVER_URL = os.getenv("GITHUB_MCP_SERVER_URL", "http://github-mcp-server.doc-gen-demo.svc.cluster.local:8080")
# Path (within the target repo) the generated document is written to, and
# the PR's base branch. Empty ARCHITECTURE_DOC_BASE_BRANCH means the repo's
# default branch — the doc is NEVER committed directly to this branch; a
# new branch is created off it and a PR opened back into it instead (see
# github-mcp-server's /propose-doc route), so this is safe to point at a
# protected branch.
ARCHITECTURE_DOC_PATH = os.getenv("ARCHITECTURE_DOC_PATH", "ARCHITECTURE.md")
ARCHITECTURE_DOC_BASE_BRANCH = os.getenv("ARCHITECTURE_DOC_BASE_BRANCH", "")

# Path (within the target repo) the independently generated OpenAPI spec is
# written to, if detect_needs_openapi() decides the repo warrants one. Kept
# as its own setting (not derived from ARCHITECTURE_DOC_PATH) so the two
# generations can be configured, enabled, or disabled independently.
OPENAPI_SPEC_PATH = os.getenv("OPENAPI_SPEC_PATH", "openapi.yaml")

_REPO_URL_RE = re.compile(r"https?://github\.com/[^\s)\]]+")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
)
logger = logging.getLogger(__name__)

# ── Quick-start prompts shown as example buttons ────────────────────────────
EXAMPLE_PROMPTS = [
    "https://github.com/quarkusio/quarkus-quickstarts",
    "https://github.com/kserve/kserve",
    "https://github.com/opendatahub-io/odh-dashboard",
    "https://github.com/fastapi/fastapi",
]

HEADER_HTML = f"""
<div style="text-align:center; margin-bottom: 0.5rem;">
  <h1>📐 Agentic Doc Generation</h1>
  <p style="color:#666;">Powered by {MODEL_NAME} (external OpenAI-compatible endpoint) · GitHub MCP · OGX</p>
</div>
"""


async def chat(message: str, history: list[dict]):
    """
    Gradio streaming chat handler.

    The "message" is expected to be a GitHub repo URL (or contain one).
    Streams the agent's architecture-documentation response back chunk
    by chunk.

    Also yields the repo URL and the full generated text into two
    gr.State outputs (last_repo_url, last_content) so the "Propose as
    PR" button below can act on the most recent run without re-invoking
    the agent — this chat path itself never opens a PR (see
    _propose_architecture_doc's docstring and github-mcp-server's
    /propose-doc route for why that's opt-in only).
    """
    if not message.strip():
        yield history + [{"role": "assistant", "content": "Please paste a GitHub repository URL (e.g. https://github.com/owner/repo)."}], "", ""
        return

    repo_url = message.strip()
    logger.info("User repo URL: %s", repo_url[:200])

    history = history + [{"role": "user", "content": message}]
    yield history, "", ""

    response_so_far = ""
    history = history + [{"role": "assistant", "content": ""}]

    async for chunk in run_agent(repo_url):
        response_so_far += chunk
        history[-1] = {"role": "assistant", "content": response_so_far}
        yield history, "", ""

    # Final yield: PR-ready doc only (stream may include tool-status lines).
    yield history, repo_url, extract_architecture_document(response_so_far)


class InvokeRequest(BaseModel):
    """Body for POST /invoke — the prompt is typically a GitHub repo URL,
    optionally wrapped in instruction text (see agent.py's run_agent)."""

    prompt: str


def _extract_repo_url(prompt: str) -> str | None:
    """Pull the first github.com URL out of a prompt (which may be a bare
    repo URL, as the Gradio UI sends, or wrapped in instruction text, as
    ci-cd-pipeline's invoke-doc-gen-agent Task sends)."""
    match = _REPO_URL_RE.search(prompt)
    return match.group(0).rstrip(".,)") if match else None


async def _propose_architecture_doc(repo_url: str, content: str, job_id: str) -> dict:
    """
    Open a pull request proposing the generated architecture document back
    into its source repo, via the GitHub MCP server's POST /propose-doc
    route — a plain REST call, not an MCP tool invocation, so the
    (potentially large) document is sent exactly as generated rather than
    relayed through another model tool call.

    This NEVER commits directly to an existing branch: /propose-doc creates
    a brand-new branch off ARCHITECTURE_DOC_BASE_BRANCH (or the repo's
    default branch), commits the doc there, and opens a PR — safe even if
    the target branch is protected.

    Returns the /propose-doc response body on success, or {"error": ...}
    on any failure (bad response, network error) — callers (the /invoke
    background task, and the chat UI's opt-in "Propose as PR" button)
    decide how to surface that to their respective audiences.
    """
    payload = {
        "repo_url": repo_url,
        "path": ARCHITECTURE_DOC_PATH,
        "content": content,
        "commit_message": f"docs: update {ARCHITECTURE_DOC_PATH} (agentic-doc-generation job {job_id})",
        "pr_title": f"docs: update {ARCHITECTURE_DOC_PATH}",
        "pr_body": (
            f"Auto-generated architecture documentation for this repository "
            f"(agentic-doc-generation job `{job_id}`). Please review before merging."
        ),
    }
    if ARCHITECTURE_DOC_BASE_BRANCH:
        payload["base_branch"] = ARCHITECTURE_DOC_BASE_BRANCH

    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(30.0)) as client:
            resp = await client.post(f"{GITHUB_MCP_SERVER_URL}/propose-doc", json=payload)
        data = resp.json()
        if resp.status_code == 200 and "error" not in data:
            logger.info(
                "Job %s: opened PR #%s (%s) for %s: %s -> %s",
                job_id, data.get("pr_number"), data.get("pr_url"), repo_url,
                data.get("head_branch"), data.get("base_branch"),
            )
            return data
        else:
            error = data.get("error", resp.text[:300])
            logger.warning(
                "Job %s: failed to propose %s to %s: %s",
                job_id, ARCHITECTURE_DOC_PATH, repo_url, error,
            )
            return {"error": error}
    except Exception as exc:
        logger.exception("Job %s: error proposing %s to %s", job_id, ARCHITECTURE_DOC_PATH, repo_url)
        return {"error": str(exc)}


async def _fetch_repo_structure(repo_url: str) -> dict:
    """
    Fetch a repo's file tree via the GitHub MCP server's plain REST
    GET /repo-structure route — a deterministic, non-LLM-mediated call
    (same pattern as _propose_architecture_doc's POST /propose-doc) used
    only to feed agent.detect_needs_openapi(), so that gate costs zero
    model/tool-call budget on either agent.

    Returns {"error": ...} on any failure; callers treat that as
    "detection inconclusive" and skip OpenAPI generation rather than
    guessing.
    """
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(30.0)) as client:
            resp = await client.get(f"{GITHUB_MCP_SERVER_URL}/repo-structure", params={"repo_url": repo_url})
        data = resp.json()
        if resp.status_code == 200 and "error" not in data:
            return data
        logger.warning("Failed to fetch repo structure for %s: %s", repo_url, data.get("error", resp.text[:300]))
        return {"error": data.get("error", resp.text[:300])}
    except Exception as exc:
        logger.warning("Error fetching repo structure for %s: %s", repo_url, exc)
        return {"error": str(exc)}


async def _propose_openapi_spec(repo_url: str, content: str, job_id: str) -> dict:
    """
    Open a pull request proposing a generated OpenAPI spec back into its
    source repo — the OpenAPI-generation counterpart to
    _propose_architecture_doc. Deliberately kept as its own function with
    its own path (OPENAPI_SPEC_PATH), commit message, PR title/body, and
    branch_prefix ("agentic-openapi" vs "agentic-docs") so this PR is
    always distinct from, and reviewable/mergeable/rejectable
    independently of, any architecture-doc PR opened for the same repo.
    """
    payload = {
        "repo_url": repo_url,
        "path": OPENAPI_SPEC_PATH,
        "content": content,
        "commit_message": f"docs: add {OPENAPI_SPEC_PATH} (agentic-doc-generation job {job_id})",
        "pr_title": f"docs: add {OPENAPI_SPEC_PATH}",
        "pr_body": (
            "Auto-generated OpenAPI specification for this repository, produced by an independent "
            f"agent run (agentic-doc-generation job `{job_id}`), separate from any architecture "
            "documentation PR for this repo. Please review before merging."
        ),
        "branch_prefix": "agentic-openapi",
    }
    if ARCHITECTURE_DOC_BASE_BRANCH:
        payload["base_branch"] = ARCHITECTURE_DOC_BASE_BRANCH

    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(30.0)) as client:
            resp = await client.post(f"{GITHUB_MCP_SERVER_URL}/propose-doc", json=payload)
        data = resp.json()
        if resp.status_code == 200 and "error" not in data:
            logger.info(
                "Job %s: opened OpenAPI PR #%s (%s) for %s: %s -> %s",
                job_id, data.get("pr_number"), data.get("pr_url"), repo_url,
                data.get("head_branch"), data.get("base_branch"),
            )
            return data
        else:
            error = data.get("error", resp.text[:300])
            logger.warning("Job %s: failed to propose %s to %s: %s", job_id, OPENAPI_SPEC_PATH, repo_url, error)
            return {"error": error}
    except Exception as exc:
        logger.exception("Job %s: error proposing %s to %s", job_id, OPENAPI_SPEC_PATH, repo_url)
        return {"error": str(exc)}


async def _maybe_generate_openapi_spec(repo_url: str, job_id: str) -> str:
    """
    Run the full, independent OpenAPI-generation path for one repo: fetch
    its file tree (zero model budget — see _fetch_repo_structure), decide
    whether to bother (agent.detect_needs_openapi), and if so drain
    run_openapi_agent() to completion.

    This never touches the architecture-doc agent's budget, and its
    outcome never affects the architecture-doc agent's own run — callers
    run this as a fully separate asyncio task (see _run_openapi_job and
    chat() below).

    Returns the extracted OpenAPI YAML, or "" if detection declined to
    run the agent, the fetch failed, or the agent produced nothing usable
    (e.g. it determined the repo isn't an API).
    """
    structure = await _fetch_repo_structure(repo_url)
    if "error" in structure:
        logger.info("Job %s: could not fetch repo structure for %s — skipping OpenAPI check", job_id, repo_url)
        return ""
    if not detect_needs_openapi(structure):
        return ""

    logger.info("Job %s: running independent OpenAPI agent for %s", job_id, repo_url)
    openapi_text = ""
    async for chunk in run_openapi_agent(repo_url):
        openapi_text += chunk
    spec = extract_openapi_spec(openapi_text)
    if not spec.strip():
        logger.info("Job %s: OpenAPI agent produced no spec for %s (not an API, or no output)", job_id, repo_url)
        return ""
    return spec


async def _run_openapi_job(repo_url: str, job_id: str) -> None:
    """
    Fully independent companion to the architecture-doc job: detects
    whether repo_url needs an OpenAPI spec, generates one if so, and opens
    its own PR via _propose_openapi_spec. Any exception here is caught and
    logged without ever propagating to, or affecting the outcome of, the
    architecture-doc job it runs alongside.
    """
    try:
        spec = await _maybe_generate_openapi_spec(repo_url, job_id)
    except Exception:
        logger.exception("Job %s: OpenAPI generation failed for %s", job_id, repo_url)
        return
    if not spec:
        return
    await _propose_openapi_spec(repo_url, spec, job_id)


async def _run_agent_in_background(prompt: str, job_id: str) -> None:
    """
    Drain run_agent() to completion outside of any HTTP request/response
    cycle, then propose the result back to its source repo as a PR. This is
    the CI/CD trigger path (no UI session to stream into), so the only
    observable outcomes are: the log lines below, and (if a repo URL could
    be extracted and GITHUB_TOKEN is configured with write access) up to
    TWO pull requests against the source repo — ARCHITECTURE_DOC_PATH from
    this function, and (independently, via _run_openapi_job, started
    concurrently below) OPENAPI_SPEC_PATH if agent.detect_needs_openapi()
    decides the repo needs one. The two generations run concurrently, each
    with its own budget, and neither's failure affects the other's PR.
    """
    logger.info("Background agent job %s started for prompt=%s", job_id, prompt[:200])

    repo_url = _extract_repo_url(prompt)
    openapi_task: asyncio.Task | None = None
    if repo_url:
        # Fire the independent OpenAPI-generation path concurrently now —
        # it has its own token/iteration budget and its own PR, and must
        # never block on, or be blocked by, the architecture-doc run below.
        openapi_task = asyncio.create_task(_run_openapi_job(repo_url, job_id))
    else:
        logger.warning("Job %s: could not find a github.com URL in the prompt — skipping OpenAPI check", job_id)

    try:
        response_text = ""
        async for chunk in run_agent(prompt):
            response_text += chunk
        logger.info("Background agent job %s completed (%d chars generated)", job_id, len(response_text))

        doc_content = extract_architecture_document(response_text)
        if not doc_content.strip():
            logger.warning("Job %s: agent produced no document — skipping PR", job_id)
        elif not repo_url:
            logger.warning("Job %s: could not find a github.com URL in the prompt — skipping PR", job_id)
        else:
            await _propose_architecture_doc(repo_url, doc_content, job_id)
    except Exception:
        logger.exception("Background agent job %s failed", job_id)
    finally:
        # Ensure the independent OpenAPI job (and its own exception
        # handling/logging) is fully observed before this background task
        # ends, regardless of how the architecture-doc path above fared.
        if openapi_task is not None:
            await openapi_task


async def propose_pr_clicked(repo_url: str, content: str) -> str:
    """
    Click handler for the chat UI's opt-in "📤 Propose as PR" button.

    Reuses the same _propose_architecture_doc helper as the /invoke
    background path, but is only ever triggered by an explicit user
    click — never by the model — preserving the guardrail described in
    _propose_architecture_doc's and /propose-doc's docstrings.
    """
    if not repo_url or not content.strip():
        return "⚠️ Generate an architecture doc for a repository first, then click **Propose as PR**."

    job_id = str(uuid.uuid4())
    logger.info("UI-triggered PR proposal job %s for %s", job_id, repo_url)
    result = await _propose_architecture_doc(repo_url, content, job_id)

    if "error" in result:
        return f"⚠️ Failed to open PR for `{repo_url}`: {result['error']}"
    return (
        f"✅ Opened [PR #{result.get('pr_number')}]({result.get('pr_url')}) on `{repo_url}` "
        f"(`{result.get('head_branch')}` → `{result.get('base_branch')}`)."
    )


async def generate_openapi_if_needed(repo_url: str):
    """
    Gradio UI companion to chat(), chained via `.then()` so it starts only
    once a doc-gen run's final repo_url is known, and never extends or
    blocks the main chat stream (it runs as its own event after chat()
    finishes, with its own, independent agent run and budget).

    Streams status text into the "openapi_status" Markdown element and the
    generated spec (if any) into the "last_openapi_content" State, which
    drives the opt-in "Propose OpenAPI Spec as PR" button below — that
    button, not this function, is what opens the (separate) PR, mirroring
    how chat() never opens the architecture-doc PR itself.

    Yields (status_markdown, openapi_content) tuples.
    """
    if not repo_url:
        yield "", ""
        return

    yield "🔍 Checking whether this repo needs an OpenAPI spec…", ""

    structure = await _fetch_repo_structure(repo_url)
    if "error" in structure:
        logger.info("UI OpenAPI check: could not fetch repo structure for %s — skipping", repo_url)
        yield "", ""
        return

    if not detect_needs_openapi(structure):
        yield "ℹ️ No OpenAPI spec needed (one already exists, or this doesn't look like an API service).", ""
        return

    yield "🤖 Generating OpenAPI spec — independent agent run, will propose its own separate PR…", ""

    openapi_text = ""
    async for chunk in run_openapi_agent(repo_url):
        openapi_text += chunk

    if openapi_text.strip().upper().startswith("NOT_AN_API"):
        reason = openapi_text.split(":", 1)[1].strip() if ":" in openapi_text else "not an HTTP API"
        logger.info("UI OpenAPI agent bailed out for %s: %s", repo_url, reason)
        yield f"ℹ️ No OpenAPI spec generated: {reason}", ""
        return

    spec = extract_openapi_spec(openapi_text)
    if not spec.strip():
        yield "⚠️ OpenAPI agent did not produce a usable spec.", ""
        return

    yield "✅ OpenAPI spec generated — click **Propose OpenAPI Spec as PR** to open a pull request.", spec


async def propose_openapi_clicked(repo_url: str, content: str) -> str:
    """
    Click handler for the opt-in "📤 Propose OpenAPI Spec as PR" button —
    the OpenAPI counterpart to propose_pr_clicked: reuses
    _propose_openapi_spec (distinct path/branch-prefix/PR from the
    architecture doc's), only ever triggered by an explicit user click —
    never by the model or by generate_openapi_if_needed itself.
    """
    if not repo_url or not content.strip():
        return "⚠️ Generate an OpenAPI spec for a repository first, then click **Propose OpenAPI Spec as PR**."

    job_id = str(uuid.uuid4())
    logger.info("UI-triggered OpenAPI PR proposal job %s for %s", job_id, repo_url)
    result = await _propose_openapi_spec(repo_url, content, job_id)

    if "error" in result:
        return f"⚠️ Failed to open OpenAPI PR for `{repo_url}`: {result['error']}"
    return (
        f"✅ Opened [PR #{result.get('pr_number')}]({result.get('pr_url')}) on `{repo_url}` "
        f"(`{result.get('head_branch')}` → `{result.get('base_branch')}`)."
    )


def build_ui() -> gr.Blocks:
    with gr.Blocks(
        title="Agentic Doc Generation",
        theme=gr.themes.Soft(
            primary_hue="red",
            secondary_hue="blue",
            neutral_hue="slate",
        ),
        css="""
        .chatbot { background: #ffffff !important; border-radius: 8px; }
        .chatbot .message { font-size: 0.95rem; }
        .chatbot pre { background: #1e1e2e; color: #cdd6f4; padding: 12px; border-radius: 6px; }
        footer { display: none !important; }
        """,
    ) as demo:
        gr.HTML(HEADER_HTML)

        chatbot = gr.Chatbot(
            label="Architecture Doc Generator",
            height=640,
            render_markdown=True,
            avatar_images=(
                None,
                "https://www.redhat.com/cms/managed-files/Asset-Red_Hat-Logo_page-Logo-RGB.svg",
            ),
        )

        with gr.Row():
            txt = gr.Textbox(
                placeholder="Paste a GitHub repository URL, e.g. https://github.com/owner/repo",
                show_label=False,
                scale=9,
                container=False,
            )
            send_btn = gr.Button("Generate", variant="primary", scale=1, min_width=100)

        # Populated by chat() once a run completes; empty while a run is
        # streaming or hasn't produced a repo URL yet. Drives the opt-in
        # PR button below — the chat path itself never opens a PR.
        last_repo_url = gr.State("")
        last_content = gr.State("")

        with gr.Row():
            propose_btn = gr.Button("📤 Propose as PR", scale=1, min_width=160)
            propose_status = gr.Markdown("", elem_id="propose-status")

        # Populated by generate_openapi_if_needed(), an independent
        # follow-up run chained after chat() via .then() below — its own
        # agent, its own budget, and (via the button beneath it) its own,
        # separate PR from the architecture doc's.
        last_openapi_content = gr.State("")

        with gr.Row():
            openapi_status = gr.Markdown("", elem_id="openapi-status")

        with gr.Row():
            propose_openapi_btn = gr.Button("📤 Propose OpenAPI Spec as PR", scale=1, min_width=220)
            propose_openapi_status = gr.Markdown("", elem_id="propose-openapi-status")

        gr.Examples(
            examples=EXAMPLE_PROMPTS,
            inputs=txt,
            label="Try one of these repositories",
        )

        with gr.Accordion("ℹ️ About this demo", open=False):
            gr.Markdown(
                f"""
                This agent automatically generates architecture documentation for any public
                GitHub repository — a simplified, single-agent version of
                [opendatahub-io/architecture-context](https://github.com/opendatahub-io/architecture-context).

                **How it works:**
                1. You paste a GitHub repository URL
                2. The agent queries the **GitHub MCP server** to map the repo structure and read key files
                   (README, entry points, manifests, Dockerfiles, k8s/CI configs)
                3. **{MODEL_NAME}**, served via a configurable external OpenAI-compatible endpoint, synthesizes the findings into:
                   - A structured architecture document (overview, components, data flow, dependencies, deployment, security)
                   - Three Mermaid diagrams: component, data flow, and dependency diagrams

                The in-cluster components (Gradio UI, OGX sidecar, GitHub MCP server) need no GPU — only the
                configured external endpoint performs inference.

                **OpenAPI spec generation (independent):** after the architecture doc finishes, a second,
                fully independent agent run (its own budget, its own system prompt) checks whether this
                repo looks like an HTTP API service that doesn't already ship an OpenAPI/Swagger spec. If
                so, it generates one and offers its own **"Propose OpenAPI Spec as PR"** button — a
                completely separate pull request from the architecture doc's, reviewable/acceptable/
                rejectable on its own.
                """
            )

        submit_event = txt.submit(
            fn=chat, inputs=[txt, chatbot], outputs=[chatbot, last_repo_url, last_content]
        )
        submit_event.then(lambda: ("", ""), outputs=[txt, propose_status])
        # Independent OpenAPI-generation follow-up: starts only after chat()
        # fully completes, runs its own agent/budget, and never blocks or
        # extends the chat stream above.
        submit_event.then(
            fn=generate_openapi_if_needed, inputs=[last_repo_url], outputs=[openapi_status, last_openapi_content]
        )

        send_event = send_btn.click(
            fn=chat, inputs=[txt, chatbot], outputs=[chatbot, last_repo_url, last_content]
        )
        send_event.then(lambda: ("", ""), outputs=[txt, propose_status])
        send_event.then(
            fn=generate_openapi_if_needed, inputs=[last_repo_url], outputs=[openapi_status, last_openapi_content]
        )

        propose_btn.click(
            fn=propose_pr_clicked, inputs=[last_repo_url, last_content], outputs=[propose_status]
        )
        propose_openapi_btn.click(
            fn=propose_openapi_clicked,
            inputs=[last_repo_url, last_openapi_content],
            outputs=[propose_openapi_status],
        )

    return demo


def build_app() -> FastAPI:
    """
    Build the FastAPI app that serves both the Gradio chat UI (mounted at
    "/") and a plain REST API for programmatic/CI-CD callers — notably
    POST /invoke, which triggers an agent run without blocking the caller
    on its full (potentially multi-minute) execution. This is what lets an
    external pipeline (e.g. a Tekton Task) fire the agent and move on
    immediately instead of waiting for the response.
    """
    fastapi_app = FastAPI(title="Doc-Gen Agent")

    @fastapi_app.get("/healthz")
    async def healthz() -> dict:
        return {"status": "ok"}

    @fastapi_app.post("/invoke", status_code=202)
    async def invoke(req: InvokeRequest, background_tasks: BackgroundTasks) -> dict:
        prompt = req.prompt.strip()
        if not prompt:
            raise HTTPException(status_code=400, detail="prompt must not be empty")

        job_id = str(uuid.uuid4())
        logger.info("POST /invoke accepted job %s, prompt=%s", job_id, prompt[:200])
        background_tasks.add_task(_run_agent_in_background, prompt, job_id)
        # Returns immediately (202 Accepted) — the agent run happens after
        # this response is sent, via starlette's BackgroundTasks, so a
        # caller (e.g. a CI/CD pipeline step) is never blocked on it.
        return {"status": "accepted", "job_id": job_id}

    demo = build_ui()
    gr.mount_gradio_app(fastapi_app, demo, path="/")
    return fastapi_app


if __name__ == "__main__":
    port = int(os.getenv("GRADIO_PORT", "7860"))
    uvicorn.run(build_app(), host="0.0.0.0", port=port)
