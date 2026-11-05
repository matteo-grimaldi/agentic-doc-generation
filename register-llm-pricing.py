#!/usr/bin/env python3
"""
Seed / update MLflow Model Registry USD rates for the demo's external LLM.

Agents do not own pricing — they read ``input_usd_per_million`` and
``output_usd_per_million`` tags from a Registered Model whose name is the
sanitized ``cluster-config.model_name`` (``/`` → ``--``). Run this once
(per rate change) against the same tracking server the agents use:

  export MLFLOW_TRACKING_URI=https://mlflow.example:8443/mlflow
  export MLFLOW_TRACKING_INSECURE_TLS=true   # if needed
  # optional: export MLFLOW_TRACKING_TOKEN=…
  ./register-llm-pricing.py \\
    --model-name external-model/qwen38-27b \\
    --input-usd-per-million 0.15 \\
    --output-usd-per-million 0.60

Or pull model_name + tracking URI from the live cluster-config ConfigMap:

  ./register-llm-pricing.py --from-cluster --port-forward \\
    --input-usd-per-million 0.15 --output-usd-per-million 0.60

``--port-forward`` is required from a laptop when ``mlflow_tracking_uri`` uses
``*.svc.cluster.local`` (pods reach that DNS; your shell does not).
"""

from __future__ import annotations

import argparse
import os
import socket
import subprocess
import sys
import time
from contextlib import contextmanager
from urllib.parse import urlparse, urlunparse


REGISTRY_TAG_INPUT_USD = "input_usd_per_million"
REGISTRY_TAG_OUTPUT_USD = "output_usd_per_million"
REGISTRY_TAG_INFERENCE_MODEL = "inference_model_id"


def registry_model_name_for(inference_model: str) -> str:
    name = (inference_model or "").strip()
    return name.replace("/", "--").replace("%", "_pct_").replace(":", "_")


def _oc_configmap_key(key: str, *, namespace: str) -> str:
    try:
        out = subprocess.check_output(
            [
                "oc",
                "-n",
                namespace,
                "get",
                "configmap",
                "cluster-config",
                "-o",
                f"jsonpath={{.data.{key}}}",
            ],
            text=True,
            stderr=subprocess.STDOUT,
        )
    except (subprocess.CalledProcessError, FileNotFoundError) as exc:
        raise SystemExit(
            f"Failed to read cluster-config.{key} via oc "
            f"(namespace={namespace}): {exc}"
        ) from exc
    value = (out or "").strip()
    if not value:
        raise SystemExit(
            f"cluster-config.{key} is empty in namespace {namespace}"
        )
    return value


def _ensure_mlflow():
    try:
        import mlflow  # noqa: F401
        from mlflow import MlflowClient  # noqa: F401
    except ImportError as exc:
        raise SystemExit(
            "mlflow is required: pip install 'mlflow>=3.10' "
            "(or use a venv that already has it)"
        ) from exc


def upsert_pricing(
    *,
    inference_model: str,
    input_usd_per_million: float,
    output_usd_per_million: float,
    description: str | None = None,
) -> str:
    from mlflow import MlflowClient
    from mlflow.exceptions import RestException

    reg_name = registry_model_name_for(inference_model)
    if not reg_name:
        raise SystemExit("--model-name must be non-empty")

    client = MlflowClient()
    tags = {
        REGISTRY_TAG_INPUT_USD: str(input_usd_per_million),
        REGISTRY_TAG_OUTPUT_USD: str(output_usd_per_million),
        REGISTRY_TAG_INFERENCE_MODEL: inference_model,
    }
    desc = description or (
        f"External OpenAI-compatible LLM pricing for {inference_model} "
        f"(USD per 1M tokens)."
    )

    try:
        client.get_registered_model(reg_name)
        created = False
    except RestException:
        client.create_registered_model(reg_name, tags=tags, description=desc)
        created = True
    except Exception:
        # Older servers may raise MlflowException without RestException.
        try:
            client.create_registered_model(
                reg_name, tags=tags, description=desc
            )
            created = True
        except Exception:
            # Race: created between get and create — fall through to tag update.
            created = False

    if not created:
        for key, value in tags.items():
            client.set_registered_model_tag(reg_name, key, value)
        if description:
            try:
                client.update_registered_model(reg_name, description=desc)
            except Exception:
                pass

    return reg_name


def _validate_tracking_uri(uri: str) -> str:
    """Reject doc placeholders and local file-store URIs before MlflowClient()."""
    u = (uri or "").strip()
    if not u or u in {"…", "...", ".."}:
        raise SystemExit(
            "MLFLOW_TRACKING_URI must be a real HTTP(S) tracking server URL, "
            "not a placeholder. Use:\n"
            "  ./register-llm-pricing.py --from-cluster ...\n"
            "or set MLFLOW_TRACKING_URI to the URI from cluster-config / "
            "the MLflow UI quickstart (often requires oc port-forward)."
        )
    if u.startswith(("./", "../")) or u == "mlruns":
        raise SystemExit(
            f"MLFLOW_TRACKING_URI={u!r} looks like a local file path. "
            "Point it at the OpenShift AI MLflow server (http(s)://…)."
        )
    if not (u.startswith("http://") or u.startswith("https://")):
        raise SystemExit(
            f"MLFLOW_TRACKING_URI must start with http:// or https:// (got {u!r}). "
            "Use --from-cluster to read mlflow_tracking_uri from cluster-config."
        )
    return u


def _parse_in_cluster_service(uri: str) -> tuple[str, str, int] | None:
    """Return (namespace, service_name, remote_port) for *.svc.cluster.local URIs."""
    parsed = urlparse(uri)
    host = (parsed.hostname or "").lower()
    if not host.endswith(".svc.cluster.local"):
        return None
    labels = host[: -len(".svc.cluster.local")].split(".")
    if len(labels) < 2:
        return None
    service, namespace = labels[0], labels[1]
    remote_port = parsed.port or (443 if parsed.scheme == "https" else 80)
    return namespace, service, remote_port


def _localhost_tracking_uri(uri: str, local_port: int) -> str:
    parsed = urlparse(uri)
    return urlunparse(
        (parsed.scheme, f"127.0.0.1:{local_port}", parsed.path, "", "", "")
    )


def _wait_for_local_port(host: str, port: int, *, timeout_sec: float = 15.0) -> None:
    deadline = time.monotonic() + timeout_sec
    while time.monotonic() < deadline:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.settimeout(0.5)
            if sock.connect_ex((host, port)) == 0:
                return
        time.sleep(0.2)
    raise SystemExit(
        f"Timed out waiting for oc port-forward on {host}:{port} "
        f"(is another forward already bound to that port?)"
    )


@contextmanager
def _oc_port_forward(
    *,
    namespace: str,
    service: str,
    remote_port: int,
    local_port: int,
):
    cmd = [
        "oc",
        "port-forward",
        "-n",
        namespace,
        f"svc/{service}",
        f"{local_port}:{remote_port}",
    ]
    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
        )
    except FileNotFoundError as exc:
        raise SystemExit(
            "oc not found — install the OpenShift CLI or run without "
            "--port-forward using a reachable MLFLOW_TRACKING_URI (Route URL)."
        ) from exc

    try:
        if proc.poll() is not None:
            err = (proc.stderr.read() if proc.stderr else "") or "unknown error"
            raise SystemExit(f"oc port-forward failed: {err.strip()}")
        _wait_for_local_port("127.0.0.1", local_port)
        print(
            f"Port-forward active: 127.0.0.1:{local_port} → "
            f"{namespace}/svc/{service}:{remote_port}",
            file=sys.stderr,
        )
        yield
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()


def _needs_port_forward(uri: str) -> bool:
    parsed = _parse_in_cluster_service(uri)
    if not parsed:
        return False
    host = urlparse(uri).hostname or ""
    try:
        socket.getaddrinfo(host, parsed[2], type=socket.SOCK_STREAM)
        return False
    except socket.gaierror:
        return True


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Register / update USD-per-million pricing tags on an MLflow "
            "Registered Model for the demo external LLM."
        )
    )
    parser.add_argument(
        "--model-name",
        default=os.getenv("MODEL_NAME") or os.getenv("INFERENCE_MODEL") or "",
        help=(
            "OGX model id (cluster-config model_name), e.g. "
            "external-model/qwen38-27b"
        ),
    )
    parser.add_argument(
        "--input-usd-per-million",
        type=float,
        required=True,
        help="USD cost per 1M input/prompt tokens",
    )
    parser.add_argument(
        "--output-usd-per-million",
        type=float,
        required=True,
        help="USD cost per 1M output/completion tokens",
    )
    parser.add_argument(
        "--tracking-uri",
        default=(os.getenv("MLFLOW_TRACKING_URI") or "").strip(),
        help="MLflow tracking URI (default: $MLFLOW_TRACKING_URI)",
    )
    parser.add_argument(
        "--workspace",
        default=(os.getenv("MLFLOW_WORKSPACE") or "doc-gen-demo").strip(),
        help="RHOAI MLflow workspace (default: doc-gen-demo)",
    )
    parser.add_argument(
        "--from-cluster",
        action="store_true",
        help=(
            "Read model_name + mlflow_tracking_uri (+ insecure TLS flag) "
            "from the live cluster-config ConfigMap via oc"
        ),
    )
    parser.add_argument(
        "--namespace",
        default="doc-gen-demo",
        help="OpenShift namespace for --from-cluster (default: doc-gen-demo)",
    )
    parser.add_argument(
        "--description",
        default="",
        help="Optional Registered Model description override",
    )
    parser.add_argument(
        "--port-forward",
        nargs="?",
        const=18443,
        type=int,
        metavar="LOCAL_PORT",
        help=(
            "Tunnel in-cluster MLflow with oc port-forward (default local "
            "port 18443). Implied when the tracking URI uses "
            "*.svc.cluster.local and that host does not resolve locally."
        ),
    )
    args = parser.parse_args(argv)

    if args.from_cluster:
        args.model_name = _oc_configmap_key(
            "model_name", namespace=args.namespace
        )
        args.tracking_uri = _oc_configmap_key(
            "mlflow_tracking_uri", namespace=args.namespace
        )
        try:
            insecure = _oc_configmap_key(
                "mlflow_tracking_insecure_tls", namespace=args.namespace
            )
        except SystemExit:
            insecure = ""
        if insecure.strip().lower() == "true":
            os.environ["MLFLOW_TRACKING_INSECURE_TLS"] = "true"

    if not args.model_name.strip():
        raise SystemExit(
            "Provide --model-name, set MODEL_NAME/INFERENCE_MODEL, "
            "or pass --from-cluster"
        )
    if not args.tracking_uri:
        raise SystemExit(
            "Provide --tracking-uri, set MLFLOW_TRACKING_URI, "
            "or pass --from-cluster"
        )
    args.tracking_uri = _validate_tracking_uri(args.tracking_uri)
    if args.input_usd_per_million < 0 or args.output_usd_per_million < 0:
        raise SystemExit("USD rates must be >= 0")

    in_cluster = _parse_in_cluster_service(args.tracking_uri)
    local_port = args.port_forward
    if local_port is None and in_cluster and _needs_port_forward(args.tracking_uri):
        local_port = 18443
        print(
            "Tracking URI is in-cluster only — enabling oc port-forward "
            f"on 127.0.0.1:{local_port}.",
            file=sys.stderr,
        )

    if local_port is not None:
        if not in_cluster:
            raise SystemExit(
                "--port-forward only applies to *.svc.cluster.local tracking URIs."
            )
        namespace, service, remote_port = in_cluster
        os.environ["MLFLOW_TRACKING_INSECURE_TLS"] = "true"
        with _oc_port_forward(
            namespace=namespace,
            service=service,
            remote_port=remote_port,
            local_port=local_port,
        ):
            return _register_with_mlflow(args, local_port=local_port)

    return _register_with_mlflow(args, local_port=None)


def _register_with_mlflow(args: argparse.Namespace, *, local_port: int | None) -> int:
    tracking_uri = args.tracking_uri
    if local_port is not None:
        tracking_uri = _localhost_tracking_uri(tracking_uri, local_port)

    if not os.getenv("MLFLOW_TRACKING_TOKEN", "").strip():
        try:
            token = subprocess.check_output(
                ["oc", "whoami", "-t"], text=True, stderr=subprocess.DEVNULL
            ).strip()
        except (subprocess.CalledProcessError, FileNotFoundError):
            token = ""
        if token:
            os.environ["MLFLOW_TRACKING_TOKEN"] = token
            print("Using MLFLOW_TRACKING_TOKEN from oc whoami -t", file=sys.stderr)

    _ensure_mlflow()
    import mlflow

    mlflow.set_tracking_uri(tracking_uri)
    set_workspace = getattr(mlflow, "set_workspace", None)
    if callable(set_workspace):
        try:
            set_workspace(args.workspace)
        except Exception as exc:
            print(
                f"warning: set_workspace({args.workspace!r}) failed: {exc}",
                file=sys.stderr,
            )
    else:
        os.environ.setdefault("MLFLOW_WORKSPACE", args.workspace)

    reg_name = upsert_pricing(
        inference_model=args.model_name.strip(),
        input_usd_per_million=args.input_usd_per_million,
        output_usd_per_million=args.output_usd_per_million,
        description=args.description.strip() or None,
    )
    print(
        f"Registered model {reg_name!r} "
        f"(inference_model_id={args.model_name.strip()!r})\n"
        f"  {REGISTRY_TAG_INPUT_USD}={args.input_usd_per_million}\n"
        f"  {REGISTRY_TAG_OUTPUT_USD}={args.output_usd_per_million}\n"
        f"Agents pick this up within ~5 minutes (or on next pod restart)."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
