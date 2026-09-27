"""Export `BAAI/bge-reranker-v2-m3` to a quantized int8 ONNX model (issue #14).

Run once (or whenever `BAAI_REVISION` below changes), never at request time:

    uv sync --group export
    uv run python scripts/export_reranker_onnx.py

Requires the `export` dependency group (`torch`, `optimum`, `transformers`) —
deliberately NOT a main dependency (`pyproject.toml`'s own comment explains
why) and NOT installed in the reranker service's Docker image. The service
loads only the resulting `models/reranker-onnx/model.onnx` via plain
`onnxruntime` (`src/compliance_rag/reranker_service/app.py`).

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

import torch
from onnxruntime.quantization import QuantType, quantize_dynamic
from optimum.onnxruntime import ORTModelForSequenceClassification
from transformers import AutoModelForSequenceClassification, AutoTokenizer

BAAI_REVISION = "953dc6f"
"""The exact commit of `BAAI/bge-reranker-v2-m3` these weights are pulled
from — record this alongside the conversion's BUILD_LOG.md entry. Never
`main`: issue #32's benchmark isn't reproducible against a moving target."""

MODEL_ID = "BAAI/bge-reranker-v2-m3"
OUTPUT_DIR = Path(__file__).resolve().parents[1] / "models" / "reranker-onnx"

# A small, fixed, real query/document set for the fidelity check below — the
# same "real fixture, not a representative sample" standard this repo
# applies everywhere else (`CLAUDE.md`), just for ranking rather than
# parsing: these are the real APP 10/11 sentences from issue #9's spike
# fixture, not synthetic filler.
FIDELITY_QUERY = (
    "What steps must an entity take to protect personal information "
    "from misuse, interference and loss?"
)
FIDELITY_DOCUMENTS = (
    "An APP entity must take such steps (if any) as are reasonable in the "
    "circumstances to protect the information from misuse, interference and "
    "loss, and from unauthorised access, modification or disclosure.",
    "An APP entity must give the individual access to the personal "
    "information on request, subject to the exceptions in this Schedule.",
    "The weather in Canberra in autumn is mild, with occasional frost "
    "overnight and clear skies through most of the season.",
)
"""Two real, on-topic APP clauses and one deliberately irrelevant sentence —
the ranking a reranker exists to get right (the two real clauses ahead of
the irrelevant one) is exactly what quantization could plausibly disturb."""


def score_with_transformers(
    model: AutoModelForSequenceClassification, tokenizer: AutoTokenizer, pairs: list[list[str]]
) -> list[float]:
    with torch.no_grad():
        inputs = tokenizer(
            pairs, padding=True, truncation=True, return_tensors="pt", max_length=512
        )
        scores = model(**inputs, return_dict=True).logits.view(-1).float()
    return [float(s) for s in scores]


def score_with_onnx_int8(
    onnx_model: ORTModelForSequenceClassification,
    tokenizer: AutoTokenizer,
    pairs: list[list[str]],
) -> list[float]:
    inputs = tokenizer(pairs, padding=True, truncation=True, return_tensors="pt", max_length=512)
    scores = onnx_model(**inputs, return_dict=True).logits.view(-1).float()
    return [float(s) for s in scores]


def rank_order(scores: list[float]) -> list[int]:
    """Indices sorted by score descending — what the reranker's caller
    actually consumes (`Reranker.rerank`'s `ScoredDocument` ordering), not
    the raw score values quantization is expected to perturb slightly."""
    return sorted(range(len(scores)), key=lambda i: scores[i], reverse=True)


def main() -> None:
    pairs = [[FIDELITY_QUERY, doc] for doc in FIDELITY_DOCUMENTS]

    print(f"Loading {MODEL_ID}@{BAAI_REVISION} (fp32, PyTorch)...")
    tokenizer = AutoTokenizer.from_pretrained(MODEL_ID, revision=BAAI_REVISION)
    fp32_model = AutoModelForSequenceClassification.from_pretrained(
        MODEL_ID, revision=BAAI_REVISION
    )
    fp32_model.eval()
    fp32_scores = score_with_transformers(fp32_model, tokenizer, pairs)
    fp32_order = rank_order(fp32_scores)
    print(f"fp32 scores: {fp32_scores}")
    print(f"fp32 rank order: {fp32_order}")

    if OUTPUT_DIR.exists():
        shutil.rmtree(OUTPUT_DIR)
    OUTPUT_DIR.mkdir(parents=True)

    fp32_onnx_dir = OUTPUT_DIR / "_fp32_onnx"
    print(f"Exporting to ONNX (fp32) at {fp32_onnx_dir}...")
    ort_model = ORTModelForSequenceClassification.from_pretrained(
        MODEL_ID, revision=BAAI_REVISION, export=True
    )
    ort_model.save_pretrained(fp32_onnx_dir)

    fp32_onnx_path = fp32_onnx_dir / "model.onnx"
    int8_onnx_path = OUTPUT_DIR / "model.onnx"
    print(f"Quantizing to int8 at {int8_onnx_path}...")
    quantize_dynamic(
        model_input=str(fp32_onnx_path),
        model_output=str(int8_onnx_path),
        weight_type=QuantType.QInt8,
    )
    # ORTModelForSequenceClassification.from_pretrained (below, and the
    # runtime service) needs config.json alongside model.onnx to infer the
    # model type — copy it out before discarding the rest of the fp32
    # intermediate directory (its own, much larger model.onnx/model.onnx_data
    # are not needed once the int8 model.onnx exists).
    shutil.copy(fp32_onnx_dir / "config.json", OUTPUT_DIR / "config.json")
    shutil.rmtree(fp32_onnx_dir)

    print("Scoring the same pairs through the quantized int8 ONNX model...")
    int8_model = ORTModelForSequenceClassification.from_pretrained(OUTPUT_DIR)
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


if __name__ == "__main__":
    main()
