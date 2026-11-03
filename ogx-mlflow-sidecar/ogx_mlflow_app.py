"""OGX FastAPI app factory with MLflow trace propagation middleware."""

from __future__ import annotations

from mlflow_trace_middleware import MlflowTraceContextMiddleware


def create_app():
    from ogx.core.server.server import create_app as ogx_create_app

    app = ogx_create_app()
    app.add_middleware(MlflowTraceContextMiddleware)
    return app
