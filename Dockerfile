# text-query's prototype server, as an unprivileged image with its DuckDB extension baked in,
# so the pod can run non-root on a read-only root filesystem (as geo-query does since the NRP
# incident of 2026-11-06). Nothing is cloned or installed at pod start.
FROM python:3.12-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY prototype/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Numeric uid and gid 10001, matching the manifests, so Kubernetes can check runAsNonRoot.
RUN groupadd --system --gid 10001 app \
    && useradd --system --uid 10001 --gid 10001 --create-home --home-dir /home/app app
ENV HOME=/home/app PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
USER 10001

# Installed as the app user, into /home/app/.duckdb/extensions, where LOAD finds it.
RUN python -c "import duckdb; duckdb.connect().sql('INSTALL httpfs')"

COPY prototype/*.py /app/

ARG GIT_SHA=unknown
ENV GIT_SHA=$GIT_SHA
EXPOSE 8000
CMD ["python", "server.py"]
