"""
OpenAPI-Gen Agent — Gradio Chat UI.

Launches a Gradio chat interface. The user pastes a GitHub repository
URL; the agent (OGX sidecar + GitHub MCP server + an external, configurable
OpenAI-compatible model endpoint) checks whether the repo looks like an
HTTP API service that doesn't already ship an OpenAPI/Swagger spec, and if
so, generates one and streams it back.

Independent counterpart to doc-gen-agent's app.py — its own Gradio UI, its
own /invoke endpoint for CI/CD triggers, and its own PR-proposal path, so
this component can be deployed, scaled, and iterated on separately from
architecture-documentation generation.
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

from agent import MODEL_NAME, detect_needs_openapi, extract_openapi_spec, run_agent

# Base URL of the GitHub MCP server's plain REST API (not its /mcp MCP
# endpoint) — used by _fetch_repo_structure (zero-model-budget detection)
# and _propose_openapi_spec (opening the PR). Keep separate from agent.py's
# GITHUB_MCP_URL (which points OGX's MCP tool at the /mcp path): one is an
# MCP tool endpoint for the model, this is a deterministic server-to-server
# REST call that never goes through the model.
GITHUB_MCP_SERVER_URL = os.getenv("GITHUB_MCP_SERVER_URL", "http://github-mcp-server.doc-gen-demo.svc.cluster.local:8080")
# Path (within the target repo) the generated spec is written to, and the
# PR's base branch. Empty OPENAPI_SPEC_BASE_BRANCH means the repo's default
# branch — the spec is NEVER committed directly to this branch; a new
# branch is created off it and a PR opened back into it instead (see
# github-mcp-server's /propose-doc route), so this is safe to point at a
# protected branch.
OPENAPI_SPEC_PATH = os.getenv("OPENAPI_SPEC_PATH", "openapi.yaml")
OPENAPI_SPEC_BASE_BRANCH = os.getenv("OPENAPI_SPEC_BASE_BRANCH", "")

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
  <h1>🧩 Agentic OpenAPI Generation</h1>
  <p style="color:#666;">Powered by {MODEL_NAME} (external OpenAI-compatible endpoint) · GitHub MCP · OGX</p>
</div>
"""


async def _fetch_repo_structure(repo_url: str) -> dict:
    """
    Fetch a repo's file tree via the GitHub MCP server's plain REST
    GET /repo-structure route — a deterministic, non-LLM-mediated call
    used only to feed agent.detect_needs_openapi(), so that gate costs
    zero model/tool-call budget.

    Returns {"error": ...} on any failure; callers treat that as
    "detection inconclusive" and skip generation rather than guessing.
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


def _extract_repo_url(prompt: str) -> str | None:
    """Pull the first github.com URL out of a prompt (which may be a bare
    repo URL, as the Gradio UI sends, or wrapped in instruction text, as
    a CI/CD pipeline's invoke Task sends)."""
    match = _REPO_URL_RE.search(prompt)
    return match.group(0).rstrip(".,)") if match else None


async def _propose_openapi_spec(repo_url: str, content: str, job_id: str) -> dict:
    """
    Open a pull request proposing the generated OpenAPI spec back into its
    source repo, via the GitHub MCP server's POST /propose-doc route — a
    plain REST call, not an MCP tool invocation, so the (potentially
    large) document is sent exactly as generated rather than relayed
    through another model tool call.

    This NEVER commits directly to an existing branch: /propose-doc creates
    a brand-new branch off OPENAPI_SPEC_BASE_BRANCH (or the repo's default
    branch), commits the spec there, and opens a PR — safe even if the
    target branch is protected. Uses a distinct branch_prefix so this PR
    is always separate from any architecture-doc PR the doc-gen-agent
    component may also open for the same repo.

    Returns the /propose-doc response body on success, or {"error": ...}
    on any failure (bad response, network error) — callers decide how to
    surface that to their respective audiences.
    """
    payload = {
        "repo_url": repo_url,
        "path": OPENAPI_SPEC_PATH,
        "content": content,
        "commit_message": f"docs: add {OPENAPI_SPEC_PATH} (agentic-openapi-generation job {job_id})",
        "pr_title": f"docs: add {OPENAPI_SPEC_PATH}",
        "pr_body": (
            f"Auto-generated OpenAPI specification for this repository "
            f"(agentic-openapi-generation job `{job_id}`). Please review before merging."
        ),
        "branch_prefix": "agentic-openapi",
    }
    if OPENAPI_SPEC_BASE_BRANCH:
        payload["base_branch"] = OPENAPI_SPEC_BASE_BRANCH

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
                job_id, OPENAPI_SPEC_PATH, repo_url, error,
            )
            return {"error": error}
    except Exception as exc:
        logger.exception("Job %s: error proposing %s to %s", job_id, OPENAPI_SPEC_PATH, repo_url)
        return {"error": str(exc)}


async def chat(message: str, history: list[dict]):
    """
    Gradio streaming chat handler.

    The "message" is expected to be a GitHub repo URL (or contain one).
    First checks (zero model budget) whether this repo needs an OpenAPI
    spec via detect_needs_openapi(); if not, reports that and stops. If
    so, streams the agent's response chunk by chunk.

    Also yields the repo URL and the full generated text into two
    gr.State outputs (last_repo_url, last_content) so the "Propose as
    PR" button below can act on the most recent run without re-invoking
    the agent — this chat path itself never opens a PR.
    """
    if not message.strip():
        yield history + [{"role": "assistant", "content": "Please paste a GitHub repository URL (e.g. https://github.com/owner/repo)."}], "", ""
        return

    repo_url = message.strip()
    logger.info("User repo URL: %s", repo_url[:200])

    history = history + [{"role": "user", "content": message}]
    yield history, "", ""

    history = history + [{"role": "assistant", "content": "🔍 Checking whether this repo needs an OpenAPI spec…"}]
    yield history, "", ""

    structure = await _fetch_repo_structure(repo_url)
    if "error" in structure:
        history[-1] = {
            "role": "assistant",
            "content": f"⚠️ Could not fetch the repo structure: {structure['error']}",
        }
        yield history, "", ""
        return

    if not detect_needs_openapi(structure):
        history[-1] = {
            "role": "assistant",
            "content": "ℹ️ No OpenAPI spec needed (one already exists, or this doesn't look like an API service).",
        }
        yield history, "", ""
        return

    history[-1] = {"role": "assistant", "content": ""}
    response_so_far = ""

    async for chunk in run_agent(repo_url):
        response_so_far += chunk
        history[-1] = {"role": "assistant", "content": response_so_far}
        yield history, "", ""

    if response_so_far.strip().upper().startswith("NOT_AN_API"):
        reason = response_so_far.split(":", 1)[1].strip() if ":" in response_so_far else "not an HTTP API"
        logger.info("Agent bailed out for %s: %s", repo_url, reason)
        history[-1] = {"role": "assistant", "content": f"ℹ️ No OpenAPI spec generated: {reason}"}
        yield history, "", ""
        return

    # Final yield: PR-ready spec only (stream may include narration if the
    # model deviated from the fenced-block format; extract_openapi_spec
    # strips that).
    yield history, repo_url, extract_openapi_spec(response_so_far)


class InvokeRequest(BaseModel):
    """Body for POST /invoke — the prompt is typically a GitHub repo URL,
    optionally wrapped in instruction text (see agent.py's run_agent)."""

    prompt: str


async def _run_agent_in_background(prompt: str, job_id: str) -> None:
    """
    Drain the detection + agent run to completion outside of any HTTP
    request/response cycle, then propose the result back to its source
    repo as a PR. This is the CI/CD trigger path (no UI session to stream
    into), so the only observable outcomes are: the log lines below, and
    (if a repo URL could be extracted, the repo needs a spec, and
    GITHUB_TOKEN is configured with write access) a pull request against
    the source repo.
    """
    logger.info("Background agent job %s started for prompt=%s", job_id, prompt[:200])

    repo_url = _extract_repo_url(prompt)
    if not repo_url:
        logger.warning("Job %s: could not find a github.com URL in the prompt — skipping", job_id)
        return

    try:
        structure = await _fetch_repo_structure(repo_url)
        if "error" in structure:
            logger.info("Job %s: could not fetch repo structure for %s — skipping", job_id, repo_url)
            return
        if not detect_needs_openapi(structure):
            logger.info("Job %s: repo %s does not need an OpenAPI spec — skipping", job_id, repo_url)
            return

        response_text = ""
        async for chunk in run_agent(repo_url):
            response_text += chunk
        logger.info("Background agent job %s completed (%d chars generated)", job_id, len(response_text))

        spec = extract_openapi_spec(response_text)
        if not spec.strip():
            logger.warning("Job %s: agent produced no usable spec — skipping PR", job_id)
            return
        await _propose_openapi_spec(repo_url, spec, job_id)
    except Exception:
        logger.exception("Background agent job %s failed", job_id)


async def propose_pr_clicked(repo_url: str, content: str) -> str:
    """
    Click handler for the chat UI's opt-in "📤 Propose as PR" button.

    Reuses the same _propose_openapi_spec helper as the /invoke background
    path, but is only ever triggered by an explicit user click — never by
    the model — preserving the guardrail described in
    _propose_openapi_spec's and /propose-doc's docstrings.
    """
    if not repo_url or not content.strip():
        return "⚠️ Generate an OpenAPI spec for a repository first, then click **Propose as PR**."

    job_id = str(uuid.uuid4())
    logger.info("UI-triggered PR proposal job %s for %s", job_id, repo_url)
    result = await _propose_openapi_spec(repo_url, content, job_id)

    if "error" in result:
        return f"⚠️ Failed to open PR for `{repo_url}`: {result['error']}"
    return (
        f"✅ Opened [PR #{result.get('pr_number')}]({result.get('pr_url')}) on `{repo_url}` "
        f"(`{result.get('head_branch')}` → `{result.get('base_branch')}`)."
    )


def build_ui() -> gr.Blocks:
    with gr.Blocks(
        title="Agentic OpenAPI Generation",
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
            label="OpenAPI Spec Generator",
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
        # streaming or hasn't produced a usable spec yet. Drives the opt-in
        # PR button below — the chat path itself never opens a PR.
        last_repo_url = gr.State("")
        last_content = gr.State("")

        with gr.Row():
            propose_btn = gr.Button("📤 Propose as PR", scale=1, min_width=160)
            propose_status = gr.Markdown("", elem_id="propose-status")

        gr.Examples(
            examples=EXAMPLE_PROMPTS,
            inputs=txt,
            label="Try one of these repositories",
        )

        with gr.Accordion("ℹ️ About this demo", open=False):
            gr.Markdown(
                f"""
                This agent automatically generates an OpenAPI specification for any public GitHub
                repository that exposes an HTTP API — the independent OpenAPI-generation counterpart
                to the architecture-documentation agent (doc-gen-agent).

                **How it works:**
                1. You paste a GitHub repository URL
                2. The agent fetches the repo's file tree (zero model budget) and checks whether it
                   already ships an OpenAPI/Swagger spec, or looks like an HTTP API service without one
                3. If so, it queries the **GitHub MCP server** to find route/controller/handler files
                   and request/response models
                4. **{MODEL_NAME}**, served via a configurable external OpenAI-compatible endpoint,
                   synthesizes the findings into a valid OpenAPI 3.0.x specification (YAML)
                5. You can review the generated spec and click **"Propose as PR"** to open a pull
                   request against the source repo — this never happens automatically from the chat UI

                The in-cluster components (Gradio UI, OGX sidecar, GitHub MCP server) need no GPU — only
                the configured external endpoint performs inference.
                """
            )

        send_event = txt.submit(
            fn=chat, inputs=[txt, chatbot], outputs=[chatbot, last_repo_url, last_content]
        )
        send_event.then(lambda: ("", ""), outputs=[txt, propose_status])

        click_event = send_btn.click(
            fn=chat, inputs=[txt, chatbot], outputs=[chatbot, last_repo_url, last_content]
        )
        click_event.then(lambda: ("", ""), outputs=[txt, propose_status])

        propose_btn.click(
            fn=propose_pr_clicked, inputs=[last_repo_url, last_content], outputs=[propose_status]
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
    fastapi_app = FastAPI(title="OpenAPI-Gen Agent")

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
