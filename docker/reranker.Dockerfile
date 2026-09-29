# The reranker service (issue #14): FastAPI + plain onnxruntime, CPU, $0.
# Deliberately does NOT install torch or optimum — those convert the model
# (scripts/export_reranker_onnx.py, run on the host or in a dedicated CI
# job, never in this image). Build context must be the repo root, so this
# can COPY the already-converted artifact and the vendored tokenizer:
#
#   docker build -f docker/reranker.Dockerfile -t compliance-rag-reranker .
#   docker run --rm -p 8001:8001 compliance-rag-reranker

FROM python:3.12-slim

WORKDIR /app

RUN pip install --no-cache-dir uv

# Only pyproject.toml/uv.lock first, so the dependency layer caches across
# code changes. --no-group export: this image never installs torch/optimum.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-group export --no-install-project

COPY src/compliance_rag ./src/compliance_rag
COPY vendor/bge-reranker-v2-m3-tokenizer ./vendor/bge-reranker-v2-m3-tokenizer
COPY models/reranker-onnx ./models/reranker-onnx

# Now that the source is present, install the project itself (previous
# step deliberately installed only its dependencies).
RUN uv sync --frozen --no-dev --no-group export

EXPOSE 8001
# The venv's own uvicorn, not `uv run uvicorn`: `uv run` re-syncs against
# the lockfile by default, which re-adds the dev group (ruff, pyright,
# pytest) that the build steps above deliberately excluded — harmless
# packages, but a wasted download and install on every container start.
CMD ["/app/.venv/bin/uvicorn", "compliance_rag.reranker_service.main:app", "--host", "0.0.0.0", "--port", "8001"]
