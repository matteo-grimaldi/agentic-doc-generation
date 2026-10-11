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
import uuid

# Disable Gradio telemetry — prevents outbound calls to api.gradio.app / HuggingFace
os.environ.setdefault("GRADIO_ANALYTICS_ENABLED", "False")

import gradio as gr
import uvicorn
from fastapi import BackgroundTasks, FastAPI, HTTPException
from pydantic import BaseModel

from agent import MODEL_NAME, run_agent

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


async def _run_agent_in_background(prompt: str, job_id: str) -> None:
    """
    Drain run_agent() to completion outside of any HTTP request/response
    cycle. This is the CI/CD trigger path (no UI session to stream into),
    so chunks are only logged — the point of this endpoint is to kick off
    doc generation asynchronously, not to return its result synchronously.
    """
    logger.info("Background agent job %s started for prompt=%s", job_id, prompt[:200])
    response_chars = 0
    try:
        async for chunk in run_agent(prompt):
            response_chars += len(chunk)
        logger.info("Background agent job %s completed (%d chars generated)", job_id, response_chars)
    except Exception:
        logger.exception("Background agent job %s failed", job_id)


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
            show_copy_button=True,
            render_markdown=True,
            type="messages",
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
