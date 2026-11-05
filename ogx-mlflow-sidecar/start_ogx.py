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


def _propagation_enabled() -> bool:
    return (os.getenv("MLFLOW_OGX_TRACE_PROPAGATION") or "false").strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    )


def _resolve_app_factory(*, mlflow_enabled: bool) -> str:
    """
    Stock OGX when MLflow is off; otherwise ogx_mlflow_app (session middleware,
    plus optional W3C traceparent when MLFLOW_OGX_TRACE_PROPAGATION is on).
    """
    if not mlflow_enabled:
        return _STOCK_OGX_APP
    try:
        import ogx_mlflow_app  # noqa: F401
    except ImportError:
        logger.warning(
            "MLflow tracing is enabled but ogx_mlflow_app is missing — "
            "using stock OGX app (no session/traceparent middleware)"
        )
        return _STOCK_OGX_APP
    logger.info(
        "MLflow OGX middleware enabled (session headers always; "
        "traceparent propagation=%s)",
        _propagation_enabled(),
    )
    return "ogx_mlflow_app:create_app"


def main() -> None:
    service_name = (
        os.getenv("OGX_MLFLOW_SERVICE_NAME")
        or os.getenv("MLFLOW_EXPERIMENT")
        or "ogx-sidecar"
    ).strip()

    # Import and configure before uvicorn loads OGX (and its OpenAI client).
    from mlflow_telemetry import (
        configure_mlflow,
        install_openai_chat_completions_hooks,
    )

    # Patch chat.completions.create *before* autolog so autolog wraps our
    # hooks and they run inside the LLM span (include_usage + prompt link).
    if (os.getenv("MLFLOW_TRACKING_URI") or "").strip():
        install_openai_chat_completions_hooks()

    enabled = configure_mlflow(
        service_name=service_name,
        enable_openai_autolog=True,
    )
    if enabled and importlib.util.find_spec("openai") is not None:
        logger.info(
            "MLflow openai.autolog active — inference requests export prompts/"
            "messages to the tracking server (Chat tab in MLflow UI)"
        )
    app_factory = _resolve_app_factory(mlflow_enabled=enabled)
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
