"""
Doc-Gen Agent — Gradio Chat UI.

Launches a Gradio chat interface. The user pastes a GitHub repository
URL; the agent (OGX sidecar + GitHub MCP server + an external, configurable
OpenAI-compatible model endpoint) explores the repo and streams back an
architecture summary with Mermaid diagrams.
"""

from __future__ import annotations

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

from agent import MODEL_NAME, run_agent

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


async def chat(message: str, history: list[dict]) -> gr.Chatbot:
    """
    Gradio streaming chat handler.

    The "message" is expected to be a GitHub repo URL (or contain one).
    Streams the agent's architecture-documentation response back chunk
    by chunk.
    """
    if not message.strip():
        yield history + [{"role": "assistant", "content": "Please paste a GitHub repository URL (e.g. https://github.com/owner/repo)."}]
        return

    logger.info("User repo URL: %s", message.strip()[:200])

    history = history + [{"role": "user", "content": message}]
    yield history

    response_so_far = ""
    history = history + [{"role": "assistant", "content": ""}]

    async for chunk in run_agent(message.strip()):
        response_so_far += chunk
        history[-1] = {"role": "assistant", "content": response_so_far}
        yield history


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


async def _propose_architecture_doc(repo_url: str, content: str, job_id: str) -> None:
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
        else:
            logger.warning(
                "Job %s: failed to propose %s to %s: %s",
                job_id, ARCHITECTURE_DOC_PATH, repo_url, data.get("error", resp.text[:300]),
            )
    except Exception:
        logger.exception("Job %s: error proposing %s to %s", job_id, ARCHITECTURE_DOC_PATH, repo_url)


async def _run_agent_in_background(prompt: str, job_id: str) -> None:
    """
    Drain run_agent() to completion outside of any HTTP request/response
    cycle, then propose the result back to its source repo as a PR. This is
    the CI/CD trigger path (no UI session to stream into), so the only
    observable outcomes are: the log lines below, and (if a repo URL could
    be extracted and GITHUB_TOKEN is configured with write access) a new
    pull request against the source repo adding/updating ARCHITECTURE_DOC_PATH.
    """
    logger.info("Background agent job %s started for prompt=%s", job_id, prompt[:200])
    response_text = ""
    try:
        async for chunk in run_agent(prompt):
            response_text += chunk
        logger.info("Background agent job %s completed (%d chars generated)", job_id, len(response_text))
    except Exception:
        logger.exception("Background agent job %s failed", job_id)
        return

    if not response_text.strip():
        logger.warning("Job %s: agent produced no output — skipping PR", job_id)
        return

    repo_url = _extract_repo_url(prompt)
    if not repo_url:
        logger.warning("Job %s: could not find a github.com URL in the prompt — skipping PR", job_id)
        return

    await _propose_architecture_doc(repo_url, response_text, job_id)


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
                """
            )

        submit_event = txt.submit(fn=chat, inputs=[txt, chatbot], outputs=[chatbot])
        submit_event.then(lambda: "", outputs=[txt])

        send_event = send_btn.click(fn=chat, inputs=[txt, chatbot], outputs=[chatbot])
        send_event.then(lambda: "", outputs=[txt])

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
