# The product API (issue #292): `compliance_rag.api.main:build_app` on Cloud Run
# as `compliance-rag-api`, australia-southeast1. FastAPI + uvicorn, CPU, no model
# in the image: the reranker is its own service (docker/reranker.Dockerfile),
# generation is Claude on Vertex or, with GENERATOR=demo, the in-process stand-in.
#
# Build context must be the repo root. Built for linux/amd64 (Cloud Run's
# architecture; an arm64 build from an Apple-silicon laptop fails there with
# "exec format error", BUILD_LOG 2026-10-01). CI doesn't build images (Actions
# minutes), so this is built by hand:
#
#   docker build --platform linux/amd64 -f docker/api.Dockerfile -t compliance-rag-api .
#
# Run it against local Postgres, Qdrant and Neo4j (ENV=local logs spans to
# stderr; every setting is an environment variable, `.env` is never copied in):
#
#   docker run --rm -p 8080:8080 -e PORT=8080 -e ENV=local \
#       -e GENERATOR=demo -e DATABASE_URL=... -e QDRANT_URL=... -e NEO4J_URI=... \
#       -e FIREBASE_PROJECT_ID=... compliance-rag-api
#
# What is in the image, and why nothing else is:
# - src/compliance_rag: the package (templates, static assets, htmx included);
# - corpus/instruments, corpus/register/instruments.yaml,
#   corpus/synthetic/registry.json: read at runtime through repo-relative paths
#   (the signed incomplete.yaml / part_year.yaml, the instrument register, the
#   synthetic registry);
# - vendor/bge-reranker-v2-m3-tokenizer: the tokenizer that counts the chunk and
#   the prefix cap (ADR-0054), read from the vendored files, no network;
# - the fastembed Qdrant/bm25 model, downloaded here at build time so that no
#   Hugging Face download happens at request time (HF_HUB_OFFLINE=1 enforces it).
# Not in it: .env, .git, models/ (the reranker's ONNX weights), tests, docs,
# corpus/golden and corpus/mappings (the loaders' input, not read by the API).

FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    UV_LINK_MODE=copy

WORKDIR /app

RUN pip install --no-cache-dir uv

# Only pyproject.toml/uv.lock first, so the dependency layer caches across code
# changes. --no-group export: this image never installs torch/optimum.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-group export --no-install-project

# The sparse (BM25) model, where QdrantRetrievalStore looks for it: the
# relative `.fastembed_cache`, i.e. /app/.fastembed_cache (WORKDIR above).
RUN /app/.venv/bin/python -c "from fastembed import SparseTextEmbedding; SparseTextEmbedding('Qdrant/bm25', cache_dir='.fastembed_cache')"

COPY src/compliance_rag ./src/compliance_rag
COPY corpus/instruments ./corpus/instruments
COPY corpus/register/instruments.yaml ./corpus/register/instruments.yaml
COPY corpus/synthetic/registry.json ./corpus/synthetic/registry.json
COPY vendor/bge-reranker-v2-m3-tokenizer ./vendor/bge-reranker-v2-m3-tokenizer

# Now that the source is present, install the project itself.
RUN uv sync --frozen --no-dev --no-group export

# Never root. The cache is owned by the user because fastembed opens it for
# writing even when the model is already there.
RUN useradd --uid 10001 --create-home app \
    && chown -R app:app /app/.fastembed_cache
USER app

# Offline from here on: the model is baked in above.
ENV HF_HUB_OFFLINE=1

EXPOSE 8080
# The venv's own uvicorn, not `uv run uvicorn` (which re-syncs against the
# lockfile at every start). Shell form so ${PORT} expands: Cloud Run injects PORT
# and expects the container to listen on it. `exec` makes uvicorn PID 1, so it
# receives Cloud Run's SIGTERM on scale-down directly. --factory: importing the
# module builds nothing; uvicorn calls build_app().
CMD exec /app/.venv/bin/uvicorn --factory compliance_rag.api.main:build_app --host 0.0.0.0 --port ${PORT:-8080}
