"""
Compliance-Scan Agent — Gradio Chat UI.

Launches a Gradio chat interface. The user pastes a GitHub repository
URL; the agent (OGX sidecar + GitHub MCP server + an external, configurable
OpenAI-compatible model endpoint) scans the repo's K8s/OpenShift manifests,
project structure, and pom.xml against separately-maintained guideline
Markdown files, and streams back a structured compliance report.

Independent counterpart to doc-gen-agent's and openapi-gen-agent's app.py
— its own Gradio UI, its own /invoke endpoint for CI/CD or Governance Hub
triggers. Unlike those two agents, this one never opens a PR: it only
reports findings back to the Governance Hub (POST /api/runs) for a human
to act on from the dashboard.
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

from agent import MODEL_NAME, extract_compliance_report, run_agent

# Base URL of the GitHub MCP server's plain REST API (not its /mcp MCP
# endpoint) — used only by _fetch_repo_structure (zero-model-budget
# pre-classification). Keep separate from agent.py's GITHUB_MCP_URL
# (which points OGX's MCP tool at the /mcp path): one is an MCP tool
# endpoint for the model, this is a deterministic server-to-server REST
# call that never goes through the model.
GITHUB_MCP_SERVER_URL = os.getenv("GITHUB_MCP_SERVER_URL", "http://github-mcp-server.doc-gen-demo.svc.cluster.local:8080")

# Base URL of the Governance Hub component's POST /api/runs callback (see
# governance-hub/app.py) — reporting here is entirely optional: leave this
# empty to run compliance-scan-agent standalone with no governance
# tracking at all. Never affects this agent's own run outcome (see
# _report_to_governance's docstring). This agent's report text — the
# whole point of running it from the Governance Hub UI — is only ever
# visible there if this URL is set.
GOVERNANCE_HUB_URL = os.getenv("GOVERNANCE_HUB_URL", "http://governance-hub.doc-gen-demo.svc.cluster.local:7860")

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
  <h1>🛡️ Agentic Compliance Scan</h1>
  <p style="color:#666;">Powered by {MODEL_NAME} (external OpenAI-compatible endpoint) · GitHub MCP · OGX</p>
</div>
"""


async def _fetch_repo_structure(repo_url: str) -> dict:
    """
    Fetch a repo's file tree via the GitHub MCP server's plain REST
    GET /repo-structure route — a deterministic, non-LLM-mediated call
    used only to feed agent.run_agent()'s pre-classification, so that
    costs zero model/tool-call budget.

    Returns {"error": ...} on any failure; callers fall back to letting
    the model call get_repo_structure itself (see agent._build_user_message).
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
    repo URL, as the Gradio UI and the Governance Hub's remediate action
    send, or wrapped in instruction text, as a CI/CD pipeline's invoke
    Task might send)."""
    match = _REPO_URL_RE.search(prompt)
    return match.group(0).rstrip(".,)") if match else None


async def _report_to_governance(
    job_id: str,
    repo_url: str,
    status: str,
    report: str | None = None,
    error: str | None = None,
) -> None:
    """
    Fire-and-forget report of this run's outcome to the Governance Hub's
    POST /api/runs callback (see governance-hub/app.py), so its dashboard
    can show this run in its history and — unlike doc-gen-agent's and
    openapi-gen-agent's PR-link-only reports — render the full compliance
    report text on the repo detail page. Optional: if GOVERNANCE_HUB_URL
    is empty or the Hub is unreachable, this logs a warning and never
    raises, so a Governance Hub outage can never affect this agent's own
    run.
    """
    if not GOVERNANCE_HUB_URL:
        return
    payload = {
        "job_id": job_id,
        "repo_url": repo_url,
        "agent_type": "compliance-scan",
        "status": status,
        "report": report,
        "error": error,
    }
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(10.0)) as client:
            await client.post(f"{GOVERNANCE_HUB_URL}/api/runs", json=payload)
    except Exception as exc:
        logger.warning("Job %s: failed to report run to governance hub: %s", job_id, exc)


async def chat(message: str, history: list[dict]):
    """
    Gradio streaming chat handler.

    The "message" is expected to be a GitHub repo URL (or contain one).
    Streams the agent's compliance-report response back chunk by chunk.

    Every terminal outcome is reported to the Governance Hub via
    _report_to_governance, same as the /invoke background path
    (_run_agent_in_background), so a run started directly from this chat
    UI shows up on the dashboard too. Each outcome gets its own job_id
    since (unlike /invoke) there is no job_id assigned up front for an
    interactive chat turn.
    """
    if not message.strip():
        yield history + [{"role": "assistant", "content": "Please paste a GitHub repository URL (e.g. https://github.com/owner/repo)."}]
        return

    repo_url = message.strip()
    job_id = str(uuid.uuid4())
    logger.info("Job %s: user repo URL: %s", job_id, repo_url[:200])

    history = history + [{"role": "user", "content": message}]
    yield history

    history = history + [{"role": "assistant", "content": "🔍 Fetching repo structure…"}]
    yield history

    structure = await _fetch_repo_structure(repo_url)
    if "error" in structure:
        history[-1] = {
            "role": "assistant",
            "content": f"⚠️ Could not fetch the repo structure: {structure['error']}",
        }
        await _report_to_governance(
            job_id, repo_url, "failed", error=f"could not fetch repo structure: {structure['error']}"
        )
        yield history
        return

    history[-1] = {"role": "assistant", "content": ""}
    response_so_far = ""

    async for chunk in run_agent(repo_url, repo_structure=structure):
        response_so_far += chunk
        history[-1] = {"role": "assistant", "content": response_so_far}
        yield history

    report = extract_compliance_report(response_so_far)
    if not report.strip():
        logger.warning("Job %s: agent produced no usable report", job_id)
        await _report_to_governance(job_id, repo_url, "failed", error="agent produced no usable report")
    else:
        await _report_to_governance(job_id, repo_url, "completed", report=report)

    yield history


class InvokeRequest(BaseModel):
    """Body for POST /invoke — the prompt is typically a GitHub repo URL,
    optionally wrapped in instruction text."""

    prompt: str


async def _run_agent_in_background(prompt: str, job_id: str) -> None:
    """
    Drain run_agent() to completion outside of any HTTP request/response
    cycle. This is the trigger path used by both CI/CD and the Governance
    Hub's "Run Compliance Scan" button (no UI session to stream into), so
    the only observable outcome is a run report sent to the Governance
    Hub (if GOVERNANCE_HUB_URL is configured) containing the full
    compliance report text — this agent never opens a PR.
    """
    logger.info("Background agent job %s started for prompt=%s", job_id, prompt[:200])

    repo_url = _extract_repo_url(prompt)
    if not repo_url:
        logger.warning("Job %s: could not find a github.com URL in the prompt — skipping", job_id)
        return

    try:
        structure = await _fetch_repo_structure(repo_url)
        if "error" in structure:
            logger.info("Job %s: could not fetch repo structure for %s, falling back to model discovery", job_id, repo_url)
            structure = None

        response_text = ""
        async for chunk in run_agent(repo_url, repo_structure=structure):
            response_text += chunk
        logger.info("Background agent job %s completed (%d chars generated)", job_id, len(response_text))

        report = extract_compliance_report(response_text)
        if not report.strip():
            logger.warning("Job %s: agent produced no usable report", job_id)
            await _report_to_governance(job_id, repo_url, "failed", error="agent produced no usable report")
        else:
            await _report_to_governance(job_id, repo_url, "completed", report=report)
    except Exception as exc:
        logger.exception("Background agent job %s failed", job_id)
        await _report_to_governance(job_id, repo_url, "failed", error=str(exc))


def build_ui() -> gr.Blocks:
    with gr.Blocks(
        title="Agentic Compliance Scan",
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
            label="Compliance Scanner",
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
            send_btn = gr.Button("Scan", variant="primary", scale=1, min_width=100)

        gr.Examples(
            examples=EXAMPLE_PROMPTS,
            inputs=txt,
            label="Try one of these repositories",
        )

        with gr.Accordion("ℹ️ About this demo", open=False):
            gr.Markdown(
                f"""
                This agent scans any public GitHub repository for architectural bad practices — a
                third, independent agent alongside doc-gen-agent and openapi-gen-agent, scoped to
                reporting rather than generating/fixing content.

                **How it works:**
                1. You paste a GitHub repository URL
                2. The agent fetches the repo's file tree (zero model budget) and classifies files
                   relevant to three guideline categories: K8s/OpenShift manifests, project
                   structure, and pom.xml
                3. It queries the **GitHub MCP server** for just those files' outlines/content
                4. **{MODEL_NAME}**, served via a configurable external OpenAI-compatible endpoint,
                   checks what it found against each guideline and produces a structured compliance
                   report (pass/fail per check, with severity and remediation hints)
                5. Unlike doc-gen-agent and openapi-gen-agent, this agent never opens a pull
                   request — it only reports findings, which show up on the **Governance Hub**
                   dashboard when triggered from there

                The in-cluster components (Gradio UI, OGX sidecar, GitHub MCP server) need no GPU —
                only the configured external endpoint performs inference.
                """
            )

        txt.submit(fn=chat, inputs=[txt, chatbot], outputs=[chatbot]).then(lambda: "", outputs=[txt])
        send_btn.click(fn=chat, inputs=[txt, chatbot], outputs=[chatbot]).then(lambda: "", outputs=[txt])

    return demo


def build_app() -> FastAPI:
    """
    Build the FastAPI app that serves both the Gradio chat UI (mounted at
    "/") and a plain REST API for programmatic callers — notably
    POST /invoke, which triggers an agent run without blocking the caller
    on its full (potentially multi-minute) execution. This is what lets
    the Governance Hub's "Run Compliance Scan" button, or a CI/CD
    pipeline, fire the agent and move on immediately instead of waiting
    for the response.
    """
    fastapi_app = FastAPI(title="Compliance-Scan Agent")

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
        # caller (e.g. the Governance Hub's "Fix it"-style button, or a
        # CI/CD pipeline step) is never blocked on it.
        return {"status": "accepted", "job_id": job_id}

    demo = build_ui()
    gr.mount_gradio_app(fastapi_app, demo, path="/")
    return fastapi_app


if __name__ == "__main__":
    port = int(os.getenv("GRADIO_PORT", "7860"))
    uvicorn.run(build_app(), host="0.0.0.0", port=port)
