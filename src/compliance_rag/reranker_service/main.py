"""Uvicorn entrypoint: `uvicorn compliance_rag.reranker_service.main:app`
(issue #14, the Dockerfile's `CMD`). Loads the real vendored tokenizer and
the real converted ONNX artifact from their default on-disk locations —
tests use `create_app()` directly instead, with a model built from a test
fixture path."""

from __future__ import annotations

from compliance_rag.reranker_service.app import create_app

app = create_app()
