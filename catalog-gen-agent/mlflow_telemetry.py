"""
MLflow tracing helpers for OGX agent pods (Gradio UI + OGX sidecar).

Opt-in: if MLFLOW_TRACKING_URI is unset or empty, configure_mlflow() is a
no-op and trace_agent_run() returns the wrapped function unchanged — local
runs without a tracking server keep working.
"""

from __future__ import annotations

import functools
import importlib.util
import logging
import os
from collections.abc import AsyncIterator, Callable
from contextvars import ContextVar
from typing import Any, TypeVar

logger = logging.getLogger(__name__)

# #region agent log
def _agent_debug_log(
    hypothesis_id: str, location: str, message: str, data: dict[str, Any]
) -> None:
    import json
    import time
    from urllib.parse import urlparse

    safe_data = dict(data)
    if "tracking_uri" in safe_data:
        parsed = urlparse(str(safe_data["tracking_uri"]))
        safe_data["uri_scheme"] = parsed.scheme
        safe_data["uri_port"] = parsed.port
        safe_data["uri_host"] = parsed.hostname
    payload = {
        "sessionId": "9d0799",
        "hypothesisId": hypothesis_id,
        "location": location,
        "message": message,
        "data": safe_data,
        "timestamp": int(time.time() * 1000),
    }
    line = json.dumps(payload, default=str) + "\n"
    for path in (
        os.getenv("CURSOR_DEBUG_LOG_PATH", ""),
        "/Users/mgrimald/projects/agentic-doc-generation/.cursor/debug-9d0799.log",
    ):
        if not path:
            continue
        try:
            with open(path, "a", encoding="utf-8") as fh:
                fh.write(line)
            break
        except OSError:
            pass
    logger.info("agent_debug %s", line.strip())


# #endregion

F = TypeVar("F", bound=Callable[..., Any])

_CONFIGURED = False
_ENABLED = False
_mcp_tool_tracker: ContextVar["McpToolSpanTracker | None"] = ContextVar(
    "mcp_tool_tracker", default=None
)


def _apply_mlflow_tls_env(*, tracking_uri: str) -> str:
    """
    Ensure MLFLOW_TRACKING_INSECURE_TLS is set before importing mlflow.

    OpenShift AI in-cluster tracking URIs often use https://…svc.cluster.local
    with a service certificate that fails default TLS verification.
    """
    explicit = (os.getenv("MLFLOW_TRACKING_INSECURE_TLS") or "").strip().lower()
    if explicit == "true":
        os.environ["MLFLOW_TRACKING_INSECURE_TLS"] = "true"
        return "true"
    if explicit == "false":
        return "false"
    from urllib.parse import urlparse

    host = urlparse(tracking_uri).hostname or ""
    if tracking_uri.lower().startswith("https://") and host.endswith(
        ".svc.cluster.local"
    ):
        os.environ["MLFLOW_TRACKING_INSECURE_TLS"] = "true"
        logger.info(
            "Set MLFLOW_TRACKING_INSECURE_TLS=true for in-cluster HTTPS "
            "tracking URI (host=%s)",
            host,
        )
        return "true"
    return explicit or "(unset)"


def configure_mlflow(*, service_name: str) -> bool:
    """
    Configure MLflow tracking URI, workspace, experiment, and OpenAI autolog.

    Returns True when tracing is enabled, False when skipped (no URI).
    Safe to call more than once; subsequent calls are no-ops.
    """
    global _CONFIGURED, _ENABLED
    if _CONFIGURED:
        return _ENABLED

    tracking_uri = (os.getenv("MLFLOW_TRACKING_URI") or "").strip()
    if not tracking_uri:
        logger.info(
            "MLflow tracing disabled — MLFLOW_TRACKING_URI is unset/empty "
            "(service=%s)",
            service_name,
        )
        _CONFIGURED = True
        _ENABLED = False
        return False

    workspace = (os.getenv("MLFLOW_WORKSPACE") or "doc-gen-demo").strip()
    experiment = (os.getenv("MLFLOW_EXPERIMENT") or service_name).strip()
    token_set = bool((os.getenv("MLFLOW_TRACKING_TOKEN") or "").strip())
    insecure_tls = _apply_mlflow_tls_env(tracking_uri=tracking_uri)

    # #region agent log
    _agent_debug_log(
        "H1-H3",
        "mlflow_telemetry.py:configure_mlflow:pre_init",
        "MLflow configure entry",
        {
            "tracking_uri": tracking_uri,
            "insecure_tls_env": insecure_tls,
            "token_set": token_set,
            "workspace": workspace,
            "experiment": experiment,
        },
    )
    # #endregion

    try:
        import mlflow
    except ImportError:
        logger.warning(
            "MLflow package not installed — tracing disabled (service=%s)",
            service_name,
        )
        _CONFIGURED = True
        _ENABLED = False
        return False

    mlflow.set_tracking_uri(tracking_uri)
    set_workspace = getattr(mlflow, "set_workspace", None)
    if callable(set_workspace):
        try:
            set_workspace(workspace)
            # #region agent log
            _agent_debug_log(
                "H4",
                "mlflow_telemetry.py:configure_mlflow:set_workspace",
                "set_workspace succeeded",
                {"workspace": workspace},
            )
            # #endregion
        except Exception as exc:
            # #region agent log
            _agent_debug_log(
                "H4",
                "mlflow_telemetry.py:configure_mlflow:set_workspace",
                "set_workspace failed",
                {
                    "workspace": workspace,
                    "exc_type": type(exc).__name__,
                    "exc_msg": str(exc)[:500],
                },
            )
            # #endregion
            raise
    else:
        # Older SDKs: workspace is still honored via MLFLOW_WORKSPACE env
        # (X-Mlflow-Workspace header) when the server has workspaces enabled.
        os.environ.setdefault("MLFLOW_WORKSPACE", workspace)
        logger.warning(
            "mlflow.set_workspace unavailable in this SDK version — "
            "relying on MLFLOW_WORKSPACE=%s",
            workspace,
        )

    try:
        mlflow.set_experiment(experiment)
        # #region agent log
        _agent_debug_log(
            "H4",
            "mlflow_telemetry.py:configure_mlflow:set_experiment",
            "set_experiment succeeded",
            {"experiment": experiment},
        )
        # #endregion
    except Exception as exc:
        # #region agent log
        _agent_debug_log(
            "H4",
            "mlflow_telemetry.py:configure_mlflow:set_experiment",
            "set_experiment failed",
            {
                "experiment": experiment,
                "exc_type": type(exc).__name__,
                "exc_msg": str(exc)[:500],
            },
        )
        # #endregion
        raise
    # Gradio agent images use ogx-client, not the OpenAI SDK — autolog belongs
    # on the OGX sidecar that performs responses.create() to the model endpoint.
    if importlib.util.find_spec("openai") is not None:
        mlflow.openai.autolog()
    else:
        logger.info(
            "Skipping mlflow.openai.autolog() — openai not installed "
            "(service=%s); use sidecar autolog for model HTTP spans",
            service_name,
        )

    logger.info(
        "MLflow tracing enabled — uri=%s workspace=%s experiment=%s "
        "service=%s token=%s",
        tracking_uri,
        workspace,
        experiment,
        service_name,
        "set" if token_set else "unset",
    )
    _CONFIGURED = True
    _ENABLED = True
    return True


def mlflow_outbound_trace_headers() -> dict[str, str]:
    """
    W3C traceparent headers for outbound HTTP calls (e.g. Gradio → OGX).

    Must be called while an active MLflow span exists (inside trace_agent_run).
    Opt-in until the OGX sidecar image includes trace-context middleware.
    """
    if not _ENABLED:
        return {}
    if (os.getenv("MLFLOW_OGX_TRACE_PROPAGATION") or "").strip().lower() != "true":
        return {}
    try:
        from mlflow.tracing.distributed import (
            get_tracing_context_headers_for_http_request,
        )

        return get_tracing_context_headers_for_http_request()
    except Exception:
        logger.debug("Could not build MLflow trace propagation headers", exc_info=True)
        return {}


def get_mcp_tool_span_tracker() -> "McpToolSpanTracker":
    """Per-run tracker; closed automatically at end of trace_agent_run."""
    tracker = _mcp_tool_tracker.get()
    if tracker is None:
        tracker = McpToolSpanTracker()
        _mcp_tool_tracker.set(tracker)
    return tracker


def _mcp_item_call_id(item: Any) -> str | None:
    raw = getattr(item, "id", None)
    if raw is None:
        return None
    return str(raw)


def _flush_mlflow_traces(*, location: str) -> None:
    if not _ENABLED:
        return
    import mlflow

    try:
        mlflow.flush_trace_async_logging(terminate=True)
    except TypeError:
        mlflow.flush_trace_async_logging()
    except Exception:
        logger.debug("MLflow trace flush failed", exc_info=True)
        return
    trace_id = None
    try:
        trace_id = mlflow.get_last_active_trace_id()
    except Exception:
        pass
    # #region agent log
    _agent_debug_log(
        "T1",
        location,
        "MLflow trace flush after agent run",
        {"last_active_trace_id": trace_id or "(none)"},
    )
    # #endregion


def _tool_span_type() -> str:
    try:
        from mlflow.tracing.constant import SpanType

        return getattr(SpanType, "TOOL", "TOOL")
    except Exception:
        return "TOOL"


class McpToolSpanTracker:
    """Record MCP tool calls as child spans on the current agent trace."""

    def __init__(self) -> None:
        self._open: dict[str, Any] = {}

    def on_mcp_call_added(self, item: Any) -> None:
        if not _ENABLED or item is None:
            return
        if getattr(item, "type", None) != "mcp_call":
            return
        import mlflow

        call_id = _mcp_item_call_id(item)
        if not call_id or call_id in self._open:
            return
        server = getattr(item, "server_label", "unknown")
        tool = getattr(item, "name", "unknown")
        cm = mlflow.start_span(
            name=f"mcp/{server}/{tool}",
            span_type=_tool_span_type(),
        )
        span = cm.__enter__()
        try:
            span.set_inputs({"server": server, "tool": tool, "call_id": call_id})
        except Exception:
            logger.debug("Could not set MCP span inputs", exc_info=True)
        self._open[call_id] = (cm, span)

    def on_mcp_call_done(self, item: Any) -> None:
        if not _ENABLED or item is None:
            return
        if getattr(item, "type", None) != "mcp_call":
            return
        call_id = _mcp_item_call_id(item)
        if not call_id:
            return
        entry = self._open.pop(call_id, None)
        if entry is None:
            return
        cm, span = entry
        error = getattr(item, "error", None)
        try:
            span.set_outputs(
                {
                    "error": str(error) if error else None,
                    "status": "failed" if error else "ok",
                }
            )
        except Exception:
            logger.debug("Could not set MCP span outputs", exc_info=True)
        cm.__exit__(None, None, None)

    def close_all(self) -> None:
        while self._open:
            call_id, (cm, span) = self._open.popitem()
            try:
                span.set_outputs({"status": "closed_without_done_event"})
            except Exception:
                pass
            try:
                cm.__exit__(None, None, None)
            except Exception:
                logger.debug(
                    "Could not close MCP span call_id=%s", call_id, exc_info=True
                )


def trace_agent_run(
    *,
    name: str,
    agent_name: str,
) -> Callable[[F], F]:
    """
    Decorator for async generator run_agent() functions.

    When MLflow is enabled, wraps the run in an mlflow.trace span and sets
    tags for agent / model / repo_url. When disabled, returns fn unchanged.
    """

    def decorator(fn: F) -> F:
        @functools.wraps(fn)
        async def wrapper(*args: Any, **kwargs: Any) -> AsyncIterator[str]:
            if not _ENABLED:
                async for chunk in fn(*args, **kwargs):
                    yield chunk
                return

            import mlflow

            repo_url = kwargs.get("repo_url")
            if repo_url is None and args:
                repo_url = args[0]
            model_name = os.getenv("MODEL_NAME", "")

            span_type = "AGENT"
            try:
                from mlflow.tracing.constant import SpanType

                span_type = getattr(SpanType, "AGENT", "AGENT")
            except Exception:
                pass

            with mlflow.start_span(name=name, span_type=span_type) as span:
                try:
                    span.set_inputs({"repo_url": repo_url})
                except Exception:
                    logger.debug("Could not set span inputs", exc_info=True)
                try:
                    mlflow.update_current_trace(
                        tags={
                            "agent": agent_name,
                            "model": model_name,
                            "repo_url": str(repo_url or ""),
                        }
                    )
                except Exception:
                    logger.debug("Could not set trace tags", exc_info=True)

                text_bytes = 0
                tool_calls = 0
                error: str | None = None
                try:
                    async for chunk in fn(*args, **kwargs):
                        text_bytes += len(chunk)
                        if "Calling **" in chunk:
                            tool_calls += 1
                        if chunk.startswith("\n\n⚠️ **Error:**"):
                            error = chunk.strip()[:500]
                        yield chunk
                except Exception as exc:
                    error = str(exc)
                    raise
                finally:
                    tracker = _mcp_tool_tracker.get()
                    if tracker is not None:
                        tracker.close_all()
                        _mcp_tool_tracker.set(None)
                    try:
                        span.set_outputs(
                            {
                                "text_bytes": text_bytes,
                                "tool_call_status_lines": tool_calls,
                                "error": error,
                            }
                        )
                    except Exception:
                        logger.debug("Could not set span outputs", exc_info=True)
            _flush_mlflow_traces(
                location="mlflow_telemetry.py:trace_agent_run:post_span",
            )

        return wrapper  # type: ignore[return-value]

    return decorator
