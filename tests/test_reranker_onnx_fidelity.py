"""Fidelity check (issue #14): the converted int8 ONNX reranker ranks the
same real query/document pairs in the same order as the original fp32
PyTorch model. Converting the official weights ourselves (rather than
trusting a third-party pre-quantized build) removes the *trust* question,
not the *quantization* question — this is what actually answers it.

Self-skips when the `export` dependency group (`torch`, `optimum`,
`transformers`'s PyTorch backend) isn't installed, same pattern
`tests/test_ledger_schema.py` uses for a missing `DATABASE_URL`. Run for
real with:

    uv sync --group export --dev
    uv run pytest tests/test_reranker_onnx_fidelity.py

`scripts/export_reranker_onnx.py` also runs this exact comparison itself
and refuses to produce an artifact that fails it — this test is an
independent re-check against whatever artifact is already on disk, not a
re-run of the conversion.
"""

from __future__ import annotations

import importlib.util
from typing import Any

import pytest

_EXPORT_GROUP_INSTALLED = importlib.util.find_spec("torch") is not None

pytestmark = pytest.mark.skipif(
    not _EXPORT_GROUP_INSTALLED,
    reason="the `export` dependency group isn't installed (`uv sync --group export --dev`)",
)


def test_int8_onnx_preserves_fp32s_rank_order() -> None:
    from optimum.onnxruntime import ORTModelForSequenceClassification
    from scripts.export_reranker_onnx import (
        BAAI_REVISION,
        FIDELITY_DOCUMENTS,
        FIDELITY_QUERY,
        MODEL_ID,
        OUTPUT_DIR,
        rank_order,
        score_with_onnx_int8,
        score_with_transformers,
    )
    from transformers import AutoModelForSequenceClassification, AutoTokenizer

    if not (OUTPUT_DIR / "model.onnx").exists():
        pytest.skip(
            f"no converted ONNX model at {OUTPUT_DIR} — run "
            "`uv run python scripts/export_reranker_onnx.py` first"
        )

    # transformers/optimum ship weak/partial stubs, so every call below
    # needs an ignore — matching tests/test_policy_parser.py's precedent
    # for pymupdf, the other weakly-stubbed dependency in this repo.
    pairs = [[FIDELITY_QUERY, doc] for doc in FIDELITY_DOCUMENTS]
    tokenizer: Any = AutoTokenizer.from_pretrained(  # pyright: ignore[reportUnknownMemberType,reportUnknownVariableType]
        MODEL_ID, revision=BAAI_REVISION
    )

    fp32_model: Any = AutoModelForSequenceClassification.from_pretrained(  # pyright: ignore[reportUnknownMemberType,reportUnknownVariableType]
        MODEL_ID, revision=BAAI_REVISION
    )
    fp32_model.eval()  # pyright: ignore[reportUnknownMemberType]
    fp32_order = rank_order(
        score_with_transformers(fp32_model, tokenizer, pairs)  # pyright: ignore[reportUnknownArgumentType]
    )

    int8_model = ORTModelForSequenceClassification.from_pretrained(  # pyright: ignore[reportUnknownMemberType]
        OUTPUT_DIR
    )
    int8_order = rank_order(
        score_with_onnx_int8(int8_model, tokenizer, pairs)  # pyright: ignore[reportUnknownArgumentType]
    )

    assert fp32_order == int8_order
