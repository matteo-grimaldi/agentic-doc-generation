"""
OGX sidecar entrypoint with optional MLflow OpenAI autolog.

Configures MLflow (when MLFLOW_TRACKING_URI is set) *before* starting the
OGX uvicorn process so openai.autolog() can wrap the OpenAI SDK calls the
sidecar makes to the external inference endpoint.
"""

from __future__ import annotations

import logging
import os
import sys

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
)
logger = logging.getLogger("ogx-mlflow-sidecar")


def main() -> None:
    service_name = (
        os.getenv("OGX_MLFLOW_SERVICE_NAME")
        or os.getenv("MLFLOW_EXPERIMENT")
        or "ogx-sidecar"
    ).strip()

    # Import and configure before uvicorn loads OGX (and its OpenAI client).
    from mlflow_telemetry import configure_mlflow

    enabled = configure_mlflow(service_name=service_name)
    logger.info(
        "Starting OGX uvicorn (mlflow_tracing=%s, service=%s)",
        enabled,
        service_name,
    )

    import uvicorn

    uvicorn.run(
        "ogx.core.server.server:create_app",
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
