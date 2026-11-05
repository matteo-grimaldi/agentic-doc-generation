"""
MLflow tracing helpers for OGX agent pods (Gradio UI + OGX sidecar).

Opt-in: if MLFLOW_TRACKING_URI is unset or empty, configure_mlflow() is a
no-op and trace_agent_run() returns the wrapped function unchanged — local
runs without a tracking server keep working.
"""

from __future__ import annotations

import contextlib
import functools
import importlib.util
import logging
import os
import uuid
from collections.abc import AsyncIterator, Callable, Iterator
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

# MLflow UI Session column / metadata key (mlflow.tracing.context session_id).
MLFLOW_TRACE_SESSION_META = "mlflow.trace.session"
# Gradio → OGX: shared run session for grouping AGENT + sidecar autolog traces.
MLFLOW_TRACE_SESSION_HEADER = "X-Mlflow-Trace-Session"
# Gradio → OGX: Prompt Registry name/version for the Prompt column on LLM traces.
MLFLOW_PROMPT_NAME_HEADER = "X-Mlflow-Prompt-Name"
MLFLOW_PROMPT_VERSION_HEADER = "X-Mlflow-Prompt-Version"
PROMPT_PRODUCTION_ALIAS = "production"

_CONFIGURED = False
_ENABLED = False
_mcp_tool_tracker: ContextVar["McpToolSpanTracker | None"] = ContextVar(
    "mcp_tool_tracker", default=None
)
_run_session_id: ContextVar[str | None] = ContextVar("run_session_id", default=None)
# (name, version) for Gradio headers and sidecar load_prompt under autolog spans.
_active_prompt_ref: ContextVar[tuple[str, str] | None] = ContextVar(
    "active_prompt_ref", default=None
)
_logged_missing_chat_usage = False
_logged_missing_registry_rates = False
# Cache of (input_usd_per_m, output_usd_per_m) or None when missing/unparseable.
_usd_rates_cache: tuple[float, float] | None = None
_usd_rates_cache_model: str = ""
_usd_rates_cache_at: float = 0.0
_USD_RATES_CACHE_TTL_SEC = 300.0

# Registered-model tags written by register-llm-pricing.py and read here.
REGISTRY_TAG_INPUT_USD = "input_usd_per_million"
REGISTRY_TAG_OUTPUT_USD = "output_usd_per_million"
REGISTRY_TAG_INFERENCE_MODEL = "inference_model_id"


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


def compute_llm_cost_usd(
    *,
    input_tokens: int,
    output_tokens: int,
    input_usd_per_million: float,
    output_usd_per_million: float,
) -> dict[str, float]:
    """Pure USD cost from token counts and per-1M rates (no LiteLLM)."""
    input_cost = (input_tokens / 1_000_000.0) * input_usd_per_million
    output_cost = (output_tokens / 1_000_000.0) * output_usd_per_million
    return {
        "input_cost": input_cost,
        "output_cost": output_cost,
        "total_cost": input_cost + output_cost,
    }


def registry_model_name_for(inference_model: str) -> str:
    """
    Map an OGX/OpenAI model id to an MLflow Registered Model name.

    MLflow forbids '/', '%', and ':' in registered model names; our
    cluster-config model_name uses the OGX form ``provider/model``.
    """
    name = (inference_model or "").strip()
    return name.replace("/", "--").replace("%", "_pct_").replace(":", "_")


def _parse_usd_pair(
    raw_in: Any, raw_out: Any
) -> tuple[float, float] | None:
    if raw_in is None or raw_out is None:
        return None
    try:
        return float(str(raw_in).strip()), float(str(raw_out).strip())
    except (TypeError, ValueError):
        return None


def _usd_rates_from_registry_tags(tags: dict[str, Any]) -> tuple[float, float] | None:
    return _parse_usd_pair(
        tags.get(REGISTRY_TAG_INPUT_USD),
        tags.get(REGISTRY_TAG_OUTPUT_USD),
    )


def _fetch_usd_rates_from_registry(
    inference_model: str,
) -> tuple[float, float] | None:
    """
    Read USD/1M rates from the MLflow Model Registry for ``inference_model``.

    Looks up Registered Model ``registry_model_name_for(inference_model)`` and
    reads ``input_usd_per_million`` / ``output_usd_per_million`` tags. Seed
    those tags with ``./register-llm-pricing.py``.
    """
    global _logged_missing_registry_rates

    if not inference_model:
        return None
    reg_name = registry_model_name_for(inference_model)
    if not reg_name:
        return None

    try:
        from mlflow import MlflowClient

        client = MlflowClient()
        rm = client.get_registered_model(reg_name)
        tags = dict(getattr(rm, "tags", None) or {})
        rates = _usd_rates_from_registry_tags(tags)
        if rates is not None:
            return rates
        logger.warning(
            "Registered model %r exists but is missing valid "
            "%s / %s tags — Overview cost charts stay blank until "
            "./register-llm-pricing.py is run",
            reg_name,
            REGISTRY_TAG_INPUT_USD,
            REGISTRY_TAG_OUTPUT_USD,
        )
        return None
    except Exception as exc:
        # RESOURCE_DOES_NOT_EXIST and connectivity errors both land here.
        if not _logged_missing_registry_rates:
            _logged_missing_registry_rates = True
            logger.warning(
                "No MLflow Model Registry pricing for inference model %r "
                "(registered name %r): %s — run ./register-llm-pricing.py "
                "to seed input/output USD-per-million tags",
                inference_model,
                reg_name,
                exc,
            )
        else:
            logger.debug(
                "Registry rate lookup failed for %r: %s",
                reg_name,
                exc,
                exc_info=True,
            )
        return None


def _usd_rates_for_model(
    inference_model: str | None = None,
    *,
    force_refresh: bool = False,
) -> tuple[float, float] | None:
    """Cached Model Registry USD rates for the active inference model."""
    global _usd_rates_cache, _usd_rates_cache_model, _usd_rates_cache_at

    import time

    model = (inference_model if inference_model is not None else _inference_model_name()).strip()
    now = time.monotonic()
    if (
        not force_refresh
        and _usd_rates_cache_model == model
        and (now - _usd_rates_cache_at) < _USD_RATES_CACHE_TTL_SEC
    ):
        return _usd_rates_cache

    rates = _fetch_usd_rates_from_registry(model) if model else None
    _usd_rates_cache = rates
    _usd_rates_cache_model = model
    _usd_rates_cache_at = now
    return rates


def _inference_model_name() -> str:
    return (
        os.getenv("INFERENCE_MODEL") or os.getenv("MODEL_NAME") or ""
    ).strip()


def _span_type_name(span: Any) -> str:
    try:
        attrs = getattr(span, "attributes", None) or {}
        raw = attrs.get("mlflow.spanType")
        if raw:
            return str(raw)
    except Exception:
        pass
    raw = getattr(span, "span_type", None)
    return str(raw) if raw else ""


def _normalize_usage_dict(raw: Any) -> dict[str, int] | None:
    if not isinstance(raw, dict):
        return None
    input_tokens = raw.get("input_tokens", raw.get("prompt_tokens"))
    output_tokens = raw.get("output_tokens", raw.get("completion_tokens"))
    if input_tokens is None and output_tokens is None:
        return None
    try:
        in_tok = int(input_tokens or 0)
        out_tok = int(output_tokens or 0)
    except (TypeError, ValueError):
        return None
    total = raw.get("total_tokens")
    try:
        total_tok = int(total) if total is not None else in_tok + out_tok
    except (TypeError, ValueError):
        total_tok = in_tok + out_tok
    return {
        "input_tokens": in_tok,
        "output_tokens": out_tok,
        "total_tokens": total_tok,
    }


def _extract_usage_from_span(span: Any) -> dict[str, int] | None:
    try:
        attrs = getattr(span, "attributes", None) or {}
        usage = _normalize_usage_dict(attrs.get("mlflow.chat.tokenUsage"))
        if usage:
            return usage
    except Exception:
        pass
    try:
        outputs = getattr(span, "outputs", None)
        if isinstance(outputs, dict):
            usage = _normalize_usage_dict(outputs.get("usage"))
            if usage:
                return usage
    except Exception:
        pass
    return None


def _is_llm_chat_span(span: Any) -> bool:
    span_type = _span_type_name(span).upper()
    return span_type in {"LLM", "CHAT_MODEL", "CHAT"}


def _publish_trace_level_cost(span: Any, cost: dict[str, float]) -> None:
    """
    Write ``mlflow.trace.cost`` onto the in-memory TraceInfo.

    Self-hosted / OpenShift AI MLflow only client-aggregates cost for
    Databricks and SageMaker tracking URIs (``should_compute_cost_client_side``).
    Without this metadata, span-level ``mlflow.llm.cost`` is visible on the
    trace detail view but Overview **Cost breakdown** / **Cost over time**
    stay empty (``TraceInfo.cost`` is None).
    """
    import json

    trace_id = (
        getattr(span, "request_id", None)
        or getattr(span, "trace_id", None)
        or ""
    )
    cost_payload = cost
    try:
        from mlflow.tracing.constant import TraceMetadataKey
        from mlflow.tracing.trace_manager import InMemoryTraceManager
        from mlflow.tracing.utils import aggregate_cost_from_spans

        mgr = InMemoryTraceManager.get_instance()
        if trace_id:
            with mgr.get_trace(str(trace_id)) as trace:
                if trace is not None:
                    spans = list(getattr(trace, "span_dict", {}).values())
                    aggregated = aggregate_cost_from_spans(spans)
                    if aggregated:
                        cost_payload = aggregated
            mgr.set_trace_metadata(
                str(trace_id),
                TraceMetadataKey.COST,
                json.dumps(cost_payload),
            )
            return

        # Fallback when request_id is missing: still try fluent API.
        import mlflow

        mlflow.update_current_trace(
            metadata={TraceMetadataKey.COST: json.dumps(cost_payload)}
        )
    except Exception:
        try:
            import mlflow

            mlflow.update_current_trace(
                metadata={"mlflow.trace.cost": json.dumps(cost_payload)}
            )
        except Exception:
            logger.debug(
                "Could not publish mlflow.trace.cost metadata", exc_info=True
            )


def enrich_span_usage_and_cost(span: Any) -> None:
    """
    Span processor: model identity on AGENT/LLM spans; token + cost on LLM spans.

    Cost attributes are written when the MLflow Model Registry has
    input/output USD-per-million tags for the active inference model
    (see register-llm-pricing.py). Span-level OpenInference ``llm.usage.*``
    plus trace metadata ``mlflow.trace.cost`` feed Overview cost widgets.
    """
    global _logged_missing_chat_usage

    model_name = _inference_model_name()
    span_type = _span_type_name(span).upper()
    if model_name and (
        span_type in {"AGENT", "LLM", "CHAT_MODEL", "CHAT"} or not span_type
    ):
        try:
            span.set_attribute("llm.model_name", model_name)
            span.set_attribute("llm.model_provider", "vllm")
            span.set_attribute("mlflow.llm.model", model_name)
            span.set_attribute("mlflow.llm.provider", "vllm")
        except Exception:
            logger.debug("Could not set llm.model_* on span", exc_info=True)

    usage = _extract_usage_from_span(span)
    if usage is None:
        if _is_llm_chat_span(span) and not _logged_missing_chat_usage:
            _logged_missing_chat_usage = True
            logger.warning(
                "MLflow chat/LLM span ended without token usage — stream "
                "may lack stream_options.include_usage or the endpoint did "
                "not return usage (span=%s)",
                getattr(span, "name", "?"),
            )
        return

    try:
        span.set_attribute("mlflow.chat.tokenUsage", usage)
    except Exception:
        logger.debug("Could not set mlflow.chat.tokenUsage", exc_info=True)

    if not _is_llm_chat_span(span) and span_type == "AGENT":
        # Agent root: model identity only (no cost — avoids double-count /
        # NULL_VALUE series). Token attrs may still appear if somehow present.
        return

    if not _is_llm_chat_span(span):
        return

    rates = _usd_rates_for_model(model_name)
    if rates is None:
        return

    input_rate, output_rate = rates
    cost = compute_llm_cost_usd(
        input_tokens=usage["input_tokens"],
        output_tokens=usage["output_tokens"],
        input_usd_per_million=input_rate,
        output_usd_per_million=output_rate,
    )
    try:
        span.set_attribute("mlflow.llm.cost", cost)
        span.set_attribute("llm.usage.prompt_tokens", usage["input_tokens"])
        span.set_attribute(
            "llm.usage.completion_tokens", usage["output_tokens"]
        )
        span.set_attribute("llm.usage.total_tokens", usage["total_tokens"])
        span.set_attribute("llm.usage.prompt_tokens_cost", cost["input_cost"])
        span.set_attribute(
            "llm.usage.completion_tokens_cost", cost["output_cost"]
        )
        span.set_attribute("llm.usage.total_cost", cost["total_cost"])
    except Exception:
        logger.debug("Could not set llm cost attributes", exc_info=True)
        return

    _publish_trace_level_cost(span, cost)


def _register_span_processors() -> None:
    try:
        import mlflow

        configure = getattr(getattr(mlflow, "tracing", None), "configure", None)
        if not callable(configure):
            logger.warning(
                "mlflow.tracing.configure unavailable — cost/usage span "
                "processor not registered"
            )
            return
        configure(span_processors=[enrich_span_usage_and_cost])
    except Exception:
        logger.warning(
            "Failed to register MLflow span processors", exc_info=True
        )


def configure_mlflow(*, service_name: str, enable_openai_autolog: bool = False) -> bool:
    """
    Configure MLflow tracking URI, workspace, and experiment.

    OpenAI autolog (prompts/messages for the MLflow Chat tab) is enabled only
    from ogx-mlflow-sidecar/start_ogx.py — not from Gradio app.py.

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

    _register_span_processors()

    if enable_openai_autolog and importlib.util.find_spec("openai") is not None:
        mlflow.openai.autolog(log_traces=True)
    elif enable_openai_autolog:
        logger.warning(
            "enable_openai_autolog requested but openai package missing (service=%s)",
            service_name,
        )

    rates = _usd_rates_for_model(force_refresh=True)
    logger.info(
        "MLflow tracing enabled — uri=%s workspace=%s experiment=%s "
        "service=%s token=%s model=%s registry_rates=%s",
        tracking_uri,
        workspace,
        experiment,
        service_name,
        "set" if token_set else "unset",
        _inference_model_name() or "(unset)",
        "set" if rates else "unset",
    )
    _CONFIGURED = True
    _ENABLED = True
    return True


def _propagation_enabled() -> bool:
    return (os.getenv("MLFLOW_OGX_TRACE_PROPAGATION") or "false").strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    )


def mlflow_outbound_trace_headers() -> dict[str, str]:
    """
    W3C traceparent headers for outbound HTTP calls (e.g. Gradio → OGX).

    Links sidecar openai.autolog spans under the same trace as the Gradio AGENT
    span when MLFLOW_OGX_TRACE_PROPAGATION is enabled. Prefer
    mlflow_ogx_request_headers() for Gradio → OGX (includes session id).
    """
    if not _ENABLED or not _propagation_enabled():
        return {}
    try:
        from mlflow.tracing.distributed import (
            get_tracing_context_headers_for_http_request,
        )

        return get_tracing_context_headers_for_http_request()
    except Exception:
        logger.debug("Could not build MLflow trace propagation headers", exc_info=True)
        return {}


def get_active_prompt_ref() -> tuple[str, str] | None:
    """Return (prompt_name, version) for the current run/request, if any."""
    return _active_prompt_ref.get()


def set_active_prompt_ref(name: str | None, version: str | None) -> None:
    """Set or clear the active Prompt Registry ref (middleware / agent run)."""
    if name and version:
        _active_prompt_ref.set((str(name), str(version)))
    else:
        _active_prompt_ref.set(None)


def mlflow_ogx_request_headers() -> dict[str, str]:
    """
    Headers for Gradio → OGX responses.create calls.

    Always includes X-Mlflow-Trace-Session when a run session is active so
    sidecar openai.autolog traces share the MLflow UI Session with the AGENT
    trace (even when propagation is off). Includes prompt name/version when
    ensure_and_link_agent_prompt() has run. Optionally merges W3C traceparent
    when MLFLOW_OGX_TRACE_PROPAGATION is enabled.
    """
    if not _ENABLED:
        return {}
    headers: dict[str, str] = {}
    session_id = _run_session_id.get()
    if session_id:
        headers[MLFLOW_TRACE_SESSION_HEADER] = session_id
    prompt_ref = _active_prompt_ref.get()
    if prompt_ref:
        headers[MLFLOW_PROMPT_NAME_HEADER] = prompt_ref[0]
        headers[MLFLOW_PROMPT_VERSION_HEADER] = prompt_ref[1]
    headers.update(mlflow_outbound_trace_headers())
    return headers


def _prompt_template_for_registry(instructions: str) -> str:
    """Escape Jinja delimiters so literal braces are not template variables."""
    if "{{" in instructions or "{%" in instructions:
        return "{% raw %}" + instructions + "{% endraw %}"
    return instructions


def ensure_and_link_agent_prompt(
    *, agent_name: str, instructions: str
) -> tuple[str, str] | None:
    """
    Register/load `{agent}-instructions` and link it to the active AGENT span.

    Reuses the production alias when the template is unchanged. Failures are
    logged and swallowed so tracing never breaks the agent run.
    """
    if not _ENABLED:
        return None
    name = f"{agent_name}-instructions"
    template = _prompt_template_for_registry(instructions)
    try:
        import mlflow

        existing = None
        try:
            existing = mlflow.genai.load_prompt(
                f"prompts:/{name}@{PROMPT_PRODUCTION_ALIAS}",
                allow_missing=True,
                link_to_model=False,
            )
        except TypeError:
            try:
                existing = mlflow.genai.load_prompt(
                    f"prompts:/{name}@{PROMPT_PRODUCTION_ALIAS}",
                    allow_missing=True,
                )
            except Exception:
                existing = None
        except Exception:
            existing = None

        if existing is not None and getattr(existing, "template", None) == template:
            prompt = existing
        else:
            prompt = mlflow.genai.register_prompt(
                name=name,
                template=template,
                commit_message=(
                    "Initial instructions"
                    if existing is None
                    else "Updated agent instructions"
                ),
            )
            set_alias = getattr(mlflow.genai, "set_prompt_alias", None)
            if callable(set_alias):
                set_alias(name, PROMPT_PRODUCTION_ALIAS, prompt.version)
            else:
                logger.debug(
                    "mlflow.genai.set_prompt_alias unavailable — "
                    "production alias not updated for %s",
                    name,
                )

        # load_prompt links to the active trace/span (Prompt column).
        try:
            mlflow.genai.load_prompt(
                f"prompts:/{name}/{prompt.version}",
                link_to_model=False,
            )
        except TypeError:
            mlflow.genai.load_prompt(f"prompts:/{name}/{prompt.version}")

        version = str(prompt.version)
        _active_prompt_ref.set((name, version))
        return name, version
    except Exception:
        logger.warning(
            "Failed to register/link MLflow prompt for agent=%s",
            agent_name,
            exc_info=True,
        )
        return None


def link_prompt_ref_to_active_span(name: str, version: str) -> None:
    """Load a prompt version so the active LLM span gets the Prompt column."""
    if not _ENABLED or not name or not version:
        return
    try:
        import mlflow

        try:
            mlflow.genai.load_prompt(
                f"prompts:/{name}/{version}",
                link_to_model=False,
            )
        except TypeError:
            mlflow.genai.load_prompt(f"prompts:/{name}/{version}")
    except Exception:
        logger.debug(
            "Could not link prompt %s/%s to active span",
            name,
            version,
            exc_info=True,
        )


def ensure_stream_usage_kwargs(kwargs: dict[str, Any]) -> dict[str, Any]:
    """Force stream_options.include_usage for streamed chat.completions calls."""
    if not kwargs.get("stream"):
        return kwargs
    opts = dict(kwargs.get("stream_options") or {})
    opts["include_usage"] = True
    return {**kwargs, "stream_options": opts}


def install_openai_chat_completions_hooks() -> None:
    """
    Patch OpenAI chat.completions.create before mlflow.openai.autolog().

    Autolog wraps the patched method, so our body runs inside the autolog
    span: we can force include_usage and load_prompt for the Prompt column.
    """
    if importlib.util.find_spec("openai") is None:
        logger.warning(
            "openai package missing — chat.completions hooks not installed"
        )
        return
    try:
        from openai.resources.chat.completions import AsyncCompletions, Completions
    except Exception:
        logger.warning(
            "Could not import OpenAI Completions classes — hooks skipped",
            exc_info=True,
        )
        return

    if getattr(Completions.create, "_mlflow_ogx_hooks", False):
        return

    orig_sync = Completions.create
    orig_async = AsyncCompletions.create

    @functools.wraps(orig_sync)
    def sync_create(self: Any, *args: Any, **kwargs: Any) -> Any:
        kwargs = ensure_stream_usage_kwargs(kwargs)
        prompt_ref = _active_prompt_ref.get()
        if prompt_ref:
            link_prompt_ref_to_active_span(prompt_ref[0], prompt_ref[1])
        return orig_sync(self, *args, **kwargs)

    @functools.wraps(orig_async)
    async def async_create(self: Any, *args: Any, **kwargs: Any) -> Any:
        kwargs = ensure_stream_usage_kwargs(kwargs)
        prompt_ref = _active_prompt_ref.get()
        if prompt_ref:
            link_prompt_ref_to_active_span(prompt_ref[0], prompt_ref[1])
        return await orig_async(self, *args, **kwargs)

    sync_create._mlflow_ogx_hooks = True  # type: ignore[attr-defined]
    async_create._mlflow_ogx_hooks = True  # type: ignore[attr-defined]
    Completions.create = sync_create  # type: ignore[method-assign]
    AsyncCompletions.create = async_create  # type: ignore[method-assign]
    logger.info(
        "OpenAI chat.completions hooks installed "
        "(stream include_usage + prompt link)"
    )


@contextlib.contextmanager
def _trace_session_context(session_id: str) -> Iterator[None]:
    """Apply MLflow session_id for the current agent run (AGENT + TOOL spans)."""
    import mlflow

    tracing_context = getattr(getattr(mlflow, "tracing", None), "context", None)
    if callable(tracing_context):
        with tracing_context(session_id=session_id):
            yield
        return
    # Older SDKs without mlflow.tracing.context — metadata set after span start.
    yield


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

    # Do not pass terminate=True — Gradio is long-lived; terminating the async
    # exporter breaks later runs and can drop LLM span payloads.
    try:
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
            run_session_id = str(uuid.uuid4())
            session_token = _run_session_id.set(run_session_id)
            prompt_token = _active_prompt_ref.set(None)

            span_type = "AGENT"
            try:
                from mlflow.tracing.constant import SpanType

                span_type = getattr(SpanType, "AGENT", "AGENT")
            except Exception:
                pass

            try:
                with _trace_session_context(run_session_id):
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
                                },
                                metadata={
                                    MLFLOW_TRACE_SESSION_META: run_session_id,
                                },
                            )
                        except TypeError:
                            # Older SDKs may not accept metadata= on update_current_trace.
                            try:
                                mlflow.update_current_trace(
                                    tags={
                                        "agent": agent_name,
                                        "model": model_name,
                                        "repo_url": str(repo_url or ""),
                                    }
                                )
                                mlflow.update_current_trace(
                                    metadata={
                                        MLFLOW_TRACE_SESSION_META: run_session_id,
                                    }
                                )
                            except Exception:
                                logger.debug(
                                    "Could not set trace tags/session", exc_info=True
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
                                logger.debug(
                                    "Could not set span outputs", exc_info=True
                                )
                _flush_mlflow_traces(
                    location="mlflow_telemetry.py:trace_agent_run:post_span",
                )
            finally:
                _run_session_id.reset(session_token)
                _active_prompt_ref.reset(prompt_token)

        return wrapper  # type: ignore[return-value]

    return decorator
