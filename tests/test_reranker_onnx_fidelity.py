"""Fidelity check (issue #14): the converted int8 ONNX reranker ranks the
same real query/document pairs in the same order as the original fp32
PyTorch model. Converting the official weights ourselves (rather than
trusting a third-party pre-quantized build) removes the *trust* question,
not the *quantization* question — this is what actually answers it.

Runs in CI against an artifact fetched from the corpus bucket (see
`.github/workflows/ci.yml`), through plain `onnxruntime` — same path as the
runtime service (`reranker_service/model.py`), no `torch`/`optimum` here.
`scripts/export_reranker_onnx.py` is where the fp32 comparison actually
happens, once, locally, the only place torch runs; this test re-checks the
int8 artifact against `fidelity.FP32_RANK_ORDER`, the fp32 order that
script recorded, rather than recomputing it — installing torch and
downloading 2.27GB of fp32 weights on every CI run is exactly what was
overrunning the hosted runner's disk (BUILD_LOG.md 2026-09-28).

Self-skips when no artifact is on disk, same pattern
`tests/test_reranker_service.py` uses.
"""

from __future__ import annotations

import pytest

from compliance_rag.reranker_service.fidelity import (
    FIDELITY_DOCUMENTS,
    FIDELITY_QUERY,
    FP32_RANK_ORDER,
    rank_order,
)
from compliance_rag.reranker_service.model import DEFAULT_ONNX_MODEL_PATH, RerankerModel

pytestmark = pytest.mark.skipif(
    not DEFAULT_ONNX_MODEL_PATH.exists(),
    reason=f"no converted ONNX model at {DEFAULT_ONNX_MODEL_PATH} — fetch it "
    "(ci.yml) or run `uv run python scripts/export_reranker_onnx.py` locally",
)


def test_int8_onnx_preserves_fp32s_rank_order() -> None:
    model = RerankerModel()
    int8_scores = model.score(FIDELITY_QUERY, FIDELITY_DOCUMENTS)
    int8_order = rank_order(int8_scores)
    assert int8_order == list(FP32_RANK_ORDER)
