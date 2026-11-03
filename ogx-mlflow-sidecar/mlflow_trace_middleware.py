"""ASGI middleware: continue MLflow traces from the Gradio agent into OGX."""

from __future__ import annotations

from starlette.types import ASGIApp, Receive, Scope, Send


class MlflowTraceContextMiddleware:
    """Extract W3C traceparent from incoming HTTP requests into MLflow context."""

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
        traceparent = headers.get("traceparent")
        if not traceparent:
            await self._app(scope, receive, send)
            return

        from mlflow.tracing.distributed import (
            set_tracing_context_from_http_request_headers,
        )

        with set_tracing_context_from_http_request_headers({"traceparent": traceparent}):
            await self._app(scope, receive, send)
