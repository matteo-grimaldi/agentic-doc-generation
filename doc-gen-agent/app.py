"""
Doc-Gen Agent — Gradio Chat UI.

Launches a Gradio chat interface. The user pastes a GitHub repository
URL; the agent (OGX sidecar + GitHub MCP server + local vLLM model)
explores the repo and streams back an architecture summary with Mermaid
diagrams.
"""

from __future__ import annotations

import logging
import os

# Disable Gradio telemetry — prevents outbound calls to api.gradio.app / HuggingFace
os.environ.setdefault("GRADIO_ANALYTICS_ENABLED", "False")

import gradio as gr

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
  <p style="color:#666;">Powered by {MODEL_NAME} (vLLM on OpenShift AI) · GitHub MCP · OGX</p>
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
                3. **{MODEL_NAME}**, served locally via vLLM on OpenShift AI, synthesizes the findings into:
                   - A structured architecture document (overview, components, data flow, dependencies, deployment, security)
                   - Three Mermaid diagrams: component, data flow, and dependency diagrams

                Everything runs in-cluster — no external LLM API calls.
                """
            )

        submit_event = txt.submit(fn=chat, inputs=[txt, chatbot], outputs=[chatbot])
        submit_event.then(lambda: "", outputs=[txt])

        send_event = send_btn.click(fn=chat, inputs=[txt, chatbot], outputs=[chatbot])
        send_event.then(lambda: "", outputs=[txt])

    return demo


if __name__ == "__main__":
    port = int(os.getenv("GRADIO_PORT", "7860"))
    demo = build_ui()
    demo.launch(
        server_name="0.0.0.0",
        server_port=port,
        show_api=False,
    )
