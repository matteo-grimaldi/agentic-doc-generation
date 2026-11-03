# MLflow-instrumented wrapper around the stock OGX distribution image.
# Adds mlflow.openai.autolog() before uvicorn so external-model OpenAI
# calls from the sidecar are captured as MLflow traces.
FROM ogxai/distribution-starter:latest

USER root

COPY requirements.txt /tmp/ogx-mlflow-requirements.txt
RUN pip install --no-cache-dir -r /tmp/ogx-mlflow-requirements.txt \
    && rm -f /tmp/ogx-mlflow-requirements.txt

WORKDIR /app/ogx-mlflow
COPY mlflow_telemetry.py mlflow_trace_middleware.py ogx_mlflow_app.py start_ogx.py ./

# Match agent images: non-root UID 1001, OpenShift-compatible group bits.
RUN chown -R 1001:0 /app/ogx-mlflow && chmod -R g=u /app/ogx-mlflow
USER 1001

ENV PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app/ogx-mlflow

EXPOSE 8321

ENTRYPOINT ["python3", "/app/ogx-mlflow/start_ogx.py"]
