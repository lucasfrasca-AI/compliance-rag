"""The reranker as a standalone service (issue #14) — its own deployable,
not part of the main app's request path. `app.py` is the FastAPI service;
`model.py` loads the quantized ONNX artifact plain `onnxruntime`, no torch,
no optimum. `adapters.reranker_client.RerankerClient` is the `Reranker`
implementation that calls it over HTTP.
"""

from __future__ import annotations
