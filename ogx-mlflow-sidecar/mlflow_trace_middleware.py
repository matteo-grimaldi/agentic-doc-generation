"""ASGI middleware: MLflow session + optional W3C trace context into OGX."""

from __future__ import annotations

import contextlib
import os

from starlette.types import ASGIApp, Receive, Scope, Send

# Keep in sync with mlflow_telemetry header constants (lowercase on wire).
_SESSION_HEADER = "x-mlflow-trace-session"
_PROMPT_NAME_HEADER = "x-mlflow-prompt-name"
_PROMPT_VERSION_HEADER = "x-mlflow-prompt-version"


def _propagation_enabled() -> bool:
    return (os.getenv("MLFLOW_OGX_TRACE_PROPAGATION") or "false").strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    )


class MlflowTraceContextMiddleware:
    """
    Apply Gradio → OGX MLflow context on each HTTP request.

    - X-Mlflow-Trace-Session: always (groups AGENT + openai.autolog traces).
    - X-Mlflow-Prompt-Name / Version: link Prompt Registry on LLM spans.
    - traceparent: only when MLFLOW_OGX_TRACE_PROPAGATION is enabled.
    """

    def __init__(self, app: ASGIApp) -> None:
        self._app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return

        raw_headers = scope.get("headers") or []
        headers = {
            name.decode("latin-1").lower(): value.decode("latin-1")
            for name, value in raw_headers
        }
        session_id = (headers.get(_SESSION_HEADER) or "").strip()
        prompt_name = (headers.get(_PROMPT_NAME_HEADER) or "").strip()
        prompt_version = (headers.get(_PROMPT_VERSION_HEADER) or "").strip()
        traceparent = (headers.get("traceparent") or "").strip()
        use_propagation = bool(traceparent) and _propagation_enabled()

        if not session_id and not use_propagation and not (
            prompt_name and prompt_version
        ):
            await self._app(scope, receive, send)
            return

        from mlflow_telemetry import set_active_prompt_ref

        with contextlib.ExitStack() as stack:
            if session_id:
                import mlflow

                tracing_context = getattr(
                    getattr(mlflow, "tracing", None), "context", None
                )
                if callable(tracing_context):
                    stack.enter_context(tracing_context(session_id=session_id))
            if use_propagation:
                from mlflow.tracing.distributed import (
                    set_tracing_context_from_http_request_headers,
                )

                stack.enter_context(
                    set_tracing_context_from_http_request_headers(
                        {"traceparent": traceparent}
                    )
                )
            if prompt_name and prompt_version:
                set_active_prompt_ref(prompt_name, prompt_version)
            else:
                set_active_prompt_ref(None, None)
            try:
                await self._app(scope, receive, send)
            finally:
                set_active_prompt_ref(None, None)
