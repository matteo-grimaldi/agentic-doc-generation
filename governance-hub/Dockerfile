FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py models.py compliance.py ./
COPY templates ./templates
COPY static ./static

# Non-root user for OpenShift compatibility. /data is where DATABASE_PATH
# points by default (the SQLite file lives on a mounted PVC in-cluster;
# this directory also exists for local `docker run` iteration without one).
RUN useradd -u 1001 -r -g 0 -d /app -s /sbin/nologin appuser && \
    mkdir -p /data && \
    chown -R 1001:0 /app /data && \
    chmod -R g=u /app /data
USER 1001

EXPOSE 7860

ENV PYTHONUNBUFFERED=1 \
    DATABASE_PATH=/data/governance.db \
    GOVERNANCE_PORT=7860

ENTRYPOINT ["python3", "app.py"]
