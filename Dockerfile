# syntax=docker/dockerfile:1

# Build and runtime share one image: the API and the synthesis worker run the
# same code and the same container, differing only in their command.

FROM python:3.12-slim AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# faiss-cpu ships manylinux wheels, but numpy's BLAS needs libgomp at runtime.
RUN apt-get update \
    && apt-get install -y --no-install-recommends libgomp1 curl \
    && rm -rf /var/lib/apt/lists/*

# Dependencies first, so a source change does not invalidate the install layer.
COPY pyproject.toml README.md ./
COPY app ./app
COPY sdk ./sdk

RUN pip install --no-cache-dir .

COPY config ./config
COPY eval ./eval
COPY scripts ./scripts

# The FAISS index lives on disk and is shared between the API and the worker,
# so it must be a volume rather than image content.
RUN mkdir -p /data/faiss

# Never run as root: the process only needs to write its own index directory.
RUN useradd --create-home --uid 10001 dream \
    && chown -R dream:dream /app /data
USER dream

ENV DREAM_FAISS_PATH=/data/faiss \
    DREAM_CONFIG_FILE=/app/config/dream.yaml

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD curl -fsS http://localhost:8000/health || exit 1

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
