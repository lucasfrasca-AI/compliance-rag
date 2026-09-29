"""Export `BAAI/bge-reranker-v2-m3` to a quantized int8 ONNX model (issue #14).

Run once (or whenever `fidelity.BAAI_REVISION` changes), never at request time:

    uv sync --group export
    uv run python scripts/export_reranker_onnx.py

Requires the `export` dependency group (`torch`, `optimum`, `transformers`) —
deliberately NOT a main dependency (`pyproject.toml`'s own comment explains
why) and NOT installed in the reranker service's Docker image, or in CI. The
service loads only the resulting `models/reranker-onnx/model.onnx` via plain
`onnxruntime` (`src/compliance_rag/reranker_service/app.py`); so does CI's
fidelity re-check (`tests/test_reranker_onnx_fidelity.py`), against a copy
fetched from the corpus bucket rather than converted fresh — converting in
CI is what was overrunning the runner's disk (BUILD_LOG.md 2026-09-28).
After running this script, upload the result with
`scripts/upload_reranker_artifact.py`.

No official ONNX build of this model exists (checked: BAAI's own repo has
only `model.safetensors`). Third-party pre-quantized builds exist on
Hugging Face, but this script converts the official weights directly rather
than trusting someone else's quantization for a component that affects
retrieval ranking — the human's own call, not assumed.

Pinned to an exact revision, not `main`: issue #32's reranker benchmark
needs the same weights every time it's run, and `main` can move under it
without warning.
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path
from typing import Any

import torch
from onnxruntime.quantization import (
    QuantType,
    quantize_dynamic,  # pyright: ignore[reportUnknownVariableType]
)
from optimum.onnxruntime import ORTModelForSequenceClassification
from transformers import AutoModelForSequenceClassification, AutoTokenizer

from compliance_rag.reranker_service.fidelity import (
    BAAI_REVISION,
    FIDELITY_DOCUMENTS,
    FIDELITY_QUERY,
    FP32_RANK_ORDER,
    MODEL_ID,
    rank_order,
)

OUTPUT_DIR = Path(__file__).resolve().parents[1] / "models" / "reranker-onnx"


def score_with_transformers(model: Any, tokenizer: Any, pairs: list[list[str]]) -> list[float]:
    # transformers/torch ship weak/partial stubs; Any on model/tokenizer
    # stops that Unknown-ness cascading through every call site.
    with torch.no_grad():
        inputs = tokenizer(
            pairs, padding=True, truncation=True, return_tensors="pt", max_length=512
        )
        scores = model(**inputs, return_dict=True).logits.view(-1).float()
    return [float(s) for s in scores]


def score_with_onnx_int8(onnx_model: Any, tokenizer: Any, pairs: list[list[str]]) -> list[float]:
    # optimum ships weak/partial stubs; Any on onnx_model/tokenizer stops
    # that Unknown-ness cascading through every call site.
    inputs = tokenizer(pairs, padding=True, truncation=True, return_tensors="pt", max_length=512)
    scores = onnx_model(**inputs, return_dict=True).logits.view(-1).float()
    return [float(s) for s in scores]


def main() -> None:
    pairs = [[FIDELITY_QUERY, doc] for doc in FIDELITY_DOCUMENTS]

    print(f"Loading {MODEL_ID}@{BAAI_REVISION} (fp32, PyTorch)...")
    # transformers/optimum ship weak/partial stubs; Any stops that
    # Unknown-ness cascading through every call site below.
    tokenizer: Any = AutoTokenizer.from_pretrained(  # pyright: ignore[reportUnknownMemberType,reportUnknownVariableType]
        MODEL_ID, revision=BAAI_REVISION
    )
    fp32_model: Any = AutoModelForSequenceClassification.from_pretrained(  # pyright: ignore[reportUnknownMemberType,reportUnknownVariableType]
        MODEL_ID, revision=BAAI_REVISION
    )
    fp32_model.eval()  # pyright: ignore[reportUnknownMemberType]
    fp32_scores = score_with_transformers(fp32_model, tokenizer, pairs)
    fp32_order = rank_order(fp32_scores)
    print(f"fp32 scores: {fp32_scores}")
    print(f"fp32 rank order: {fp32_order}")

    # CI's fidelity re-check trusts fidelity.FP32_RANK_ORDER instead of
    # recomputing this — if what was just computed for real disagrees, that
    # constant is stale (a revision or fixture change) and must be updated
    # by hand, deliberately, not silently drift out of sync with what CI
    # actually checks the int8 artifact against.
    if fp32_order != list(FP32_RANK_ORDER):
        print(
            f"fidelity.FP32_RANK_ORDER is stale: recorded {list(FP32_RANK_ORDER)}, "
            f"just computed {fp32_order}. Update FP32_RANK_ORDER in "
            "src/compliance_rag/reranker_service/fidelity.py to match — after "
            "checking why it moved.",
            file=sys.stderr,
        )
        sys.exit(1)

    if OUTPUT_DIR.exists():
        shutil.rmtree(OUTPUT_DIR)
    OUTPUT_DIR.mkdir(parents=True)

    fp32_onnx_dir = OUTPUT_DIR / "_fp32_onnx"
    print(f"Exporting to ONNX (fp32) at {fp32_onnx_dir}...")
    ort_model: Any = ORTModelForSequenceClassification.from_pretrained(  # pyright: ignore[reportUnknownMemberType]
        MODEL_ID, revision=BAAI_REVISION, export=True
    )
    ort_model.save_pretrained(fp32_onnx_dir)

    fp32_onnx_path = fp32_onnx_dir / "model.onnx"
    int8_onnx_path = OUTPUT_DIR / "model.onnx"

    # use_external_data_format=True: this model's fp32 weights are ~2.27GB —
    # keeps quantize_dynamic from requiring the whole model resident in
    # memory at once.
    print(f"Quantizing to int8 at {int8_onnx_path}...")
    quantize_dynamic(  # pyright: ignore[reportUnknownVariableType]
        model_input=str(fp32_onnx_path),
        model_output=str(int8_onnx_path),
        weight_type=QuantType.QInt8,
        use_external_data_format=True,
    )
    # ORTModelForSequenceClassification.from_pretrained (below, and the
    # runtime service) needs config.json alongside model.onnx to infer the
    # model type — copy it out before discarding the rest of the fp32
    # intermediate directory (its own, much larger model.onnx/model.onnx_data
    # are not needed once the int8 model.onnx exists).
    shutil.copy(fp32_onnx_dir / "config.json", OUTPUT_DIR / "config.json")
    shutil.rmtree(fp32_onnx_dir)

    print("Scoring the same pairs through the quantized int8 ONNX model...")
    int8_model: Any = ORTModelForSequenceClassification.from_pretrained(  # pyright: ignore[reportUnknownMemberType]
        OUTPUT_DIR
    )
    int8_scores = score_with_onnx_int8(int8_model, tokenizer, pairs)
    int8_order = rank_order(int8_scores)
    print(f"int8 scores: {int8_scores}")
    print(f"int8 rank order: {int8_order}")

    (OUTPUT_DIR / "REVISION").write_text(BAAI_REVISION + "\n", encoding="utf-8")

    if fp32_order != int8_order:
        print(
            f"FIDELITY CHECK FAILED: fp32 rank order {fp32_order} != "
            f"int8 rank order {int8_order}. Quantization changed which "
            "document ranks first — do not ship this artifact.",
            file=sys.stderr,
        )
        sys.exit(1)

    print(f"Fidelity check passed: rank order preserved ({fp32_order}).")
    print(f"Artifact ready at {OUTPUT_DIR}")
    print("Next: uv run python scripts/upload_reranker_artifact.py")


if __name__ == "__main__":
    main()
