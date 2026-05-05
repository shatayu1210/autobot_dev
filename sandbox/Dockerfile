# Sandbox — Patcher Validation Container
#
# Applies a unified diff to a read-only Airflow repo mount,
# runs the specified pytest tests, and returns the result.
#
# The repo is mounted at /workspace (read-only).
# Diffs are applied in a writable tmpfs overlay so the original is never modified.

FROM python:3.11-slim

WORKDIR /app

# Install system tools needed for applying patches and running tests
RUN apt-get update && apt-get install -y --no-install-recommends \
    git \
    patch \
    && rm -rf /var/lib/apt/lists/*

# Install Airflow test dependencies + FastAPI for the HTTP interface
# Pin to a compatible Airflow version (matches the repo checkout)
RUN pip install --no-cache-dir \
    apache-airflow==2.9.0 \
    pytest \
    pytest-timeout \
    fastapi>=0.111.0 \
    uvicorn[standard]>=0.29.0 \
    requests

COPY app.py /app/app.py

EXPOSE 5001

CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "5001"]
