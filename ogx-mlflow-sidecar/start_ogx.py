"""
OGX sidecar entrypoint with optional MLflow OpenAI autolog.

Configures MLflow (when MLFLOW_TRACKING_URI is set) *before* starting the
OGX uvicorn process so openai.autolog() can wrap the OpenAI SDK calls the
sidecar makes to the external inference endpoint.
"""

from __future__ import annotations

import importlib.util
import logging
import os
import sys

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
)
logger = logging.getLogger("ogx-mlflow-sidecar")

_STOCK_OGX_APP = "ogx.core.server.server:create_app"


def _resolve_app_factory() -> str:
    """Stock OGX app by default; optional trace-propagation middleware."""
    propagate = (
        os.getenv("MLFLOW_OGX_TRACE_PROPAGATION") or "false"
    ).strip().lower() in ("1", "true", "yes", "on")
    if not propagate:
        return _STOCK_OGX_APP
    try:
        import ogx_mlflow_app  # noqa: F401
    except ImportError:
        logger.warning(
            "MLFLOW_OGX_TRACE_PROPAGATION is enabled but ogx_mlflow_app is "
            "missing — using stock OGX app (LLM Chat spans export separately)"
        )
        return _STOCK_OGX_APP
    logger.info("MLflow trace propagation middleware enabled on OGX HTTP")
    return "ogx_mlflow_app:create_app"


def main() -> None:
    service_name = (
        os.getenv("OGX_MLFLOW_SERVICE_NAME")
        or os.getenv("MLFLOW_EXPERIMENT")
        or "ogx-sidecar"
    ).strip()

    # Import and configure before uvicorn loads OGX (and its OpenAI client).
    from mlflow_telemetry import configure_mlflow

    enabled = configure_mlflow(
        service_name=service_name,
        enable_openai_autolog=True,
    )
    if enabled and importlib.util.find_spec("openai") is not None:
        logger.info(
            "MLflow openai.autolog active — inference requests export prompts/"
            "messages to the tracking server (Chat tab in MLflow UI)"
        )
    app_factory = _resolve_app_factory()
    logger.info(
        "Starting OGX uvicorn (mlflow_tracing=%s, service=%s, app=%s)",
        enabled,
        service_name,
        app_factory,
    )

    import uvicorn

    uvicorn.run(
        app_factory,
        host="0.0.0.0",
        port=int(os.getenv("OGX_PORT", "8321")),
        factory=True,
        log_level=os.getenv("OGX_UVICORN_LOG_LEVEL", "info"),
    )


if __name__ == "__main__":
    try:
        main()
    except Exception:
        logger.exception("OGX sidecar failed to start")
        sys.exit(1)
